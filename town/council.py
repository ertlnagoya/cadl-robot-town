"""委員会（COUNCIL）による規則の改正ループ。

    python -m town.council                  # 1 期ぶん改正を試みる
    python -m town.council --rounds 5       # 5 期続ける
    python -m town.council --objective violations        # 違反全体の少なさを目標にする
    python -m town.council --objective road_violations   # 道路の規則の違反の少なさを目標にする
    python -m town.council --proposer llm                # 改正案を LLM（Claude）が書く
    python -m town.council --proposer replay --replay-dir examples/llm-replay
                                                         # 保存した LLM の案を再生する

1 期ごとに、次の 4 つの関門を順に通った改正案だけを採択する。

  1. 検査   ：cadl check / cadl sim-ir を通る
  2. 憲法   ：変えたのが @amend 行の数値だけで、1 パラメータ 1 ステップの範囲内。
              さらに、改正後のシミュレーションで不変条件（各社の定時率の下限など）を守る
  3. 改善   ：現行の規則より目標（objective）が min_improvement 以上よい
  4. 投票   ：自社の効用が下がらない会社が quorum 社以上いる

改正案の出し方（--proposer）は 3 つ。
- neighbors（既定）：amendable のパラメータを 1 つだけ 1 ステップ動かした案をすべて試す
- llm：LLM が観測を読んで改正案（CADL ファイル全体）を書く（town/llm_council.py）
- replay：--replay-dir に保存した LLM の案を再生する
比較は同じ乱数シードで行う。採択された規則は runs/<名前>/ に版として残る。

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

from . import llm_council
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


def _canonical(text: str) -> list[str]:
    """@amend 行の数値を伏せた、比較用の行の並び。"""
    out = []
    for line in text.splitlines():
        m = TAG.search(line)
        if m:
            body = line[:m.start()]
            line = NUM.sub("<v>", body, count=1) + line[m.start():]
        out.append(line.rstrip())
    while out and not out[-1]:
        out.pop()
    return out


def check_amendment(cur_text: str, cand_text: str, amendable: dict) -> tuple[bool, str]:
    """憲法の文面の関門：@amend 行の数値以外を変えていないか、変え方が範囲内か。"""
    if _canonical(cur_text) != _canonical(cand_text):
        return False, "@amend 行の数値以外を書き換えている"
    old, new = read_params(cur_text), read_params(cand_text)
    changed = {k: (old[k], new[k]) for k in old if abs(old[k] - new[k]) > 1e-9}
    if not changed:
        return False, "何も変えていない"
    if len(changed) > 1:
        return False, f"{len(changed)} つのパラメータを同時に変えている"
    (name, (a, b)), = changed.items()
    spec = amendable[name]
    if not spec["min"] <= b <= spec["max"]:
        return False, f"{name}={b:g} は範囲 {spec['min']:g}〜{spec['max']:g} の外"
    if abs(abs(b - a) - spec["step"]) > 1e-9:
        return False, f"{name} を {abs(b - a):g} 動かしている（1 回に {spec['step']:g} まで）"
    return True, ""


def neighbor_proposals(text: str, amendable: dict) -> list[dict]:
    out = []
    for name, old, new in candidates(read_params(text), amendable):
        out.append({"title": fmt(name, old, new, amendable[name]["unit"]),
                    "rationale": "", "cadl": write_param(text, name, new)})
    return out


def score(m: dict, objective: str) -> float:
    """大きいほどよい。"""
    if objective == "on_time_deliveries":
        return m["on_time_deliveries"]
    if objective == "violations":
        return -m["violation_total"]
    if objective == "road_violations":
        return -m["road_violations"]
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
                   choices=["on_time_deliveries", "violations", "road_violations"])
    p.add_argument("--mode", default="adaptive",
                   choices=["adaptive", "DISTRIBUTED", "CENTRALIZED"],
                   help="運用モード。adaptive は CADL の条件で切り替える。ほかは固定")
    p.add_argument("--proposer", default="neighbors", choices=["neighbors", "llm", "replay"])
    p.add_argument("--replay-dir", type=Path, help="--proposer replay で読む llm-round<k>.json の場所")
    p.add_argument("--check", action="store_true",
                   help="--proposer llm の配線だけ確かめる（LLM は呼ばず、依頼の大きさを表示して終わる）")
    p.add_argument("--name", default=dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    args = p.parse_args()
    if args.proposer == "replay" and not args.replay_dir:
        p.error("--proposer replay には --replay-dir が必要です")
    const_text = CONSTITUTION.read_text(encoding="utf-8")

    periods = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))["periods"]
    period = const["period_s"]
    run_dir = ROOT / "runs" / args.name
    run_dir.mkdir(parents=True, exist_ok=True)
    text = args.rules.read_text(encoding="utf-8")
    version = 1
    (run_dir / f"v{version}.cadl").write_text(text, encoding="utf-8")
    history = [f"# 改正履歴（目標: {args.objective}，運用モード: {args.mode}，"
               f"改正案: {args.proposer}）\n"]

    try:
        ir = compile_rules(run_dir / f"v{version}.cadl")
    except CompileError as e:
        print(f"[REJECT] 現行の規則が検査を通りません。\n{e}")
        return 1

    for rnd in range(1, args.rounds + 1):
        env = periods[(rnd - 1) % len(periods)]
        seeds = [s + env["seed_offset"] for s in const["seeds"]]
        interval = env["order_interval_s"]
        rush = env.get("rush")
        cur = evaluate(ir, seeds, period, interval, args.mode, rush)
        params = read_params(text)
        print(f"\n=== 第 {rnd} 期（{env['label']}：注文の平均間隔 {interval:g} 秒）"
              f"  現行 v{version}: "
              + ", ".join(f"{k}={v:g}" for k, v in params.items()) + " ===")
        print(f"  現行: 定時配達 {cur['on_time_deliveries']:.1f}  違反 {cur['violation_total']:.1f}"
              f"（うち道路 {cur['road_violations']:.1f}）"
              f"  ふさがれた時間 {cur['blocked_s']:.0f}s  集中 {100 * cur['central_share']:.0f}%  効用 "
              + " ".join(f"{c}={x['utility']:.1f}" for c, x in cur["companies"].items()))
        if args.proposer == "neighbors":
            proposals = neighbor_proposals(text, const["amendable"])
        else:
            if args.proposer == "llm":
                req = llm_council.build_request(text, const_text, env, cur, history, args.objective,
                                                args.mode)
                if args.check:
                    print(f"  [check] {req['model']} に送る依頼: system {len(req['system'])} 字、"
                          f"user {len(req['messages'][0]['content'])} 字。LLM は呼ばずに終わります。")
                    return 0
                proposals, note, view = llm_council.propose_llm(req)
                llm_council.save(run_dir / f"llm-round{rnd}.json", proposals, note, view)
            else:
                proposals, note, view = llm_council.replay(args.replay_dir / f"llm-round{rnd}.json")
            print(f"  LLM の委員会: {note}、{len(proposals)} 件")
            if view:
                print(f"  見立て: {view}")
        print("  改正案                                  定時配達  違反 (道路) ふさがれた  検査 憲法 改善 投票(A B C)")
        best = None
        for i, prop in enumerate(proposals, 1):
            label, cand_text = prop["title"][:38], prop["cadl"]
            if prop.get("rationale"):
                print(f"  [{i}] {prop['title']}\n      {prop['rationale']}")
            path = run_dir / f"round{rnd}-{i}.cadl"
            path.write_text(cand_text, encoding="utf-8")
            try:
                cand_ir = compile_rules(path)
            except CompileError as e:
                first = str(e).splitlines()[0] if str(e) else ""
                print(f"  {label:38s}       -     -      -          -    ×    -    -    -     {first}")
                continue
            ok_text, why = check_amendment(text, cand_text, const["amendable"])
            if not ok_text:
                print(f"  {label:38s}       -     -      -          -    ○    ×    -    -     {why}")
                continue
            m = evaluate(cand_ir, seeds, period, interval, args.mode, rush)
            ok_inv, why = invariants_ok(m, const["invariants"])
            gain = score(m, args.objective) - score(cur, args.objective)
            ok_gain = gain >= const["min_improvement"]
            votes = ballots(cur, m)
            ok_vote = sum(votes.values()) >= const["vote"]["quorum"]
            v = " ".join("○" if votes[c] else "×" for c in sorted(votes))
            mark = lambda b: "○" if b else "×"
            print(f"  {label:38s} {m['on_time_deliveries']:7.1f} {m['violation_total']:5.1f}"
                  f" ({m['road_violations']:4.1f}) {m['blocked_s']:8.0f}s"
                  f"    ○    {mark(ok_inv)}    {mark(ok_gain)}   {v}"
                  + (f"   {why}" if why else ""))
            if ok_inv and ok_gain and ok_vote and (best is None or gain > best[0]):
                best = (gain, cand_text, cand_ir, m, votes)

        if best is None:
            print("  → 採択なし（すべての案がどこかの関門で止まった）。規則は変わらない。")
            history.append(f"- 第 {rnd} 期（{env['label']}）: 採択なし\n")
            continue
        gain, new_text, ir, cur, votes = best
        (name, (old, new)), = {k: (a, read_params(new_text)[k]) for k, a in read_params(text).items()
                               if abs(a - read_params(new_text)[k]) > 1e-9}.items()
        text = new_text
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
