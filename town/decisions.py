"""判断の記録（decisions.jsonl）：仕様の付録 L（decisions 0.1）の形式で書き出す。

steward と council の実行ごとに、runs/<名前>/decisions.jsonl を書く。人が読む
decisions.md、gsn.yaml と同じ実行を、機械で読める形で残す。

- 当事者：KPI を書き判断する人間（operator）、提案し推奨する LLM（llm）、判断要求に
  人間に代わって答える LLM（llm-decider、--decider llm のとき）、測って判定し、
  判定をすべて通った案を自動で採択するツール（steward または council）、投票する
  3 社（A、B、C）、投票に加わらない顧客（customers）。
- 項目：目的（KPI）、改正案、測定、判定、投票、判断要求、推奨、判断、バージョン、
  後で確かめる約束。人間でも LLM でもツールでも、提案・推奨・判断には同じ証拠の
  フィールド（理由、前提、分からないこと、予測）を持たせる。
- 完全性：各行に、付録 G の G.8 のハッシュの連鎖（seq、prev、digest）を付ける。

    cadl decisions-verify runs/<名前>/decisions.jsonl     # 検査と、確認のための指摘

書き出しには cadl 0.3.37 以降の cadl.decisions を使う。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path

FILE = "decisions.jsonl"
KEEP = "keep"
GATE_NAMES = {"check": "cadl check", "const": "constitution", "inv": "invariants",
              "kpi_guard": "kpi guards", "gain": "gain", "seeds": "seeds", "vote": "vote"}
_LABEL = re.compile(r"^(?P<name>[A-Za-z_.]+)\s+(?P<a>-?[\d.]+)(?P<unit>[a-z]*)\s+→\s+(?P<b>-?[\d.]+)")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def party(pid, kind, roles, name=None, actor=None, model=None, on_behalf_of=None) -> dict:
    return {"id": pid, "kind": kind, "roles": roles, "name": name, "actor": actor,
            "model": model, "on_behalf_of": on_behalf_of}


def parties(tool: str, tool_version: str, llm_model: str | None, proposer: str,
            decider: str = "human") -> list[dict]:
    """proposer: llm か tool（council の neighbors）。decider: human か llm。"""
    tool_roles = ["assessor", "decider"] + (["proposer"] if proposer == "tool" else [])
    out = [party("operator", "human", ["owner"] + (["decider"] if decider == "human" else []),
                 "service operator", "OPERATOR")]
    if llm_model:
        out.append(party("llm", "llm", ["proposer", "advisor"] if proposer == "llm" else ["advisor"],
                         None, None, llm_model))
    if decider == "llm":
        out.append(party("llm-decider", "llm", ["decider"], "LLM deciding for the operator",
                         None, llm_model, on_behalf_of="operator"))
    out.append(party(tool, "tool", tool_roles, f"town.{tool}", None, tool_version))
    out += [party(c, "agent", ["voter"], f"company {c}") for c in ("A", "B", "C")]
    out.append(party("customers", "organisation", ["affected"], "customers"))
    return out


def change(label: str, layer: str | None) -> list[dict]:
    """「block_k 3 → 2」「hold_limit 60s → 70s」を、改正の変更の並びに。"""
    m = _LABEL.match(label or "")
    if not m:
        return [{"parameter": (label or "?").split(" ")[0] or "?", "from": None, "to": None,
                 "layer": layer}]
    unit = m.group("unit")
    return [{"parameter": m.group("name"), "from": m.group("a") + unit, "to": m.group("b") + unit,
             "layer": layer}]


def evidence(reason=None, assumptions=(), unknowns=(), predictions=()) -> dict:
    return {"reason": reason, "assumptions": [str(x) for x in assumptions or []],
            "unknowns": [str(x) for x in unknowns or []],
            "predictions": list(predictions or [])}


def predictions(items, option: str, voters=("A", "B", "C")) -> list[dict]:
    """LLM や人間が書いた予測（subject, expected, note）を、付録 L の形に。形の合わないものは除く。"""
    out = []
    for p in items or []:
        if not isinstance(p, dict):
            continue
        subject, expected = str(p.get("subject", "")).strip(), str(p.get("expected", "")).strip()
        if subject.startswith("vote:") and subject[5:] not in voters:
            continue
        if not (subject.startswith("metric:") or subject.startswith("vote:")) or not expected:
            continue
        out.append({"option": str(p.get("option") or option), "subject": subject,
                    "expected": expected, "note": p.get("note") or None})
    return out


class Record:
    """判断の記録の項目を集め、期の終わりごとに decisions.jsonl を書き直す。"""

    def __init__(self, run_dir: Path, model_name: str, first: Path, parties_: list[dict],
                 time_base: str, key: bytes | None = None):
        try:
            from cadl.decisions import DecisionChain    # cadl 0.3.37 以降
        except ImportError:                             # 古い cadl：記録は書かずに続ける
            DecisionChain = None
            print("  [注意] cadl 0.3.37 以降がないので、判断の記録（decisions.jsonl）は書き出しません。")
        self.chain_cls = DecisionChain
        self.run_dir, self.parties, self.time_base, self.key = run_dir, parties_, time_base, key
        self.model = {"name": model_name, "version": "v1", "source_sha256": sha256(first)}
        self.started = now()
        self.entries: list[dict] = []
        self.run_id = None

    def add(self, eid: str, type_: str, by: str, period: int, basis=(), fulfils=(),
            attachments=(), generated=None, **fields) -> str:
        self.entries.append({"id": eid, "type": type_, "point": {"period": period},
                             "written": now(), "by": by, "basis": list(basis),
                             "fulfils": list(fulfils), "attachments": list(attachments),
                             "generated": generated, **fields})
        return eid

    def ids(self, *types: str, period: int | None = None) -> list[str]:
        return [e["id"] for e in self.entries if e["type"] in types
                and (period is None or e["point"]["period"] == period)]

    def save(self, complete: bool = False) -> str:
        if self.chain_cls is None:
            return "—"
        chain = self.chain_cls(key=self.key, run_id=self.run_id)
        self.run_id = chain.run_id
        lines = [chain.header(self.model, self.parties, self.started, self.time_base)]
        lines += [chain.entry(e) for e in self.entries]
        if complete:
            lines.append(chain.end())
        (self.run_dir / FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return chain.last_digest

    # --- 共通の項目 ---------------------------------------------------------
    def measurement(self, eid: str, by: str, period: int, of: str, m: dict, vals: dict,
                    sim: dict, compared_with: str | None = None, runs_better: int | None = None,
                    version: str | None = None, fulfils=()) -> str:
        per = {c: {"utility": round(x["utility"], 3), "on_time_rate": round(x["on_time_rate"], 4),
                   "late_deliveries": round(x["late"], 3), "p95_wait_s": round(x["p95_wait_s"], 3)}
               for c, x in sorted(m["companies"].items())}
        per["customers"] = {k: round(vals[k], 4) for k in
                            ("late_deliveries", "unfinished", "worst_company_p95_wait_s", "p95_wait_s")
                            if k in vals}
        return self.add(eid, "measurement", by, period, fulfils=fulfils, of=of, version=version,
                        method={"runs": len(sim["seeds"]), "window": f"{sim['period_s']}s",
                                "description": f"seeds {', '.join(map(str, sim['seeds']))}; mean "
                                               f"order interval {sim['order_interval_s']} s"},
                        values={k: round(float(v), 4) for k, v in vals.items()},
                        per_party=per, compared_with=compared_with, runs_better=runs_better)

    def checks(self, eid: str, by: str, period: int, proposal: str, gates: dict, notes: str) -> str:
        results = [{"name": GATE_NAMES.get(g, g), "passed": v, "note": None}
                   for g, v in gates.items()]
        if notes and results:
            results[-1]["note"] = notes
        return self.add(eid, "check", by, period, proposal=proposal, results=results)

    def votes(self, prefix: str, period: int, proposal: str, votes: dict) -> list[str]:
        return [self.add(f"{prefix}.v{c}", "vote", c, period, proposal=proposal,
                         value="yes" if ok else "no", reason=None)
                for c, ok in sorted(votes.items())]
