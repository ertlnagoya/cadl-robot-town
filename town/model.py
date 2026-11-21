"""記録に残す規則のバージョン（付録 K の改正の宣言つき）を書き出す。

LLM と改正のループが読み書きするのは、`cadl/robot_town.cadl` と同じ形の規則のファイル
（改正してよい値の行に `# @amend:<名前>`）。判断の記録（付録 L）に添付するバージョンの
ファイル `v<n>.cadl` には、ツールが次を書き足す。

- `sos.version` を `v<n>` に、`extensions` に `amendment: 0.1`
- 判断する者の actor：運営者（OPERATOR）と規制の担当者（REGULATOR）
- 規制の値（定足数、定時率の下限、未配達の上限）を `context.environment` に
- `amendment:`：`previous`、`effective`、層ごとの決定権（K.3 の `layers`）、
  改正してよいパラメータ（値、範囲、刻み、`at:`）

こうすると `cadl decisions-verify` が、バージョンの版と順序（L-9）、案の値の範囲（L-10）、
決定権と投票の結果（L-11、L-6）を確かめられる。
"""
from __future__ import annotations

import re

# 規則と契約のパラメータの場所（付録 K の K.3 の location）
AT = {
    "hold_limit": ["contracts.ROAD_USE.lifecycle.transitions.release.deadline"],
    "block_k": ["contracts.ROAD_USE.monitors.blocking_guard.rule"],
    "sanction_lambda": ["contracts.ROAD_USE.incentives.lambda"],
    "to_central": ["transitions.DISTRIBUTED->CENTRALIZED.condition"],
    "to_distributed": ["transitions.CENTRALIZED->DISTRIBUTED.condition"],
    "pickup_deadline": ["contracts.DELIVERY_SLA.lifecycle.transitions.pickup.deadline",
                        "contracts.DELIVERY_SLA.guarantee.0"],
    "dropoff_deadline": ["contracts.DELIVERY_SLA.lifecycle.transitions.dropoff.deadline",
                         "contracts.DELIVERY_SLA.guarantee.1"],
}
# 規制（憲法）の値：constitution.yaml の名前 → パラメータの id（context.environment の名前）
REGULATION = {
    "vote.quorum": "vote_quorum",
    "invariants.min_company_on_time_rate": "min_company_on_time_rate",
    "invariants.max_unfinished_orders": "max_unfinished_orders",
}

ACTORS = """    - id: OPERATOR
      role: "service_operator"        # 街のサービスを運営する人間
      autonomy: high
      capabilities: [decide_amendment]

    - id: REGULATOR
      role: "regulator"               # 規制（憲法の値）を決める人間
      autonomy: high
      capabilities: [decide_regulation]

"""


def param_id(key: str) -> str:
    """改正の案の変更の `parameter`：規則と契約はそのまま、規制は K.3 の id。"""
    return REGULATION.get(key, key)


def _get(const: dict, key: str):
    a, b = key.split(".")
    return const[a][b]


def _num(v) -> str:
    return f"{int(v)}" if float(v).is_integer() else f"{v:g}"


def _val(v: float, unit: str) -> str:
    return f"{_num(v)}s" if unit == "s" else _num(v)


def versioned(rules: str, version: int, previous: int | None, effective: int,
              const: dict, params: dict[str, float]) -> str:
    """規則のファイル rules から、記録に残すバージョン v<version> のファイルを作る。
    const はそのバージョンで効いている憲法、params は rules の @amend の値。"""
    out, n = re.subn(r'^(  version:\s*)"[^"]*"', rf'\1"v{version}"', rules, count=1, flags=re.M)
    if n != 1:
        raise ValueError("sos.version の行が見つからない")
    out = out.replace(f'  version: "v{version}"\n',
                      f'  version: "v{version}"\n\n  extensions:\n    - amendment: 0.1\n', 1)
    env = "".join(f"      {pid}: {_num(_get(const, key))}\n" for key, pid in REGULATION.items())
    out, n = re.subn(r"^(    environment:\n(?:      .*\n)+)", lambda m: m.group(1) + env, out,
                     count=1, flags=re.M)
    if n != 1:
        raise ValueError("context.environment が見つからない")
    i = out.find("\n  contracts:")
    if i < 0:
        raise ValueError("contracts: が見つからない")
    out = out[:i + 1] + ACTORS + out[i + 1:]

    lines = ["", "  amendment:"]
    if previous is not None:
        lines.append(f'    previous: "v{previous}"')
    lines += [f"    effective: {{ period: {effective} }}",
              "    transitional:",
              "      default: keep",
              "    layers:",
              f"      rule:       {{ decided_by: OPERATOR, procedure: vote, "
              f"quorum: {_num(const['vote']['quorum'])} }}",
              "      contract:   { decided_by: OPERATOR }",
              "      regulation: { decided_by: REGULATOR }",
              "    parameters:"]
    for name, spec in const["amendable"].items():
        unit = spec.get("unit", "")
        lines += [f"      - id: {name}",
                  f"        layer: {spec.get('layer', 'rule')}"]
        if spec.get("safety"):
            lines.append("        safety: true")
        lines += [f"        value: {_val(params[name], unit)}",
                  f"        min: {_val(spec['min'], unit)}",
                  f"        max: {_val(spec['max'], unit)}",
                  f"        step: {_val(spec['step'], unit)}",
                  "        at:"]
        lines += [f'          - "{loc}"' for loc in AT[name]]
    for key, pid in REGULATION.items():
        rng = const["regulation"][key]
        lines += [f"      - id: {pid}",
                  "        layer: regulation",
                  f"        value: {_num(_get(const, key))}",
                  f"        min: {_num(rng['min'])}",
                  f"        max: {_num(rng['max'])}",
                  f"        step: {_num(rng['step'])}",
                  f"        at: [environment.{pid}]"]
    return out.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
