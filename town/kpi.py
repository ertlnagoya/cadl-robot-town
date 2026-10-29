"""KPI：人間が自然言語で書いたサービスの目標と、その測り方。

kpi.md は「## 第 k 期から」（英語なら「## From period k」）の見出しで区切る。第 k 期には、見出しの番号が
k 以下で最大の節が効く。人間は期と期のあいだに kpi.md を書き換えてよい
（town.steward は期の初めに読み直す）。

自然言語の KPI を、LLM が下の METRICS の組み合わせ（測り方）に言い換える。

    {"goals":  [{"metric": "on_time_deliveries", "text": "町全体の定時配達を増やす"}, ...],
     "guards": [{"metric": "min_company_on_time_rate", "op": ">=", "value": 0.8,
                 "text": "どの会社の定時率も 8 割を下回らない"}, ...]}

goals の先頭が主目標で、改善はこれで測る。2 番目以降の目標は、悪化を
見張る（悪化すれば、KPI どうしの衝突として人間に判断を求める）。guards は、
改正後に必ず守る条件で、憲法の不変条件に加わる。
"""
from __future__ import annotations

import re
from pathlib import Path

# 名前: (説明, 向き（+1 は大きいほどよい）, 誤差とみなす差)
METRICS = {
    "on_time_deliveries": ("町全体の定時配達件数", +1, 1.0),
    "late_deliveries": ("町全体の遅配件数", -1, 1.0),
    "unfinished": ("期の終わりに未配達のまま残る注文の件数", -1, 0.5),
    "blocked_s": ("止まっているロボットにふさがれた時間の合計（秒）", -1, 60.0),
    "road_violations": ("道路の規則の違反件数（release の期限超過と blocking_guard の退去）", -1, 1.0),
    "violation_total": ("違反の合計件数（配達の遅れを含む）", -1, 1.0),
    "min_company_on_time_rate": ("最も低い会社の定時率", +1, 0.02),
    "on_time_rate_gap": ("会社ごとの定時率の、最大と最小の差", -1, 0.02),
    "utility_gap": ("会社ごとの効用（定時配達件数 − 罰点）の、最大と最小の差", -1, 1.0),
    "sanction_total": ("全社の罰点の合計", -1, 1.0),
}
OPS = (">=", "<=")
HEADING = re.compile(r"^##\s*(?:第\s*(\d+)\s*期から|From period\s+(\d+))\s*$", re.IGNORECASE)


def measure(m: dict) -> dict[str, float]:
    """evaluate() の結果（シード平均）にも、シードごとの結果にも使える。"""
    c = m["companies"].values()
    v = m["violations"]
    rates = [x["on_time_rate"] for x in c]
    utils = [x["utility"] for x in c]
    return {
        "on_time_deliveries": m["on_time_deliveries"],
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
    """第 period 期に効く節の（開始の期, 本文）。"""
    sections: list[tuple[int, list[str]]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = HEADING.match(line)
        if m:
            sections.append((int(m.group(1) or m.group(2)), []))
        elif sections:
            sections[-1][1].append(line)
    # 段落の中の改行は詰める（日本語の文を行の途中で折り返して書いてよい）
    live = [(k, "\n".join(" ".join(par.split("\n")) if par.isascii() else par.replace("\n", "")
                           for par in "\n".join(body).strip().split("\n\n")))
            for k, body in sections if k <= period]
    if not live:
        raise SystemExit(f"[ERROR] {path} に、第 {period} 期に効く「## 第 k 期から」"
                         f"（## From period k）の節がありません。")
    return max(live)


def validate(spec: dict) -> str:
    """測り方として使えなければ、その理由（使えれば空文字）。"""
    goals = spec.get("goals") or []
    if not goals:
        return "主目標（goals の先頭）がない"
    for g in goals + (spec.get("guards") or []):
        if g.get("metric") not in METRICS:
            return f"未知の指標 {g.get('metric')!r}"
    for g in spec.get("guards") or []:
        if g.get("op") not in OPS or not isinstance(g.get("value"), (int, float)):
            return f"条件の形が正しくない（{g}）"
    return ""


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
    """測り方の要約。msg は town.messages.Messages（言語を決める）。"""
    up = lambda g: msg("goal_up") if METRICS[g["metric"]][1] > 0 else msg("goal_down")
    goals = msg("comma").join(f"{g['metric']}（{up(g)}）" if msg.lang == "ja" else f"{g['metric']} ({up(g)})"
                              for g in spec["goals"])
    guards = msg("comma").join(f"{g['metric']} {g['op']} {g['value']:g}" for g in spec.get("guards") or [])
    return msg("main") + goals + (msg("cond") + guards if guards else "")


def catalogue() -> str:
    return "\n".join(f"- {k}: {d}（{'大きい' if s > 0 else '小さい'}ほどよい）"
                     for k, (d, s, _) in METRICS.items())
