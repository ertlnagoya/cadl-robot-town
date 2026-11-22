"""見本（examples/）をもう一度実行して、結果が変わっていないかを確かめる（CI でも動かす）。

    python -m town.check_examples            # すべての見本を runs/check-<名前>/ に実行して比べる
    python -m town.check_examples --update   # 今の結果を examples/expected/ に書き出す
    python -m town.check_examples steward-d  # 名前を挙げた見本だけ

見本ごとに次を確かめる。LLM は呼ばない（記録した出力を再生する）。

- 実行が成功する（終了コード 0）
- 改正の履歴（history.md）と GSN（gsn.yaml、steward だけ）が examples/expected/<名前>/ と同じ
- 判断の記録（decisions.jsonl）が `cadl decisions-verify` でエラーなく、最後まで書かれている。
  指摘（種類と項目）の一覧が examples/expected/<名前>/findings.txt と同じ
- LLM の依頼が記録したときと同じ（依頼の要約が違うという注意が出ない）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = ROOT / "examples" / "expected"

STEWARD = ["-m", "town.steward", "--mode", "DISTRIBUTED"]
COUNCIL = ["-m", "town.council"]


def _steward(d: str, rounds: int = 6, lang: str = "ja", kpi: str | None = None,
             answers: bool = True, extra=()) -> list[str]:
    args = STEWARD + ["--rounds", str(rounds), "--llm", "replay", "--replay-dir", f"examples/{d}"]
    if lang != "ja":
        args += ["--lang", lang]
    if kpi:
        args += ["--kpi", kpi]
    if answers:
        args += ["--answers", f"examples/{d}/answers.yaml"]
    return args + list(extra)


LLM_DECIDER = ["--decider", "llm"]
CASES: dict[str, list[str]] = {
    "council-seasons": COUNCIL + ["--mode", "DISTRIBUTED", "--rounds", "6"],
    "council-goodhart": COUNCIL + ["--objective", "road_violations", "--mode", "DISTRIBUTED",
                                   "--rounds", "8"],
    "council-llm-replay": COUNCIL + ["--proposer", "replay", "--replay-dir", "examples/llm-replay",
                                     "--mode", "DISTRIBUTED"],
    "council-llm-distributed": COUNCIL + ["--proposer", "replay", "--replay-dir",
                                          "examples/llm-distributed", "--mode", "DISTRIBUTED",
                                          "--rounds", "2"],
    "steward-d": _steward("steward-d"),
    "steward-d-en": _steward("steward-d-en", lang="en"),
    "steward-goodhart-proxy": _steward("steward-goodhart/proxy", rounds=1,
                                       kpi="examples/steward-goodhart/kpi.md"),
    "steward-goodhart-guarded": _steward("steward-goodhart/guarded", rounds=1,
                                         kpi="examples/steward-goodhart/kpi.md"),
    "steward-goodhart-en": _steward("steward-goodhart/en", rounds=1, lang="en",
                                    kpi="examples/steward-goodhart/kpi.en.md"),
    "steward-contract": _steward("steward-contract", kpi="examples/steward-contract/kpi.md",
                                 answers=False, extra=LLM_DECIDER + ["--layers", "rule,contract"]),
    "steward-contract-en": _steward("steward-contract-en", lang="en",
                                    kpi="examples/steward-contract-en/kpi.md", answers=False,
                                    extra=LLM_DECIDER + ["--layers", "rule,contract"]),
    "steward-regulation": _steward("steward-regulation", kpi="examples/steward-regulation/kpi.md",
                                   answers=False,
                                   extra=LLM_DECIDER + ["--delegate", "rule,contract,regulation"]),
    "steward-regulation-en": _steward("steward-regulation-en", lang="en",
                                      kpi="examples/steward-regulation-en/kpi.md", answers=False,
                                      extra=LLM_DECIDER + ["--delegate",
                                                           "rule,contract,regulation"]),
}
# 依頼の要約が記録と違うときの注意（town/messages.py の digest）
DIGEST_WARNINGS = ("記録したときと依頼の内容が違う", "the request differs from the recorded one")


def findings(record: Path) -> tuple[list[str], str]:
    """(指摘の一覧, エラーの説明)。指摘は「種類 [項目]」を 1 行ずつ。"""
    from cadl.decisions import verify
    r = verify(record)
    errors = "; ".join(f"{e['rule']} {e.get('entry') or ''}: {e['what']}" for e in r["errors"])
    if not r["complete"]:
        errors = (errors + "; " if errors else "") + "the record has no end record"
    lines = [f"{f['finding']} [{f['entry']}]" for f in r["findings"]]
    lines += [f"made within a delegation [{d}] {g}" for d, g in r["delegated_under"].items()]
    return lines, errors


def outputs(run: Path) -> dict[str, str]:
    out = {}
    for name in ("history.md", "gsn.yaml"):
        if (run / name).exists():
            out[name] = (run / name).read_text(encoding="utf-8")
    return out


def check(name: str, update: bool) -> list[str]:
    run_name = f"check-{name}"
    run = ROOT / "runs" / run_name
    if run.exists():
        import shutil
        shutil.rmtree(run)
    cmd = [sys.executable, "-u"] + CASES[name] + ["--name", run_name]
    import os
    env = dict(os.environ)                  # この Python の環境の cadl を使う
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
    log = p.stdout + p.stderr
    if p.returncode != 0:
        return [f"exit {p.returncode}:\n{log[-2000:]}"]
    problems = []
    if any(w in log for w in DIGEST_WARNINGS):
        problems.append("an LLM request differs from the recorded one (request digest)")
    got = outputs(run)
    found, errors = findings(run / "decisions.jsonl")
    if errors:
        problems.append(f"decisions.jsonl: {errors}")
    got["findings.txt"] = "\n".join(found) + "\n"
    exp_dir = EXPECTED / name
    if update:
        exp_dir.mkdir(parents=True, exist_ok=True)
        for f in exp_dir.iterdir():
            if f.name not in got:
                f.unlink()
        for f, text in got.items():
            (exp_dir / f).write_text(text, encoding="utf-8")
        return problems
    for f, text in got.items():
        want = exp_dir / f
        if not want.exists():
            problems.append(f"{f}: not in {want.relative_to(ROOT)}")
        elif want.read_text(encoding="utf-8") != text:
            import difflib
            diff = "".join(difflib.unified_diff(want.read_text(encoding="utf-8").splitlines(True),
                                                text.splitlines(True), str(want.relative_to(ROOT)),
                                                f"runs/{run_name}/{f}", n=1))
            problems.append(f"{f} differs:\n{diff[:3000]}")
    return problems


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("names", nargs="*", help=f"見本の名前（既定はすべて）: {', '.join(CASES)}")
    p.add_argument("--update", action="store_true", help="今の結果を examples/expected/ に書く")
    args = p.parse_args()
    unknown = [n for n in args.names if n not in CASES]
    if unknown:
        p.error(f"unknown: {', '.join(unknown)}")
    bad = 0
    for name in args.names or CASES:
        problems = check(name, args.update)
        print(("ok    " if not problems else "FAIL  ") + name, flush=True)
        for x in problems:
            print("      " + x.replace("\n", "\n      "))
        bad += bool(problems)
    print(f"{len(args.names or CASES) - bad} ok, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
