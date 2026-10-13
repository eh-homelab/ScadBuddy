"""Run grants (#2481, spec 2026-10-11-flow-grants-and-git-design.md §3): what a person
approved for one flow run, and the one evaluator that says whether a call is inside it.

A grant is a `GrantSpec`, validated here and interpreted nowhere else. `match_grant` is
the only matcher (§3.3, amendment Ruling 1): the `projects` worker serves it as the
`flow_grant_check` activity, and the agent only reads whether a recorded match is still
in force (§3.5). A match consumes the rule's counters and records the call in
`flow_grant_uses` in one transaction (`FlowStore.consume_grant`), so a retried check with
the same request id answers its first verdict and consumes once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from scadbuddy.flows.models import Run

#: The latest a grant may expire, from its approval (§3.1, Ruling 7).
MAX_EXPIRY = timedelta(days=7)
MAX_RULES = 32

HostFn = Literal["queue_print", "arrange", "tool"]
JsonScalar = str | int | float | bool | None
GrantStatus = Literal["requested", "approved", "declined"]

#: The functions each bound applies to; any other use is refused when the grant is
#: created, so a bound never silently means nothing.
_BOUNDS: dict[str, frozenset[str]] = {
    "printers": frozenset({"queue_print"}),
    "models": frozenset({"queue_print", "arrange"}),
    "library_files": frozenset({"queue_print"}),
    "max_prints": frozenset({"queue_print"}),
    "max_filament_g": frozenset({"queue_print"}),
    "max_calls": frozenset({"queue_print", "arrange", "tool"}),
    "args": frozenset({"tool"}),
}


class ArgBound(BaseModel):
    """An RFC 6901 pointer into a tool call's arguments, and the values it may hold."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str
    in_: list[JsonScalar] = Field(alias="in", min_length=1, max_length=256)


class GrantRule(BaseModel):
    """What may run without asking: calls of `fn` (and, for `tool`, of `tool`) that
    every bound set here admits. A bound left out is not checked."""

    model_config = ConfigDict(extra="forbid")

    fn: HostFn
    tool: str | None = None
    printers: list[int] | None = None
    models: list[str] | None = None
    library_files: list[int] | None = None
    max_prints: int | None = Field(default=None, ge=0)
    max_filament_g: float | None = Field(default=None, ge=0)
    max_calls: int | None = Field(default=None, ge=0)
    args: list[ArgBound] | None = None


class GrantSpec(BaseModel):
    """A run grant as a person approves it (§3.1)."""

    model_config = ConfigDict(extra="forbid")

    expires_at: AwareDatetime
    #: Whether it extends to the flow turns of the sessions the run starts (§3.4).
    sessions: bool = False
    #: Whether a call outside it goes to the starter session before the person (§4.4).
    ask_starter_session: bool = True
    rules: list[GrantRule] = Field(default_factory=list, max_length=MAX_RULES)


class Narrowing(BaseModel):
    """A script's narrowing of the run grant for one child (`agent(..., grant=)`, §4.3):
    an intersection, so it can never widen what the grant admits."""

    model_config = ConfigDict(extra="forbid")

    rules: list[GrantRule] = Field(default_factory=list, max_length=MAX_RULES)


class RuleUse(BaseModel):
    """What a rule has let through so far. Never given back (Ruling 14)."""

    prints: int = 0
    filament_g: float = 0
    calls: int = 0


class CallFacts(BaseModel):
    """A call as the bounds see it, resolved by `facts_of`. `pins` are the defaults the
    bounds resolved (`/request/printer_id`, `/request/copies` for a print), pinned into
    the arguments the call runs with (§3.3 step 6)."""

    fn: str
    tool: str | None = None
    args: dict[str, Any] = {}
    pins: dict[str, JsonScalar] = {}
    printer_id: int | None = None
    models: list[str] = []
    library_file: int | None = None
    #: Print items (copies times plates); None when they could not be counted.
    prints: int | None = 0
    filament_g: float | None = None


