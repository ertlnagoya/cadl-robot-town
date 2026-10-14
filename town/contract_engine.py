# Vendored from raspimouse-swarm-simulator cadl/runtime/engine.py
# (commit 848541f, 2026-04-28), the same Python reference runtime that
# Course A uses. Copied rather than imported because that repository's
# top-level `cadl` package shadows the `cadl` compiler (cadl-lang).
"""SoS-DSL contract runtime — engine implementation (Appendix E v0.1).

Single-file, dependency-free (stdlib only). Designed to be embedded
into the raspimouse-swarm-simulator process and driven by the
arbitrator / world simulator's event loop. The runtime is fully
synchronous: callers post events and tick the clock; transitions and
violations are emitted as side-effects.

Key design decisions
--------------------

1. **No real timers, no threads.** The runtime takes simulated time
   from the host (call ``runtime.tick(now_ms)``). This makes the
   runtime trivially deterministic and replayable.

2. **Predicate evaluation is intentionally minimal.** Monitors and
   transition guards are evaluated by a small predicate evaluator
   that supports the subset actually used by the v0.1 Appendix E
   examples: ``<``, ``<=``, ``>``, ``>=``, ``==``, ``!=``, ``AND``,
   ``OR``, ``NOT``, ``IN``, simple member access (``ROBOT.battery``),
   numeric and string literals, and a small set of reserved
   identifiers (``state``, ``now``). The host supplies a
   ``world: dict[str, Any]`` snapshot at every tick.

3. **Backward compat.** Contracts without ``lifecycle:`` / ``monitors:``
   are silently skipped — they are still valid CADL.

4. **Logging.** All emitted events are appended to a ``log`` list and,
   if a ``log_path`` was supplied, also serialized as NDJSON. The
   cadl-explorer Violation Trace View consumes this NDJSON.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    Minor = "Minor"
    Major = "Major"
    Critical = "Critical"


@dataclass
class Event:
    """A discrete event posted into the runtime's EventBus.

    ``name`` is the canonical event identifier (e.g.
    ``"DISPATCHER -> ROBOT[i] : route_assignment"``). ``payload`` is
    a free-form dict the host can attach (subject id, deadline, etc.).

    If ``target_instance_id`` is non-None, only that instance receives
    the event; otherwise the runtime broadcasts to every open instance.
    """
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    t_ms: int = 0
    target_instance_id: str | None = None


@dataclass
class LifecycleEvent:
    contract_id: str
    instance_id: str
    transition_id: str
    from_state: str
    to_state: str
    t_ms: int
    cause: str = "event"        # "event" | "deadline" | "monitor"


@dataclass
class ViolationEvent:
    contract_id: str
    instance_id: str
    norm: str
    severity: Severity
    t_ms: int
    detected_by: str            # "deadline:<transition_id>" | "monitor:<id>"


# ---------------------------------------------------------------------------
# Predicate evaluator (deliberately minimal)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r'\s*(?:'
    r'(?P<NUM>\d+(?:\.\d+)?)'
    r'|(?P<STR>"[^"]*")'
    r'|(?P<OP><=|>=|==|!=|<|>)'
    r'|(?P<KW>AND|OR|NOT|IN|in)'
    r'|(?P<LB>\[)|(?P<RB>\])|(?P<LP>\()|(?P<RP>\))'
    r'|(?P<CO>,)'
    r'|(?P<ID>[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*'
    r'(?:\[[^\]]*\])?(?:\.[A-Za-z_][A-Za-z_0-9]*)*)'
    r')'
)


def _tokenize(expr: str) -> list[tuple[str, str]]:
    pos = 0
    out: list[tuple[str, str]] = []
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            if expr[pos].isspace():
                pos += 1
                continue
            raise ValueError(f"unexpected token at {pos}: {expr[pos:]!r}")
        for name, val in m.groupdict().items():
            if val is not None:
                out.append((name, val))
                break
        pos = m.end()
    return out


_SENTINEL = object()


def _resolve(name: str, world: dict[str, Any]) -> Any:
    """Resolve a dotted/indexed identifier against the world snapshot.

    Indices like ``ROBOT[i]`` are stripped — the runtime is
    *single-instance* per evaluation, so subscripts are placeholders.

    A bare (non-dotted) identifier that cannot be resolved is treated
    as an enum-like symbolic literal and returned as a string. This
    matches CADL's convention of writing ``state == Assigned`` without
    quotes around ``Assigned``. Dotted paths (``ROBOT.battery``) keep
    their strict semantics — unresolved members return None — so a
    typo in a member access is still observable.
    """
    bare = re.sub(r'\[[^\]]*\]', '', name)
    parts = bare.split('.')
    cur: Any = world
    for part in parts:
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
            else:
                cur = _SENTINEL
                break
        else:
            cur = getattr(cur, part, _SENTINEL)
            if cur is _SENTINEL:
                break
    if cur is _SENTINEL:
        # Bare identifier -> enum-like symbolic literal.
        if len(parts) == 1:
            return parts[0]
        # Dotted path with a missing component -> None.
        return None
    return cur


def _eval_atom(tok: tuple[str, str], world: dict[str, Any]) -> Any:
    kind, val = tok
    if kind == 'NUM':
        return float(val) if '.' in val else int(val)
    if kind == 'STR':
        return val[1:-1]
    if kind == 'ID':
        if val == 'true':
            return True
        if val == 'false':
            return False
        return _resolve(val, world)
    raise ValueError(f"unexpected atom: {tok}")


def _eval_value(toks: list[tuple[str, str]], i: int, world: dict[str, Any]):
    """Parse a primary expression / list literal. Returns (value, next_i)."""
    if i >= len(toks):
        raise ValueError("unexpected end of expression")
    kind, val = toks[i]
    if kind == 'LB':
        # [a, b, c] list literal
        items: list[Any] = []
        i += 1
        while i < len(toks) and toks[i][0] != 'RB':
            v, i = _eval_value(toks, i, world)
            items.append(v)
            if i < len(toks) and toks[i][0] == 'CO':
                i += 1
        if i >= len(toks):
            raise ValueError("unterminated list literal")
        return items, i + 1
    if kind == 'LP':
        v, j = _eval_or(toks, i + 1, world)
        if j >= len(toks) or toks[j][0] != 'RP':
            raise ValueError("missing ')'")
        return v, j + 1
    # Reject function-call-like patterns: ID followed by LP. The v0.1
    # evaluator does not implement function calls (e.g. min_dist(a,b))
    # — raising here makes the surrounding evaluate_predicate() return
    # False rather than silently treat the function name as a truthy
    # symbolic literal.
    if kind == 'ID' and i + 1 < len(toks) and toks[i + 1][0] == 'LP':
        raise ValueError(f"function calls are not supported: {val}(...)")
    return _eval_atom((kind, val), world), i + 1


def _eval_comparison(toks, i, world):
    lhs, i = _eval_value(toks, i, world)
    if i < len(toks):
        kind, val = toks[i]
        if kind == 'OP':
            rhs, j = _eval_value(toks, i + 1, world)
            ops = {
                '==': lambda a, b: a == b,
                '!=': lambda a, b: a != b,
                '<':  lambda a, b: a < b,
                '<=': lambda a, b: a <= b,
                '>':  lambda a, b: a > b,
                '>=': lambda a, b: a >= b,
            }
            return ops[val](lhs, rhs), j
        if kind == 'KW' and val.upper() == 'IN':
            rhs, j = _eval_value(toks, i + 1, world)
            try:
                return (lhs in rhs), j
            except TypeError:
                return False, j
    return lhs, i


def _eval_unary(toks, i, world):
    if i < len(toks) and toks[i][0] == 'KW' and toks[i][1] == 'NOT':
        v, j = _eval_unary(toks, i + 1, world)
        return (not bool(v)), j
    return _eval_comparison(toks, i, world)


def _eval_and(toks, i, world):
    lhs, i = _eval_unary(toks, i, world)
    while i < len(toks) and toks[i][0] == 'KW' and toks[i][1] == 'AND':
        rhs, i = _eval_unary(toks, i + 1, world)
        lhs = bool(lhs) and bool(rhs)
    return lhs, i


def _eval_or(toks, i, world):
    lhs, i = _eval_and(toks, i, world)
    while i < len(toks) and toks[i][0] == 'KW' and toks[i][1] == 'OR':
        rhs, i = _eval_and(toks, i + 1, world)
        lhs = bool(lhs) or bool(rhs)
    return lhs, i


def evaluate_predicate(expr: str, world: dict[str, Any]) -> bool:
    """Evaluate a predicate against a world snapshot. Returns False on
    parse / resolution failure (the runtime is intentionally lenient
    so that monitors do not crash the host)."""
    if not expr:
        return False
    try:
        toks = _tokenize(expr)
        if not toks:
            return False
        value, i = _eval_or(toks, 0, world)
        return bool(value)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# IR loader
# ---------------------------------------------------------------------------

def load_ir(source: str | Path | dict) -> dict:
    """Load a Sim-IR document from a path, JSON string, or dict."""
    if isinstance(source, dict):
        return source
    if isinstance(source, Path) or (isinstance(source, str)
                                    and (Path(source).exists()
                                         or source.endswith('.json'))):
        return json.loads(Path(source).read_text(encoding='utf-8'))
    return json.loads(source)


# ---------------------------------------------------------------------------
# EventBus / TimerService / MonitorEngine / StateMachineEngine
# ---------------------------------------------------------------------------

class EventBus:
    """In-process event bus. Synchronous; subscribers are invoked in
    FIFO subscription order."""

    def __init__(self) -> None:
        self._subs: list[Callable[[Event], None]] = []

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subs.append(fn)

    def post(self, event: Event) -> None:
        for fn in list(self._subs):
            fn(event)


@dataclass
class _Timer:
    deadline_ms: int
    instance_id: str
    contract_id: str
    transition_id: str
    target_state: str
    severity: Severity
    norm: str


class TimerService:
    """Deadline timer registry. Driven by ``tick(now_ms)``; expired
    timers are returned and removed."""

    def __init__(self) -> None:
        self._timers: list[_Timer] = []

    def arm(self, t: _Timer) -> None:
        self._timers.append(t)

    def cancel(self, instance_id: str, transition_id: str) -> None:
        self._timers = [
            t for t in self._timers
            if not (t.instance_id == instance_id
                    and t.transition_id == transition_id)
        ]

    def expired(self, now_ms: int) -> list[_Timer]:
        due = [t for t in self._timers if t.deadline_ms <= now_ms]
        if due:
            ids = {(t.instance_id, t.transition_id) for t in due}
            self._timers = [
                t for t in self._timers
                if (t.instance_id, t.transition_id) not in ids
            ]
        return due


# ---------------------------------------------------------------------------
# Contract instance
# ---------------------------------------------------------------------------

@dataclass
class ContractInstance:
    contract_id: str
    instance_id: str
    state: str
    spec: dict                 # the contract dict from IR
    closed: bool = False
    # Per-instance world fields layered on top of the global world
    # snapshot when this instance's predicates are evaluated. Lets
    # multi-robot simulations express e.g. one ROBOT.battery per
    # instance without inventing a new contract spec.
    instance_world: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class StateMachineEngine:
    """Drives lifecycle transitions for a single contract instance."""

    def __init__(self, runtime: "ContractRuntime") -> None:
        self.runtime = runtime

    def fire_transition(self, inst: ContractInstance, transition: dict,
                        cause: str, t_ms: int) -> bool:
        """Fire a transition if its `from` includes the current state.
        Returns True if the state was changed."""
        from_states = transition.get("from_states") or []
        if inst.state not in from_states:
            return False

        when = transition.get("when")
        if when:
            world = self.runtime.world_snapshot(inst)
            if not evaluate_predicate(when, world):
                return False

        old = inst.state
        new = transition["to_state"]
        inst.state = new

        # Cancel any deadline timer pending on this transition.
        self.runtime.timers.cancel(inst.instance_id, transition.get("id", ""))

        ev = LifecycleEvent(
            contract_id=inst.contract_id,
            instance_id=inst.instance_id,
            transition_id=transition.get("id", ""),
            from_state=old,
            to_state=new,
            t_ms=t_ms,
            cause=cause,
        )
        self.runtime._record_lifecycle(ev)

        # Terminal state -> close instance
        lc = inst.spec.get("lifecycle") or {}
        if new in (lc.get("terminal") or []):
            inst.closed = True
        else:
            # Arm any new deadlines for transitions out of the new state.
            self.runtime._arm_deadlines_for(inst, t_ms)

        return True


class MonitorEngine:
    """Evaluates declarative monitors. Periodic monitors are ticked
    via ``tick(now_ms)``; event-driven ones via ``on_event(event)``."""

    def __init__(self, runtime: "ContractRuntime") -> None:
        self.runtime = runtime
        # last-fire time per (instance_id, monitor_id) for periodic gating
        self._last_fire: dict[tuple[str, str], int] = {}

    def tick(self, now_ms: int) -> None:
        for inst in self.runtime.instances():
            if inst.closed:
                continue
            for m in inst.spec.get("monitors") or []:
                kind = m.get("sampling_kind", "event")
                if kind != "periodic":
                    continue
                period = m.get("sampling_period_ms") or 0
                key = (inst.instance_id, m.get("id", ""))
                last = self._last_fire.get(key, -10 ** 18)
                if now_ms - last < period:
                    continue
                self._last_fire[key] = now_ms
                self._evaluate(inst, m, now_ms)

    def on_event(self, ev: Event) -> None:
        for inst in self.runtime.instances():
            if inst.closed:
                continue
            # Targeted events: skip non-matching instances.
            if (ev.target_instance_id is not None
                    and inst.instance_id != ev.target_instance_id):
                continue
            for m in inst.spec.get("monitors") or []:
                if m.get("sampling_kind", "event") != "event":
                    continue
                self._evaluate(inst, m, ev.t_ms)

    def _evaluate(self, inst: ContractInstance, m: dict, t_ms: int) -> None:
        rule = m.get("rule") or ""
        world = self.runtime.world_snapshot(inst)
        if not evaluate_predicate(rule, world):
            return
        sev = Severity(m.get("on_match_severity") or "Major")
        target_transition_id = m.get("on_match_transition")
        violation_norm = m.get("on_match_violation") or m.get("id", "")

        ve = ViolationEvent(
            contract_id=inst.contract_id,
            instance_id=inst.instance_id,
            norm=violation_norm,
            severity=sev,
            t_ms=t_ms,
            detected_by=f"monitor:{m.get('id', '')}",
        )
        self.runtime._record_violation(ve)

        if target_transition_id:
            # Find the named lifecycle transition target.
            lc = inst.spec.get("lifecycle") or {}
            for tr in lc.get("transitions") or []:
                if tr.get("id") == target_transition_id:
                    self.runtime.sm.fire_transition(
                        inst, tr, cause="monitor", t_ms=t_ms,
                    )
                    return
            # Or interpret as a target state shortcut.
            if target_transition_id in (lc.get("states") or []):
                self.runtime._fire_state_jump(
                    inst, target_transition_id, cause="monitor", t_ms=t_ms,
                )


class ContractRuntime:
    """Top-level runtime. One instance manages many ContractInstances
    across many contracts loaded from a single IR document."""

    def __init__(
        self,
        ir: dict | str | Path,
        log_path: str | Path | None = None,
    ) -> None:
        self.ir: dict = load_ir(ir)
        self.bus = EventBus()
        self.timers = TimerService()
        self.sm = StateMachineEngine(self)
        self.mon = MonitorEngine(self)
        self._instances: dict[str, ContractInstance] = {}
        self._world: dict[str, Any] = {}
        self.log: list[dict] = []
        self.log_path: Path | None = (
            Path(log_path) if log_path else None
        )
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text("", encoding="utf-8")

        self.bus.subscribe(self._on_event)

    # --- world snapshot ----------------------------------------------------

    def update_world(self, snapshot: dict[str, Any]) -> None:
        """Merge ``snapshot`` into the world model used for predicate
        evaluation (monitors and ``when:`` guards)."""
        self._world.update(snapshot)

    def world_snapshot(self, inst: ContractInstance) -> dict[str, Any]:
        snap = dict(self._world)
        # Per-instance overrides win over the global world (deep merge
        # at one level, sufficient for the v0.1 examples that use
        # ROBOT.battery / ROBOT.position style two-level paths).
        for k, v in (inst.instance_world or {}).items():
            if isinstance(v, dict) and isinstance(snap.get(k), dict):
                merged = dict(snap[k])
                merged.update(v)
                snap[k] = merged
            else:
                snap[k] = v
        snap["state"] = inst.state
        snap["contract_id"] = inst.contract_id
        snap["instance_id"] = inst.instance_id
        return snap

    def update_instance_world(self, instance_id: str,
                              snapshot: dict[str, Any]) -> None:
        """Merge per-instance world fields. The global world is
        unchanged; only this instance's predicates see the override."""
        inst = self._instances.get(instance_id)
        if inst is None:
            return
        for k, v in (snapshot or {}).items():
            if isinstance(v, dict) and isinstance(
                inst.instance_world.get(k), dict
            ):
                inst.instance_world[k] = {
                    **inst.instance_world[k], **v,
                }
            else:
                inst.instance_world[k] = v

    # --- instance management ----------------------------------------------

    def instances(self) -> Iterable[ContractInstance]:
        return list(self._instances.values())

    def open_instance(self, contract_id: str, instance_id: str,
                      t_ms: int = 0) -> ContractInstance:
        spec = self._find_contract(contract_id)
        if spec is None:
            raise ValueError(f"unknown contract: {contract_id}")
        lc = spec.get("lifecycle") or {}
        initial = lc.get("initial")
        if not initial:
            raise ValueError(
                f"contract {contract_id} has no lifecycle.initial"
            )
        inst = ContractInstance(
            contract_id=contract_id,
            instance_id=instance_id,
            state=initial,
            spec=spec,
        )
        self._instances[instance_id] = inst
        self._record_lifecycle(LifecycleEvent(
            contract_id=contract_id,
            instance_id=instance_id,
            transition_id="<open>",
            from_state="",
            to_state=initial,
            t_ms=t_ms,
            cause="open",
        ))
        self._arm_deadlines_for(inst, t_ms)
        return inst

    def _find_contract(self, contract_id: str) -> dict | None:
        for c in self.ir.get("institution", {}).get("contracts", []) or []:
            if c.get("id") == contract_id:
                return c
        return None

    # --- engine plumbing ---------------------------------------------------

    def _arm_deadlines_for(self, inst: ContractInstance, now_ms: int) -> None:
        lc = inst.spec.get("lifecycle") or {}
        for tr in lc.get("transitions") or []:
            if inst.state not in (tr.get("from_states") or []):
                continue
            d = tr.get("deadline_ms")
            if d is None:
                continue
            on_viol_target = tr.get("on_violation_transition")
            sev = tr.get("on_violation_severity") or "Major"
            if not on_viol_target:
                continue
            self.timers.arm(_Timer(
                deadline_ms=now_ms + int(d),
                instance_id=inst.instance_id,
                contract_id=inst.contract_id,
                transition_id=tr.get("id", ""),
                target_state=on_viol_target,
                severity=Severity(sev),
                norm=tr.get("id", ""),
            ))

    def _on_event(self, ev: Event) -> None:
        # Lifecycle transitions whose `on:` matches the event name.
        for inst in list(self._instances.values()):
            if inst.closed:
                continue
            # Targeted events skip non-matching instances. None means
            # broadcast (legacy behaviour).
            if (ev.target_instance_id is not None
                    and inst.instance_id != ev.target_instance_id):
                continue
            lc = inst.spec.get("lifecycle") or {}
            for tr in lc.get("transitions") or []:
                if (tr.get("on") or "") != ev.name:
                    continue
                self.sm.fire_transition(inst, tr, cause="event",
                                        t_ms=ev.t_ms)
        # Event-driven monitors get a chance (also respect target).
        self.mon.on_event(ev)

    def _fire_state_jump(self, inst: ContractInstance, target_state: str,
                         cause: str, t_ms: int) -> None:
        old = inst.state
        inst.state = target_state
        self._record_lifecycle(LifecycleEvent(
            contract_id=inst.contract_id,
            instance_id=inst.instance_id,
            transition_id="<jump>",
            from_state=old,
            to_state=target_state,
            t_ms=t_ms,
            cause=cause,
        ))
        lc = inst.spec.get("lifecycle") or {}
        if target_state in (lc.get("terminal") or []):
            inst.closed = True

    # --- public tick -------------------------------------------------------

    def tick(self, now_ms: int) -> None:
        for t in self.timers.expired(now_ms):
            inst = self._instances.get(t.instance_id)
            if inst is None or inst.closed:
                continue
            ve = ViolationEvent(
                contract_id=t.contract_id,
                instance_id=t.instance_id,
                norm=t.norm,
                severity=t.severity,
                t_ms=now_ms,
                detected_by=f"deadline:{t.transition_id}",
            )
            self._record_violation(ve)
            self._fire_state_jump(inst, t.target_state,
                                  cause="deadline", t_ms=now_ms)
        self.mon.tick(now_ms)

    def post_event(self, name: str, payload: dict | None = None,
                   t_ms: int = 0,
                   instance_id: str | None = None) -> None:
        """Post an event. If ``instance_id`` is None the event is
        broadcast to every open instance; otherwise only that
        instance receives it."""
        self.bus.post(Event(
            name=name,
            payload=payload or {},
            t_ms=t_ms,
            target_instance_id=instance_id,
        ))

    # --- log sink ----------------------------------------------------------

    def _record_lifecycle(self, ev: LifecycleEvent) -> None:
        rec = {
            "kind": "lifecycle",
            "contract_id": ev.contract_id,
            "instance_id": ev.instance_id,
            "transition_id": ev.transition_id,
            "from_state": ev.from_state,
            "to_state": ev.to_state,
            "t_ms": ev.t_ms,
            "cause": ev.cause,
        }
        self._append(rec)

    def _record_violation(self, ev: ViolationEvent) -> None:
        rec = {
            "kind": "violation",
            "contract_id": ev.contract_id,
            "instance_id": ev.instance_id,
            "norm": ev.norm,
            "severity": ev.severity.value,
            "t_ms": ev.t_ms,
            "detected_by": ev.detected_by,
        }
        self._append(rec)

    def _append(self, rec: dict) -> None:
        self.log.append(rec)
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
