"""LLM の運用支援（steward）への 3 つの依頼と、その記録・再生。

1. kpi    ：人間が自然言語で書いた KPI を、測り方（town/kpi.py の指標の組み合わせ）に
            言い換える。読み方が分かれる・代理指標の恐れがある・過去の判断の前提が
            崩れるときは、選択肢と懸念を添えて人間に返す。
2. propose：KPI に向けて、規則・契約の改正案（CADL ファイル全体）と、規制（憲法の値）の
            改正案を出す。
3. request：人間に判断を求めるとき、測った結果をもとに、問い・選択肢の説明・
            影響を受ける当事者・推奨・分からないことを書く。

LLM の出力は、どれも機械の側で確かめてから使う。測り方は指標の名前と形を、
改正案は 4 つの関門を、判断要求は選択肢の番号を確かめ、数値は LLM の文章ではなく
測った表から示す。

出力は runs/<名前>/llm/p<k>-<依頼>.json に残る。--replay-dir に置いた同じ名前の
ファイルがあれば、LLM を呼ばずにそれを使う（API キーがない環境や授業用）。
--replay-dir になくても、この実行の llm/ にすでに記録があれば（判断要求で止まったあと、
同じ --name でやり直したとき）それを使う。記録には依頼の要約（request_digest）が入り、再生のときに依頼が記録と違えば
（kpi.md、規則、これまでの判断が変わったなど）、そう知らせたうえで記録を使う。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import kpi
from .llm_council import MODEL, call_json
from .messages import Messages
from .period import ROOT

MAX_PROPOSALS = 3
METRIC = {"type": "string", "enum": list(kpi.METRICS)}

ROLES = """\
「ロボットの街」では、3 社（A・B・C、各社 2 台）の配送ロボットが道路を共有し、
道路の規則と顧客との約束が CADL で書かれています。役割は次のとおりです。
- 人間（サービスの運営者）：KPI を自然言語で書きます。KPI は時間とともに変わります。
  あなたが出した判断要求に答え、判断の理由を残します。
- あなた（LLM）：KPI の達成に向けて、規則・規制・契約の改正を提案します。明らかには
  判断できない難しい問題や、人間の当事者が合意すべきことは、自分で決めずに人間に
  判断を求め、そのときに判断に必要な情報を示します。
- 機械の関門：検査（cadl check）・憲法・同じ乱数シードで測った改善・各社の投票。
  あなたの説明は信用されず、関門と測った値だけで判定されます。
改正の層は 3 つです。
- 規則：ROAD_USE と運用モードの切替（sos.transitions）。関門をすべて通れば採択できます。
- 契約：DELIVERY_SLA（顧客への集荷と配達の約束）。顧客は投票に加わらないので、
  変えるかどうかは必ず人間が決めます。
- 規制：憲法（constitution.yaml）の不変条件や定足数。必ず人間が決めます。
"""

KPI_SCHEMA = {
    "type": "object",
    "properties": {
        "interpretation": {"type": "string"},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "goals": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"metric": METRIC, "text": {"type": "string"}},
                        "required": ["metric", "text"], "additionalProperties": False}},
                    "guards": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"metric": METRIC, "op": {"type": "string", "enum": list(kpi.OPS)},
                                       "value": {"type": "number"}, "text": {"type": "string"}},
                        "required": ["metric", "op", "value", "text"], "additionalProperties": False}},
                    "note": {"type": "string"},
                },
                "required": ["label", "goals", "guards", "note"],
                "additionalProperties": False,
            },
        },
        "recommended": {"type": "integer"},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "revisit": {"type": "array", "items": {
            "type": "object",
            "properties": {"decision": {"type": "string"}, "why": {"type": "string"}},
            "required": ["decision", "why"], "additionalProperties": False}},
    },
    "required": ["interpretation", "options", "recommended", "concerns", "revisit"],
    "additionalProperties": False,
}

KPI_SYSTEM = ROLES + f"""
いまの依頼は、人間が書いた KPI を、測れる指標の組み合わせ（測り方）に言い換える
ことです。使える指標は次のものだけです。

{kpi.catalogue()}

測り方は goals（先頭が主目標。改善はこれで測ります。2 番目以降は悪化を見張る目標）と
guards（改正後に必ず守る条件）からなります。

- KPI の読み方が 1 通りに定まり、懸念もなければ、options を 1 つだけ返し、concerns と
  revisit を空にしてください。そのときは人間に確かめずに使われます。