class Inside(BaseModel):
    """The call matched `rule_index` of grant `grant_id`: it runs with `args`, and is
    approved for `input_hash`."""

    verdict: Literal["inside"] = "inside"
    grant_id: str
    rule_index: int
    input_hash: str
    args: dict[str, Any]


class Outside(BaseModel):
    """No rule admitted the call: it waits for a decision, `timeout_s` at its hop.
    `parent_hop` says it goes to the starter session first (Part B, #2482)."""

    verdict: Literal["outside"] = "outside"
    reason: str
    timeout_s: int = 0
    parent_hop: bool = False


Verdict = Inside | Outside


class Grant(BaseModel):
    """A run's grant (`flow_grants`)."""

    id: str
    run_id: str
    status: GrantStatus
    spec: GrantSpec
    #: Per rule index (and, for a narrowing, `<flow call id>/<index>`): its `RuleUse`.
    used: dict[str, RuleUse] = {}
    requested_by: dict[str, Any]
    approved_by: str | None = None
    approved_at: datetime | None = None
    revoked_by: str | None = None
    revoked_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime

    def in_force(self, now: datetime) -> bool:
        return (
            self.status == "approved"
            and self.revoked_at is None
            and self.expires_at is not None
            and now < self.expires_at
        )


class GrantUse(BaseModel):
    """One call a grant let through (`flow_grant_uses`)."""

    request_id: str
    grant_id: str
    run_id: str
    fn: str
    tool: str | None = None
    input_hash: str
    rule_index: int
    prints: int = 0
    filament_g: float = 0
    session_id: str | None = None
    narrowed_by: str | None = None
    #: The defaults the check pinned into the call (`CallFacts.pins`).
    pins: dict[str, JsonScalar] = {}
    created_at: datetime | None = None


class Consumption(BaseModel):
    """What one match takes: the rule's counters, and the narrowing's when there is one."""

    rule_index: int
    narrowed_key: str | None = None
    use: GrantUse


# -- validation ---------------------------------------------------------------------


def _pointer(path: str) -> list[str] | None:
    """An RFC 6901 pointer's reference tokens, or None when it is not one. The empty
    pointer (the whole document) is refused: a bound names a value."""
    if not path.startswith("/"):
        return None
    tokens = path[1:].split("/")
    out: list[str] = []
    for token in tokens:
        if "~" in token.replace("~0", "").replace("~1", ""):
            return None
        out.append(token.replace("~1", "/").replace("~0", "~"))
    return out


def validate_rules(
    rules: list[GrantRule], *, tool_tiers: Mapping[str, str], where: str = "rule"
) -> list[str]:
    problems: list[str] = []
    for i, rule in enumerate(rules):
        at = f"{where} {i + 1}"
        if rule.fn == "tool":
            if not rule.tool:
                problems.append(f"{at}: a tool rule names its tool")
            elif tool_tiers.get(rule.tool) != "outward":
                problems.append(
                    f"{at}: {rule.tool} is not an outward ScadBuddy tool; reads and writes"
                    " run without asking, and a grant names only outward tools"
                )
        elif rule.tool is not None:
            problems.append(f"{at}: only a tool rule names a tool")
        for bound, fns in _BOUNDS.items():
            if getattr(rule, bound) is not None and rule.fn not in fns:
                problems.append(f"{at}: {bound} does not bound {rule.fn}")
        for arg in rule.args or []:
            if _pointer(arg.path) is None:
                problems.append(f"{at}: {arg.path!r} is not a JSON pointer")
    return problems


def validate_spec(spec: GrantSpec, *, tool_tiers: Mapping[str, str], now: datetime) -> list[str]:
    """Every problem with `spec` for a grant approved at `now`; empty when it may be.
    `tool_tiers` are the agent's tools as the run will enforce them (`raise_tiers`)."""
    problems = validate_rules(spec.rules, tool_tiers=tool_tiers)
    if spec.expires_at <= now:
        problems.append("expires_at is in the past")
    elif spec.expires_at - now > MAX_EXPIRY:
        problems.append("expires_at is more than 7 days away")
    return problems


