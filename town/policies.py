"""各社の行動方針。規則（IR から読んだ値）に応じて振る舞いを変える。

規則は全社に公開されている（ROAD_USE の information.alpha = 1.0）ので、
各社は IR の値を読んで行動を決める。演習ではこの関数を書き換えて、
規則への反応が違う会社を作る。

- A 社（協調型）：仕事がなければ必ず車庫に戻る。
- B 社（計算型）：道路上で待つほうが次の注文に早く着けるので止まる。
  ただし期限（hold_limit）の 5 秒前には自分から離れ、違反はしない。
  1 台待たせただけで退去させられる規則（block_k == 1）なら止まらない。
- C 社（強欲型）：次の注文が来るまで止まり続ける。罰則が十分重い
  （sanction_lambda >= 1.5）ときだけ、B 社と同じ振る舞いに切り替える。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rules:
    """IR から読んだ、行動に関わる規則の値。"""
    hold_limit_s: int
    block_k: int
    sanction_lambda: float


def wants_to_park(company: str, rules: Rules) -> bool:
    """仕事がなくなったとき、道路上（中央の交差点）で待つか。"""
    if company == "A":
        return False
    if company == "B":
        return rules.block_k > 1 and rules.hold_limit_s >= 15
    if company == "C":
        if rules.sanction_lambda >= 1.5:
            return rules.block_k > 1 and rules.hold_limit_s >= 15
        return True
    raise ValueError(company)


def switch_delay_s(company: str, rules: Rules) -> int:
    """集中管理への切替が告げられてから、道路上の待機をやめるまでの秒数。
    C 社は罰則が軽いうちは渋る。"""
    if company == "C" and rules.sanction_lambda < 1.5:
        return 30
    return 0


def max_park_s(company: str, rules: Rules) -> int | None:
    """道路上で待つ最長の秒数。None なら注文が来るか退去させられるまで待つ。"""
    if company == "B" or (company == "C" and rules.sanction_lambda >= 1.5):
        return rules.hold_limit_s - 5
    if company == "C":
        return None
    return 0