- 読み方が分かれるとき、KPI が測りやすい代わりの指標（代理指標）を目標にしていて、
  それだけを追うと KPI の本来の狙いを損なう恐れがあるとき、KPI の中の目標どうしが
  ぶつかりうるときは、options を 2〜3 個にし、concerns に理由を書いてください。
  人間が選びます。recommended には、あなたが勧める option の番号（1 から）を入れます。
- これまでの判断が渡されたときは、KPI の変更で前提が崩れた判断を revisit に挙げて
  ください（decision には判断の番号、例 D1 を書きます）。
文章はすべて日本語で書いてください。"""

PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment": {"type": "string"},
        "proposals": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "rationale": {"type": "string"},
                "cadl": {"type": "string"},
                "needs_human": {"type": "boolean"},
                "why_human": {"type": "string"},
            },
            "required": ["title", "rationale", "cadl", "needs_human", "why_human"],
            "additionalProperties": False}},
        "regulation_proposals": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "rationale": {"type": "string"},
                "key": {"type": "string"},
                "value": {"type": "number"},
            },
            "required": ["title", "rationale", "key", "value"],
            "additionalProperties": False}},
    },
    "required": ["assessment", "proposals", "regulation_proposals"],
    "additionalProperties": False,
}

PROPOSE_SYSTEM = ROLES + f"""
いまの依頼は、この期の観測を見て、KPI の達成に向けた改正案を出すことです。

- 規則と契約の改正案（proposals）は最大 {MAX_PROPOSALS} 件です。各案の cadl には、改正後の
  規則ファイル全体を書きます。変えてよいのは `# @amend:<名前>` が付いた行の数値だけで、
  一度に 1 つのパラメータを 1 ステップだけ動かせます（同じ名前の行が複数あれば、すべて
  同じ値にします）。ほかの行は、コメントも含めて一字一句変えてはいけません。
- 規制の改正案（regulation_proposals）は最大 1 件です。key には憲法の regulation に
  挙がった名前（例 invariants.min_company_on_time_rate）を、value には新しい値を書きます。
  1 回に動かせるのは、そこに書かれた step だけです。
- rationale には、観測のどこから何を狙ったのか、KPI がどう変わる見込みか、各社の投票の
  見込みを 2〜4 文で書きます。
- 関門の結果を待つまでもなく人間の判断が要るとあなたが考える案は、needs_human を true に
  し、why_human に理由を書きます（契約と規制の案は、そうしなくても必ず人間が判断します）。
- これまでの人間の判断と理由を尊重してください。人間が退けた案を同じ根拠で出し直すのは
  避け、出し直すなら何が変わったのかを rationale に書きます。
- 改正すべき点がなければ、proposals と regulation_proposals を空にしてください。
まず assessment に、この期の観測から読み取れることを 2〜3 文で書いてください。
文章はすべて日本語で書いてください。"""

REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "background": {"type": "string"},
        "options": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "summary": {"type": "string"},
                "stakeholders": {"type": "string"},
            },
            "required": ["id", "summary", "stakeholders"],
            "additionalProperties": False}},
        "recommendation": {"type": "object",
                           "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
                           "required": ["id", "reason"], "additionalProperties": False},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["question", "background", "options", "recommendation", "unknowns"],
    "additionalProperties": False,
}

REQUEST_SYSTEM = ROLES + """
いまの依頼は、人間に判断を求める文書（判断要求）を書くことです。測った結果の表と、
判断を求める理由が渡されます。表は人間にもそのまま示されます。

- question：人間に決めてほしいことを 1 文で書きます。
- background：なぜあなたが自分で決めないのかを、判断を求める理由に沿って 2〜3 文で
  書きます。
- options：渡された選択肢の id ごとに、summary（選ぶと何が起きるか。表の数値を引くときは
  表のとおりに書く）と stakeholders（誰が得をし、誰が損をするか。顧客を忘れない）を書きます。
  渡されていない id を作ってはいけません。
- recommendation：あなたが勧める id と、その理由です。
- unknowns：判断に関わるのに、あなたが知らないこと、測っていないことを挙げます
  （たとえば各社の行動方針、顧客の受け止め、次の期の需要）。