# -- matching -----------------------------------------------------------------------


_MISSING = object()


def _at(document: Any, tokens: list[str]) -> Any:
    value = document
    for token in tokens:
        if isinstance(value, dict):
            if token not in value:
                return _MISSING
            value = value[token]
        elif isinstance(value, list):
            if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
                return _MISSING
            index = int(token)
            if index >= len(value):
                return _MISSING
            value = value[index]
        else:
            return _MISSING
    return value


def _same_json(a: Any, b: Any) -> bool:
    """JSON equality: `true` is not `1`, and `1` is `1.0`."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    return type(a) is type(b) and a == b


def admits(rule: GrantRule, facts: CallFacts, used: RuleUse) -> str | None:
    """None when `rule` admits the call given what it `used` already; else why not."""
    if rule.fn != facts.fn:
        return f"not a {rule.fn} rule"
    if rule.fn == "tool" and rule.tool != facts.tool:
        return f"not a {rule.tool} rule"
    if rule.printers is not None and (
        facts.printer_id is None or facts.printer_id not in rule.printers
    ):
        return "printer not in the grant"
    if rule.models is not None and (
        not facts.models or any(slug not in rule.models for slug in facts.models)
    ):
        return "model not in the grant"
    if rule.library_files is not None and (
        facts.library_file is None or facts.library_file not in rule.library_files
    ):
        return "library file not in the grant"
    if rule.max_prints is not None:
        if facts.prints is None:
            return "prints unknown"
        if used.prints + facts.prints > rule.max_prints:
            return "print limit reached"
    if rule.max_filament_g is not None:
        if facts.filament_g is None:
            return "filament unknown"
        if used.filament_g + facts.filament_g > rule.max_filament_g:
            return "filament limit reached"
    if rule.max_calls is not None and used.calls + 1 > rule.max_calls:
        return "call limit reached"
    for arg in rule.args or []:
        tokens = _pointer(arg.path)
        value = _MISSING if tokens is None else _at(facts.args, tokens)
        if value is _MISSING:
            return f"{arg.path} missing"
        if not any(_same_json(value, allowed) for allowed in arg.in_):
            return f"{arg.path} not in the grant"
    return None


def apply_pins(args: dict[str, Any], pins: Mapping[str, JsonScalar]) -> dict[str, Any]:
    """`args` with each pinned pointer set (objects on the way are created)."""
    out: dict[str, Any] = _copy(args)
    for path, value in pins.items():
        tokens = _pointer(path)
        if not tokens:
            continue
        target: Any = out
        for token in tokens[:-1]:
            nxt = target.get(token) if isinstance(target, dict) else None
            if not isinstance(nxt, dict):
                nxt = {}
                target[token] = nxt
            target = nxt
        target[tokens[-1]] = value
    return out


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def choose(
    grant: Grant,
    facts: CallFacts,
    *,
    narrowing: Narrowing | None,
    narrowed_by: str | None,
    now: datetime,
) -> tuple[int, int | None] | str:
    """The first rule that admits the call (with the first admitting narrowing rule, when
    there is a narrowing), or why none does. Pure: the store runs it under the grant's
    row lock, against the counters as they are then."""
    if not grant.in_force(now):
        return "no grant in force"
    reasons: list[str] = []
    narrowed: int | None = None
    if narrowing is not None:
        for j, rule in enumerate(narrowing.rules):
            key = f"{narrowed_by}/{j}"
            if admits(rule, facts, grant.used.get(key, RuleUse())) is None:
                narrowed = j
                break
        else:
            return "not in the script's narrowing"
    for i, rule in enumerate(grant.spec.rules):
        reason = admits(rule, facts, grant.used.get(str(i), RuleUse()))
        if reason is None:
            return i, narrowed
        reasons.append(reason)
    return reasons[0] if len(reasons) == 1 else "no rule admits it"


FactsOf = Callable[[str, str | None, dict[str, Any]], Awaitable[CallFacts]]
HashOf = Callable[[str, str | None, dict[str, Any]], str]


class GrantStore(Protocol):
    """What `match_grant` needs of `FlowStore`."""

    async def grant_use(self, request_id: str) -> GrantUse | None: ...

    async def get_run(self, run_id: str) -> Run | None: ...

    async def get_grant(self, run_id: str) -> Grant | None: ...

    async def consume_grant(
        self, run_id: str, request_id: str, pick: Callable[[Grant], Consumption | str]
    ) -> GrantUse | str: ...


async def match_grant(
    store: GrantStore,
    facts_of: FactsOf,
    hash_of: HashOf,
    *,
    run_id: str,
    request_id: str,
    fn: str,
    tool: str | None,
    args: dict[str, Any],
    session_id: str | None = None,
    flow_call_id: str | None = None,
    narrowing: Narrowing | None = None,
    now: datetime,
) -> Verdict:
    """Amendment §3.3: whether the call `request_id` is inside the run's grant, consuming
    its share when it is. A request id already recorded answers its first verdict."""
    if (prior := await store.grant_use(request_id)) is not None:
        if prior.run_id != run_id or prior.session_id != session_id:
            # Another run's (or another caller's) call: never this one's to repeat.
            return Outside(reason="the request id is another call's")
        return Inside(
            grant_id=prior.grant_id,
            rule_index=prior.rule_index,
            input_hash=prior.input_hash,
            args=apply_pins(args, prior.pins),
        )
    run = await store.get_run(run_id)
    grant = await store.get_grant(run_id)

    def outside(reason: str) -> Outside:
        starter_session = run is not None and isinstance(run.started_by.get("session"), str)
        ask = grant is None or grant.spec.ask_starter_session
        return Outside(
            reason=reason,
            timeout_s=run.approval_timeout_s if run is not None else 0,
            parent_hop=starter_session and ask,
        )

    if grant is None or not grant.in_force(now):
        return outside("no grant in force")
    if session_id is not None:
        if not grant.spec.sessions:
            return outside("the grant does not extend to sessions")
        child = next(
            (
                s
                for s in (run.steps if run is not None else [])
                if s.call_id == flow_call_id and s.fn in ("agent", "ask_session")
            ),
            None,
        )
        if child is None or child.session_id != session_id:
            return outside("not a flow turn of this run")
    try:
        facts = await facts_of(fn, tool, args)
    except Exception as err:  # a lookup that fails is outside: fail toward asking
        return outside(f"could not check the call ({type(err).__name__})")
    pinned = apply_pins(facts.args, facts.pins)
    input_hash = hash_of(fn, tool, pinned)

    def pick(locked: Grant) -> Consumption | str:
        chosen = choose(locked, facts, narrowing=narrowing, narrowed_by=flow_call_id, now=now)
        if isinstance(chosen, str):
            return chosen
        rule_index, narrowed = chosen
        return Consumption(
            rule_index=rule_index,
            narrowed_key=None if narrowed is None else f"{flow_call_id}/{narrowed}",
            use=GrantUse(
                request_id=request_id,
                grant_id=locked.id,
                run_id=run_id,
                fn=fn,
                tool=tool,
                input_hash=input_hash,
                rule_index=rule_index,
                prints=facts.prints or 0,
                filament_g=facts.filament_g or 0,
                session_id=session_id,
                narrowed_by=flow_call_id if narrowing is not None else None,
                pins=facts.pins,
            ),
        )

    taken = await store.consume_grant(run_id, request_id, pick)
    if isinstance(taken, str):
        return outside(taken)
    return Inside(
        grant_id=taken.grant_id,
        rule_index=taken.rule_index,
        input_hash=taken.input_hash,
        args=apply_pins(args, taken.pins),
    )
