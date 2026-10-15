# CADL Robot Town — rules that a community amends itself (Course D)

English | [日本語](README.ja.md)

Hands-on page: [Course D — Robot Town](https://www.ertl.jp/cadl-spec/docs/handson/robot-town-tutorial)

Three delivery companies (A, B, C; two robots each) share the 11-node road
network of the Raspberry Pi Mouse simulator. The road rules are written in CADL
(`cadl/robot_town.cadl`). After each simulated hour, a council proposes
amendments; a proposal becomes the next version of the rules only if it passes
four gates.

| Gate | Passes when |
|---|---|
| Check | `cadl check` and `cadl sim-ir` succeed and the IR carries every value the town needs |
| Constitution | only the numbers on `# @amend:<name>` lines change, one parameter by one step (`constitution.yaml`); after simulation, every company keeps an on-time rate of at least 0.6 |
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

Assumes Course A's setup (the CADL compiler cloned to `~/program/cadl_repo`).

```bash
cd ~/program
git clone https://github.com/ertlnagoya/cadl-robot-town
cd cadl-robot-town
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip   # the pip of macOS python3 (3.9) is too old for the next line
pip install -e ~/program/cadl_repo     # the cadl command and PyYAML
pip install anthropic                  # only for --proposer llm
```

## Commands

```bash
python -m town.period --mode DISTRIBUTED        # one period (one simulated hour)
python -m town.council --mode DISTRIBUTED       # one round of the amendment loop
python -m town.council --rounds 6               # six periods, demand from scenario.yaml
python -m town.council --objective road_violations --mode DISTRIBUTED --rounds 8 --name goodhart-d
python -m town.compare runs/goodhart-d/v1.cadl runs/goodhart-d/v5.cadl --mode DISTRIBUTED
python -m town.period --rush 1200 2400 25 --no-quiescence
python -m town.council --proposer llm --rounds 3                       # needs ANTHROPIC_API_KEY
python -m town.council --proposer replay --replay-dir examples/llm-distributed --mode DISTRIBUTED --rounds 2
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
  API key.

## License

Apache License 2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)).
`town/contract_engine.py` is copied from
[cadl-raspimouse-simulator](https://github.com/ertlnagoya/cadl-raspimouse-simulator)
(commit 3fdf7d3).
