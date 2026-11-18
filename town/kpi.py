"""KPI：人間が書いたサービスの目標と、その評価方法。

kpi.md は「## 第 k 期から」（英語なら「## From period k」）の見出しで区切る。第 k 期には、見出しの番号が
k 以下で最大の節が効く。人間は期と期のあいだに kpi.md を書き換えてよい
（town.steward は期の初めに読み直す）。

評価方法は、下の METRICS の組み合わせで、2 通りの決め方がある。

1. 人間が節の中に「評価方法:」の箇条書きを書く。そのまま機械が読み、LLM は使わない。

       評価方法:
       - 主目標: 定時率 >= 0.95
       - 監視: 最も待たせた会社の待ち時間p95 <= 240
       - 条件: 最低の会社の定時率 >= 0.8

   指標は名前（on_time_rate）か日本語の短い名前（定時率）で書く。主目標と監視に値を
   書くと、それが達成基準になる（書かなければ、よくする向きだけを決める）。条件は
   値が必須で、改正後に必ず守る。英語では「Evaluation:」「- goal:」「- watch:」「- guard:」。
   `python -m town.kpi` で、使える指標と、kpi.md をどう読んだかを確かめられる。

2. 箇条書きがなければ、LLM が自然言語の KPI を評価方法に言い換える。

       {"goals":  [{"metric": "on_time_rate", "target": 0.95, "text": "町全体の定時率を 95% 以上にする"}, ...],
        "guards": [{"metric": "min_company_on_time_rate", "op": ">=", "value": 0.8,
                    "text": "どの会社の定時率も 8 割を下回らない"}, ...]}

goals の先頭が主目標で、改善はこれで測る。2 番目以降の目標は、悪化を
監視する（悪化すれば、KPI どうしの衝突として人間に判断を求める）。goals の target は
達成基準で、期ごとに達成したかどうかを示す（改正を採るかどうかの判定には使わない）。
guards は、改正後に必ず守る条件で、憲法の不変条件に加わる。
"""
from __future__ import annotations

import re
from pathlib import Path

