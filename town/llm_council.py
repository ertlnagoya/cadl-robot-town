"""LLM の委員会：期の観測を読み、改正案（CADL ファイル全体）を書く。

LLM は規則ファイルを丸ごと書き直して返す。何を変えたかの申告は信用せず、
返ってきた CADL を、ルールベースの案と同じ関門（検査・憲法・改善・投票）に
かける。LLM が憲法を破る案や、検査を通らない案を書いても、関門で止まる。

提案は runs/<名前>/llm-round<k>.json に保存する。--proposer replay で、
保存した提案を LLM を呼ばずに再生できる（API キーがない環境や授業用）。
"""
from __future__ import annotations

import json
from pathlib import Path

MODEL = "claude-opus-5-5"
MAX_PROPOSALS = 3

SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "cadl": {"type": "string"},
                },
                "required": ["title", "rationale", "cadl"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["proposals"],
    "additionalProperties": False,
}

SYSTEM = f"""\
あなたは「ロボットの街」の規則改正委員会です。3 社（A・B・C）の配送ロボットが
道路を共有しており、規則は CADL で書かれています。各期の終わりに、観測結果を
見て規則の改正案を出します。

改正案は、次の順に関門にかけられ、すべて通ったものだけが採択されます。
1. 検査：cadl check と cadl sim-ir を通ること
2. 憲法：constitution.yaml を守ること。変えてよいのは `# @amend:<名前>` が付いた
   行の数値だけで、一度に動かせるのは 1 つのパラメータを 1 ステップだけです。
   ほかの行（コメントを含む）は一字一句変えてはいけません。改正後のシミュレー
   ションで、不変条件（各社の定時率の下限など）も守られる必要があります。
3. 改善：委員会の目標（objective）が、同じ乱数シードで現行より改善すること
4. 投票：各社は自社の効用（定時配達件数 − 罰点）が下がらなければ賛成し、
   定足数の賛成で採択されます

改正案を最大 {MAX_PROPOSALS} 件出してください。各案の cadl には、改正後の規則
ファイル全体を書きます。rationale には、観測のどこから何を狙ったのかと、
各社の投票の見込みを日本語で 2〜4 文で書きます。改正すべき点がなければ、
proposals を空にしてください。"""


def _observation(cur: dict) -> str:
    lines = [
        f"定時配達 {cur['on_time_deliveries']:.1f} / 注文 {cur['orders']:.1f}、"
        f"未配達 {cur['unfinished']:.1f}、ふさがれた時間 {cur['blocked_s']:.0f} 秒、"
        f"集中モードの時間 {100 * cur['central_share']:.0f}%、"
        f"モード切替 {cur['switches']:.1f} 回（安全条件の違反 {cur['invariant_breaches']:.1f} 回）",
        "違反（原因別）: " + (", ".join(f"{k}={v:.1f}" for k, v in cur["violations"].items()) or "なし"),
    ]
    for c, x in cur["companies"].items():
        lines.append(
            f"{c} 社: 定時率 {x['on_time_rate']:.2f}、遅配 {x['late']:.1f}、道路で待った回数 {x['parks']:.1f}、"
            f"ふさがれた秒数 {x['blocked_s']:.0f}、罰点 {x['sanction']:.1f}、効用 {x['utility']:.1f}")
    return "\n".join(lines)


def build_request(rules_text: str, constitution_text: str, env: dict, cur: dict,
                  history: list[str], objective: str) -> dict:
    user = (
        f"## 委員会の目標\n{objective}\n\n"
        f"## この期の環境\n{env['label']}（注文の平均間隔 {env['order_interval_s']} 秒"
        + (f"、ラッシュ {env['rush']}" if env.get("rush") else "") + "）\n\n"
        f"## この期の観測（現行の規則、シード平均）\n{_observation(cur)}\n\n"
        f"## これまでの改正履歴\n{''.join(history[1:]) or '（なし）'}\n\n"
        f"## 憲法（constitution.yaml）\n```yaml\n{constitution_text}```\n\n"
        f"## 現行の規則（cadl/robot_town.cadl）\n```yaml\n{rules_text}```\n"
    )
    return {
        "model": MODEL,
        "max_tokens": 64000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
        # 安全分類器に断られたとき、サーバー側で推奨のモデルに切り替えて続ける。
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
    }


def propose_llm(request: dict) -> tuple[list[dict], str]:
    """LLM に改正案を出させる。戻り値は（提案のリスト, 状況の説明）。"""
    import anthropic

    client = anthropic.Anthropic()
    try:
        with client.beta.messages.stream(**request) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise SystemExit("[ERROR] Claude API の認証に失敗しました。ANTHROPIC_API_KEY を確認してください。")
    except TypeError as e:   # SDK は認証情報が見つからないとき TypeError を送出する
        raise SystemExit("[ERROR] Claude API の認証情報が見つかりません。ANTHROPIC_API_KEY を設定するか、"
                         f"`ant auth login` を実行してください。（{e}）")
    except anthropic.RateLimitError:
        return [], "API の利用上限に達したため、この期は LLM の案なし"
    except anthropic.APIStatusError as e:
        return [], f"API エラー {e.status_code} のため、この期は LLM の案なし"
    except anthropic.APIConnectionError:
        return [], "API に接続できないため、この期は LLM の案なし"
    if msg.stop_reason == "refusal":
        return [], "LLM が応答を断ったため、この期は LLM の案なし"
    if msg.stop_reason == "max_tokens":
        return [], "出力が上限で途切れたため、この期は LLM の案なし"
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        proposals = json.loads(text)["proposals"][:MAX_PROPOSALS]
    except (json.JSONDecodeError, KeyError, TypeError):
        return [], "LLM の出力を JSON として読めないため、この期は LLM の案なし"
    u = msg.usage
    return proposals, f"{msg.model}、入力 {u.input_tokens} / 出力 {u.output_tokens} トークン"


def save(path: Path, proposals: list[dict], note: str) -> None:
    path.write_text(json.dumps({"note": note, "proposals": proposals}, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def replay(path: Path) -> tuple[list[dict], str]:
    if not path.exists():
        return [], f"{path} がないため、この期は案なし"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["proposals"][:MAX_PROPOSALS], f"再生: {path}（{data.get('note', '')}）"
