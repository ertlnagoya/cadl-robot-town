"""判断の記録：自然言語の decisions.md と、GSN（Goal Structuring Notation）の gsn.yaml。

GSN の木は次の形に育つ。

    G0  街の運用が、人間の定めた KPI を満たし続ける
     └ S1  KPI の版ごとに論じる
        ├ G   第 1 期からの KPI（人間が書いた文）         ┄ C 測り方
        │  ├ S   第 1 期の判断（採択した改正、または変えない）
        │  │  ├ Sn  測った結果（p1-evidence.md）
        │  │  ┄ C   その期の環境、 A 前提、 J 判断の理由
        │  └ ...
        └ G   第 5 期からの KPI ...

人間は、判断の理由を自然言語（reason）で書いても、GSN の要素（gsn: に
assumption / context / justification / solution / goal / strategy）で書いてもよい。
gsn.md には Mermaid の図を書き出す。
"""
from __future__ import annotations

from pathlib import Path

import yaml

KINDS = {"goal": "G", "strategy": "S", "solution": "Sn", "context": "C",
         "assumption": "A", "justification": "J"}
# supported_by（実線）でつなぐ種類。ほかは in_context_of（点線）
SUPPORT = {"goal", "strategy", "solution"}


class Gsn:
    def __init__(self, msg) -> None:
        self.nodes: list[dict] = []
        self.count: dict[str, int] = {}
        self.msg = msg
        self.top = self.add("goal", msg("gsn_top"))
        self.by_version = self.add("strategy", msg("gsn_by_version"), self.top)

    def add(self, kind: str, text: str, parent: str | None = None, **extra) -> str:
        prefix = KINDS[kind]
        n = self.count.get(prefix, 0) + (0 if prefix == "G" and not self.nodes else 1)
        self.count[prefix] = n
        nid = f"{prefix}{n}"
        node = {"id": nid, "type": kind, "text": text, **extra}
        self.nodes.append(node)
        if parent:
            p = next(x for x in self.nodes if x["id"] == parent)
            p.setdefault("supported_by" if kind in SUPPORT else "in_context_of", []).append(nid)
        return nid

    def save(self, path: Path) -> None:
        path.write_text(yaml.safe_dump({"nodes": self.nodes}, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
        path.with_suffix(".md").write_text(self.mermaid(), encoding="utf-8")

    def mermaid(self) -> str:
        shape = {"goal": ('["', '"]'), "strategy": ('[/"', '"/]'), "solution": ('(("', '"))'),
                 "context": ('("', '")'), "assumption": ('(["', '"])'),
                 "justification": ('(["', '"])')}
        lines = [self.msg("gsn_title"), "", "```mermaid", "flowchart TD"]
        for n in self.nodes:
            a, b = shape[n["type"]]
            text = n["text"].replace('"', "'").replace("\n", " ")
            if len(text) > 60:
                text = text[:59] + "…"
            lines.append(f"  {n['id']}{a}{n['id']}: {text}{b}")
        for n in self.nodes:
            for c in n.get("supported_by", []):
                lines.append(f"  {n['id']} --> {c}")
            for c in n.get("in_context_of", []):
                lines.append(f"  {n['id']} -.-> {c}")
        lines += ["```", ""]
        return "\n".join(lines)


def attach_answer(gsn: Gsn, parent: str, answer: dict) -> None:
    """人間の回答（自然言語の reason と、GSN の要素 gsn:）を parent の下につなぐ。"""
    if answer.get("reason"):
        gsn.add("justification", answer["reason"], parent, by="human")
    for a in answer.get("assumptions") or []:
        gsn.add("assumption", a, parent, by="human")
    for item in answer.get("gsn") or []:
        kind = item.get("type")
        if kind not in KINDS:
            raise SystemExit(f"[ERROR] GSN の要素の種類 {kind!r} が分かりません"
                             f"（使えるのは {', '.join(KINDS)}）。")
        gsn.add(kind, item["text"], parent, by="human")
