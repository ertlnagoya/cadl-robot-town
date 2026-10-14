"""委員会（COUNCIL）による規則の改正ループ。

    python -m town.council                  # 1 期ぶん改正を試みる
    python -m town.council --rounds 5       # 5 期続ける
    python -m town.council --objective violations   # 違反の少なさを目標にする

1 期ごとに、次の 4 つの関門を順に通った改正案だけを採択する。

  1. 検査   ：cadl check / cadl sim-ir を通る
  2. 憲法   ：constitution.yaml の不変条件（各社の定時率の下限など）を守る
  3. 改善   ：現行の規則より目標（objective）が min_improvement 以上よい
  4. 投票   ：自社の効用が下がらない会社が quorum 社以上いる

改正案は、constitution.yaml の amendable に挙げたパラメータを 1 つだけ
1 ステップ動かしたもの（現行の規則の「隣」）をすべて試す。比較は同じ
乱数シードで行う。採択された規則は runs/<名前>/ に版として残る。

第 k 期の環境（需要）は scenario.yaml の periods[k-1] で決まる。現行の
規則も、その期の環境で評価し直してから改正案と比べる。
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
from pathlib import Path

import yaml

from .period import (CONSTITUTION, DEFAULT_RULES, ROOT, SCENARIO, CompileError,
                     compile_rules, evaluate)

TAG = re.compile(r"#\s*@amend:(\w+)")
NUM = re.compile(r"\d+(?:\.\d+)?")


def read_params(text: str) -> dict[str, float]:
    """`# @amend:<名前>` が付いた行の、コメントより前にある最初の数値を読む。"""
    params = {}
    for line in text.splitlines():
        m = TAG.search(line)
        if m:
            params[m.group(1)] = float(NUM.search(line[:m.start()]).group())
    return params


def write_param(text: str, name: str, value: float) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        m = TAG.search(line)
        if m and m.group(1) == name:
            body = line[:m.start()]
            n = NUM.search(body)
            v = f"{value:g}" if "." not in n.group() else f"{value:.1f}"
            line = body[:n.start()] + v + body[n.end():] + line[m.start():]
        out.append(line)
    return "".join(out)


def candidates(params: dict[str, float], amendable: dict) -> list[tuple[str, float, float]]:
    """各パラメータを 1 ステップ上げ下げした案（憲法の範囲内のものだけ）。"""
    out = []
    for name, spec in amendable.items():
        cur = params[name]
        for v in (cur - spec["step"], cur + spec["step"]):
            v = round(v, 6)
            if spec["min"] <= v <= spec["max"]:
                out.append((name, cur, v))
    return out


def score(m: dict, objective: str) -> float:
    """大きいほどよい。"""
    if objective == "on_time_deliveries":
        return m["on_time_deliveries"]
    if objective == "violations":
        return -m["violation_total"]
    raise ValueError(objective)


def invariants_ok(m: dict, inv: dict) -> tuple[bool, str]:
    low = min(x["on_time_rate"] for x in m["companies"].values())
    if low < inv["min_company_on_time_rate"]:
        return False, f"定時率の下限割れ（{low:.2f}）"
    if m["unfinished"] > inv["max_unfinished_orders"]:
        return False, f"未配達 {m['unfinished']:.1f} 件"
    return True, ""


def ballots(cur: dict, cand: dict) -> dict[str, bool]:
    """各社は、自社の効用が下がらなければ賛成する（0.5 未満の差は誤差とみなす）。"""
    return {
        c: cand["companies"][c]["utility"] >= cur["companies"][c]["utility"] - 0.5
        for c in cur["companies"]
    }


def fmt(name: str, old: float, new: float, unit: str) -> str:
    return f"{name} {old:g}{unit} → {new:g}{unit}"


def main() -> int:
    const = yaml.safe_load(CONSTITUTION.read_text(encoding="utf-8"))
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--objective", default=const["objective"],
                   choices=["on_time_deliveries", "violations"])
    p.add_argument("--name", default=dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    args = p.parse_args()

    periods = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))["periods"]
    period = const["period_s"]
    run_dir = ROOT / "runs" / args.name
    run_dir.mkdir(parents=True, exist_ok=True)
    text = args.rules.read_text(encoding="utf-8")
    version = 1
    (run_dir / f"v{version}.cadl").write_text(text, encoding="utf-8")
    history = [f"# 改正履歴（目標: {args.objective}）\n"]

    try:
        ir = compile_rules(run_dir / f"v{version}.cadl")
    except CompileError as e:
        print(f"[REJECT] 現行の規則が検査を通りません。\n{e}")
        return 1

    for rnd in range(1, args.rounds + 1):
        env = periods[(rnd - 1) % len(periods)]
        seeds = [s + env["seed_offset"] for s in const["seeds"]]
        interval = env["order_interval_s"]
        cur = evaluate(ir, seeds, period, interval)
        params = read_params(text)
        print(f"\n=== 第 {rnd} 期（{env['label']}：注文の平均間隔 {interval:g} 秒）"
              f"  現行 v{version}: "
              + ", ".join(f"{k}={v:g}" for k, v in params.items()) + " ===")
        print(f"  現行: 定時配達 {cur['on_time_deliveries']:.1f}  違反 {cur['violation_total']:.1f}"
              f"  ふさがれた時間 {cur['blocked_s']:.0f}s  効用 "
              + " ".join(f"{c}={x['utility']:.1f}" for c, x in cur["companies"].items()))
        print("  改正案                              定時配達  違反   検査 憲法 改善 投票(A B C)")
        best = None
        for name, old, new in candidates(params, const["amendable"]):
            unit = const["amendable"][name]["unit"]
            label = fmt(name, old, new, unit)
            cand_text = write_param(text, name, new)
            path = run_dir / f"round{rnd}-{name}-{new:g}.cadl"
            path.write_text(cand_text, encoding="utf-8")
            try:
                cand_ir = compile_rules(path)
            except CompileError:
                print(f"  {label:34s}      -      -    ✗")
                continue
            m = evaluate(cand_ir, seeds, period, interval)
            ok_inv, why = invariants_ok(m, const["invariants"])
            gain = score(m, args.objective) - score(cur, args.objective)
            ok_gain = gain >= const["min_improvement"]
            votes = ballots(cur, m)
            ok_vote = sum(votes.values()) >= const["vote"]["quorum"]
            v = " ".join("○" if votes[c] else "×" for c in sorted(votes))
            mark = lambda b: "○" if b else "×"
            print(f"  {label:34s} {m['on_time_deliveries']:7.1f} {m['violation_total']:6.1f}"
                  f"    ○    {mark(ok_inv)}    {mark(ok_gain)}   {v}"
                  + (f"   {why}" if why else ""))
            if ok_inv and ok_gain and ok_vote and (best is None or gain > best[0]):
                best = (gain, name, old, new, cand_text, cand_ir, m, votes)

        if best is None:
            print("  → 採択なし（すべての案がどこかの関門で止まった）。規則は変わらない。")
            history.append(f"- 第 {rnd} 期（{env['label']}）: 採択なし\n")
            continue
        gain, name, old, new, text, ir, cur, votes = best
        version += 1
        (run_dir / f"v{version}.cadl").write_text(text, encoding="utf-8")
        label = fmt(name, old, new, const["amendable"][name]["unit"])
        yes = "".join(c for c in sorted(votes) if votes[c])
        print(f"  → 採択: {label}（目標 {gain:+.1f}，賛成 {yes}）。v{version}.cadl として保存。")
        history.append(f"- 第 {rnd} 期（{env['label']}）: v{version} {label}"
                       f"（目標 {gain:+.1f}，賛成 {yes}）\n")

    for f in run_dir.glob("round*.cadl"):          # 不採択の案は消し、版だけを残す
        f.unlink()
    (run_dir / "history.md").write_text("".join(history), encoding="utf-8")
    print(f"\n版と履歴: {run_dir.relative_to(ROOT)}/  （v1.cadl 〜 v{version}.cadl, history.md）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
