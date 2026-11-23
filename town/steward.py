"""KPI を人間が決め、LLM が改正を提案し、決めきれないことを人間が判断する改正ループ。

    python -m town.steward --mode DISTRIBUTED --rounds 6 --name kpi-d
    python -m town.steward --mode DISTRIBUTED --rounds 6 --name kpi-d \\
        --llm replay --replay-dir examples/steward-d --answers examples/steward-d/answers.yaml

役割分担
- 人間：kpi.md に、サービスの KPI を自然言語で書く（期ごとに変えてよい）。LLM の
  判断要求に答え、判断の理由を自然言語か GSN で残す。
- LLM：KPI を評価方法に言い換える。KPI に向けて、規則・規制・契約の改正案を出す。
  明らかには判断できないことや、当事者が合意すべきことは、判断要求にまとめて
  人間に渡す（town/llm_steward.py）。
- 機械による判定：検査・憲法・測った改善・投票（town.council と同じ）。LLM の申告は
  信用せず、測った値だけを使う。

1 期の流れ
  1. kpi.md を読み直す。KPI が変わったら、LLM が評価方法に言い換える。読み方が分かれる、
     代理指標の恐れがある、過去の判断の前提が崩れる、のどれかなら人間が選ぶ。
  2. 現行の規則を、この期の環境で測る。
  3. LLM が改正案を出し、各案を判定にかけて測る。
  4. 次のどれかに当たる案があれば、人間に判断を求める。なければ、判定をすべて通った
     案のうち、KPI の主目標が最もよくなるものを自動で採択する。
     - 合意     ：KPI はよくなるのに投票で否決され、自動で採択できる案より KPI がよい
     - 契約     ：顧客との約束（DELIVERY_SLA）を変え、KPI がよくなる
     - 規制     ：憲法の値を変える
     - 安全     ：運用モードの切替の条件を変え、KPI がよくなる
     - KPI の衝突：主目標はよくなるが、KPI の条件を破るか、ほかの目標が悪化する
     - 不確か   ：判定はすべて通るが、改善したシードが 8 割に満たない
     - LLM      ：LLM 自身が、人間の判断が要ると書いた
  5. 判断と理由を decisions.md と gsn.yaml（図は gsn.md）に残し、次の期の LLM に渡す。
     同じ記録を、仕様の付録 L の判断の記録 decisions.jsonl（town/decisions.py）にも残す。
     --decider llm では、判断要求に人間に代わって LLM が答え、同じ形で記録する。

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

from . import decisions as dr
from . import kpi, llm_steward
from .council import ballots, check_amendment, fmt, invariants_ok, read_params
from .llm_council import MODE_NOTE, MODEL, _observation
from .period import (CONSTITUTION, DEFAULT_RULES, ROOT, SCENARIO, CompileError, compile_rules,
                     evaluate)
from .messages import Messages
from .model import versioned
from .record import Gsn, attach_answer

KPI_FILE = {"ja": ROOT / "kpi.md", "en": ROOT / "kpi.en.md"}
M = Messages("ja")                    # main() で --lang に合わせて差し替える
SEED_SHARE = 0.8
KEEP = "keep"
FU_KINDS = ("remeasure", "propose", "ask")   # 約束の種類（何が果たすか）


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
    effects: dict = field(default_factory=dict)  # regulation：その期の測定に当てはめた件数
    per_effects: dict = field(default_factory=dict)  # regulation：会社ごとの件数
    reasons: list = field(default_factory=list)
    # 証拠のフィールド（付録 L）：LLM が書いた前提、分からないこと、予測
    assumptions: list = field(default_factory=list)
    unknowns: list = field(default_factory=list)
    predictions: list = field(default_factory=list)
    fulfils: list = field(default_factory=list)   # この案が果たす、案を出す約束の id

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


def measure_regulation(c: Cand, const: dict, cur: dict, others: list[Cand],
                       past: list[Cand] = ()) -> None:
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
    # 規制の案の測定（付録 L）：規制は街の動きを変えず、どの案を採れるかを変える。そこで、
    # 新しい値を、その期に測った現行の規則と案、その投票に当てはめて数える（新しい実行はしない）
    # この期の案だけでなく、記録のそれまでの期に測ったすべての案（past）にも当てはめる（_all）。
    # 会社ごとには、その会社が反対したのに投票を通る案の数（passing_against）を数える
    measured = [o for o in others if o.m]
    every = list(past) + measured
    passes = lambda o, cst: sum(o.votes.values()) >= cst["vote"]["quorum"]
    for tag, cst in (("", new), ("_current", const)):
        c.effects["in_force_meets_invariants" + tag] = int(invariants_ok(cur, cst["invariants"])[0])
        c.effects["proposals_meeting_invariants" + tag] = sum(
            invariants_ok(o.m, cst["invariants"])[0] for o in measured)
        c.effects["proposals_passing_vote" + tag] = sum(passes(o, cst) for o in measured)
        c.effects["proposals_meeting_invariants_all" + tag] = sum(
            invariants_ok(o.m, cst["invariants"])[0] for o in every)
        c.effects["proposals_passing_vote_all" + tag] = sum(passes(o, cst) for o in every)
        for x in ("A", "B", "C"):
            c.per_effects.setdefault(x, {})["passing_against" + tag] = sum(
                passes(o, cst) and o.votes.get(x) is False for o in every)


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
            out.append(M("r_agree", metric=primary, gain=kpi.fmt_gain(primary, c.gain), no=no))
        elif r == "契約":
            out.append(M("r_contract"))
        elif r == "規制":
            out.append(M("r_reg", effect=c.effect))
        elif r == "安全":
            out.append(M("r_safety"))
        elif r == "KPI の衝突":
            why = c.kpi_why or M("comma").join(M("r_worse", m=w) for w in c.worse)
            out.append(M("r_conflict", gain=kpi.fmt_gain(primary, c.gain), why=why))
        elif r == "不確か":
            out.append(M("r_uncertain", gain=kpi.fmt_gain(primary, c.gain), n=c.seeds_better, total=len(c.m["runs"])))
        elif r == "LLM":
            out.append(M("r_llm", text=c.llm_flag))
    return M("sep").join(out)


def table(cur: dict, cur_vals: dict, cands: list[Cand], spec: dict) -> str:
    """測った結果の表（Markdown）。数値はすべてシミュレーションから。"""
    primary = spec["goals"][0]["metric"]
    head = M("t_head", metric=primary)
    utils = lambda m: " / ".join(f"{m['companies'][x]['utility']:.1f}" for x in sorted(m["companies"]))
    # 監視する目標と KPI の条件の指標は、値を 1 列にまとめて見せる（表の固定の列にないものも）
    # 達成基準のある主目標も入れ、達成基準と条件には、届いているかを添える
    targeted = [g["metric"] for g in spec["goals"][:1] if g.get("target") is not None]
    watch = list(dict.fromkeys(targeted + [g["metric"] for g in spec["goals"][1:]]
                               + [g["metric"] for g in spec.get("guards") or []]))
    watched = lambda vals: M("comma").join(f"{n} {kpi.fmt(n, vals[n])}{kpi.mark(spec, n, vals[n], M)}"
                                           for n in watch)
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
                    f"| {v['road_violations']:.1f} | {v['min_company_on_time_rate']:.2f} | {kpi.fmt_gain(primary, c.gain)} "
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
            if not str(ans.get("reason") or "").strip():
                raise SystemExit(M("a_noreason", rid=rid))
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
                reason = ""
                while not reason:
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


def measure_items(spec: dict) -> list[dict]:
    """KPI の評価方法を、付録 L の測り方の項目に。"""
    out = []
    for i, g in enumerate(spec["goals"]):
        sign = kpi.METRICS[g["metric"]][1]
        t = g.get("target")
        out.append({"metric": g["metric"], "role": "goal" if i == 0 else "watch",
                    "direction": "increase" if sign > 0 else "decrease",
                    "target": None if t is None else f"{'>=' if sign > 0 else '<='} {t:g}"})
    for g in spec.get("guards") or []:
        out.append({"metric": g["metric"], "role": "guard", "direction": None,
                    "target": f"{g['op']} {g['value']:g}"})
    return out


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
    p.add_argument("--decider", default="human", choices=["human", "llm"],
                   help="判断要求に答える者。llm では、人間に代わって LLM が答え、その証拠を同じ形で残す")
    p.add_argument("--layers", default="rule,contract,regulation",
                   help="改正案として扱う層（カンマ区切り）。ほかの層の案は測らずに除く")
    p.add_argument("--delegate", default="rule,contract",
                   help="--decider llm のとき、LLM が人間に代わって判断する層（カンマ区切り）。"
                        "rule と contract は運営者、regulation は規制の担当者の層。"
                        "任せない層の判断要求は人間（--answers か端末）に出す")
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
    layers = {x.strip() for x in args.layers.split(",") if x.strip()}
    delegate = {x.strip() for x in args.delegate.split(",") if x.strip()} if args.decider == "llm" else set()
    if len(delegate & {"rule", "contract"}) == 1 or delegate - {"rule", "contract", "regulation"}:
        p.error(M("delegate_bad", given=args.delegate))
    text = args.rules.read_text(encoding="utf-8")
    version, const_version = 1, 1
    # 記録に残すバージョンのファイル：規則に、付録 K の改正の宣言を書き足したもの
    write_version = lambda n, effective: (run_dir / f"v{n}.cadl").write_text(
        versioned(text, n, n - 1 if n > 1 else None, effective, const, read_params(text)),
        encoding="utf-8")
    write_version(1, 1)
    delegated = lambda side: ("regulation" in delegate if side == "regulator"
                              else bool(delegate & {"rule", "contract"}))
    decider_of = lambda side: (("llm-regulator" if side == "regulator" else "llm-decider")
                               if delegated(side) else side)
    decider = decider_of("operator")
    record = dr.Record(run_dir, "RobotTown", run_dir / "v1.cadl",
                       dr.parties("steward", "cadl-robot-town town.steward", MODEL, "llm", args.decider,
                                  delegate=delegate, regulator=True),
                       M("dr_time_base"))
    on_layers = lambda pid: ", ".join(next(p["layers"] for p in record.parties if p["id"] == pid))
    record.delegations(1, {"steward": [M("dr_auto_rule"), M("dr_kpi_rule")],
                           "llm-decider": [M("dr_g_llm_cond", layers=on_layers("llm-decider"))]
                           if delegated("operator") else [],
                           "llm-regulator": [M("dr_g_llm_cond", layers="regulation")]
                           if delegated("regulator") else []},
                       {"steward": M("dr_g_tool"),
                        "llm-decider": M("dr_g_llm", layers=",".join(sorted(delegate))),
                        "llm-regulator": M("dr_g_llm", layers=",".join(sorted(delegate)))})
    record.version(1, "steward", 1, 1, run_dir / "v1.cadl", None)
    fus: dict = {}                      # 後で確かめる約束：id → 内容、種類、まだ果たされていないか
    measured_before: list = []          # それまでの期に測った案（規制の案を当てはめる）
    gen = lambda req: {"model": MODEL, "request_digest": llm_steward.request_digest(req)}
    kpi_in_force = None                 # 有効な KPI の記録（K<k>.d、または書かれた KPI の K<k>）
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
        record.save(complete=False)

    def open_follow_ups(kind: str | None = None) -> str:
        return "\n".join(M("follow_ups_open", fid=f, kind=M("fu_" + x["kind"]), due=x["due"] or "—",
                            by=x["by"], text=x["text"]) for f, x in fus.items()
                         if x["open"] and kind in (None, x["kind"]))

    def decide(rid: str, ids: list[str], rpath: Path, k: int, options_text: str,
               side: str = "operator", step: str | None = None) -> dict:
        """判断要求への答え：人間（--answers か端末）か、人間に代わる LLM。
        side は判断する人間：operator（規則・契約・KPI の読み方）か regulator（規制）。"""
        if not delegated(side):
            ans = dict(answers.ask(rid, ids, rpath))
            ans["by"] = side
            for x in ans.get("fulfils") or []:
                if x not in fus:
                    finish()
                    raise SystemExit(M("a_fulfils_bad", rid=rid, id=x, ids=", ".join(fus) or "—"))
            return ans
        req = llm_steward.decide_request(rpath.read_text(encoding="utf-8"), options_text,
                                         decisions_brief(log), args.lang,
                                         principal=M("principal_" + side),
                                         open_follow_ups=open_follow_ups())
        step = step or ("reg-decide" if side == "regulator" else "kpi-decide" if rid.startswith("K")
                        else "decide")
        data, dnote = rec.get(f"p{k}-{step}", req)
        if data is None or str(data.get("choice")) not in ids or not str(data.get("reason")).strip():
            finish()
            raise SystemExit(M("llm_decide_fail", rid=rid, note=dnote))
        print(M("llm_regulated" if side == "regulator" else "llm_decided", rid=rid, note=M.note(dnote),
                id=data["choice"], reason=data["reason"]))
        ans = {"choice": str(data["choice"]), "reason": data["reason"],
               "assumptions": data.get("assumptions") or [], "unknowns": data.get("unknowns") or [],
               "predictions": data.get("predictions") or [],
               # 約束の種類：remeasure、propose、ask（以前の記録の action は ask）
               "follow_ups": [{"text": f["text"], "due": f.get("due_period"),
                               "kind": f.get("kind") if f.get("kind") in FU_KINDS else "ask",
                               "metrics": f.get("metrics") or []}
                              for f in data.get("follow_ups") or []],
               # 約束の id は記録にあるものだけ（果たしたかは、検証器が fulfilled_by で確かめる）
               "fulfils": [str(x) for x in data.get("fulfils") or [] if str(x) in fus],
               "by": decider_of(side), "generated": gen(req)}
        answers.used[rid] = {kk: v for kk, v in ans.items() if kk not in ("by", "generated")}
        return ans

    def follow_ups(rid: str, k: int, ans: dict, m0: str | None = None, sim0: dict | None = None) -> None:
        """判断とともにした約束を記録する。種類は 3 つ。測り直し（remeasure）は、この期の現行の
        測定（m0）と同じ条件で steward が測った値だけが果たす（m0 がない KPI の読み方の判断では
        問い）。案を出す約束（propose）は、後の期の改正案が果たす。問い（ask）は、後の判断が
        果たす。"""
        human = not ans["by"].startswith("llm")
        for n, f in enumerate(ans.get("follow_ups") or [], 1):
            fid, due = f"{rid}.f{n}", f.get("due")
            kind = f.get("kind") or ("remeasure" if f.get("remeasure") else "ask")
            if kind not in FU_KINDS:
                finish()
                raise SystemExit(M("a_kind_bad", rid=rid, kind=kind))
            if kind == "remeasure" and m0 is None:
                kind = "ask"
            remeasure = kind == "remeasure"
            subjects = []
            for s in (f.get("metrics") or []) if remeasure else []:
                s = str(s) if str(s).startswith("metric:") else f"metric:{s}"
                if dr.subject_value(record.get(m0), s) is None:
                    if human:
                        finish()
                        raise SystemExit(M("a_metric_bad", rid=rid, m=s,
                                           ex="metric:late_deliveries, metric:utility@C"))
                    continue                # LLM が測っていない値を挙げたら、その値は除く
                subjects.append(s)
            record.follow_up(fid, ans["by"], k, str(f["text"]),
                             int(due) if isinstance(due, int) else None, rid,
                             remeasure_of=m0 if remeasure else None, subjects=subjects, kind=kind)
            fus[fid] = {"text": str(f["text"]), "kind": kind,
                        "due": due, "by": ans["by"], "open": True, "k0": k, "sim": sim0,
                        "m0": m0, "subjects": subjects}
        for x in ans.get("fulfils") or []:
            if x in fus and fus[x]["kind"] == "ask":
                fus[x]["open"] = False

    def answer_asks(k: int, env: dict, last: bool, kpi_in_force: str, kpi_goal) -> None:
        """期限の来た問い（ask）を、それを約束した側の判断する者に渡して答えてもらう。
        改正案がなく判断要求がない期でも、問いは期限で判断に付す。最後の期には残った問いを
        すべて渡す（この後に答える場はない）。規則・契約・規制はこの判断では変えない。"""
        for side, owners, tag in (("operator", {"operator", "llm-decider"}, "Q"),
                                  ("regulator", {"regulator", "llm-regulator"}, "QR")):
            due = [f for f, x in fus.items() if x["open"] and x["kind"] == "ask" and x["by"] in owners
                   and (last or (isinstance(x["due"], int) and x["due"] <= k))]
            if not due:
                continue
            rid = f"{tag}{k}"
            items = [M("follow_ups_open", fid=f, kind=M("fu_ask"), due=fus[f]["due"] or "—",
                       by=fus[f]["by"], text=fus[f]["text"]) for f in due]
            body = [M("q_req_title", rid=rid, k=k, label=label(env)), "",
                    M("q_req_body") + (" " + M("q_req_last") if last else ""), ""]
            body += [f"- {x}" for x in items]
            rpath = run_dir / f"p{k}-{'ask' if side == 'operator' else 'reg-ask'}.md"
            rpath.write_text("\n".join(body) + "\n", encoding="utf-8")
            print(M("q_ask", rid=rid, ids=", ".join(due)))
            question = M("q_question", ids=", ".join(due))
            record.add(f"{rid}.req", "request", "steward", k, basis=due, to=decider_of(side),
                       question=question, options=[KEEP], why=[])
            ans = decide(rid, [KEEP], rpath, k, M("opt_keep_ask"), side=side,
                         step="ask" if side == "operator" else "reg-ask")
            human = not ans["by"].startswith("llm")
            who = (M("human") if side == "operator" else M("regulator")) if human else \
                (M("llm_decider") if side == "operator" else M("llm_regulator"))
            what = M("q_answered", ids=", ".join(due))
            record.add(rid, "decision", ans["by"], k, basis=[kpi_in_force, f"{rid}.req"] + due,
                       fulfils=due, generated=ans.get("generated"), question=question,
                       options=[KEEP], choice=KEEP, procedure="single", rule=None, affected=[],
                       result=f"v{version}",
                       **dr.evidence(ans["reason"], ans.get("assumptions"), ans.get("unknowns"),
                                     dr.predictions(ans.get("predictions"), KEEP)))
            for f in due:
                fus[f]["open"] = False
            follow_ups(rid, k, ans)
            print(M("outcome", who=who, what=what))
            history.append(M("history", k=k, label=label(env), what=what, who=who))
            md.extend([M("q_sec", rid=rid, k=k, label=label(env), who=who)]
                      + [f"- {x}" for x in items]
                      + [M("md_result", who=who, what=what),
                         M("md_reason" if human else "md_reason_llm", text=ans["reason"])])
            for f in ans.get("follow_ups") or []:
                md.append(M("md_follow_up", text=f["text"], due=f.get("due") or "—"))
            md.append("")
            s_ = gsn.add("strategy", M("gsn_s", k=k, what=what), kpi_goal, ref=rid)
            attach_answer(gsn, s_, ans)

    def regulate(k, env, regs, period_entries, kpi_in_force, kpi_goal, m0, sim, spec, evidence,
                 version, const_version) -> tuple[int, int]:
        """規制の案への判断要求 R<k>、推奨、判断、憲法の新しい版。規制を変えたら、規制の値を
        書いたモデルの新しいバージョン（付録 K）も作る。(const_version, version) を返す。"""
        pid = lambda cid: f"P{k}.{cid}"
        rid, ids = f"R{k}", [c.cid for c in regs] + [KEEP]
        reasons = "\n".join(M("item", id=c.cid, label=c.label or c.title, text=reason_text(c, spec))
                            for c in regs)
        opt_text = "\n".join(M("opt_line", id=c.cid, label=c.label or c.title, title=c.title)
                             for c in regs) + "\n" + M("opt_keep_reg", v=const_version)
        rreq = llm_steward.request_request(kpi_text, kpi.describe(spec, M), reasons, evidence, opt_text,
                                           decisions_brief(log), args.lang)
        rdata, rnote = rec.get(f"p{k}-reg-request", rreq)
        known = {o["id"] for o in (rdata or {}).get("options", [])}
        if rdata is None or not known <= set(ids):
            rnote = (rnote if rdata is None else M("bad_ids")) + M("tmpl_note")
            rdata = {"question": M("r_tmpl_q", k=k), "background": "",
                     "options": [{"id": c.cid, "summary": c.rationale, "stakeholders": ""} for c in regs],
                     "recommendation": {"id": KEEP, "reason": M("tmpl_keep")}, "unknowns": []}
        print(M("r_ask", k=k, note=M.note(rnote)))
        body = [M("r_req_title", k=k, label=label(env)), "", M("req_q", q=rdata["question"]), "",
                rdata["background"], "", M("req_why"), "", reasons, "", M("req_opts"), ""]
        explain = {o["id"]: o for o in rdata["options"]}
        for i in ids:
            o = explain.get(i, {})
            head = next((c.label or c.title for c in regs if c.cid == i), M("keep"))
            body.append(M("req_opt", id=i, head=head, summary=o.get("summary", ""))
                        + (M("req_stake", text=o["stakeholders"]) if o.get("stakeholders") else ""))
        body += ["", M("req_table"), "", evidence, "",
                 M("req_reco", id=rdata["recommendation"]["id"], reason=rdata["recommendation"]["reason"]),
                 "", M("req_unknown"), ""]
        body += [f"- {u}" for u in rdata["unknowns"]] or ["- " + M("none")]
        if k == args.rounds:              # 最後の期：この後に約束を果たす場はない
            body += ["", M("q_req_last")]
        rpath = run_dir / f"p{k}-reg-request.md"
        rpath.write_text("\n".join(body) + "\n", encoding="utf-8")
        print("  " + "\n  ".join(body[2:4]))
        print(M("reco", id=rdata["recommendation"]["id"], reason=rdata["recommendation"]["reason"]))
        oid = lambda i: KEEP if i == KEEP else pid(i)
        basis = [kpi_in_force, m0] + [x for c in regs for x in
                                      (pid(c.cid), f"{pid(c.cid)}.m", f"{pid(c.cid)}.c")
                                      if record.get(x) is not None]
        record.add(f"{rid}.req", "request", "steward", k, to=decider_of("regulator"),
                   question=rdata["question"], options=[oid(i) for i in ids],
                   why=[{"option": pid(c.cid), "reason": reason_text(c, spec)} for c in regs])
        basis.append(f"{rid}.req")
        if M("tmpl_note") not in rnote:
            reco_ = rdata["recommendation"]
            record.add(f"{rid}.rec", "recommendation", "llm", k, basis=[f"{rid}.req"], generated=gen(rreq),
                       request=f"{rid}.req",
                       options=[{"option": oid(o["id"]), "summary": o["summary"], "affected": o["stakeholders"]}
                                for o in rdata["options"] if o["id"] in ids],
                       recommends=oid(reco_["id"]) if reco_["id"] in ids else None,
                       **dr.evidence(reco_["reason"], reco_.get("assumptions"), rdata["unknowns"],
                                     dr.predictions(reco_.get("predictions"),
                                                    oid(reco_["id"]) if reco_["id"] in ids else KEEP)))
            basis.append(f"{rid}.rec")
        ans = decide(rid, ids, rpath, k, opt_text, side="regulator")
        chosen = next((c for c in regs if c.cid == ans["choice"]), None)
        attachments, what = [], M("reg_kept")
        if chosen is not None:
            _set(const, chosen.key, int(chosen.value) if chosen.key == "vote.quorum" else chosen.value)
            const_version += 1
            cfile = run_dir / f"constitution-v{const_version}.yaml"
            cfile.write_text(yaml.safe_dump(const, allow_unicode=True, sort_keys=False), encoding="utf-8")
            attachments = [{"path": cfile.name, "sha256": dr.sha256(cfile), "media_type": "application/yaml"}]
            version += 1
            write_version(version, k + 1)
            what = M("reg_adopted", label=chosen.label, v=const_version, mv=version)
        human = not ans["by"].startswith("llm")
        who = M("regulator") if human else M("llm_regulator")
        print(M("outcome", who=who, what=what))
        history.append(M("history", k=k, label=label(env), what=what, who=who))
        md.extend([M("r_sec", k=k, label=label(env), how=M("asked")),
                   M("md_whyasked") + "\n" + "\n".join("  " + x for x in reasons.splitlines()),
                   M("md_r_request", k=k, q=rdata["question"]),
                   M("md_reco_r", id=rdata["recommendation"]["id"], reason=rdata["recommendation"]["reason"])])
        md.extend(M("md_unknown", text=u) for u in rdata["unknowns"])
        md.append(M("md_chosen_reg" if human else "md_chosen_reg_llm", id=ans["choice"]))
        md.append(M("md_result", who=who, what=what))
        md.append(M("md_reason_reg" if human else "md_reason_reg_llm", text=ans["reason"]))
        for a in ans.get("assumptions") or []:
            md.append(M("md_assume" if human else "md_assume_llm", text=a))
        for u in ans.get("unknowns") or []:
            md.append(M("md_unknown_d", text=u))
        for f in ans.get("follow_ups") or []:
            md.append(M("md_follow_up", text=f["text"], due=f.get("due") or "—"))
        md.append("")
        s = gsn.add("strategy", M("gsn_s", k=k, what=what), kpi_goal, ref=rid)
        gsn.add("solution", M("gsn_sn", k=k), s, file=f"p{k}-evidence.md")
        attach_answer(gsn, s, ans)
        choice_id = oid(ans["choice"])
        record.add(rid, "decision", ans["by"], k, basis=basis, fulfils=ans.get("fulfils") or [],
                   attachments=attachments, generated=ans.get("generated"), question=rdata["question"],
                   options=[oid(i) for i in ids], choice=choice_id, procedure="single", rule=None,
                   affected=["A", "B", "C", "customers"] if chosen is not None else [],
                   result=f"v{version}",
                   **dr.evidence(ans["reason"], ans.get("assumptions"), ans.get("unknowns"),
                                 dr.predictions([{**x, "option": (oid(x["option"]) if x.get("option")
                                                                  not in (None, KEEP) else x.get("option"))}
                                                 for x in ans.get("predictions") or []], choice_id)))
        follow_ups(rid, k, ans, m0, sim)
        if chosen is not None:
            record.add(f"C{const_version}", "note", "steward", k, attachments=attachments,
                       text=M("dr_constitution", v=const_version, label=chosen.label))
            record.version(version, "steward", k, k + 1, run_dir / f"v{version}.cadl", rid)
        log.append({"id": rid, "period": k, "who": who, "summary": what, "reason": ans["reason"]})
        return const_version, version

    for k in range(1, args.rounds + 1):
        env = periods[(k - 1) % len(periods)]
        v_before, cv_before, rdata = version, const_version, None
        seeds = [s + env["seed_offset"] for s in const["seeds"]]
        sim = dict(seeds=seeds, period_s=const["period_s"], order_interval_s=env["order_interval_s"],
                   mode=args.mode, rush=env.get("rush"))
        print(M("period_head", k=k, label=label(env), interval=env["order_interval_s"], v=version,
                params=", ".join(f"{n}={v:g}" for n, v in read_params(text).items())))

        # 1. KPI
        since, new_kpi = kpi.load(kpi_path, k)
        wspec, bad = kpi.written(new_kpi) if new_kpi != kpi_text else (None, "")
        if bad:
            finish()
            raise SystemExit(M("kpi_bad", since=since, why=bad))
        if wspec:
            # 人間が評価方法を書いた節は、そのまま使う（LLM に読み方を頼まない）
            shown = kpi.prose(new_kpi)
            print(M("kpi_line", since=since, text=shown))
            spec = wspec
            print(M("kpi_written", desc=kpi.describe(spec, M)))
            md += [M("kpi_sec", k=k, since=since, how=M("written")), M("md_kpi", text=shown),
                   M("md_written", desc=kpi.describe(spec, M)), ""]
            kpi_goal = gsn.add("goal", M("gsn_kpi", since=since, text=shown), gsn.by_version, ref=f"K{k}")
            kpi_in_force = record.add(f"K{k}", "objective", "operator", k, text=shown,
                                      measure=measure_items(spec))
            gsn.add("context", M("gsn_measure", who=M("by_written"), desc=kpi.describe(spec, M)), kpi_goal)
            log.append({"id": f"K{k}", "period": k, "who": M("human"),
                        "summary": M("log_kpi", text=shown[:60], desc=kpi.describe(spec, M)), "reason": ""})
            revisit, kpi_text = "", new_kpi
        elif new_kpi != kpi_text:
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
            kgen = gen(llm_steward.kpi_request(new_kpi, since, kpi_text, decisions_brief(log), args.lang))
            record.add(f"K{k}", "objective", "operator", k, text=new_kpi, measure=None)
            for i, o in opts:
                record.add(f"K{k}.r{i}", "proposal", "llm", k, generated=kgen, kind="measure",
                           title=o["label"], changes=None, objective=f"K{k}", measure=measure_items(o),
                           affected=[], **dr.evidence(o["note"] or o["label"]))
            if ask:
                rpath = run_dir / f"p{k}-kpi-request.md"
                rpath.write_text("\n".join(sec) + "\n", encoding="utf-8")
                print(M("kpi_ask", k=k, id=data["recommended"]))
                kids = [f"K{k}.r{i}" for i, _ in opts]
                record.add(f"K{k}.req", "request", "steward", k, to=decider,
                           question=M("dr_kpi_q", since=since), options=kids,
                           why=[{"option": x, "reason": M("dr_kpi_why")} for x in kids])
                record.add(f"K{k}.rec", "recommendation", "llm", k, basis=[f"K{k}.req"], generated=kgen,
                           request=f"K{k}.req",
                           options=[{"option": f"K{k}.r{i}", "summary": kpi.describe(o, M),
                                     "affected": ""} for i, o in opts],
                           recommends=(f"K{k}.r{data['recommended']}"
                                       if data["recommended"] != "—" else None),
                           **dr.evidence(data["interpretation"],
                                         unknowns=data["concerns"]
                                         + [f"{x['decision']}: {x['why']}" for x in data["revisit"]]))
                ans = decide(f"K{k}", [i for i, _ in opts], rpath, k,
                             "\n".join(f"- {i}: {o['label']}（{kpi.describe(o, M)}）" for i, o in opts))
                spec = dict(opts[[i for i, _ in opts].index(ans["choice"])][1])
                md.append(M("md_chosen" if ans["by"] == "operator" else "md_chosen_llm",
                            id=ans["choice"], label=spec["label"]))
                if ans.get("reason"):
                    md.append(M("md_reason" if ans["by"] == "operator" else "md_reason_llm",
                                text=ans["reason"]))
                for a in ans.get("assumptions") or []:
                    md.append(M("md_assume" if ans["by"] == "operator" else "md_assume_llm", text=a))
            else:
                ans, spec = None, dict(opts[0][1])
            md.append("")
            kpi_goal = gsn.add("goal", M("gsn_kpi", since=since, text=new_kpi), gsn.by_version, ref=f"K{k}")
            kids = [f"K{k}.r{i}" for i, _ in opts]
            if ans:
                kpi_in_force = record.add(
                    f"K{k}.d", "decision", ans["by"], k,
                    basis=[f"K{k}"] + kids + [f"K{k}.req", f"K{k}.rec"],
                    fulfils=ans.get("fulfils") or [], generated=ans.get("generated"),
                    question=M("dr_kpi_q", since=since), options=kids, choice=f"K{k}.r{ans['choice']}",
                    procedure="single", rule=None, affected=[], result=None,
                    **dr.evidence(ans["reason"], ans.get("assumptions"), ans.get("unknowns"),
                                  dr.predictions(ans.get("predictions"), f"K{k}.r{ans['choice']}")))
                follow_ups(f"K{k}.d", k, ans)
            else:
                kpi_in_force = record.add(
                    f"K{k}.d", "decision", "steward", k, basis=[f"K{k}"] + kids,
                    question=None, options=kids, choice=kids[0], procedure="automatic",
                    rule=M("dr_kpi_rule"), affected=[], result=None, **dr.evidence(None))
            gsn.add("context", M("gsn_measure", who=M("by_human") if ans else M("by_llm"),
                                 desc=kpi.describe(spec, M)), kpi_goal)
            if ans:
                attach_answer(gsn, kpi_goal, ans)
            log.append({"id": f"K{k}", "period": k,
                        "who": (M("human") if ans["by"] == "operator" else M("llm_decider")) if ans else M("auto"),
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
        ach = kpi.achievement(spec, cur_vals, M)
        if ach:
            print(M("achieved", text=ach))

        # 2b. 前の期の判断で約束した測り直し：約束した期と同じ条件（シードと需要）で、いまの規則を測る
        remeasured = []
        for fid, f in fus.items():
            if not (f["open"] and f["kind"] == "remeasure" and f["k0"] < k):
                continue
            m_ = evaluate(ir, **f["sim"])
            eid = record.measurement(f"F{k}.{fid}", "steward", k, "in_force", m_, kpi.measure(m_),
                                     f["sim"], compared_with=f["m0"], version=f"v{version}",
                                     fulfils=[fid])
            f["open"] = False
            base, now_ = record.get(f["m0"]), record.get(eid)
            subjects = f["subjects"] or [f"metric:{primary}"]
            values = M("comma").join(
                f"{s[7:]} {dr.subject_value(base, s):g} → {dr.subject_value(now_, s):g}"
                for s in subjects if dr.subject_value(now_, s) is not None)
            print(M("remeasured", fid=fid, k0=f["k0"], v=version, values=values))
            remeasured.append(M("md_remeasure", fid=fid, k0=f["k0"], v=version, values=values))
            log.append({"id": eid, "period": k, "who": M("auto"),
                        "summary": M("log_remeasure", fid=fid, k0=f["k0"], values=values), "reason": ""})

        # 3. LLM が改正案を出し、判定にかけて測る
        req = llm_steward.propose_request(
            kpi_text, spec, MODE_NOTE[args.mode], env,
            _observation(cur) + "\n" + M("t_metrics") + M("comma").join(
                f"{n} {kpi.fmt(n, x)}" for n, x in cur_vals.items()), "".join(history),
            decisions_brief(log), revisit,
            const_text if const_version == 1 else yaml.safe_dump(const, allow_unicode=True, sort_keys=False),
            text, args.lang, open_follow_ups=open_follow_ups("propose"))
        data, note = rec.get(f"p{k}-propose", req)
        print(M("llm", note=M.note(note)))
        if data is None and args.llm == "replay":
            finish()
            raise SystemExit(M("no_propose", note=note))
        data = data or {"assessment": "", "proposals": [], "regulation_proposals": []}
        if data["assessment"]:
            print(M("assessment", text=data["assessment"]))
        proposable = {f for f, x in fus.items() if x["open"] and x["kind"] == "propose"}
        carries = lambda x: [str(f) for f in x.get("fulfils") or [] if str(f) in proposable]
        cands = [Cand(str(i), x["title"], x["rationale"], "amend", text=x["cadl"],
                      llm_flag=x.get("why_human", "") if x.get("needs_human") else "",
                      assumptions=x.get("assumptions") or [], unknowns=x.get("unknowns") or [],
                      predictions=x.get("predictions") or [], fulfils=carries(x))
                 for i, x in enumerate((data.get("proposals") or [])[:llm_steward.MAX_PROPOSALS], 1)]
        for c in cands:
            measure_amend(c, run_dir / f"p{k}-{c.cid}.cadl", text, cur, cur_vals, const, spec, sim)
        dropped = [c for c in cands if c.layer and c.layer not in layers]
        for c in dropped:
            print(M("layer_dropped", id=c.cid, label=c.label or c.title, layer=c.layer))
        cands = [c for c in cands if c not in dropped]
        regs = [Cand(f"R{i}", x["title"], x["rationale"], "regulation", key=x["key"], value=x["value"],
                 assumptions=x.get("assumptions") or [], unknowns=x.get("unknowns") or [],
                 predictions=x.get("predictions") or [], fulfils=carries(x))
                for i, x in enumerate((data.get("regulation_proposals") or [])[:1], 1)
                if "regulation" in layers]
        for c in regs:
            measure_regulation(c, const, cur, cands, past=measured_before)
        measured_before += [c for c in cands if c.m and c.votes]
        cands += regs
        best, escalated = triage(cands, spec)
        # 規制の案は、規則と契約とは別の判断要求（R<k>）で、規制の担当者に出す
        escalated_reg = [c for c in escalated if c.kind == "regulation"]
        escalated = [c for c in escalated if c.kind != "regulation"]
        evidence = table(cur, cur_vals, cands, spec)
        (run_dir / f"p{k}-evidence.md").write_text(
            M("evidence_head", k=k, label=label(env), desc=kpi.describe(spec, M))
            + (M("ev_ach", text=ach) if ach else "") + evidence + "\n"
            + "".join("\n" + M("item", id=c.cid, label=c.title, text=c.rationale) for c in cands) + "\n",
            encoding="utf-8")
        print("  " + evidence.replace("\n", "\n  "))
        pid = lambda cid: f"P{k}.{cid}"
        m0 = record.measurement(f"P{k}.m0", "steward", k, "in_force", cur, cur_vals, sim,
                                version=f"v{version}")
        period_entries = [m0]
        pgen = gen(req)
        for c in cands:
            layer = c.layer or None
            for f in c.fulfils:                     # 案を出す約束は、その案が果たす
                fus[f]["open"] = False
            period_entries.append(record.add(
                pid(c.cid), "proposal", "llm", k, generated=pgen, fulfils=c.fulfils,
                kind="amendment", title=c.title,
                changes=dr.change(c.label, layer), objective=None, measure=None,
                affected=(["customers"] if layer == "contract" else
                          ["A", "B", "C", "customers"] if layer == "regulation" else []),
                **dr.evidence(c.rationale or c.title, c.assumptions, c.unknowns,
                              dr.predictions(c.predictions, pid(c.cid)))))
            if c.vals is not None:
                period_entries.append(record.measurement(
                    f"{pid(c.cid)}.m", "steward", k, pid(c.cid), c.m, c.vals, sim,
                    compared_with=m0, runs_better=c.seeds_better))
            elif c.effects:
                # 規制の案：その期の測定に当てはめた件数と、影響を受ける 3 社と顧客の値
                # （規制は規則を変えないので、現行の規則の値がそのまま規制の案の下の値）
                period_entries.append(record.measurement(
                    f"{pid(c.cid)}.m", "steward", k, pid(c.cid), cur, c.effects, sim,
                    customers=cur_vals, note=M("dr_reg_method"), extra=c.per_effects))
            period_entries.append(record.checks(
                f"{pid(c.cid)}.c", "steward", k, pid(c.cid), c.gates,
                "; ".join(x for x in (c.why, c.kpi_why, c.effect) if x)))
            if c.votes:
                period_entries += record.votes(pid(c.cid), k, pid(c.cid), c.votes)

        # 4. 自動で採択するか、人間に判断を求めるか
        md.append(M("d_sec", k=k, label=label(env), how=M("asked") if escalated else M("auto")))
        md += remeasured
        md.append(M("md_measure", desc=kpi.describe(spec, M)))
        if ach:
            md.append(M("md_ach", text=ach))
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
            if k == args.rounds:              # 最後の期：この後に約束を果たす場はない
                body += ["", M("q_req_last")]
            rpath = run_dir / f"p{k}-request.md"
            rpath.write_text("\n".join(body) + "\n", encoding="utf-8")
            print("  " + "\n  ".join(body[2:4] + body[body.index(M("req_opts")) + 2:body.index(M("req_table")) - 1]))
            print(M("reco", id=rdata["recommendation"]["id"], reason=rdata["recommendation"]["reason"]))
            for u in rdata["unknowns"]:
                print(M("unknown", text=u))
            oid = lambda i: KEEP if i == KEEP else pid(i)
            record.add(f"D{k}.req", "request", "steward", k, to=decider, question=rdata["question"],
                       options=[oid(i) for i in ids],
                       why=[{"option": pid(c.cid), "reason": reason_text(c, spec)} for c in escalated])
            period_entries.append(f"D{k}.req")
            if M("tmpl_note") not in rnote:
                reco_ = rdata["recommendation"]
                record.add(f"D{k}.rec", "recommendation", "llm", k, basis=[f"D{k}.req"],
                           generated=gen(llm_steward.request_request(
                               kpi_text, kpi.describe(spec, M), reasons, evidence, opt_text,
                               decisions_brief(log), args.lang)),
                           request=f"D{k}.req",
                           options=[{"option": oid(o["id"]), "summary": o["summary"],
                                     "affected": o["stakeholders"]} for o in rdata["options"]
                                    if o["id"] in ids],
                           recommends=oid(reco_["id"]) if reco_["id"] in ids else None,
                           **dr.evidence(reco_["reason"], reco_.get("assumptions"), rdata["unknowns"],
                                         dr.predictions(reco_.get("predictions"),
                                                        oid(reco_["id"]) if reco_["id"] in ids else KEEP)))
                period_entries.append(f"D{k}.rec")
            ans = decide(f"D{k}", ids, rpath, k, opt_text)
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
        ir_before = ir
        if chosen is not None and chosen.kind == "amend":
            text, ir = chosen.text, compile_rules(run_dir / f"p{k}-{chosen.cid}.cadl")
            version += 1
            write_version(version, k + 1)
            yes = "".join(x for x in sorted(chosen.votes) if chosen.votes[x]) or M("nobody")
            what = M("adopted", v=version, label=chosen.label, layer=M("layer")[chosen.layer],
                     gain=kpi.fmt_gain(spec["goals"][0]["metric"], chosen.gain), yes=yes)
            if not chosen.gates["vote"]:
                what += M("override")
        who = (M("human") if ans["by"] == "operator" else M("llm_decider")) if ans else M("auto")
        print(M("outcome", who=who, what=what))
        history.append(M("history", k=k, label=label(env), what=what, who=who))
        md.append(M("md_result", who=who, what=what))
        if ans:
            human = ans["by"] == "operator"
            if ans.get("reason"):
                md.append(M("md_reason" if human else "md_reason_llm", text=ans["reason"]))
            for a in ans.get("assumptions") or []:
                md.append(M("md_assume" if human else "md_assume_llm", text=a))
            for u in ans.get("unknowns") or []:
                md.append(M("md_unknown_d", text=u))
            for f in ans.get("follow_ups") or []:
                md.append(M("md_follow_up", text=f["text"], due=f.get("due") or "—"))
            for g in ans.get("gsn") or []:
                md.append(M("md_gsn", type=g["type"], text=g["text"]))
        md.append("")
        s = gsn.add("strategy", M("gsn_s", k=k, what=what), kpi_goal, ref=f"D{k}")
        gsn.add("solution", M("gsn_sn", k=k), s, file=f"p{k}-evidence.md")
        gsn.add("context", M("gsn_c", k=k, label=label(env), interval=env["order_interval_s"]), s)
        if ans:
            attach_answer(gsn, s, ans)
        else:
            gsn.add("justification", M("gsn_j_auto") if chosen else M("gsn_j_none"), s, by="tool")
        # 判断の記録（付録 L）：人間でも LLM でもツールでも、同じ形で
        if escalated:
            opts_ = [pid(c.cid) for c in options] + [KEEP]
            first = escalated[0]
            if chosen is not None:
                considered = chosen
            else:
                considered = first
            # 手続き（付録 L、L-6）：投票が結果を決めたか、判断した者が投票を覆したか、
            # 投票ではなくほかの判定や判断した者自身の判断で決まったか
            others_ok = considered.passed("check", "const", "inv", "kpi_guard", "gain", "seeds")
            if considered.layer != "rule" or not considered.votes:
                procedure = "single"
            elif chosen is not None:
                procedure = "vote" if chosen.gates.get("vote") else "override"
            elif not first.gates.get("vote"):
                procedure = "vote"              # 否決のとおりに変えない
            else:
                procedure = "override" if others_ok else "single"
            choice_id = pid(chosen.cid) if chosen is not None else KEEP
            dev = dr.evidence(ans["reason"], ans.get("assumptions"), ans.get("unknowns"),
                              dr.predictions([{**x, "option": (pid(x["option"]) if x.get("option")
                                                               not in (None, KEEP) else x.get("option"))}
                                              for x in ans.get("predictions") or []], choice_id))
            record.add(f"D{k}", "decision", ans["by"], k,
                       basis=[kpi_in_force] + period_entries, fulfils=ans.get("fulfils") or [],
                       generated=ans.get("generated"), question=rdata["question"], options=opts_,
                       choice=choice_id, procedure=procedure, rule=None,
                       affected=(["customers"] if chosen is not None and chosen.layer == "contract" else [])
                       + ([x for x in sorted(chosen.votes) if not chosen.votes[x]]
                          if chosen is not None and chosen.votes else []),
                       result=f"v{version}", **dev)
            follow_ups(f"D{k}", k, ans, m0, sim)
        else:
            record.add(f"D{k}", "decision", "steward", k, basis=[kpi_in_force] + period_entries,
                       question=None, options=[pid(c.cid) for c in cands if c.kind != "regulation"]
                       + [KEEP],
                       choice=pid(chosen.cid) if chosen is not None else KEEP, procedure="automatic",
                       rule=M("dr_auto_rule"), affected=[], result=f"v{version}",
                       **dr.evidence(what if chosen is not None else None))
        if chosen is not None and chosen.kind == "amend":
            record.version(version, "steward", k, k + 1, run_dir / f"v{version}.cadl", f"D{k}")
            # 採択した案を、次の期の条件（シードと需要）で、いまの規則と並べて測る。判断で使わなかった
            # 条件で、判断（と推奨、案）の予測を後の測定が確かめられるように（付録 L、prediction missed）
            nxt = periods[k % len(periods)]
            nsim = dict(sim, seeds=[s + nxt["seed_offset"] for s in const["seeds"]],
                        order_interval_s=nxt["order_interval_s"], rush=nxt.get("rush"))
            b0, b1 = evaluate(ir_before, **nsim), evaluate(ir, **nsim)
            v0, v1 = kpi.measure(b0), kpi.measure(b1)
            a0 = record.measurement(f"D{k}.n0", "steward", k, "in_force", b0, v0, nsim,
                                    version=f"v{v_before}", note=M("dr_next_method", n=k % len(periods) + 1, label=label(nxt)))
            record.measurement(f"D{k}.n", "steward", k, pid(chosen.cid), b1, v1, nsim, compared_with=a0,
                               runs_better=sum(kpi.gain(spec, kpi.measure(r0), kpi.measure(r1)) > 0
                                               for r0, r1 in zip(b0["runs"], b1["runs"])),
                               note=M("dr_next_method", n=k % len(periods) + 1, label=label(nxt)))
            values = M("comma").join(f"{n} {kpi.fmt(n, v0[n])} → {kpi.fmt(n, v1[n])}"
                                     for n in dict.fromkeys(g["metric"] for g in spec["goals"]) if n in v0 and n in v1)
            print(M("next_check", n=k % len(periods) + 1, label=label(nxt), v0=v_before, v=version, values=values))
            md.append(M("md_next_check", n=k % len(periods) + 1, label=label(nxt), v0=v_before, v=version, values=values))
        log.append({"id": f"D{k}", "period": k, "who": who, "summary": what,
                    "reason": (ans or {}).get("reason", "")})

        # 6. 規制（憲法の値）の判断：規則と契約とは別に、規制の担当者（または代わりの LLM）が決める。
        #    規制の変更は次の期から効く。規則のバージョンは変わらない
        if escalated_reg:
            const_version, version = regulate(k, env, escalated_reg, period_entries, kpi_in_force, kpi_goal,
                                     m0, sim, spec, evidence, version, const_version)
        answer_asks(k, env, k == args.rounds, kpi_in_force, kpi_goal)
        for f in run_dir.glob(f"p{k}-*.cadl"):          # 不採択の案は消し、採択したバージョンだけを残す
            f.unlink()
        finish()

    anchor = record.save(complete=True)
    print(M("done", dir=run_dir.relative_to(ROOT), v=version))
    print(M("dr_saved", path=(run_dir / dr.FILE).relative_to(ROOT), anchor=anchor))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