# 名前: (説明, 向き（+1 は大きいほどよい）, 誤差とみなす差, kpi.md に書ける日本語の短い名前)
# 待ち時間は、注文から届くまでの秒数。期の終わりに届いていない注文は、期の終わりまでの秒数で数える。
METRICS = {
    "on_time_deliveries": ("町全体の定時配達件数", +1, 1.0, "定時配達件数"),
    "on_time_rate": ("町全体の定時率（定時配達件数 ÷ 注文件数）", +1, 0.01, "定時率"),
    "late_deliveries": ("町全体の遅配件数", -1, 1.0, "遅配件数"),
    "unfinished": ("期の終わりに未配達のまま残る注文の件数", -1, 0.5, "未配達件数"),
    "p95_wait_s": ("町全体の待ち時間の 95 パーセンタイル（秒）", -1, 5.0, "待ち時間p95"),
    "max_wait_s": ("町全体で最も長い待ち時間（秒）", -1, 10.0, "最長の待ち時間"),
    "worst_company_p95_wait_s": ("会社ごとの待ち時間の 95 パーセンタイルのうち、最も長いもの（秒）",
                                 -1, 5.0, "最も待たせた会社の待ち時間p95"),
    "blocked_s": ("止まっているロボットにふさがれた時間の合計（秒）", -1, 60.0, "ふさがれた時間"),
    "road_violations": ("道路の規則の違反件数（release の期限超過と blocking_guard の退去）", -1, 1.0,
                        "道路の違反件数"),
    "violation_total": ("違反の合計件数（配達の遅れを含む）", -1, 1.0, "違反件数"),
    "min_company_on_time_rate": ("最も低い会社の定時率", +1, 0.02, "最低の会社の定時率"),
    "on_time_rate_gap": ("会社ごとの定時率の、最大と最小の差", -1, 0.02, "定時率の差"),
    "utility_gap": ("会社ごとの効用（定時配達件数 − 罰点）の、最大と最小の差", -1, 1.0, "効用の差"),
    "sanction_total": ("全社の罰点の合計", -1, 1.0, "罰点の合計"),
}
ALIASES = {v[3]: k for k, v in METRICS.items()}
OPS = (">=", "<=")
HEADING = re.compile(r"^##\s*(?:第\s*(\d+)\s*期から|From period\s+(\d+))\s*$", re.IGNORECASE)
BLOCK = re.compile(r"^\s*(?:評価方法|Evaluation)\s*[:：]\s*$", re.IGNORECASE)
ITEM = re.compile(r"^\s*[-*]\s*(主目標|監視|条件|goal|watch|guard)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
KIND = {"主目標": "goal", "監視": "watch", "条件": "guard"}
COND = re.compile(r"^(.+?)\s*(>=|<=|≧|≦)\s*(-?\d+(?:\.\d+)?)\s*(%?)$")
COMPARE = re.compile(r"[<>=≧≦]")


def measure(m: dict) -> dict[str, float]:
    """evaluate() の結果（シード平均）にも、シードごとの結果にも使える。"""
    c = m["companies"].values()
    v = m["violations"]
    rates = [x["on_time_rate"] for x in c]
    utils = [x["utility"] for x in c]
    return {
        "on_time_deliveries": m["on_time_deliveries"],
        "on_time_rate": m["on_time_deliveries"] / (m["orders"] or 1),
        "p95_wait_s": m["p95_wait_s"],
        "max_wait_s": m["max_wait_s"],
        "worst_company_p95_wait_s": max(x["p95_wait_s"] for x in c),
        "late_deliveries": sum(x["late"] for x in c),
        "unfinished": m["unfinished"],
        "blocked_s": m["blocked_s"],
        "road_violations": v.get("deadline:release", 0) + v.get("monitor:blocking_guard", 0),
        "violation_total": sum(v.values()),
        "min_company_on_time_rate": min(rates),
        "on_time_rate_gap": max(rates) - min(rates),
        "utility_gap": max(utils) - min(utils),
        "sanction_total": sum(x["sanction"] for x in c),
    }


def load(path: Path, period: int) -> tuple[int, str]:
    """第 period 期に効く節の（開始の期, 本文）。本文には「評価方法:」の箇条書きも含む。"""
    sections: list[tuple[int, list[str]]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = HEADING.match(line)
        if m:
            sections.append((int(m.group(1) or m.group(2)), []))
        elif sections:
            sections[-1][1].append(line)
    # 段落の中の改行は詰める（日本語の文を行の途中で折り返して書いてよい）。評価方法の
    # 箇条書きの段落は、行のまま残す。
    def join(par: str) -> str:
        if any(BLOCK.match(x) or ITEM.match(x) for x in par.split("\n")):
            return par
        return " ".join(par.split("\n")) if par.isascii() else par.replace("\n", "")
    live = [(k, "\n".join(join(par) for par in "\n".join(body).strip().split("\n\n")))
            for k, body in sections if k <= period]
    if not live:
        raise SystemExit(f"[ERROR] {path} に、第 {period} 期に効く「## 第 k 期から」"
                         f"（## From period k）の節がありません。")
    return max(live)


def metric_name(word: str) -> str | None:
    w = word.strip().strip("`")
    return w if w in METRICS else ALIASES.get(w)


def prose(text: str) -> str:
    """本文から「評価方法:」の箇条書きを除いた、自然言語の部分（表示用）。"""
    out, skip = [], False
    for line in text.splitlines():
        if BLOCK.match(line):
            skip = True
        elif skip and (ITEM.match(line) or not line.strip()):
            continue
        else:
            skip = False
            out.append(line)
    return "\n".join(out).strip()


def written(text: str) -> tuple[dict | None, str]:
    """本文に人間が書いた「評価方法:」の箇条書きを読む。
    （評価方法, 読めなかった理由）。箇条書きがなければ (None, "")。"""
    lines = text.splitlines()
    start = next((i for i, x in enumerate(lines) if BLOCK.match(x)), None)
    if start is None:
        return None, ""
    goals, guards = [], []
    for line in lines[start + 1:]:
        if not line.strip():
            if goals or guards:
                break
            continue
        m = ITEM.match(line)
        if not m:
            if re.match(r"^\s*[-*]\s", line) or not (goals or guards):
                return None, f"評価方法の行の形が正しくない（{line.strip()}）"
            break                                  # 箇条書きのあとの文
        if COMPARE.search(m.group(2)) and not COND.match(m.group(2)):
            return None, f"比較は「>=」か「<=」と数値で書く（{line.strip()}）"
        kind = KIND.get(m.group(1), m.group(1).lower())
        body = m.group(2)
        cm = COND.match(body)
        word, op, value = (cm.group(1), cm.group(2), cm.group(3)) if cm else (body, None, None)
        name = metric_name(word)
        if name is None:
            return None, f"未知の指標 {word.strip()!r}（`python -m town.kpi` で一覧を見られます）"
        op = {"≧": ">=", "≦": "<="}.get(op, op)
        x = None if value is None else float(value) / (100 if cm.group(4) else 1)
        if kind == "guard":
            if x is None:
                return None, f"条件には値が要る（{line.strip()}）"
            guards.append({"metric": name, "op": op, "value": x, "text": line.strip()[1:].strip()})
            continue
        if op and op != (">=" if METRICS[name][1] > 0 else "<="):
            return None, (f"{name} は{'大きい' if METRICS[name][1] > 0 else '小さい'}ほどよい指標なので、"
                          f"目標は {'>=' if METRICS[name][1] > 0 else '<='} で書く（{line.strip()}）")
        g = {"metric": name, "text": line.strip()[1:].strip()}
        if x is not None:
            g["target"] = x
        if kind == "goal":
            if any(gg.get("main") for gg in goals):
                return None, "主目標は 1 つだけ書く"
            g["main"] = True
        goals.append(g)
    goals.sort(key=lambda g: not g.get("main"))
    if not goals or not goals[0].pop("main", False):
        return None, "主目標がない"
    spec = {"label": "人間が書いた評価方法", "goals": goals, "guards": guards, "note": ""}
    return spec, validate(spec)


def validate(spec: dict) -> str:
    """評価方法として使えなければ、その理由（使えれば空文字）。"""
    goals = spec.get("goals") or []
    if not goals:
        return "主目標（goals の先頭）がない"
    for g in goals + (spec.get("guards") or []):
        if g.get("metric") not in METRICS:
            return f"未知の指標 {g.get('metric')!r}"
    for g in goals:
        if g.get("target") is not None and not isinstance(g["target"], (int, float)):
            return f"目標値の形が正しくない（{g}）"
    for g in spec.get("guards") or []:
        if g.get("op") not in OPS or not isinstance(g.get("value"), (int, float)):
            return f"条件の形が正しくない（{g}）"
    return ""


def met(metric: str, x: float, target: float) -> bool:
    """目標値に届いているか（向きは指標で決まる）。"""
    return x >= target if METRICS[metric][1] > 0 else x <= target


def fmt(metric: str, x: float) -> str:
    return f"{x:.2f}" if "rate" in metric else f"{x:.1f}" if abs(x) < 100 else f"{x:.0f}"


def mark(spec: dict, metric: str, x: float, msg) -> str:
    """その指標に達成基準か条件があれば、届いているか（なければ空文字）。"""
    oks = [met(metric, x, g["target"]) for g in spec["goals"]
           if g["metric"] == metric and g.get("target") is not None]
    oks += [x >= g["value"] if g["op"] == ">=" else x <= g["value"]
            for g in spec.get("guards") or [] if g["metric"] == metric]
    if not oks:
        return ""
    word = msg("ach_ok") if all(oks) else msg("ach_ng")
    return f"（{word}）" if msg.lang == "ja" else f" ({word})"


def fmt_gain(metric: str, g: float) -> str:
    return f"{g:+.2f}" if "rate" in metric else f"{g:+.1f}"


def achievement(spec: dict, vals: dict[str, float], msg) -> str:
    """目標値と条件に、いまの値が届いているか。目標値も条件もなければ空文字。"""
    out = []
    for g in spec["goals"]:
        if g.get("target") is not None:
            op = ">=" if METRICS[g["metric"]][1] > 0 else "<="
            ok = met(g["metric"], vals[g["metric"]], g["target"])
            out.append(msg("ach_item", m=g["metric"], x=fmt(g["metric"], vals[g["metric"]]), op=op,
                           v=f"{g['target']:g}", ok=msg("ach_ok") if ok else msg("ach_ng")))
    for g in spec.get("guards") or []:
        x = vals[g["metric"]]
        ok = x >= g["value"] if g["op"] == ">=" else x <= g["value"]
        out.append(msg("ach_item", m=g["metric"], x=fmt(g["metric"], x), op=g["op"],
                       v=f"{g['value']:g}", ok=msg("ach_ok") if ok else msg("ach_ng")))
    return msg("comma").join(out)


def gain(spec: dict, cur: dict[str, float], cand: dict[str, float], metric: str | None = None) -> float:
    """改善量（大きいほどよい）。metric を省くと主目標で測る。"""
    name = metric or spec["goals"][0]["metric"]
    return METRICS[name][1] * (cand[name] - cur[name]) + 0.0     # -0.0 を 0.0 にする


def guards_ok(spec: dict, vals: dict[str, float], msg) -> tuple[bool, str]:
    for g in spec.get("guards") or []:
        x = vals[g["metric"]]
        if not (x >= g["value"] if g["op"] == ">=" else x <= g["value"]):
            return False, msg("guard_ng", m=g["metric"], x=round(x, 3), op=g["op"], v=g["value"])
    return True, ""


def worsened(spec: dict, cur: dict[str, float], cand: dict[str, float]) -> list[str]:
    """2 番目以降の目標のうち、誤差を超えて悪化したもの。"""
    return [g["metric"] for g in spec["goals"][1:]
            if gain(spec, cur, cand, g["metric"]) < -METRICS[g["metric"]][2]]


def describe(spec: dict, msg) -> str:
    """評価方法の要約。msg は town.messages.Messages（言語を決める）。"""
    def up(g: dict) -> str:
        word = msg("goal_up") if METRICS[g["metric"]][1] > 0 else msg("goal_down")
        if g.get("target") is None:
            return word
        op = ">=" if METRICS[g["metric"]][1] > 0 else "<="
        return word + msg("comma") + msg("target", op=op, v=f"{g['target']:g}")
    goals = msg("comma").join(f"{g['metric']}（{up(g)}）" if msg.lang == "ja" else f"{g['metric']} ({up(g)})"
                              for g in spec["goals"])
    guards = msg("comma").join(f"{g['metric']} {g['op']} {g['value']:g}" for g in spec.get("guards") or [])
    return msg("main") + goals + (msg("cond") + guards if guards else "")


WAIT_NOTE = ("待ち時間は、注文から届くまでの秒数（道路で止まっている時間ではない）。約束に遅れて届いた"
             "注文も含める。期の終わりに届いていない注文は、期の終わりまでの秒数で数える。"
             "95 パーセンタイルは補間しない（最近傍順位法）。x 秒を超えて待つ注文が 5% 以下のときだけ、"
             "95 パーセンタイルは x 秒以下になる。")


def catalogue() -> str:
    return "\n".join([f"- {k}: {d}（{'大きい' if s > 0 else '小さい'}ほどよい）"
                      for k, (d, s, _, _) in METRICS.items()] + [WAIT_NOTE])


def main() -> int:
    """使える指標と、kpi.md の各節をどう読むかを示す。"""
    import argparse
    p = argparse.ArgumentParser(description=main.__doc__)
    p.add_argument("kpi", nargs="?", type=Path, default=Path(__file__).resolve().parent.parent / "kpi.md")
    args = p.parse_args()
    print("使える指標（名前 / kpi.md に書ける日本語の短い名前：説明）")
    print(f"  {WAIT_NOTE}")
    for k, (d, sign, noise, ja) in METRICS.items():
        print(f"  {k} / {ja}：{d}。{'大きい' if sign > 0 else '小さい'}ほどよい（誤差 {noise:g}）")
    starts = sorted({int(m.group(1) or m.group(2)) for line in args.kpi.read_text(encoding="utf-8").splitlines()
                     if (m := HEADING.match(line))})
    from .messages import Messages
    msg = Messages("ja")
    for k in starts:
        _, text = load(args.kpi, k)
        spec, why = written(text)
        print(f"\n{args.kpi.name} 第 {k} 期から：", end="")
        if spec is None and not why:
            print("評価方法の箇条書きはない（LLM が自然言語から読む）")
        elif why:
            print(f"[ERROR] {why}")
        else:
            print(describe(spec, msg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
