"""複数の版の規則を、同じ環境・同じシードで並べて比べる。

    python -m town.compare runs/goodhart/v1.cadl runs/goodhart/v6.cadl
    python -m town.compare runs/*/v*.cadl --interval 45 --seeds 1001 1002 1003

改正ループが評価に使っていないシード（既定 1001〜1005）で比べると、
ループが目標の数字だけを良くしたのか、街が本当に良くなったのかが分かる。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .period import CompileError, compile_rules, evaluate
from .world import ORDER_MEAN_INTERVAL_S, read_rules


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("rules", type=Path, nargs="+")
    p.add_argument("--interval", type=float, default=ORDER_MEAN_INTERVAL_S,
                   help="各社の注文の平均間隔（秒）")
    p.add_argument("--seeds", type=int, nargs="+", default=[1001, 1002, 1003, 1004, 1005])
    p.add_argument("--period", type=int, default=3600)
    p.add_argument("--rush", type=float, nargs=3, metavar=("FROM_S", "TO_S", "INTERVAL"))
    p.add_argument("--mode", default="adaptive",
                   choices=["adaptive", "DISTRIBUTED", "CENTRALIZED"])
    args = p.parse_args()
    rush = dict(zip(("from_s", "to_s", "order_interval_s"), args.rush)) if args.rush else None

    print(f"需要: 注文の平均間隔 {args.interval:g} 秒"
          + (f"（{args.rush[0]:g}〜{args.rush[1]:g} 秒は {args.rush[2]:g} 秒）" if rush else "")
          + f"  運用モード {args.mode}  シード {args.seeds}")
    print("  規則                          hold block   λ  定時配達  遅配  道路違反  ふさがれた"
          "  集中   A定時率  効用 A / B / C")
    for path in args.rules:
        try:
            ir = compile_rules(path)
        except CompileError as e:
            print(f"  {str(path):28s}  [REJECT] {str(e).splitlines()[0] if str(e) else ''}")
            continue
        r = read_rules(ir)
        m = evaluate(ir, args.seeds, args.period, args.interval, args.mode, rush)
        c = m["companies"]
        late = sum(x["late"] for x in c.values())
        print(f"  {str(path):28s} {r.hold_limit_s:4d} {r.block_k:5d} {r.sanction_lambda:4.1f}"
              f" {m['on_time_deliveries']:8.1f} {late:5.1f} {m['road_violations']:8.1f}"
              f" {m['blocked_s']:9.0f}s  {100 * m['central_share']:3.0f}%  {c['A']['on_time_rate']:6.2f}"
              f"  {c['A']['utility']:.1f} / {c['B']['utility']:.1f} / {c['C']['utility']:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
