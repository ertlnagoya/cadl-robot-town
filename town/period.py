"""1 期（既定 1 時間）を、規則ファイル 1 つで、複数の乱数シードについて回す。

    python -m town.period                         # cadl/robot_town.cadl
    python -m town.period cadl/robot_town.cadl --seeds 1 2 3
    python -m town.period --interval 45          # 繁忙期（注文の平均間隔 45 秒）

規則は必ず `cadl check` と `cadl sim-ir` を通してから使う。通らない
規則では街を動かさない（フェイルクローズド）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path

import yaml

from .world import ORDER_MEAN_INTERVAL_S, Town, read_regimes, read_rules

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES = ROOT / "cadl" / "robot_town.cadl"
CONSTITUTION = ROOT / "constitution.yaml"
SCENARIO = ROOT / "scenario.yaml"


class CompileError(Exception):
    pass


def compile_rules(path: Path) -> dict:
    """cadl check → cadl sim-ir。どちらかが失敗したら CompileError。"""
    check = subprocess.run(["cadl", "check", str(path)], capture_output=True, text=True)
    if check.returncode != 0:
        raise CompileError((check.stdout + check.stderr).strip())
    ir = subprocess.run(["cadl", "sim-ir", str(path), "--format", "json"],
                        capture_output=True, text=True)
    if ir.returncode != 0:
        raise CompileError((ir.stdout + ir.stderr).strip())
    doc = json.loads(ir.stdout)
    try:
        read_rules(doc)                  # 街を動かすのに必要な値が IR にそろっているか
        read_regimes(doc)
    except (KeyError, StopIteration, TypeError, ValueError, IndexError) as e:
        raise CompileError(f"IR に規則の値がそろっていません（{type(e).__name__}: {e}）")
    # cadl 0.3.8 の sim-ir は sos.transitions[*].safety_invariant を IR に出力しない
    # （cadl check は式として検査する）。切替の安全条件を判定するため、検査を通った
    # ソースから読んで添える。
    src = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    doc["x_safety_invariants"] = {
        f"{tr['from']}->{tr['to']}": tr["safety_invariant"]
        for tr in (src.get("sos", {}).get("transitions") or [])
        if tr.get("safety_invariant")
    }
    return doc


def evaluate(ir: dict, seeds: list[int], period_s: int,
             order_interval_s: float = ORDER_MEAN_INTERVAL_S,
             mode: str = "adaptive", rush: dict | None = None,
             quiescence: bool = True) -> dict:
    """シードごとに街を動かし、平均した指標と、シードごとの結果を返す。"""
    runs = [Town(ir, seed, period_s, order_interval_s, mode, rush, quiescence).run()
            for seed in seeds]
    n = len(runs)
    companies = {}
    for c in runs[0]["companies"]:
        keys = runs[0]["companies"][c].keys()
        companies[c] = {k: sum(r["companies"][c][k] for r in runs) / n for k in keys}
    violations = Counter()
    for r in runs:
        violations.update(r["violations"])
    avg = lambda k: sum(r[k] for r in runs) / n
    return {
        "central_share": avg("central_share"),
        "switches": avg("switches"),
        "switch_s": avg("switch_s"),
        "forced_evictions": avg("forced_evictions"),
        "invariant_breaches": avg("invariant_breaches"),
        "on_time_deliveries": sum(r["on_time_deliveries"] for r in runs) / n,
        "orders": sum(r["orders"] for r in runs) / n,
        "unfinished": sum(r["unfinished"] for r in runs) / n,
        "blocked_s": sum(r["blocked_s"] for r in runs) / n,
        "violations": {k: v / n for k, v in sorted(violations.items())},
        "violation_total": sum(violations.values()) / n,
        # 道路の規則（ROAD_USE）の違反だけ：期限 release の超過と blocking_guard の退去
        "road_violations": sum(v for k, v in violations.items()
                               if k in ("deadline:release", "monitor:blocking_guard")) / n,
        "companies": companies,
        "runs": runs,
    }


def print_report(rules_path: Path, ir: dict, m: dict) -> None:
    r = read_rules(ir)
    print(f"規則: {rules_path.name}  (hold_limit={r.hold_limit_s}s, "
          f"block_k={r.block_k}, sanction_lambda={r.sanction_lambda})")
    print(f"  定時配達 {m['on_time_deliveries']:.1f} / 注文 {m['orders']:.1f}"
          f"  未配達 {m['unfinished']:.1f}  ふさがれた時間 {m['blocked_s']:.0f}s"
          f"  （シード平均）")
    print("  会社  定時率  遅配  道路で待った回数  ふさがれた秒数  罰点   効用")
    for c, x in m["companies"].items():
        print(f"   {c}    {x['on_time_rate']:5.2f}  {x['late']:4.1f}  {x['parks']:16.1f}"
              f"  {x['blocked_s']:14.0f}  {x['sanction']:5.1f}  {x['utility']:5.1f}")
    v = ", ".join(f"{k}={n:.1f}" for k, n in m["violations"].items()) or "(なし)"
    print(f"  違反（原因別，シード平均）: {v}")
    print(f"  運用モード: 集中の時間 {100 * m['central_share']:.0f}%  切替 {m['switches']:.1f} 回"
          f"（切替中の合計 {m['switch_s']:.0f}s，管理者による退去 {m['forced_evictions']:.1f} 台，"
          f"安全条件の違反 {m['invariant_breaches']:.1f} 回）")


def main() -> int:
    const = yaml.safe_load(CONSTITUTION.read_text(encoding="utf-8"))
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("rules", nargs="?", type=Path, default=DEFAULT_RULES)
    p.add_argument("--seeds", type=int, nargs="+", default=const["seeds"])
    p.add_argument("--period", type=int, default=const["period_s"])
    p.add_argument("--interval", type=float, default=ORDER_MEAN_INTERVAL_S,
                   help="各社の注文の平均間隔（秒）")
    p.add_argument("--rush", type=float, nargs=3, metavar=("FROM_S", "TO_S", "INTERVAL"),
                   help="期の途中のラッシュ（開始秒・終了秒・その間の注文の平均間隔）")
    p.add_argument("--no-quiescence", action="store_true",
                   help="道路上の待機が残っていても集中管理への切替をすぐ有効にする")
    p.add_argument("--mode", default="adaptive",
                   choices=["adaptive", "DISTRIBUTED", "CENTRALIZED"],
                   help="adaptive は CADL の条件で切り替える。ほかは固定")
    args = p.parse_args()
    try:
        ir = compile_rules(args.rules)
    except CompileError as e:
        print(f"[REJECT] 規則が検査を通りません。街は動かしません。\n{e}")
        return 1
    print(f"需要: 各社の注文の平均間隔 {args.interval:g} 秒")
    rush = None
    if args.rush:
        rush = dict(zip(("from_s", "to_s", "order_interval_s"), args.rush))
        print(f"ラッシュ: {rush['from_s']:g}〜{rush['to_s']:g} 秒は注文の平均間隔 {rush['order_interval_s']:g} 秒")
    print(f"運用モード: {args.mode}")
    print_report(args.rules, ir,
                 evaluate(ir, args.seeds, args.period, args.interval, args.mode, rush,
                          not args.no_quiescence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