文章はすべて日本語で書いてください。"""


def _request(system: str, user: str, schema: dict, effort: str = "high") -> dict:
    return {
        "model": MODEL,
        "max_tokens": 64000,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        # 安全分類器に断られたとき、サーバー側で推奨のモデルに切り替えて続ける。
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
    }


def _lang(system: str, lang: str) -> str:
    """system prompt の末尾の言語の指示を、--lang に合わせる。"""
    return system.replace(Messages("ja")("write_in"), Messages(lang)("write_in"))


def kpi_request(text: str, since: int, previous: str | None, decisions: str, lang: str = "ja") -> dict:
    user = (f"## KPI（第 {since} 期から、人間が書いたもの）\n{text}\n\n"
            + (f"## 直前の KPI\n{previous}\n\n" if previous else "")
            + f"## これまでの判断\n{decisions or '（なし）'}\n")
    return _request(_lang(KPI_SYSTEM, lang), user, KPI_SCHEMA)


def propose_request(kpi_text: str, spec: dict, mode_note: str, env: dict, observation: str,
                    history: str, decisions: str, revisit: str, constitution: str,
                    rules: str, lang: str = "ja") -> dict:
    user = (
        f"## KPI（人間が書いたもの）\n{kpi_text}\n\n"
        f"## KPI の測り方（人間が確かめたもの）\n```json\n{json.dumps(spec, ensure_ascii=False, indent=1)}\n```\n\n"
        f"## 運用モードの設定\n{mode_note}\n\n"
        f"## この期の環境\n{env['label']}（注文の平均間隔 {env['order_interval_s']} 秒"
        + (f"、ラッシュ {env['rush']}" if env.get("rush") else "") + "）\n\n"
        f"## この期の観測（現行の規則、シード平均）\n{observation}\n\n"
        f"## これまでの改正\n{history or '（なし）'}\n\n"
        f"## これまでの判断（人間の理由を含む）\n{decisions or '（なし）'}\n\n"
        + (f"## KPI の変更で見直し対象になった判断\n{revisit}\n\n" if revisit else "")
        + f"## 憲法（constitution.yaml）\n```yaml\n{constitution}```\n\n"
        f"## 現行の規則（cadl/robot_town.cadl）\n```yaml\n{rules}```\n"
    )
    return _request(_lang(PROPOSE_SYSTEM, lang), user, PROPOSE_SCHEMA)


def request_request(kpi_text: str, spec_text: str, reasons: str, table: str, options: str,
                    decisions: str, lang: str = "ja") -> dict:
    user = (f"## KPI（人間が書いたもの）\n{kpi_text}\n\n## KPI の測り方\n{spec_text}\n\n"
            f"## 判断を求める理由\n{reasons}\n\n## 測った結果\n{table}\n\n"
            f"## 選択肢（id: 内容）\n{options}\n\n"
            f"## これまでの判断（人間の理由を含む）\n{decisions or '（なし）'}\n")
    return _request(_lang(REQUEST_SYSTEM, lang), user, REQUEST_SCHEMA, effort="medium")


class Recorder:
    """LLM の出力を run_dir/llm/ に残し、replay_dir にあればそれを使う。"""

    def __init__(self, run_dir: Path, mode: str, replay_dir: Path | None, msg: Messages | None = None):
        self.dir = run_dir / "llm"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mode, self.replay_dir = mode, replay_dir
        self.msg = msg or Messages("ja")

    def get(self, step: str, request: dict) -> tuple[dict | None, str]:
        out = self.dir / f"{step}.json"
        digest = hashlib.sha256(json.dumps([request["system"], request["messages"]],
                                           ensure_ascii=False).encode()).hexdigest()[:16]
        src = next((d / f"{step}.json" for d in (self.replay_dir, self.dir)
                    if d and (d / f"{step}.json").exists()), None)
        if src:
            rec = json.loads(src.read_text(encoding="utf-8"))
            shown = src.resolve().relative_to(ROOT) if src.resolve().is_relative_to(ROOT) else src
            note = self.msg("replay", path=shown, note=rec.get("note", ""))
            if rec.get("request_digest", digest) != digest:
                note += self.msg("digest")
            self._save(out, rec["note"], rec["data"], digest)
            return rec["data"], note
        if self.mode == "replay":
            return None, self.msg("no_record", step=step)
        data, note = call_json(request)
        if data is not None:
            self._save(out, note, data, digest)
        return data, note

    @staticmethod
    def _save(path: Path, note: str, data: dict, digest: str) -> None:
        path.write_text(json.dumps({"note": note, "request_digest": digest, "data": data},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
