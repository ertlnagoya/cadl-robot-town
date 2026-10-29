# CADL Robot Town — rules that a community amends itself (Course D)

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
  B waits on the road until just before the time limit, C waits on the road
  until sanctions become heavy.
- Two operating modes, DISTRIBUTED and CENTRALIZED, switched by the conditions
  in CADL `sos.transitions`. A switch to CENTRALIZED takes effect only once no
  robot is waiting on the road, and the transition's `safety_invariant` is
  checked then.
- The council can be rule-based (try every one-step amendment) or an LLM
  (Claude Opus 5.5 writes whole CADL files, which go through the same gates).

## Setup

Assumes Course A's setup: the CADL compiler cloned to `~/program/cadl_repo`, as in
[Section 2.4 of the Textbook](https://www.ertl.jp/cadl-spec/docs/textbook/toolchain-setup/#24-setup-5-min),
which also lists fixes for install problems.

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
python -m town.compare runs/goodhart-d/v1.cadl runs/goodhart-d/v5.cadl runs/seasons-d/v7.cadl --mode DISTRIBUTED
python -m town.period --rush 1200 2400 25 --no-quiescence
python -m town.council --proposer llm --mode DISTRIBUTED --rounds 2 --name llm-distributed   # needs ANTHROPIC_API_KEY
python -m town.council --proposer replay --replay-dir examples/llm-distributed --mode DISTRIBUTED --rounds 2
python -m town.steward --mode DISTRIBUTED --rounds 6 --name kpi-d \
    --llm replay --replay-dir examples/steward-d --answers examples/steward-d/answers.yaml
```

Program output is in Japanese. The results of each experiment, with the
numbers, are in [README.ja.md](README.ja.md) and on the hands-on page.

## What the experiments show

- **Vetoes**: the amendment that helps the town most (`block_k 3 → 2`, evict
  a robot once two robots wait behind it) is voted down by B and C, who gain
  from waiting on the road; a small amendment all three accept passes instead.
- **Demand**: in quiet periods B switches sides and the same amendment passes.
- **Goodhart drift**: with the objective "fewer road-rule violations", the
  rules loosen period after period; counted violations fall while blocked time
  and late deliveries rise by about half.
- **Operating modes**: CENTRALIZED wins except at very high demand; with a
  mid-period rush, switching beats both fixed modes. Switching without waiting
  for the road to clear breaks the safety invariant.
- **LLM council**: Claude's proposals all pass the check and the constitution
  and pick the same best amendment as the rule-based search, but it mispredicts
  company B's vote and never proposes the loosening amendment that everyone
  accepts. `examples/llm-distributed/` is a real run you can replay without an
  API key. On 2026-10-19 the starting rules gained two AMEND steps
  (`TOWN_STEWARD -> COUNCIL : violation_report`, `COUNCIL -> TOWN_STEWARD :
  rule_update`) and their interface entries. The same lines were inserted,
  mechanically, into every recorded proposal in `examples/llm-distributed/`
  and `examples/llm-replay/`, because the constitution gate compares a
  proposal with the current rules line by line; nothing else in the proposals
  changed, and the replays print the same results as before. Each file
  records this in its `edits` field.

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
proposal is vetoed although it beats every adoptable one (agreement); changes
the regulation; changes the contract or an operating-mode threshold while
improving the KPI and keeping the invariants (a contract proposal that does not
improve the KPI reaches the human only if the LLM asks); improves the main KPI
while breaking a KPI condition or worsening a watched goal (KPI conflict);
improves on fewer than 4 of 5 seeds (uncertain); or when the LLM itself says a
human should decide (for proposals that keep the invariants). A new KPI is
confirmed with the human when it can be read in more than one way, relies on a
proxy, or undercuts earlier decisions. Every decision goes to `decisions.md` and
`gsn.yaml` (diagram in `gsn.md`) in `runs/<name>/`, and is passed to the LLM in
later periods. `--lang en` makes the text shown to the human, and the LLM's
writing, English (KPIs in `kpi.en.md`, headings `## From period k`).

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

The following records come from real runs with Claude Opus 5.5 on 2026-10-29/30.
The answers to the decision requests (`answers.yaml`) were written by Claude
playing the human operator when the records were made.

| Record | Contents |
|---|---|
| `examples/steward-d-en/` | six periods with `kpi.en.md` (English); about 60,600 input / 52,200 output tokens |
| `examples/steward-d/` | the same in Japanese (`kpi.md`); about 61,600 / 52,800 tokens |
| `examples/steward-goodhart/en/` | a proxy KPI ("reduce road-rule violations"), one period, in English |
| `examples/steward-goodhart/proxy/`, `guarded/` | the same proxy KPI in Japanese, answered two ways |

- Period by period: the vetoed `block_k 3 → 2` is adopted by the human against
  the LLM's recommendation; an uncertain tightening is kept back; nothing is
  proposed or adopted in the busy season; after the KPI changes to fairness,
  the LLM flags "switch off sanctions" as a Goodhart pattern and the human
  adopts `hold_limit 60s → 70s`; a further step that widens the spread of
  on-time rates is kept back. Both languages end with the same outcomes.
- In English the LLM also proposed a regulation (raise the constitution's
  floor on on-time rates), which the Japanese run did not; in Japanese the
  period-1 recommendation changed when the same request was sent again.
- In period 2 the LLM did not know the quorum (the decision-request call is not
  given the constitution) and listed that as unknown.
- With the proxy KPI, the LLM proposed no loosening of the rules, and in English
  did not offer a count-only measure at all.

On 2026-10-29 the rules file gained contract-layer tags on the `DELIVERY_SLA`
deadlines and guarantees, and two comments changed to match; the same lines were
inserted, mechanically, into every recorded proposal of `examples/llm-distributed/`
and `examples/llm-replay/` (see `edits`). The replays print the same results.
The second hand-written proposal in `examples/llm-replay/` is still stopped at
the constitution gate, now because its pickup deadline no longer matches its
guarantee line (before, because it changed a line that was not tagged).

## License

Apache License 2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)).
`town/contract_engine.py` is copied from
[cadl-raspimouse-simulator](https://github.com/ertlnagoya/cadl-raspimouse-simulator)
(commit 3fdf7d3).
