"""各社の行動方針。規則（IR から読んだ値）に応じて振る舞いを変える。

規則は全社に公開されている（ROAD_USE の information.alpha = 1.0）ので、
各社は IR の値を読んで行動を決める。演習ではこの関数を書き換えて、
規則への反応が違う会社を作る。

- A 社（協調型）：仕事がなければ必ず車庫に戻る。罰点の倍率には左右されない。
- B 社（計算型）：道路上で待つほうが次の注文に早く着けるので止まる。
  ただし期限（hold_limit）の 5 秒前には自分から離れ、違反はしない。
  1 台待たせただけで退去させられる規則（block_k == 1）なら止まらない。
  罰点がない規則（sanction_lambda == 0）では違反しても損をしないので、
  C 社と同じく、次の注文が来るまで止まり続ける。
- C 社（強欲型）：倍率 0.5 以下では、次の注文が来るまで止まり続ける。
  倍率が上限の 1.0 になると、止まりはするが、期限を 15 秒過ぎたら離れる
  （違反は 1 回で済ませ、退去させられるまでは居座らない）。

CADL の検査は、罰点の倍率（incentive の lambda）を 0〜1 に限る。改正で動かせるのは
0.5 刻みなので、倍率は 0、0.5、1.0 のどれかになる。
"""
from __future__ import annotations

from dataclasses import dataclass

OVERSTAY_S = 15        # C 社が倍率 1.0 のとき、期限を過ぎてから離れるまでの秒数


@dataclass(frozen=True)
class Rules:
    """IR から読んだ、行動に関わる規則の値。"""
    hold_limit_s: int
    block_k: int
    sanction_lambda: float


def _greedy(company: str, rules: Rules) -> bool:
    """罰点を気にせず、注文が来るまで止まり続ける段階か。"""
    if company == "B":
        return rules.sanction_lambda == 0
    if company == "C":
        return rules.sanction_lambda <= 0.5
    return False


def _careful(company: str, rules: Rules) -> bool:
    """違反しないように期限の前に離れる段階か（B 社の通常の振る舞い）。"""
    if company == "B":
        return rules.sanction_lambda > 0
    return False


def wants_to_park(company: str, rules: Rules) -> bool:
    """仕事がなくなったとき、道路上（中央の交差点）で待つか。"""
    if company == "A":
        return False
    if company not in ("B", "C"):
        raise ValueError(company)
    if _careful(company, rules):
        return rules.block_k > 1 and rules.hold_limit_s >= 15
    return True


def switch_delay_s(company: str, rules: Rules) -> int:
    """集中管理への切替が告げられてから、道路上の待機をやめるまでの秒数。
    罰点を気にしない会社ほど渋る。"""
    if _greedy(company, rules):
        return 30
    if company == "C" and not _careful(company, rules):
        return 15
    return 0


def max_park_s(company: str, rules: Rules) -> int | None:
    """道路上で待つ最長の秒数。None なら注文が来るか退去させられるまで待つ。"""
    if _careful(company, rules):
        return rules.hold_limit_s - 5
    if _greedy(company, rules):
        return None
    if company == "C":
        return rules.hold_limit_s + OVERSTAY_S
    return 0
