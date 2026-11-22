# CADL Robot Town: rules that a community amends itself (Course D)

English | [日本語](README.ja.md)

Hands-on page: [Course D — Robot Town](https://www.ertl.jp/cadl-spec/docs/handson/robot-town-tutorial)

Three delivery companies (A, B, C; two robots each) share the 11-node road
network of the Raspberry Pi Mouse simulator. The road rules and the delivery
promise to customers are written in CADL (`cadl/robot_town.cadl`). After each
simulated hour, amendments are proposed; a proposal becomes the next version of
the rules only if it passes four gates.

There are two amendment loops.

| Loop | Goal set by | Proposals by | What cannot be settled mechanically |
|---|---|---|---|
| `town.steward` ([below](#humans-set-the-kpi-the-llm-proposes-townsteward)) | a human, as KPIs in natural language (`kpi.en.md` or `kpi.md`, may change over time) | an LLM (rules, regulation, contract) | the LLM writes a decision request; a human answers and records the reason in prose or GSN |
| `town.council` | `objective` in `constitution.yaml` (fixed) | rule-based search, or an LLM (rules only) | settled by the gates (a vetoed proposal is dropped) |

| Gate | Passes when |
|---|---|
| Check | `cadl check` and `cadl sim-ir` succeed and the IR carries every value the town needs |
| Constitution | only the numbers on `# @amend:<name>` lines change, one parameter by one step (`constitution.yaml`); the council may change the rule layer only; after simulation, every company keeps an on-time rate of at least 0.6 |
| Improvement | the objective improves on the same random seeds |
| Vote | at least two of the three companies do not lose utility (on-time deliveries minus sanction points) |

- Norms (deadlines, monitors, lifecycle states) are judged by the Python
  reference runtime of Course A (`town/contract_engine.py`), reading the IR.
- Moving robots pass through intersections; a robot stopped at an
  intersection (loading, or waiting on the road for its next order) blocks it.
- Company policies (`town/policies.py`): A always returns to its garage,
  B waits on the road until just before the time limit (and stays as long as it
  likes when there are no sanctions), C waits on the road until an order comes or
  it is evicted; at the highest sanction weight (1.0) it leaves 15 seconds after
  the time limit.
- Two operating modes, DISTRIBUTED and CENTRALIZED, switched by the conditions
  in CADL `sos.transitions`. A switch to CENTRALIZED takes effect only once no
  robot is waiting on the road, and the transition's `safety_invariant` is
  checked then.
- The council can be rule-based (try every one-step amendment) or an LLM
  (Claude Opus 5.5 writes whole CADL files, which go through the same gates).

## Setup

Assumes Course A's setup: the CADL compiler cloned to `~/program/cadl_repo`, as in
[Section 2.4 of the Textbook](https://www.ertl.jp/cadl-spec/docs/textbook/toolchain-setup/#24-setup-5-min),
which also lists fixes for install problems. Use cadl 0.3.32 or later (checked
with 0.3.39; see `cadl --version`; the decision records need 0.3.39): an older compiler leaves the safety invariant of a
regime transition out of the IR, and the town refuses the rules. A rule file with
`sos.regimes` (per-regime bindings, cadl 0.3.32) is refused too, since the town does
not apply those bindings yet.

```bash
cd ~/program
git clone https://github.com/ertlnagoya/cadl-robot-town
cd cadl-robot-town
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip   # the pip of macOS python3 (3.9) is too old for the next line
pip install -e ~/program/cadl_repo     # the cadl command and PyYAML
pip install anthropic                  # only to call a real LLM (town.steward, --proposer llm)
```

## Commands

```bash
python -m town.period --mode DISTRIBUTED        # one period (one simulated hour)
python -m town.council --mode DISTRIBUTED       # one round of the amendment loop
python -m town.council --mode DISTRIBUTED --rounds 6 --name seasons-d   # six periods, demand from scenario.yaml
python -m town.council --objective road_violations --mode DISTRIBUTED --rounds 8 --name goodhart-d
python -m town.compare runs/goodhart-d/v1.cadl runs/goodhart-d/v5.cadl runs/seasons-d/v4.cadl --mode DISTRIBUTED
python -m town.period --rush 1200 2400 25 --no-quiescence
python -m town.council --proposer llm --mode DISTRIBUTED --rounds 2 --name llm-distributed   # needs ANTHROPIC_API_KEY
python -m town.council --proposer replay --replay-dir examples/llm-distributed --mode DISTRIBUTED --rounds 2
python -m town.steward --mode DISTRIBUTED --rounds 6 --name kpi-d \
    --llm replay --replay-dir examples/steward-d --answers examples/steward-d/answers.yaml
```

Program output is in Japanese. The results of each experiment, with the
numbers, are in [README.ja.md](README.ja.md) and on the hands-on page.

## What the experiments show

- **Sanctions and vetoes**: the amendment that helps the town most doubles the
  sanction weight (`sanction_lambda 0.5 → 1`); C then leaves the intersection
  15 seconds after the time limit, and blocked time falls from 2404 s to 845 s.
  C votes against, but A and B make the quorum of two. Evicting a robot once two
  robots wait behind it (`block_k 3 → 2`) also helps, and is voted down by B and C,
  who gain from waiting on the road, in every period.
- **Demand**: after period 1 only small `hold_limit` steps pass in the busy
  season, and nothing improves the goal in the quiet season.
- **Goodhart's law**: with the objective "fewer road-rule violations", the
  rules loosen period after period; counted violations fall while blocked time
  and late deliveries rise by about half. Heavier sanctions also reduce the count
  in every period, but a loosening reduces it as much or more, and C always votes
  against heavier sanctions. The rules of the on-time objective, which doubled the
  sanctions, end with fewer road violations (19.8) than the loosened ones (21.0).
- **Operating modes**: CENTRALIZED delivers more on time except at very high
  demand; with a mid-period rush, switching delivers more on time than either fixed mode. Switching without waiting
  for the road to clear breaks the safety invariant.
- **LLM council**: Claude's proposals all pass the check and the constitution
  (on 2026-11-16: `block_k 3 → 2` and `hold_limit 60s → 50s`), but none is
  adopted. It never proposes the heavier sanction weight the rule-based council
  adopts, since it is not told how the companies behave, and it mispredicts
  company B's vote. `examples/llm-distributed/` is a real run you can replay
  without an API key.

## Humans set the KPI, the LLM proposes (town.steward)

- **Human**: writes the service KPIs in natural language in `kpi.en.md`
  (sections `## From period k`) or `kpi.md` (`## 第 k 期から`), answers the LLM's decision
  requests, and records the reason in prose (`reason`) or as GSN elements
  (`gsn:`).
- **LLM** (`town/llm_steward.py`): turns the KPI into a measure built from the
  metrics in `town/kpi.py`, proposes amendments to rules, regulation
  (constitution values) and the contract (`DELIVERY_SLA` deadlines), and writes
  a decision request when it should not decide alone.
- **Gates**: the same four as `town.council`; numbers come from the simulation,
  never from the LLM's text.

`town/steward.py` does not adopt automatically, and asks the human, when a
proposal is vetoed although it improves the KPI more than every proposal that could be
adopted automatically (agreement); changes
the regulation; changes the contract or an operating-mode threshold while
improving the KPI and keeping the invariants (a contract proposal that does not
improve the KPI is put to the human only if the LLM asks); improves the main KPI
while breaking a KPI condition or worsening a watched goal (KPI conflict);
improves on fewer than 4 of 5 seeds (uncertain); or when the LLM itself says a
human should decide (for proposals that keep the invariants). A new KPI is
confirmed with the human when it can be read in more than one way, relies on a
proxy, or removes a premise of an earlier decision. Every decision goes to `decisions.md` and
`gsn.yaml` (diagram in `gsn.md`) in `runs/<name>/`, and is passed to the LLM in
later periods. `--lang en` makes the text shown to the human, and the LLM's
writing, English (KPIs in `kpi.en.md`, headings `## From period k`).

### Writing the measure and the targets yourself

A KPI in prose alone does not say what to measure or when it is met. Add an
`Evaluation:` list to a section of the KPI file and `town.steward` uses it as the
measure as is, without asking the LLM to read the KPI (Japanese example:
`examples/kpi-written/kpi.md`).

```markdown
## From period 1

Deliver at least 95% of orders on time across the town. In every company, no more
than 5% of customers should wait longer than the 240 s the contract allows.

Evaluation:
- goal: on_time_rate >= 0.95
- watch: worst_company_p95_wait_s <= 240
- guard: min_company_on_time_rate >= 0.8
```

- `goal` (exactly one) is the main goal that improvements are measured by; `watch`
  goals ask the human when they get worse; `guard` lines need a value and must hold
  after every amendment. A value on a goal or watch line is its target. Write
  `>=` for metrics where larger is better and `<=` for the others (otherwise the
  run stops with an error); `95%` also works. In Japanese the keywords are
  `評価方法:`, `主目標`, `監視`, `条件`, and metrics may be written by their short
  Japanese names.
- Waiting time is the seconds from order to delivery; an order still open at the
  end of the period counts the seconds until then. The metrics are `p95_wait_s`,
  `max_wait_s` and `worst_company_p95_wait_s`; `on_time_rate` is the town-wide
  on-time rate.
- Each period reports, for the current rules, whether every target and guard is met
  ("KPI attainment") in the terminal, `decisions.md` and `p<k>-evidence.md`; the
  table of proposals (in `p<k>-evidence.md` and the decision request) marks, for each
  proposal, whether the targets and guards would be met. Targets
  are for reporting; they do not change which amendments are adopted.
- Sections without the list are read by the LLM as before; the LLM may also set
  `target`, but is told not to invent a level the KPI does not state.

`python -m town.kpi [file]` lists the metrics and shows how each section is read.

```bash
python -m town.steward --lang en --mode DISTRIBUTED --rounds 6 --name kpi-d-en   # calls the LLM; answer in the terminal
python -m town.steward --lang en --mode DISTRIBUTED --rounds 6 --name kpi-d-en \
    --llm replay --replay-dir examples/steward-d-en --answers examples/steward-d-en/answers.yaml
```

Without a terminal and without an answer, the run writes the decision request to
`runs/<name>/p<k>-request.md` and stops; rerun the same command with the same
`--name`, adding `--answers <YAML>`, and the LLM outputs already in
`runs/<name>/llm/` are reused. Each record carries a digest of the request
(`request_digest`); when a replay sends a different request (because the KPI
file, the rules or an earlier answer changed), the run says so and uses the
record anyway.

### Real LLM records

The steward records come from real runs with Claude Opus 5.5 on 2026-11-18 (cadl 0.3.36);
the council record from 2026-11-16 (cadl 0.3.31). The answers to the decision requests
(`answers.yaml`) were written by Claude playing the human operator when the records were made.

| Record | Contents |
|---|---|
| `examples/steward-d-en/` | six periods with `kpi.en.md` (English); about 61,600 input / 51,100 output tokens |
| `examples/steward-d/` | the same in Japanese (`kpi.md`); about 63,300 / 50,100 tokens |
| `examples/steward-goodhart/en/` | a proxy KPI ("reduce road-rule violations"), one period, in English |
| `examples/steward-goodhart/proxy/`, `guarded/` | the same proxy KPI in Japanese, answered two ways: the count alone, or the count with service, waits and blocked time watched |
| `examples/llm-distributed/` | the LLM as the council of `town.council`, two periods |

- Reading the KPI: neither LLM set a target, since the KPI gives no level for
  "waiting badly". The English LLM offered 240 s (the two promised legs added up)
  as an option and said the human must confirm it; the human chose to watch the
  worst company's 95th-percentile wait with no level.
- Period 1 ends the same way in both languages. Neither LLM proposes heavier
  sanctions; both propose `block_k 3 → 2`, which raises on-time deliveries on all 5
  seeds and cuts the worst company's p95 wait from 273 s to 95.6 s. B and C vote it
  down and the human adopts it against the vote. The Japanese LLM recommends it; the
  English LLM recommends keeping the rules, since C's utility falls by a third for
  reasons it cannot explain.
- In Japanese period 2 the LLM wrote that adopting a proposal A and B voted for would
  override the vote. The decision request does not include the constitution, so the
  LLM does not know the quorum.
- The two runs decide differently in period 5. `hold_limit 60s → 70s` narrows the spread of utilities
  by 1.7 in both. The Japanese reading of the new KPI still watches the worst company's
  p95 wait, which the change lengthens from 178 s to 212 s, and the human keeps the
  rules. The English reading does not watch it; the LLM recommends the change and the
  human adopts it (v3). In English period 6 both proposals lower the worst company's
  on-time rate, and the human keeps the rules.
- Removing sanctions (`sanction_lambda 0.5 → 0`) and returning to `block_k 3` narrow
  the spread most, but raise late deliveries and lower the worst company's on-time
  rate. Neither LLM recommends them; of removing sanctions, the English LLM wrote that
  it narrows the gap "by switching off the measurement of road misuse".
- With the proxy KPI, the Japanese LLM proposed both the heavier sanction weight and
  a loosening (`hold_limit 60s → 70s`); both cut the count from 25.8 to 23.6, but
  only the first cuts blocked time. Even when the human made the count the only goal,
  the LLM flagged the loosening for the human and recommended the sanctions. With
  service watched, and in English, the LLM proposed the sanctions and `block_k 3 → 2`,
  and the sanctions were adopted automatically.

The hand-written sample in `examples/llm-replay/` has had lines inserted
mechanically when the starting rules changed (two AMEND steps on 2026-10-19,
contract-layer tags on the `DELIVERY_SLA` lines on 2026-10-29; see `edits`),
because the constitution gate compares a proposal with the current rules line by
line. Its second proposal is stopped at the constitution gate because its pickup
deadline no longer matches its guarantee line.

Council records carry a request digest (`request_digest`), as the `town.steward`
records do. On replay, the request is rebuilt from the current rules,
constitution, amendment history and prompt and compared with the record; if they
differ, the replay prints a note. The hand-written sample holds the digest of the
current request.

### Decision records (decisions.jsonl)

`town.steward` and `town.council` write `runs/<name>/decisions.jsonl` in each run, in the
format of [Appendix L](https://www.ertl.jp/cadl-spec/docs/spec/appendix-l-decisions) of the
CADL specification (decisions 0.3): who proposed what, what was measured, how each company
voted, who recommended what, and who decided with which reason, one entry per line, with the
hash chain of G.8.

```bash
cadl decisions-verify runs/<name>/decisions.jsonl     # cadl 0.3.39 or later
```

- Each version of the rules is kept as `runs/<name>/v<n>.cadl` and attached to its `version`
  entry in the record, the first version `v1` included. These files are the rules with the
  amendment extension of [Appendix K](https://www.ertl.jp/cadl-spec/docs/spec/appendix-k-amendment)
  written in by the tool (`town/model.py`): `sos.version` `v<n>`, the operator and the regulator
  as actors, the values of the regulation in `context.environment`, and `amendment:` with
  `previous`, `effective`, who decides each layer (the operator decides the rule layer after a
  vote with the quorum of the constitution, and the contract layer; the regulator decides the
  regulation layer) and the amendable parameters with their values, ranges, steps and places.
  The LLM still reads and writes the rules as in `cadl/robot_town.cadl`. With these files the
  verifier checks the versions and their order (L-9), that each proposal moves a parameter of
  the version in force within its range and step (L-10), and who decided and the outcome of the
  votes (L-11, L-6). A change of the regulation makes a new version of the model too.

- A proposal, a recommendation and a decision carry the same evidence (reason, assumptions,
  unknowns, predictions) whether a person, the LLM or a tool made them. `town.steward` stops
  when a human answer in `answers.yaml` has no reason. An answer may also give `unknowns`,
  `follow_ups` and `fulfils` (ids of earlier follow-ups, such as `D1.f1`).
- Who decides what: the operator decides on the rules and the contract (and on the reading of
  the KPI), the regulator, another person, on the regulation (the values of the constitution
  under `regulation:`). A regulation proposal goes to the regulator in a request of its own
  (`R<k>`, `p<k>-reg-request.md`, answered under `R<k>` in `answers.yaml`), after the decision
  on the rules and the contract of the same period; a change takes effect in the next period
  (`constitution-v<n>.yaml` and a new version of the model). In the record each decider gives
  its `layers`. The steward (and the council), which adopt rule changes by themselves when all
  the checks pass, act on behalf of the operator (`on_behalf_of`) with `["rule"]`.
- Who let whom decide: at the start of the record the operator delegates to each party that
  acts in its place (a `delegation` entry `G.<party>`, decisions 0.3): to the steward or the
  council the rule layer by its own rule (`procedures: ["automatic"]`, with that rule and the
  checks as `conditions`), and, with `--decider llm`, to the LLM the layers given with
  `--delegate`; the regulator likewise to `llm-regulator`. Running the tool, or giving
  `--delegate`, is that instruction of the human. The verifier then shows each decision made
  within a delegation under it, and reports the finding delegated only for one outside it.
- A regulation proposal is measured before the decision: the steward applies the proposed value
  to the measurements of the version in force and of the proposals of the period and to their
  votes (no new run), and records how many proposals then meet the invariants and pass the
  vote, in that period and over every proposal measured in the earlier periods of the record
  (`..._all`), and for each company how many proposals would pass although it voted against
  them (`passing_against` in its values), with the values of the companies and the customers
  under the rules in force (`P<k>.R<i>.m`).
- A follow-up says what fulfils it, and promises only what someone in the town does. In an
  answer, `follow_ups` items have `text`, `due` (a period) and `kind`:
  - `remeasure` (with `metrics`, such as `utility@C`, `road_violations`): the steward measures
    the rules in force at the start of the next period under the conditions of the period of
    the decision (the same seeds and demand), as `F<k>.<follow-up id>`, which fulfils it
    (`remeasure: true` is read as this kind);
  - `propose`: an amendment to measure; the LLM that proposes amendments sees the open ones in a
    later period, and a proposal that carries one out names it in `fulfils`;
  - `ask` (the default): a question that a later decision settles by naming it in `fulfils`.
    When its `due` period comes and no decision has settled it, the steward puts it to the
    decider of its side in a request of its own (`Q<k>` for the operator, `QR<k>` for the
    regulator; `p<k>-ask.md`, answered with `keep` and a reason under `Q<k>` in
    `answers.yaml`), and in the last period every open question. The requests of the last
    period say that no promise made then can be kept.
  A decision that names a promise of another kind does not fulfil it, and the verifier reports
  `fulfilment not shown`.
- With `--decider llm`, the LLM answers the decision requests in place of the human for the
  layers given with `--delegate` (default `rule,contract`; `rule` and `contract` go together,
  since they are decided in one request). It acts for the operator as `llm-decider`
  (`llm/p<k>-decide.json`, `llm/p<k>-kpi-decide.json`) and for the regulator as
  `llm-regulator` (`llm/p<k>-reg-decide.json`); requests on the other layers go to the human.
  The LLM sees the open follow-ups, says which questions a decision settles (`fulfils`), and
  gives each of its own follow-ups a kind (`remeasure`, `propose`, `ask`). It is told not to
  promise investigations that nobody in the town carries out, and not to restate an open
  promise. Its decisions are made within the delegation `G.llm-decider` (or
  `G.llm-regulator`).
- `--layers rule,contract` keeps the proposals to the rule and contract layers (regulation
  proposals are left out unmeasured).
- Samples, decided by the LLM in place of the humans (Claude Opus 5.5, 2026-11-22):
  `examples/steward-contract/` (Japanese) and `examples/steward-contract-en/` (English), six
  periods under a KPI that allows the promised times to be revised (`--layers rule,contract`);
  `examples/steward-regulation/` and `examples/steward-regulation-en/`, six periods under a KPI
  that allows the regulation to be revised (`--delegate rule,contract,regulation`).

## Checking the examples

`python -m town.check_examples` runs every example again, replaying the recorded LLM outputs
(no API key), and compares the history, the GSN and the findings of the decision record with
`examples/expected/<name>/`; it also requires the record to verify without errors and every
LLM request to be the recorded one. `--update` writes the current results there. GitHub
Actions runs it on Python 3.9 and 3.14 for each pull request and push to main.

## License

Apache License 2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)).
`town/contract_engine.py` is copied from
[cadl-raspimouse-simulator](https://github.com/ertlnagoya/cadl-raspimouse-simulator)
(commit 3fdf7d3).
