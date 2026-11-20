"""判断の証拠（evidence.json）：合意形成と説明責任の記録を、機械で読める形に残す。

decisions.md と gsn.yaml は人が読むための記録で、数値は表の中の文字列にしかない。
evidence.json は同じ実行を、次の問いに答えられる形で残す。

- どの KPI のもとで（K の記録：人間が書いた文、評価方法、LLM の読み方と選択肢、人間の選択と理由）
- 何を測り（期ごとの D の記録：現行の値、各案の値・判定・各社の投票）
- 誰が何を勧め（LLM の評価・推奨・分からないこと）
- 誰がどう決めたか（自動か人間か、投票を覆したか、LLM の推奨に従ったか、理由と前提）

改ざんに気づけるように、記録は順に鎖のハッシュ（chain）でつなぎ、実行ディレクトリの
ファイル（規則のバージョン、測った表、LLM の出力）は SHA-256 を残す。

    python -m town.evidence verify runs/<名前>     # 鎖とファイルのハッシュを確かめる

cadl-explorer の Decision Evidence のページは、このファイルを読んで図にする。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

FORMAT = "robot-town-evidence/1"
FILE = "evidence.json"
# triage() が付ける理由（日本語の内部名）を、言語によらない名前に
REASON_CODE = {"合意": "agreement", "契約": "contract", "規制": "regulation", "安全": "safety",
               "KPI の衝突": "kpi_conflict", "不確か": "uncertain", "LLM": "llm"}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def utilities(m: dict | None) -> dict[str, float] | None:
    if not m:
        return None
    return {c: round(m["companies"][c]["utility"], 3) for c in sorted(m["companies"])}


def rounded(vals: dict | None) -> dict | None:
    return None if vals is None else {k: round(float(v), 4) for k, v in vals.items()}


class Evidence:
    """steward の実行中に記録を集め、期の終わりごとに evidence.json を書き直す。"""

    def __init__(self, run_dir: Path, run: dict, inputs: dict[str, Path]):
        self.run_dir = run_dir
        self.doc = {
            "format": FORMAT,
            "run": {**run, "created": dt.datetime.now().astimezone().isoformat(timespec="seconds")},
            "inputs": {k: {"path": str(p.name), "sha256": sha256_file(p)} for k, p in inputs.items()},
            "records": [],
            "gsn": [],
            "artifacts": [],
            "chain": [],
        }

    def add(self, record: dict) -> None:
        self.doc["records"].append(record)

    def save(self, gsn_nodes: list[dict]) -> None:
        self.doc["gsn"] = gsn_nodes
        self.doc["chain"] = chain(self.doc["records"])
        self.doc["artifacts"] = [
            {"path": str(f.relative_to(self.run_dir)), "sha256": sha256_file(f), "bytes": f.stat().st_size}
            for f in sorted(self.run_dir.rglob("*"))
            if f.is_file() and f.name != FILE and not f.name.startswith(".")
            and not (f.parent == self.run_dir and f.name.startswith("p") and f.suffix == ".cadl")]
        (self.run_dir / FILE).write_text(json.dumps(self.doc, ensure_ascii=False, indent=1) + "\n",
                                         encoding="utf-8")


def chain(records: list[dict]) -> list[dict]:
    """記録ごとに、前の鎖の値と記録の内容から SHA-256 を作る。途中の記録を変えると後ろがすべて変わる。"""
    out, prev = [], "0" * 64
    for r in records:
        h = hashlib.sha256(prev.encode() + canonical(r)).hexdigest()
        out.append({"id": r["id"], "prev": prev, "hash": h})
        prev = h
    return out


def candidate(c, reason_text: str) -> dict:
    """改正案 1 つ：LLM が書いたもの（title, rationale）と、機械が測ったもの（それ以外）。"""
    return {
        "id": c.cid, "kind": c.kind, "title": c.title, "label": c.label or c.title,
        "layer": c.layer or None, "safety": c.safety,
        "rationale": c.rationale,                         # LLM
        "llm_flag": c.llm_flag or None,                   # LLM が人間に回したいと書いた理由
        "gates": c.gates, "rejected_because": c.why or None, "kpi_guard_note": c.kpi_why or None,
        "metrics": rounded(c.vals), "utility": utilities(c.m),
        "gain": round(c.gain, 4) if c.vals is not None else None,
        "seeds_better": c.seeds_better if c.m else None, "seeds": len(c.m["runs"]) if c.m else None,
        "votes": c.votes or None, "worse": c.worse,
        "escalation": [REASON_CODE.get(r, r) for r in c.reasons], "escalation_text": reason_text or None,
        "effect": c.effect or None,
    }


def verify(run_dir: Path) -> list[str]:
    """鎖と、残っているファイルのハッシュを確かめる。問題のリストを返す（空なら問題なし）。"""
    doc = json.loads((run_dir / FILE).read_text(encoding="utf-8"))
    problems = []
    if doc.get("format") != FORMAT:
        problems.append(f"format {doc.get('format')!r} (expected {FORMAT})")
    recomputed = chain(doc["records"])
    for got, want in zip(doc["chain"], recomputed):
        if got != want:
            problems.append(f"chain: record {want['id']} does not match its hash")
            break
    if len(doc["chain"]) != len(recomputed):
        problems.append(f"chain: {len(doc['chain'])} links for {len(recomputed)} records")
    for a in doc["artifacts"]:
        f = run_dir / a["path"]
        if not f.exists():
            problems.append(f"missing: {a['path']}")
        elif sha256_file(f) != a["sha256"]:
            problems.append(f"changed: {a['path']}")
    return problems


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="鎖とファイルのハッシュを確かめる")
    v.add_argument("run_dir", type=Path)
    args = p.parse_args()
    problems = verify(args.run_dir)
    for x in problems:
        print("NG", x)
    if not problems:
        doc = json.loads((args.run_dir / FILE).read_text(encoding="utf-8"))
        print(f"OK {len(doc['records'])} records, {len(doc['artifacts'])} files, "
              f"last hash {doc['chain'][-1]['hash'][:16] if doc['chain'] else '—'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
