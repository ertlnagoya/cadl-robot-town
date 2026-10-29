"""KPI を人間が決め、LLM が改正を提案し、決めきれないことを人間が判断する改正ループ。

    python -m town.steward --mode DISTRIBUTED --rounds 6 --name kpi-d
    python -m town.steward --mode DISTRIBUTED --rounds 6 --name kpi-d \\
        --llm replay --replay-dir examples/steward-d --answers examples/steward-d/answers.yaml

役割分担
- 人間：kpi.md に、サービスの KPI を自然言語で書く（期ごとに変えてよい）。LLM の
  判断要求に答え、判断の理由を自然言語か GSN で残す。
- LLM：KPI を測り方に言い換える。KPI に向けて、規則・規制・契約の改正案を出す。
  明らかには判断できないことや、当事者が合意すべきことは、判断要求にまとめて
  人間に渡す（town/llm_steward.py）。
- 機械の関門：検査・憲法・測った改善・投票（town.council と同じ）。LLM の申告は
  信用せず、測った値だけを使う。

1 期の流れ
  1. kpi.md を読み直す。KPI が変わったら、LLM が測り方に言い換える。読み方が分かれる、
     代理指標の恐れがある、過去の判断の前提が崩れる、のどれかなら人間が選ぶ。
  2. 現行の規則を、この期の環境で測る。
  3. LLM が改正案を出し、各案を関門にかけて測る。
  4. 次のどれかに当たる案があれば、人間に判断を求める。なければ、関門をすべて通った
     案のうち、KPI の主目標が最もよくなるものを自動で採択する。
     - 合意     ：KPI はよくなるのに投票で否決され、自動で採択できる案より KPI がよい
     - 契約     ：顧客との約束（DELIVERY_SLA）を変え、KPI がよくなる
     - 規制     ：憲法の値を変える
     - 安全     ：運用モードの切替の条件を変え、KPI がよくなる
     - KPI の衝突：主目標はよくなるが、KPI の条件を破るか、ほかの目標が悪化する
     - 不確か   ：関門はすべて通るが、改善したシードが 8 割に満たない
     - LLM      ：LLM 自身が、人間の判断が要ると書いた
  5. 判断と理由を decisions.md と gsn.yaml（図は gsn.md）に残し、次の期の LLM に渡す。

判断要求への答えは、端末で打ち込むか、--answers の YAML に書く。
端末がなく答えもないときは、判断要求を p<k>-request.md に書き出して止まる。
--lang en を付けると、人間に見せる文言と LLM が書く文章が英語になる（KPI は
kpi.en.md のように英語で書く。見出しは「## From period k」）。
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import kpi, llm_steward
from .council import ballots, check_amendment, fmt, invariants_ok, read_params
from .llm_council import MODE_NOTE, _observation
from .period import (CONSTITUTION, DEFAULT_RULES, ROOT, SCENARIO, CompileError, compile_rules,
                     evaluate)
from .messages import Messages
from .record import Gsn, attach_answer

KPI_FILE = {"ja": ROOT / "kpi.md", "en": ROOT / "kpi.en.md"}
M = Messages("ja")                    # main() で --lang に合わせて差し替える
SEED_SHARE = 0.8
KEEP = "keep"


@dataclass
class Cand:
    cid: str
    title: str
    rationale: str
    kind: str                         # amend（規則・契約）/ regulation（憲法）
    text: str = ""                    # amend：改正後の規則ファイル
    key: str = ""                     # regulation：憲法の値の名前
    value: float = 0.0
    layer: str = ""
    label: str = ""                   # 何をどう変えるか（測った側が書く）
    safety: bool = False
    llm_flag: str = ""
    gates: dict = field(default_factory=dict)
    why: str = ""
    m: dict | None = None
    vals: dict | None = None
    gain: float = 0.0
    seeds_better: int = 0
    votes: dict = field(default_factory=dict)
    worse: list = field(default_factory=list)
    kpi_why: str = ""
    effect: str = ""                  # regulation：この規制が何を変えるか
    reasons: list = field(default_factory=list)

    def passed(self, *names: str) -> bool:
        return all(self.gates.get(n) for n in names)


def _get(const: dict, key: str) -> float:
    a, b = key.split(".") if "." in key else (key, None)
    return const[a][b] if b else const[a]


def _set(const: dict, key: str, value: float) -> None:
    a, b = key.split(".") if "." in key else (key, None)
    if b:
        const[a][b] = value
    else:
        const[a] = value


def measure_amend(c: Cand, path: Path, cur_text: str, cur: dict, cur_vals: dict, const: dict,
                  spec: dict, sim: dict) -> None:
    path.write_text(c.text, encoding="utf-8")
    try:
        ir = compile_rules(path)
    except CompileError as e:
        c.gates["check"], c.why = False, (str(e).splitlines() or [""])[0]
        return
    c.gates["check"] = True
    ok, why = check_amendment(cur_text, c.text, const["amendable"], layers=("rule", "contract"))
    c.gates["const"] = ok
    if not ok:
        c.why = M.gate(why)
        return
    old, new = read_params(cur_text), read_params(c.text)
    (name, (a, b)), = {k: (old[k], new[k]) for k in old if abs(old[k] - new[k]) > 1e-9}.items()
    spec_p = const["amendable"][name]
    c.layer, c.safety = spec_p["layer"], bool(spec_p.get("safety"))
    c.label = fmt(name, a, b, spec_p["unit"])
    c.m = evaluate(ir, **sim)
    c.vals = kpi.measure(c.m)
    ok, why = invariants_ok(c.m, const["invariants"])
    c.gates["inv"] = ok
    if not ok:
        c.why = M.gate(why)
    c.gates["kpi_guard"], c.kpi_why = kpi.guards_ok(spec, c.vals, M)
    c.gain = kpi.gain(spec, cur_vals, c.vals)
    c.gates["gain"] = c.gain >= kpi.METRICS[spec["goals"][0]["metric"]][2]
    c.seeds_better = sum(kpi.gain(spec, kpi.measure(r0), kpi.measure(r1)) > 0
                         for r0, r1 in zip(cur["runs"], c.m["runs"]))
    c.gates["seeds"] = c.seeds_better >= SEED_SHARE * len(cur["runs"])
    c.votes = ballots(cur, c.m)
    c.gates["vote"] = sum(c.votes.values()) >= const["vote"]["quorum"]
    c.worse = kpi.worsened(spec, cur_vals, c.vals)


def measure_regulation(c: Cand, const: dict, cur: dict, others: list[Cand]) -> None:
    c.layer = "regulation"
    rng = const.get("regulation", {}).get(c.key)
    c.gates["check"] = None                     # 規則ファイルを変えないので、検査はない
    if rng is None:
        c.gates["const"], c.why = False, M("reg_notkey", key=c.key)
        return
    old = _get(const, c.key)
    if not rng["min"] <= c.value <= rng["max"]:
        c.gates["const"], c.why = False, M("reg_range", key=c.key, v=c.value, lo=rng["min"], hi=rng["max"])
        return
    if abs(abs(c.value - old) - rng["step"]) > 1e-9:
        c.gates["const"], c.why = False, M("reg_step", key=c.key, d=abs(c.value - old), step=rng["step"])
        return
    c.gates["const"] = True
    c.label = f"{c.key} {old:g} → {c.value:g}"
    new = copy.deepcopy(const)
    _set(new, c.key, int(c.value) if float(c.value).is_integer() and c.key == "vote.quorum" else c.value)
    if c.key.startswith("invariants."):
        ok, why = invariants_ok(cur, new["invariants"])
        c.effect = M("reg_ok") if ok else M("reg_ng", why=M.gate(why))
    else:
        flips = [o.cid for o in others if o.m and
                 (sum(o.votes.values()) >= new["vote"]["quorum"]) != o.gates["vote"]]
        c.effect = M("reg_same") if not flips else M("reg_flip", ids=", ".join(flips))


def triage(cands: list[Cand], spec: dict) -> tuple[Cand | None, list[Cand]]:
    """自動で採択できる最もよい案と、人間に判断を求める案。"""
    noise = kpi.METRICS[spec["goals"][0]["metric"]][2]
    auto = [c for c in cands if c.kind == "amend" and c.layer == "rule" and not c.safety
            and not c.llm_flag and not c.worse
            and c.passed("check", "const", "inv", "kpi_guard", "gain", "seeds", "vote")]
    best = max(auto, key=lambda c: c.gain, default=None)
    for c in cands:
        base = c.passed("check", "const", "inv", "gain")
        if c.kind == "regulation" and c.passed("const"):
            c.reasons.append("規制")
        if c.layer == "contract" and base:
            c.reasons.append("契約")
        if c.safety and base:
            c.reasons.append("安全")
        if (c.layer == "rule" and base and not c.gates["vote"]
                and (best is None or c.gain > best.gain + noise)):
            c.reasons.append("合意")
        if base and (not c.gates["kpi_guard"] or c.worse):
            c.reasons.append("KPI の衝突")
        if (c.layer == "rule" and not c.safety and base and c.gates["vote"] and c.gates["kpi_guard"]
                and not c.worse and not c.gates["seeds"]):
            c.reasons.append("不確か")
        if c.llm_flag and c.passed("check", "const", "inv"):
            c.reasons.append("LLM")
    return best, [c for c in cands if c.reasons]


def reason_text(c: Cand, spec: dict) -> str:
    primary = spec["goals"][0]["metric"]
    out = []
    for r in c.reasons:
        if r == "合意":
            no = M("and").join(k for k in sorted(c.votes) if not c.votes[k])
            out.append(M("r_agree", metric=primary, gain=c.gain, no=no))
        elif r == "契約":
            out.append(M("r_contract"))
        elif r == "規制":
            out.append(M("r_reg", effect=c.effect))
        elif r == "安全":
            out.append(M("r_safety"))
        elif r == "KPI の衝突":
            why = c.kpi_why or M("comma").join(M("r_worse", m=w) for w in c.worse)
            out.append(M("r_conflict", gain=c.gain, why=why))
        elif r == "不確か":
            out.append(M("r_uncertain", gain=c.gain, n=c.seeds_better, total=len(c.m["runs"])))
        elif r == "LLM":
            out.append(M("r_llm", text=c.llm_flag))
    return M("sep").join(out)


def table(cur: dict, cur_vals: dict, cands: list[Cand], spec: dict) -> str:
    """測った結果の表（Markdown）。数値はすべてシミュレーションから。"""
    primary = spec["goals"][0]["metric"]
    head = M("t_head", metric=primary)
    utils = lambda m: " / ".join(f"{m['companies'][x]['utility']:.1f}" for x in sorted(m["companies"]))
    # 見張る目標と KPI の条件の指標は、値を 1 列にまとめて見せる（表の固定の列にないものも）
    watch = list(dict.fromkeys([g["metric"] for g in spec["goals"][1:]]
                               + [g["metric"] for g in spec.get("guards") or []]))
    fmt_v = lambda n, x: f"{x:.2f}" if n.endswith("rate") or n == "on_time_rate_gap" else f"{x:.1f}"
    watched = lambda vals: M("comma").join(f"{n} {fmt_v(n, vals[n])}" for n in watch)
    if watch:
        head = head.replace(" | 備考 |", f" | {M('t_watch')} | 備考 |").replace(" | notes |", f" | {M('t_watch')} | notes |")
    extra = lambda vals: f" {watched(vals)} |" if watch else ""
    rows = [head, "|" + "---|" * (17 if watch else 16),
            f"| — | {M('t_current')} | — | — | — | {cur_vals['on_time_deliveries']:.1f} | {cur_vals['late_deliveries']:.1f} "
            f"| {cur_vals['blocked_s']:.0f} | {cur_vals['road_violations']:.1f} "
            f"| {cur_vals['min_company_on_time_rate']:.2f} | — | — | — | {utils(cur)} | —"
            f" |{extra(cur_vals)} |"]
    mark = lambda b: "—" if b is None else "○" if b else "×"
    for c in cands:
        label = c.label or c.title
        layer = M("layer").get(c.layer, "—")
        if c.vals is None:
            rows.append(f"| {c.cid} | {label} | {layer} | {mark(c.gates.get('check'))} "
                        f"| {mark(c.gates.get('const'))} | | | | | | | | | | |{' |' if watch else ''}"
                        f" {c.why or c.effect} |")
            continue
        v = c.vals
        votes = " ".join("○" if c.votes[k] else "×" for k in sorted(c.votes))
        note = M("sep").join(x for x in (c.why, c.kpi_why, M("t_worse") + M("comma").join(c.worse)
                                         if c.worse else "") if x)
        rows.append(f"| {c.cid} | {label} | {layer} | ○ | {mark(c.gates['inv'])} "
                    f"| {v['on_time_deliveries']:.1f} | {v['late_deliveries']:.1f} | {v['blocked_s']:.0f} "
                    f"| {v['road_violations']:.1f} | {v['min_company_on_time_rate']:.2f} | {c.gain:+.1f} "
                    f"| {c.seeds_better}/{len(c.m['runs'])} | {mark(c.gates['kpi_guard'])} | {utils(c.m)} "
                    f"| {votes} |{extra(v)} {note} |")
    return "\n".join(rows)


class Answers:
    """判断要求への答え。--answers の YAML になければ端末で聞く。"""

    def __init__(self, path: Path | None, out: Path):
        self.given = yaml.safe_load(path.read_text(encoding="utf-8")) if path else {}
        self.given = self.given or {}
        self.out = out
        self.used: dict = {}

    def ask(self, rid: str, ids: list[str], request_path: Path) -> dict:
        if rid in self.given:
            given = self.given[rid]
            if not isinstance(given, dict) or "choice" not in given:
                raise SystemExit(M("a_shape", rid=rid))
            for g in given.get("gsn") or []:
                if not isinstance(g, dict) or not g.get("type") or not g.get("text"):
                    raise SystemExit(M("a_gsn", rid=rid))
            ans = dict(given)
            ans["choice"] = str(ans["choice"])
            if ans["choice"] not in ids:
                raise SystemExit(M("a_bad", rid=rid, id=ans["choice"], ids=", ".join(ids)))
            print(M("a_given", id=ans["choice"]) + (M("a_reason", text=ans["reason"]) if ans.get("reason") else ""))
        elif sys.stdin.isatty():
            try:
                while True:
                    choice = input(M("a_choose", ids=" / ".join(ids))).strip()
                    if choice in ids:
                        break
                reason = input(M("a_reason_q")).strip()
                assumptions = []
                while (a := input(M("a_assume_q")).strip()):
                    assumptions.append(a)
            except EOFError:
                self.save()
                raise SystemExit(M("a_eof", rid=rid, path=self.out.relative_to(ROOT)))
            ans = {"choice": choice, "reason": reason}
            if assumptions:
                ans["assumptions"] = assumptions
        else:
            self.save()
            sys.stdout.flush()
            raise SystemExit(M("a_stop", rid=rid, req=request_path.relative_to(ROOT), ids=" / ".join(ids),
                               llm=(self.out.parent / "llm").relative_to(ROOT)))
        self.used[rid] = ans
        return ans

    def save(self) -> None:
        self.out.write_text(yaml.safe_dump(self.used, allow_unicode=True, sort_keys=False), encoding="utf-8")


def decisions_brief(log: list[dict]) -> str:
    out = []
    for d in log:
        line = M("brief", id=d["id"], k=d["period"], who=d["who"], summary=d["summary"])
        if d.get("reason"):
            line += M("brief_r", text=d["reason"])
        out.append(line)
    return "\n".join(out)


def main() -> int:
    global M
    const0 = yaml.safe_load(CONSTITUTION.read_text(encoding="utf-8"))
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    p.add_argument("--kpi", type=Path, help="人間が書いた KPI（既定 kpi.md、--lang en なら kpi.en.md）")
    p.add_argument("--lang", default="ja", choices=["ja", "en"],
                   help="人間に見せる文言と、LLM が書く文章の言語")
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--mode", default="adaptive", choices=["adaptive", "DISTRIBUTED", "CENTRALIZED"])
    p.add_argument("--llm", default="live", choices=["live", "replay"],
                   help="live は LLM を呼ぶ（--replay-dir に記録があればそれを使う）。replay は記録だけを使う")
    p.add_argument("--replay-dir", type=Path, help="LLM の出力の記録（p<k>-kpi.json など）の場所")
    p.add_argument("--answers", type=Path, help="判断要求への答えの YAML。なければ端末で聞く")
    p.add_argument("--name", default=dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    args = p.parse_args()
    if args.llm == "replay" and not args.replay_dir:
        p.error("--llm replay には --replay-dir が必要です")
    M = Messages(args.lang)
    kpi_path = args.kpi or KPI_FILE[args.lang]
    label = lambda env: env.get(f"label_{args.lang}", env["label"])

    const = copy.deepcopy(const0)
    const_text = CONSTITUTION.read_text(encoding="utf-8")
    periods = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))["periods"]
    run_dir = ROOT / "runs" / args.name
    run_dir.mkdir(parents=True, exist_ok=True)
    rec = llm_steward.Recorder(run_dir, args.llm, args.replay_dir, M)
    answers = Answers(args.answers, run_dir / "answers.yaml")
    gsn = Gsn(M)
    text = args.rules.read_text(encoding="utf-8")
    version, const_version = 1, 1
    (run_dir / "v1.cadl").write_text(text, encoding="utf-8")
    ir = compile_rules(run_dir / "v1.cadl")
    history: list[str] = []
    log: list[dict] = []
    md = [M("decisions_title", name=args.name, mode=args.mode)]
    kpi_text, spec, kpi_goal, revisit = None, None, None, ""

    def finish() -> None:
        answers.save()
        gsn.save(run_dir / "gsn.yaml")
        (run_dir / "decisions.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        (run_dir / "history.md").write_text(M("history_title") + "".join(history), encoding="utf-8")

    for k in range(1, args.rounds + 1):
        env = periods[(k - 1) % len(periods)]
        seeds = [s + env["seed_offset"] for s in const["seeds"]]
        sim = dict(seeds=seeds, period_s=const["period_s"], order_interval_s=env["order_interval_s"],
                   mode=args.mode, rush=env.get("rush"))
        print(M("period_head", k=k, label=label(env), interval=env["order_interval_s"], v=version,
                params=", ".join(f"{n}={v:g}" for n, v in read_params(text).items())))

        # 1. KPI
        since, new_kpi = kpi.load(kpi_path, k)
        if new_kpi != kpi_text:
            print(M("kpi_line", since=since, text=new_kpi))
            data, note = rec.get(f"p{k}-kpi", llm_steward.kpi_request(new_kpi, since, kpi_text,
                                                                      decisions_brief(log), args.lang))
            if data is None:
                finish()
                raise SystemExit(M("kpi_fail", note=note))
            print(M("llm", note=M.note(note)))
            opts = []
            for i, o in enumerate(data["options"], 1):
                bad = kpi.validate(o)
                if bad:
                    print(M("kpi_reject", i=i, label=o["label"], why=bad))
                else:
                    opts.append((str(i), o))
            if not opts:
                finish()
                raise SystemExit(M("kpi_none"))
            ask = len(opts) > 1 or data["concerns"] or data["revisit"]
            if str(data["recommended"]) not in [i for i, _ in opts]:
                data["recommended"] = "—"
            sec = [M("kpi_sec", k=k, since=since, how=M("asked") if ask else M("unasked")),
                   M("md_kpi", text=new_kpi), M("md_reading", text=data["interpretation"])]
            sec += [M("md_option", i=i, label=o["label"], desc=kpi.describe(o, M), note=o["note"]) for i, o in opts]
            sec += [M("md_concern", text=c) for c in data["concerns"]]
            sec += [M("md_revisit", d=r["decision"], why=r["why"]) for r in data["revisit"]]
            sec.append(M("md_reco", id=data["recommended"]))
            md += sec
            print(M("reading", text=data["interpretation"]))
            for i, o in opts:
                print(f"   [{i}] {o['label']}: {kpi.describe(o, M)}")
            for c in data["concerns"]:
                print(M("concern", text=c))
            for r in data["revisit"]:
                print(M("revisit", d=r["decision"], why=r["why"]))
            if ask:
                rpath = run_dir / f"p{k}-kpi-request.md"
                rpath.write_text("\n".join(sec) + "\n", encoding="utf-8")
                print(M("kpi_ask", k=k, id=data["recommended"]))
                ans = answers.ask(f"K{k}", [i for i, _ in opts], rpath)
                spec = dict(opts[[i for i, _ in opts].index(ans["choice"])][1])
                md.append(M("md_chosen", id=ans["choice"], label=spec["label"]))
                if ans.get("reason"):
                    md.append(M("md_reason", text=ans["reason"]))
                for a in ans.get("assumptions") or []:
                    md.append(M("md_assume", text=a))
            else:
                ans, spec = None, dict(opts[0][1])
            md.append("")
            kpi_goal = gsn.add("goal", M("gsn_kpi", since=since, text=new_kpi), gsn.by_version)
            gsn.add("context", M("gsn_measure", who=M("by_human") if ans else M("by_llm"),
                                 desc=kpi.describe(spec, M)), kpi_goal)
            if ans:
                attach_answer(gsn, kpi_goal, ans)
            log.append({"id": f"K{k}", "period": k, "who": M("human") if ans else M("auto"),
                        "summary": M("log_kpi", text=new_kpi[:60], desc=kpi.describe(spec, M)),
                        "reason": (ans or {}).get("reason", "")})
            revisit = "\n".join(f"- {r['decision']}: {r['why']}" for r in data["revisit"])
            kpi_text = new_kpi

        # 2. 現行の規則を測る
        cur = evaluate(ir, **sim)
        cur_vals = kpi.measure(cur)
        primary = spec["goals"][0]["metric"]
        print(M("current", on=cur_vals["on_time_deliveries"], late=cur_vals["late_deliveries"],
                blk=cur_vals["blocked_s"], road=cur_vals["road_violations"], metric=primary,
                val=cur_vals[primary]))

        # 3. LLM が改正案を出し、関門にかけて測る
        req = llm_steward.propose_request(
            kpi_text, spec, MODE_NOTE[args.mode], env, _observation(cur), "".join(history),
            decisions_brief(log), revisit,
            const_text if const_version == 1 else yaml.safe_dump(const, allow_unicode=True, sort_keys=False),
            text, args.lang)
        data, note = rec.get(f"p{k}-propose", req)
        print(M("llm", note=M.note(note)))
        if data is None and args.llm == "replay":
            finish()
            raise SystemExit(M("no_propose", note=note))
        data = data or {"assessment": "", "proposals": [], "regulation_proposals": []}
        if data["assessment"]:
            print(M("assessment", text=data["assessment"]))
        cands = [Cand(str(i), x["title"], x["rationale"], "amend", text=x["cadl"],
                      llm_flag=x.get("why_human", "") if x.get("needs_human") else "")
                 for i, x in enumerate((data.get("proposals") or [])[:llm_steward.MAX_PROPOSALS], 1)]
        for c in cands:
            measure_amend(c, run_dir / f"p{k}-{c.cid}.cadl", text, cur, cur_vals, const, spec, sim)
        regs = [Cand(f"R{i}", x["title"], x["rationale"], "regulation", key=x["key"], value=x["value"])
                for i, x in enumerate((data.get("regulation_proposals") or [])[:1], 1)]
        for c in regs:
            measure_regulation(c, const, cur, cands)
        cands += regs
        best, escalated = triage(cands, spec)
        evidence = table(cur, cur_vals, cands, spec)
        (run_dir / f"p{k}-evidence.md").write_text(
            M("evidence_head", k=k, label=label(env), desc=kpi.describe(spec, M)) + evidence + "\n"
            + "".join("\n" + M("item", id=c.cid, label=c.title, text=c.rationale) for c in cands) + "\n",
            encoding="utf-8")
        print("  " + evidence.replace("\n", "\n  "))

        # 4. 自動で採択するか、人間に判断を求めるか
        md.append(M("d_sec", k=k, label=label(env), how=M("asked") if escalated else M("auto")))
        md.append(M("md_measure", desc=kpi.describe(spec, M)))
        md.append(M("md_assess", text=data["assessment"] or M("none")))
        md.append(M("md_evidence", k=k))
        ans = None
        if escalated:
            options = escalated + ([best] if best and best not in escalated else [])
            ids = [c.cid for c in options] + [KEEP]
            reasons = "\n".join(M("item", id=c.cid, label=c.label or c.title, text=reason_text(c, spec))
                                for c in escalated)
            opt_text = "\n".join(M("opt_line", id=c.cid, label=c.label or c.title, title=c.title)
                                 + (M("opt_auto") if c is best else "") for c in options)
            opt_text += "\n" + M("opt_keep", v=version)
            rdata, rnote = rec.get(f"p{k}-request", llm_steward.request_request(
                kpi_text, kpi.describe(spec, M), reasons, evidence, opt_text, decisions_brief(log), args.lang))
            known = {o["id"] for o in (rdata or {}).get("options", [])}
            if rdata is None or not known <= set(ids):
                rnote = (rnote if rdata is None else M("bad_ids")) + M("tmpl_note")
                rdata = {"question": M("tmpl_q", k=k), "background": "",
                         "options": [{"id": c.cid, "summary": c.rationale, "stakeholders": ""} for c in options],
                         "recommendation": {"id": best.cid if best else KEEP,
                                            "reason": M("tmpl_best") if best else M("tmpl_keep")},
                         "unknowns": []}
            print(M("d_ask", k=k, note=M.note(rnote)))
            body = [M("req_title", k=k, label=label(env)), "", M("req_q", q=rdata["question"]), "",
                    rdata["background"], "", M("req_why"), "", reasons, "", M("req_opts"), ""]
            explain = {o["id"]: o for o in rdata["options"]}
            for i in ids:
                o = explain.get(i, {})
                head = next((f"{c.label or c.title}" for c in options if c.cid == i), M("keep"))
                body.append(M("req_opt", id=i, head=head, summary=o.get("summary", ""))
                            + (M("req_stake", text=o["stakeholders"]) if o.get("stakeholders") else ""))
            body += ["", M("req_table"), "", evidence, "",
                     M("req_reco", id=rdata["recommendation"]["id"], reason=rdata["recommendation"]["reason"]), "",
                     M("req_unknown"), ""]
            body += [f"- {u}" for u in rdata["unknowns"]] or ["- " + M("none")]
            rpath = run_dir / f"p{k}-request.md"
            rpath.write_text("\n".join(body) + "\n", encoding="utf-8")
            print("  " + "\n  ".join(body[2:4] + body[body.index(M("req_opts")) + 2:body.index(M("req_table")) - 1]))
            print(M("reco", id=rdata["recommendation"]["id"], reason=rdata["recommendation"]["reason"]))
            for u in rdata["unknowns"]:
                print(M("unknown", text=u))
            ans = answers.ask(f"D{k}", ids, rpath)
            chosen = next((c for c in options if c.cid == ans["choice"]), None)
            md.append(M("md_whyasked") + "\n" + "\n".join("  " + x for x in reasons.splitlines()))
            md.append(M("md_request", k=k, q=rdata["question"]))
            md.append(M("md_reco_r", id=rdata["recommendation"]["id"], reason=rdata["recommendation"]["reason"]))
            for u in rdata["unknowns"]:
                md.append(M("md_unknown", text=u))
        else:
            chosen = best

        # 5. 採択と記録
        what = M("keep")
        if chosen is not None and chosen.kind == "amend":
            text, ir = chosen.text, compile_rules(run_dir / f"p{k}-{chosen.cid}.cadl")
            version += 1
            (run_dir / f"v{version}.cadl").write_text(text, encoding="utf-8")
            yes = "".join(x for x in sorted(chosen.votes) if chosen.votes[x]) or M("nobody")
            what = M("adopted", v=version, label=chosen.label, layer=M("layer")[chosen.layer],
                     gain=chosen.gain, yes=yes)
            if not chosen.gates["vote"]:
                what += M("override")
        elif chosen is not None:
            _set(const, chosen.key, int(chosen.value) if chosen.key == "vote.quorum" else chosen.value)
            const_version += 1
            (run_dir / f"constitution-v{const_version}.yaml").write_text(
                yaml.safe_dump(const, allow_unicode=True, sort_keys=False), encoding="utf-8")
            what = M("reg_adopted", label=chosen.label, v=const_version)
        who = M("human") if ans else M("auto")
        print(M("outcome", who=who, what=what))
        history.append(M("history", k=k, label=label(env), what=what, who=who))
        md.append(M("md_result", who=who, what=what))
        if ans:
            if ans.get("reason"):
                md.append(M("md_reason", text=ans["reason"]))
            for a in ans.get("assumptions") or []:
                md.append(M("md_assume", text=a))
            for g in ans.get("gsn") or []:
                md.append(M("md_gsn", type=g["type"], text=g["text"]))
        md.append("")
        s = gsn.add("strategy", M("gsn_s", k=k, what=what), kpi_goal)
        gsn.add("solution", M("gsn_sn", k=k), s)
        gsn.add("context", M("gsn_c", k=k, label=label(env), interval=env["order_interval_s"]), s)
        if ans:
            attach_answer(gsn, s, ans)
        else:
            gsn.add("justification", M("gsn_j_auto") if chosen else M("gsn_j_none"), s, by="tool")
        log.append({"id": f"D{k}", "period": k, "who": who, "summary": what,
                    "reason": (ans or {}).get("reason", "")})
        for f in run_dir.glob(f"p{k}-*.cadl"):          # 不採択の案は消し、版だけを残す
            f.unlink()
        finish()

    print(M("done", dir=run_dir.relative_to(ROOT), v=version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
