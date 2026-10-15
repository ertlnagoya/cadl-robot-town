"""ロボットの街：道路網・ロボット・注文の離散時間シミュレーション（1 tick = 1 秒）。

規範（期限・監視・状態遷移）は判定しない。それは IR を読み込んだ
契約ランタイム（contract_engine.ContractRuntime）の仕事で、この
モジュールは観測を契約イベントに翻訳して渡す「Bridge」を兼ねる。

- 道路網：ラズパイマウスのシミュレータと同じ 11 ノード・17 エッジ。
- 走っているロボットは交差点（ノード）を通り抜けられる。交差点に
  止まっているロボット（積み下ろし中・道路上で待機中）がいると、その
  交差点には誰も入れない。止まれるのは 1 交差点に 1 台まで。
- 車庫は道路の外にあり、何台でも入る（入口のノードだけを使う）。

運用モード（regime）は 2 つ。CADL の sos.transitions に、切り替える条件が書かれる。
- DISTRIBUTED（分散）：各社が自分の判断で、道路上（中央の交差点）で待つ。
- CENTRALIZED（集中）：道路の管理者が待避所（BAYS）を割り当てる。待避所は道路の
  外にあり交差点をふさがないが、数が限られる。道路上で待つことは禁じられる。
  そのかわり、管理者はエッジを 1 台ずつ専有で割り当てる（cadl-raspimouse-simulator の
  C-SoS 調停器と同じ）。エッジに入るたびに許可待ち（GRANT_S 秒）がかかり、他の
  ロボットが走っているエッジには入れない。混雑するほど待ちが増える。
分散 → 集中の切替では、道路上で待っているロボットが全員いなくなるまで新しい
モードを有効にしない（切替中）。その時点で、CADL の safety_invariant を判定する。
"""
from __future__ import annotations

import heapq
import random
from collections import Counter
from dataclasses import dataclass, field

from .contract_engine import ContractRuntime, evaluate_predicate
from .policies import Rules, max_park_s, switch_delay_s, wants_to_park

NODES = list(range(11))
EDGES = [
    (0, 1, 1.0), (1, 2, 1.0), (2, 3, 2.0), (3, 4, 2.1), (4, 5, 1.3),
    (5, 6, 2.0), (6, 7, 1.2), (7, 0, 1.0), (7, 8, 2.5), (0, 8, 1.0),
    (1, 8, 1.0), (8, 9, 3.0), (6, 9, 1.0), (5, 9, 0.8), (4, 10, 1.4),
    (3, 10, 0.8), (2, 10, 1.7),
]
SECONDS_PER_UNIT = 4          # 長さ 1.0 のエッジを 4 秒で走る
HUB = 8                       # 道路上で待つなら、ここが一番便利
GARAGE = {"A": 10, "B": 9, "C": 4}
ROBOTS_PER_COMPANY = 2
ORDER_MEAN_INTERVAL_S = 75    # 各社の注文の平均間隔（既定。scenario.yaml で期ごとに変わる）
DWELL_S = 3                   # 積み下ろしにかかる秒数
REPLAN_AFTER_S = 6            # これだけ待たされたら迂回路を探す

REGIMES = ("DISTRIBUTED", "CENTRALIZED")
BAYS = (5, 2, 7)              # 集中管理のときの待避所（各 1 台。道路の外）
GRANT_S = 1                   # 集中管理のとき、エッジに入るたびにかかる許可待ち
WINDOW_S = 300                # 切替の条件を判定する間隔
SWITCH_TIMEOUT_S = 60         # 切替中、これを過ぎても残っていれば管理者が退去させる

SEVERITY_POINTS = {"Minor": 1.0, "Major": 3.0, "Critical": 5.0}

ADJ: dict[int, list[tuple[int, float]]] = {n: [] for n in NODES}
for _u, _v, _l in EDGES:
    ADJ[_u].append((_v, _l))
    ADJ[_v].append((_u, _l))


def shortest_path(src: int, dst: int, avoid: set[int] = frozenset()) -> list[int] | None:
    """src から dst までのノード列（src を含まない）。見つからなければ None。"""
    if src == dst:
        return []
    dist = {src: 0.0}
    prev: dict[int, int] = {}
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == dst:
            break
        if d > dist.get(u, float("inf")):
            continue
        for v, length in ADJ[u]:
            if v in avoid and v != dst:
                continue
            nd = d + length
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if dst not in dist:
        return None
    path = [dst]
    while path[-1] != src:
        path.append(prev[path[-1]])
    return path[::-1][1:]


def edge_seconds(u: int, v: int) -> int:
    for w, length in ADJ[u]:
        if w == v:
            return max(1, round(length * SECONDS_PER_UNIT))
    raise ValueError((u, v))


def read_rules(ir: dict) -> Rules:
    """行動に関わる規則の値を IR から読む（各社はこれを見て行動を決める）。"""
    road = _contract(ir, "ROAD_USE")
    release = next(t for t in road["lifecycle"]["transitions"] if t["id"] == "release")
    guard = next(m for m in road["monitors"] if m["id"] == "blocking_guard")
    k = int(guard["rule"].split(">=")[1].split()[0])
    return Rules(
        hold_limit_s=int(release["deadline_ms"] // 1000),
        block_k=k,
        sanction_lambda=float(road["governance"]["lambda"] or 0.0),
    )


def read_regimes(ir: dict) -> tuple[str, list[dict]]:
    """運用モードの切替（sos.transitions）を IR から読む。最初の from が初期モード。"""
    trs = ir.get("transitions") or []
    if not trs:
        raise ValueError("sos.transitions がない")
    for tr in trs:
        for name in (tr["from_regime"], tr["to_regime"]):
            if name not in REGIMES:
                raise ValueError(f"未知の運用モード: {name}")
    return trs[0]["from_regime"], trs


def _contract(ir: dict, cid: str) -> dict:
    return next(c for c in ir["institution"]["contracts"] if c["id"] == cid)


@dataclass
class Order:
    oid: str
    company: str
    t: int
    pickup: int
    dropoff: int


@dataclass
class Robot:
    rid: str
    company: str
    garage: int
    pos: int | None = None            # いるノード。車庫内・走行中は None
    in_garage: bool = True
    edge_to: int | None = None        # 走行中なら行き先のノード
    edge_left: int = 0
    path: list[int] = field(default_factory=list)
    goal: str = "idle"                # idle / to_pickup / to_dropoff / to_park / to_garage / parked
    order: Order | None = None
    dwell: int = 0
    waited: int = 0
    park_inst: str | None = None
    park_since: int = 0
    parks: int = 0
    bay: int | None = None            # 割り当てられた待避所
    edge: frozenset | None = None     # 走行中のエッジ
    must_leave_at: int | None = None  # 切替で道路上の待機をやめる時刻


class Town:
    def __init__(self, ir: dict, seed: int, period_s: int,
                 order_interval_s: float = ORDER_MEAN_INTERVAL_S,
                 mode: str = "adaptive", rush: dict | None = None,
                 quiescence: bool = True) -> None:
        """mode: adaptive（CADL の条件で切り替える）／DISTRIBUTED／CENTRALIZED（固定）。
        rush: {from_s, to_s, order_interval_s}。期の途中で注文が集中する時間帯。
        quiescence: False にすると、道路上の待機が残っていても切替をすぐ有効にする
        （安全条件の判定が何を捕まえるかを見る実験用）。"""
        self.quiescence = quiescence
        self.ir = ir
        self.rush = rush
        initial, self.regime_transitions = read_regimes(ir)
        self.adaptive = mode == "adaptive"
        self.regime = initial if self.adaptive else mode
        # safety_invariant は IR に載らないので、period.compile_rules がソースから
        # 読んで x_safety_invariants に添える（仕様の課題。README 参照）。
        self.invariants = ir.get("x_safety_invariants") or {}
        self.switching: dict | None = None
        self.switch_log: list[dict] = []
        self.regime_s = Counter()
        self.window = Counter()                # 判定窓の中の、ふさがれた秒数と注文数
        self.bays: dict[int, str] = {}
        self.edge_busy: dict[frozenset, str] = {}   # 走行中のエッジ → ロボット
        self.order_interval_s = order_interval_s
        self.rules = read_rules(ir)
        self.rng = random.Random(seed)
        self.period_s = period_s
        self.rt = ContractRuntime(ir)
        self.events = {
            (c["id"], t["id"]): t["on"]
            for c in ir["institution"]["contracts"]
            for t in (c.get("lifecycle") or {}).get("transitions") or []
        }
        self.robots = [
            Robot(f"{c}{i}", c, GARAGE[c])
            for c in GARAGE for i in range(1, ROBOTS_PER_COMPANY + 1)
        ]
        self.stopped: dict[int, str] = {}    # ノード → そこに止まっているロボット
        self.orders = self._make_orders()
        self.queue: dict[str, list[Order]] = {c: [] for c in GARAGE}
        self.order_of_inst: dict[str, Order] = {}
        self.owner: dict[str, str] = {}      # 契約インスタンス → 会社
        self.blocked_s = Counter()           # 会社ごとの、ふさがれて待った秒数

    # --- 注文 -----------------------------------------------------------------

    def _make_orders(self) -> list[Order]:
        orders = []
        for c in GARAGE:
            t, n = 0.0, 0
            while True:
                t += self.rng.expovariate(1 / self._interval_at(t))
                if t >= self.period_s - 300:   # 最後の 5 分は受け付けない
                    break
                n += 1
                p, d = self.rng.sample(NODES, 2)
                orders.append(Order(f"order-{c}-{n}", c, int(t), p, d))
        return sorted(orders, key=lambda o: (o.t, o.oid))

    def _interval_at(self, t: float) -> float:
        r = self.rush
        if r and r["from_s"] <= t < r["to_s"]:
            return r["order_interval_s"]
        return self.order_interval_s

    # --- 契約ランタイムへの翻訳（Bridge） ----------------------------------------

    def _fire(self, contract: str, transition: str, inst: str, t: int) -> None:
        self.rt.post_event(self.events[(contract, transition)], t_ms=t * 1000, instance_id=inst)

    def _state(self, inst: str) -> str:
        return self.rt._instances[inst].state

    # --- 1 tick ---------------------------------------------------------------

    def run(self) -> dict:
        pending = list(self.orders)
        for t in range(self.period_s + 1):
            while pending and pending[0].t <= t:
                o = pending.pop(0)
                self.rt.open_instance("DELIVERY_SLA", o.oid, t_ms=t * 1000)
                self.order_of_inst[o.oid] = o
                self.owner[o.oid] = o.company
                self.queue[o.company].append(o)
                self.window["orders"] += 1
            self._assign(t)
            n = len(self.robots)
            for i in range(n):                    # 毎 tick 順番を回して公平にする
                self._step(self.robots[(t + i) % n], t)
            self._observe(t)
            if t > 0:
                self.rt.tick(now_ms=t * 1000)
            self._react(t)
            self._regime_step(t)
        return self._metrics()

    def _assign(self, t: int) -> None:
        for c, q in self.queue.items():
            while q:
                free = [r for r in self.robots if r.company == c and r.order is None]
                if not free:
                    break
                o = q.pop(0)
                r = min(free, key=lambda r: (self._eta(r, o.pickup), r.rid))
                self._leave_parking(r, t)
                r.order, r.goal = o, "to_pickup"
                r.path = self._route(r, o.pickup)
                if not r.path and r.edge_to is None:   # すでに集荷地点にいる
                    r.dwell = DWELL_S

    def _eta(self, r: Robot, node: int) -> float:
        start = r.edge_to if r.edge_to is not None else (r.pos if r.pos is not None else r.garage)
        p = shortest_path(start, node)
        return float("inf") if p is None else len(p)

    def _route(self, r: Robot, node: int) -> list[int]:
        if r.in_garage:                           # 車庫を出て入口のノードに入るところから
            return [r.garage] + (shortest_path(r.garage, node) or [])
        start = r.edge_to if r.edge_to is not None else r.pos
        return shortest_path(start, node) or []

    def _leave_parking(self, r: Robot, t: int) -> None:
        if r.goal == "parked" and r.bay is None:
            self.stopped.pop(r.pos, None)
        if r.bay is not None:
            self.bays.pop(r.bay, None)
            r.bay = None
        r.must_leave_at = None
        if r.park_inst is not None:
            if self._state(r.park_inst) in ("Holding", "Overstay"):
                self._fire("ROAD_USE", "release", r.park_inst, t)
            r.park_inst = None
        if r.goal == "parked":
            r.goal = "idle"

    def _step(self, r: Robot, t: int) -> None:
        if r.edge_to is not None:                 # 走行中
            r.edge_left -= 1
            if r.edge_left <= 0:
                self.edge_busy.pop(r.edge, None)
                r.pos, r.edge_to = r.edge_to, None
                if not r.path:
                    self._arrive(r, t)
            return
        if r.dwell > 0:                           # 積み下ろし中
            r.dwell -= 1
            if r.dwell == 0:
                self._done_dwell(r, t)
            return
        if r.goal == "parked":
            limit = max_park_s(r.company, self.rules)
            if limit is not None and t - r.park_since >= limit:
                self._leave_parking(r, t)
                self._go_garage(r)
            return
        if not r.path:
            return
        nxt = r.path[0]
        if self._blocked(r, nxt):
            self._wait(r, t)
            return
        if (not r.in_garage and self.regime == "CENTRALIZED" and self.switching is None
                and self.edge_busy.get(frozenset((r.pos, nxt)), r.rid) != r.rid):
            self._wait(r, t)                      # 管理者がエッジの使用を許可しない
            return
        if r.in_garage:                           # 車庫から入口のノードへ
            r.in_garage, r.pos = False, nxt
            r.path.pop(0)
            r.waited = 0
            if not r.path:
                self._arrive(r, t)
            return
        r.edge = frozenset((r.pos, nxt))
        self.edge_busy[r.edge] = r.rid
        r.edge_to, r.edge_left = nxt, edge_seconds(r.pos, nxt)
        if self.regime == "CENTRALIZED" and self.switching is None:
            r.edge_left += GRANT_S
        r.pos = None
        r.path.pop(0)
        r.waited = 0

    def _blocked(self, r: Robot, node: int) -> bool:
        return self.stopped.get(node, r.rid) != r.rid

    def _wait(self, r: Robot, t: int) -> None:
        """行く手をふさがれた（止まっているロボット、または集中管理でのエッジの許可待ち）。"""
        r.waited += 1
        self.blocked_s[r.company] += 1
        self.window["blocked_s"] += 1
        if (r.goal == "to_park" and r.bay is None and not self._central()
                and r.pos is not None and not self._blocked(r, r.pos)):
            r.path = []                          # 待機場所がふさがれたら、その場で待つ
            self._start_parking(r, t)
            return
        if r.waited % REPLAN_AFTER_S == 0 and r.pos is not None:
            dest = r.path[-1]
            alt = shortest_path(r.pos, dest, avoid=set(self.stopped) - {dest})
            if alt:
                r.path = alt

    def _arrive(self, r: Robot, t: int) -> None:
        if r.goal in ("to_pickup", "to_dropoff"):
            r.dwell = DWELL_S
            self.stopped[r.pos] = r.rid
        elif r.goal == "to_park":
            self._start_parking(r, t)
        elif r.goal == "to_garage":
            r.pos, r.in_garage, r.goal = None, True, "idle"

    def _done_dwell(self, r: Robot, t: int) -> None:
        self.stopped.pop(r.pos, None)
        o = r.order
        if r.goal == "to_pickup":
            if self._state(o.oid) == "Assigned":
                self._fire("DELIVERY_SLA", "pickup", o.oid, t)
            r.goal, r.path = "to_dropoff", self._route(r, o.dropoff)
        else:
            if self._state(o.oid) == "PickedUp":
                self._fire("DELIVERY_SLA", "dropoff", o.oid, t)
            r.order = None
            self._idle(r, t)

    def _central(self) -> bool:
        """道路上での待機が禁じられているか（集中管理中、または集中への切替中）。"""
        return self.regime == "CENTRALIZED" or self.switching is not None

    def _idle(self, r: Robot, t: int) -> None:
        if wants_to_park(r.company, self.rules) and self._central():
            here = r.pos if r.pos is not None else r.garage
            free = [b for b in BAYS if b not in self.bays]
            if not free:
                self._go_garage(r)
                return
            r.bay = min(free, key=lambda b: (len(shortest_path(here, b) or []), b))
            self.bays[r.bay] = r.rid
            r.goal, r.path = "to_park", self._route(r, r.bay)
            if not r.path:
                self._start_parking(r, t)
        elif wants_to_park(r.company, self.rules):
            target = HUB if HUB not in self.stopped else r.pos
            r.goal, r.path = "to_park", self._route(r, target)
            if not r.path:
                self._start_parking(r, t)
        else:
            self._go_garage(r)

    def _go_garage(self, r: Robot) -> None:
        r.goal, r.path = "to_garage", self._route(r, r.garage)
        if not r.path and r.pos is not None:
            r.pos, r.in_garage, r.goal = None, True, "idle"

    def _start_parking(self, r: Robot, t: int) -> None:
        r.parks += 1
        r.park_inst = f"park-{r.rid}-{r.parks}"
        r.goal, r.park_since = "parked", t
        if r.bay is None:
            self.stopped[r.pos] = r.rid
        self.rt.open_instance("ROAD_USE", r.park_inst, t_ms=t * 1000)
        self.owner[r.park_inst] = r.company

    def _observe(self, t: int) -> None:
        """止まっているロボットごとに、後ろで待たされている台数を渡す。"""
        for r in self.robots:
            if r.park_inst is None or r.bay is not None:
                continue
            waiting = sum(
                1 for o in self.robots
                if o is not r and o.edge_to is None and o.dwell == 0
                and o.path and o.path[0] == r.pos and o.waited > 0
            )
            self.rt.update_instance_world(r.park_inst, {"ROBOT": {"blocking": waiting}})

    def _react(self, t: int) -> None:
        """契約ランタイムの判定（退去）に、道路の管理者として従わせる。"""
        for r in self.robots:
            if r.park_inst is not None and self._state(r.park_inst) == "Evicted":
                if r.bay is None:
                    self.stopped.pop(r.pos, None)
                else:
                    self.bays.pop(r.bay, None)
                    r.bay = None
                r.park_inst = None
                r.goal = "idle"
                self._go_garage(r)

    # --- 運用モードの切替 ----------------------------------------------------------

    def _on_road(self) -> list[Robot]:
        """道路上（待避所でない場所）で待機しているロボット。"""
        return [r for r in self.robots if r.goal == "parked" and r.bay is None]

    def _regime_step(self, t: int) -> None:
        self.regime_s["SWITCHING" if self.switching else self.regime] += 1
        for r in self._on_road():                     # 待機をやめる約束の時刻が来た
            if self.switching is None and r.must_leave_at is not None and t >= r.must_leave_at:
                self._leave_parking(r, t)
                self._idle(r, t)
        if self.switching is not None:
            sw = self.switching
            for r in self._on_road():
                if r.must_leave_at is not None and t >= r.must_leave_at:
                    self._leave_parking(r, t)
                    self._idle(r, t)
            if self._on_road() and t - sw["since"] >= SWITCH_TIMEOUT_S:
                for r in self._on_road():             # 管理者が退去させる
                    sw["forced"] += 1
                    self._leave_parking(r, t)
                    self._go_garage(r)
            if not self._on_road():
                self._activate(sw["to"], t, sw)
            return
        if not self.adaptive or t == 0 or t % WINDOW_S:
            return
        world = {
            "blocked_pct": 100 * self.window["blocked_s"] / (WINDOW_S * len(self.robots)),
            "orders_in_window": self.window["orders"],
        }
        self.window.clear()
        for tr in self.regime_transitions:
            if tr["from_regime"] == self.regime and evaluate_predicate(tr["condition"], world):
                sw = {"from": self.regime, "to": tr["to_regime"], "since": t, "forced": 0}
                if tr["to_regime"] == "CENTRALIZED" and not self.quiescence:
                    self._activate("CENTRALIZED", t, sw)
                    for r in self._on_road():
                        r.must_leave_at = t + switch_delay_s(r.company, self.rules)
                elif tr["to_regime"] == "CENTRALIZED":
                    self.switching = sw
                    for r in self._on_road():
                        r.must_leave_at = t + switch_delay_s(r.company, self.rules)
                    if not self._on_road():
                        self._activate("CENTRALIZED", t, sw)
                else:
                    self._activate(tr["to_regime"], t, sw)
                break

    def _activate(self, regime: str, t: int, sw: dict) -> None:
        self.regime, self.switching = regime, None
        inv = self.invariants.get(f"{sw['from']}->{regime}")
        ok = True if not inv else evaluate_predicate(inv, {"robots_on_road": len(self._on_road())})
        self.switch_log.append({"t": t, "from": sw["from"], "to": regime,
                                "took_s": t - sw["since"], "forced": sw["forced"],
                                "invariant_ok": ok})

    # --- 集計 -----------------------------------------------------------------

    def _metrics(self) -> dict:
        per = {c: Counter() for c in GARAGE}
        for inst, c in self.owner.items():
            s = self._state(inst)
            if inst.startswith("order-"):
                per[c]["orders"] += 1
                per[c]["on_time" if s == "Delivered" else "late" if s == "Late" else "unfinished"] += 1
            else:
                per[c]["parks"] += 1
        violations = Counter()
        lam = self.rules.sanction_lambda
        for rec in self.rt.log:
            if rec["kind"] != "violation":
                continue
            violations[rec["detected_by"]] += 1
            if rec["contract_id"] == "ROAD_USE":
                per[self.owner[rec["instance_id"]]]["sanction"] += lam * SEVERITY_POINTS[rec["severity"]]
        companies = {}
        for c, m in per.items():
            orders = m["orders"] or 1
            companies[c] = {
                "orders": m["orders"],
                "on_time": m["on_time"],
                "late": m["late"],
                "unfinished": m["unfinished"],
                "on_time_rate": m["on_time"] / orders,
                "parks": m["parks"],
                "blocked_s": self.blocked_s[c],
                "sanction": m["sanction"],
                "utility": m["on_time"] - m["sanction"],
            }
        total = sum(self.regime_s.values()) or 1
        return {
            "central_share": self.regime_s["CENTRALIZED"] / total,
            "switches": len(self.switch_log),
            "switch_s": sum(s["took_s"] for s in self.switch_log),
            "forced_evictions": sum(s["forced"] for s in self.switch_log),
            "invariant_breaches": sum(not s["invariant_ok"] for s in self.switch_log),
            "on_time_deliveries": sum(m["on_time"] for m in companies.values()),
            "orders": sum(m["orders"] for m in companies.values()),
            "unfinished": sum(m["unfinished"] for m in companies.values()),
            "blocked_s": sum(self.blocked_s.values()),
            "violations": dict(violations),
            "companies": companies,
        }
