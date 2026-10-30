"""The tool-access model (#2171, spec 2026-10-18 §4.1 to §4.3), as agent/src/access/model.ts and
resolve.ts have it: a source (where tools come from) and an item (a tool, or ``*`` for the
whole source) have an access, kept in ``ai_access``. The rules are pure; the agent's
test/fixtures/access-vectors.json pins them to the TypeScript (tests/test_access_vectors.py).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg

TOOL_ACCESS = ("off", "read", "write", "outward")
# agent/src/harness/permissions.ts RISK_TIERS, least to most.
_TIERS = ("read", "write", "outward")
# The item that stands for a whole source: its access is `on` or `off`.
SOURCE_ITEM = "*"
# `updated_by` of the rows migration 20261026T2201Z_access_copy.sql copied. A closed set:
# nothing has written it since 20261030T1020Z_access_drop_mirrors.sql dropped the mirror triggers (#4828).
MIGRATED_BY = "migration:2171"
BROWSER_SOURCE = "builtin:browser"
REGISTRY_SOURCES = ("builtin:scadbuddy", BROWSER_SOURCE)
OWN_PLUGIN_SOURCE = "builtin:own-plugin"
# A package's MCP server, ``package:<pkg>/mcp:<server>`` (agent/src/access/model.ts).
_PACKAGE_SERVER_RE = re.compile(r"package:.+/mcp:.+")


@dataclass(frozen=True)
class AccessRow:
    source: str
    item: str
    access: str
    updated_by: str


def registry_source(name: str) -> str:
    """The source of a registry tool: the browser's tools are their own."""
    return BROWSER_SOURCE if name.startswith("browser_") else "builtin:scadbuddy"


def _rank(tier: str) -> int:
    # A declared tier that is no tier ranks below every tier, as indexOf's -1 does (#3882).
    return _TIERS.index(tier) if tier in _TIERS else -1


def tool_access(declared: str, row: AccessRow | None, *, migrated_raises_only: bool) -> str:
    """A tool's effective access: the row's when it holds one, else the declared tier. With
    ``migrated_raises_only`` (a registry source), a row the migration copied counts only when
    it is ``off`` or above ``declared``: the old registry overrides could only raise. Kept for
    good: a copied tier the old code ignored must not start to lower a tool's gate (#4828)."""
    if row is None or row.access not in TOOL_ACCESS:
        return declared
    if migrated_raises_only and row.updated_by == MIGRATED_BY and row.access != "off":
        return row.access if _rank(row.access) > _rank(declared) else declared
    return row.access


def enabled_by_default(source: str) -> bool:
    """Built-in sources and package servers are on until turned off (a server is offered while
    its package is approved and enabled); remote plugins and packages off until turned on."""
    return source.startswith("builtin:") or _PACKAGE_SERVER_RE.fullmatch(source) is not None


_ITEM_PREFIXES = (
    ("skill:", "skill"),
    ("subagent:", "subagent"),
    ("builtin:", "builtin"),
    ("hook:", "hook"),
)


def item_kind_of(item: str) -> str:
    """The kind a name's prefix gives (items.ts ``itemKindOf``)."""
    for prefix, kind in _ITEM_PREFIXES:
        if item.startswith(prefix):
            return kind
    return "tool"


def holds_items(source: str) -> bool:
    """Whether ``source`` holds skills, subagents, hooks and built-ins (agent/src/access/items.ts
    ``holdsItems``): a package's own source and the own plugin. Every other source holds only
    tools, whatever their names."""
    return source == OWN_PLUGIN_SOURCE or (source.startswith("package:") and "/mcp:" not in source)


def item_kind_in(source: str, item: str) -> str:
    """An item's kind in its source (items.ts ``itemKindIn``): by prefix only where the source
    holds items, else ``tool``."""
    return item_kind_of(item) if holds_items(source) else "tool"


def source_enabled(source: str, rows: Sequence[AccessRow]) -> bool:
    """The source's ``*`` row when it is ``on`` or ``off``, else its default
    (``enabled_by_default``)."""
    for row in rows:
        if row.source == source and row.item == SOURCE_ITEM:
            if row.access == "on":
                return True
            if row.access == "off":
                return False
            break
    return enabled_by_default(source)


async def read_rows(conn: psycopg.AsyncConnection[Any], sources: Sequence[str]) -> list[AccessRow]:
    cur = await conn.execute(
        "SELECT source, item, access, updated_by FROM ai_access WHERE source = ANY(%s)",
        (list(sources),),
    )
    return [AccessRow(str(s), str(i), str(a), str(u)) for s, i, a, u in await cur.fetchall()]


# ---- the call rule (#4757, spec §4.4): agent/src/access/items.ts ``offCallItem`` ----

# The calls that name a plugin's item: the input field that names it, and the item kind.
# ``Task`` is Agent's old name.
_CALLS = {
    "Skill": ("skill", "skill"),
    "Agent": ("subagent_type", "subagent"),
    "Task": ("subagent_type", "subagent"),
}
CALL_TOOLS = frozenset(_CALLS)
# JavaScript's ``trim()`` set past the space separators (WhiteSpace and LineTerminator):
# Python's ``str.strip()`` leaves U+FEFF and strips \x1c to \x1f and U+0085, which it does not.
_JS_TRIMMED = frozenset("\t\n\v\f\r\ufeff\u2028\u2029")
# Unicode White_Space past the space separators, for ``agent_type_form``.
_WHITE_SPACE = frozenset("\t\n\v\f\r\x85\u2028\u2029")


@dataclass(frozen=True)
class CallPlugin:
    """A plugin a segment loads, as the call rule sees it (items.ts ``CallPlugin``): its name,
    its ``ai_access`` source, its items (``skill:<name>``, ``subagent:<name>``) and its
    aliases (``PackageItems.aliases``)."""

    name: str
    source: str
    items: tuple[str, ...]
    aliases: Mapping[str, Sequence[str]]


@dataclass(frozen=True)
class OffCallItem:
    """What a refused call names: the source, and its item (``*``: the whole source is off)."""

    source: str
    item: str


def _js_trim(text: str) -> str:
    def trimmed(c: str) -> bool:
        return c in _JS_TRIMMED or unicodedata.category(c) == "Zs"

    start, end = 0, len(text)
    while start < end and trimmed(text[start]):
        start += 1
    while end > start and trimmed(text[end - 1]):
        end -= 1
    return text[start:end]


def agent_type_form(name: str) -> str:
    """An agent type as Claude Code 2.1.310 compares it when none is named exactly
    (items.ts ``agentTypeForm``): NFKC, lower case, without White_Space, dashes (Pd) or
    underscores. Case is folded only in the BMP, where Python's Unicode (16 in 3.14) and
    Node's (17 in Node 24) agree; past it they differ (the U+16EA0 block), so a name with a
    letter there is ``unsure_agent_type``, as is one with a code point either has unassigned."""
    out: list[str] = []
    run: list[str] = []
    for c in unicodedata.normalize("NFKC", name):
        if ord(c) > 0xFFFF:
            out.append("".join(run).lower())
            out.append(c)
            run = []
        else:
            run.append(c)
    out.append("".join(run).lower())
    # Final sigma as items.ts has it: U+03C2 as U+03C3 (whether a sigma is final differs too).
    return "".join(
        "\u03c3" if c == "\u03c2" else c
        for c in "".join(out)
        if c != "_" and c not in _WHITE_SPACE and unicodedata.category(c) not in ("Zs", "Pd")
    )


def unsure_agent_type(name: str) -> bool:
    """Whether an agent type holds a letter past the BMP or an unassigned code point
    (items.ts ``unsureAgentType``): the call rule then reaches every subagent. Unassigned is
    this Python's Unicode (16); the agent takes the code points Node's Unicode (17) added as
    unsure too (``unicodeDoubt.ts``, checked by ``test_access_vectors.py``), so the two
    runtimes answer alike. Neither knows Claude Code's own Unicode version: a fold it does
    differently past what both are sure of is the residual, and only ever toward refusal."""
    for c in unicodedata.normalize("NFKC", name):
        category = unicodedata.category(c)
        if category == "Cn" or (ord(c) > 0xFFFF and category.startswith("L")):
            return True
    return False


def _reaches(kind: str, called: str, name: str) -> bool:
    return called == name or (
        kind == "subagent" and agent_type_form(called) == agent_type_form(name)
    )


def _called_name(kind: str, raw: str) -> str:
    """A skill name trimmed (by JavaScript's set), one leading ``/`` dropped; a subagent
    type as it is (items.ts ``calledName``)."""
    if kind == "subagent":
        return raw
    trimmed = _js_trim(raw)
    return trimmed[1:] if trimmed.startswith("/") else trimmed


def off_call_item(
    tool: str, tool_input: Any, plugins: Sequence[CallPlugin], rows: Sequence[AccessRow]
) -> OffCallItem | None:
    """The item a Skill, Agent or Task call names that is off, or None: items.ts
    ``offCallItem``, the same rule, held to it by the ``calls`` access vectors. In each
    plugin the call's name reaches every item it names (whole, or as
    ``<plugin>:<name>``), and every target of an alias it names (the key, a whole frontmatter
    name: the agent's vet refuses one longer than items.ts ``ALIAS_KEY_CAP``, so none is ever
    cut), by the whole name or the rest once a ``<plugin>:`` naming that plugin comes off. Of
    what it reaches, the first whose plugin is switched off, or whose row is ``off``, answers
    (fail closed)."""
    call = _CALLS.get(tool)
    if call is None or not isinstance(tool_input, dict):
        return None
    field, kind = call
    raw = tool_input.get(field)
    if not isinstance(raw, str):
        return None
    called = _called_name(kind, raw)
    if called == "":
        return None
    prefix = f"{kind}:"

    def is_off(source: str, item: str) -> bool:
        return any(r.source == source and r.item == item and r.access == "off" for r in rows)

    at = called.find(":")
    # An agent type the two runtimes might fold apart reaches every subagent (fail closed).
    unsure = kind == "subagent" and unsure_agent_type(called)
    for plugin in plugins:
        spellings = [called]
        if at > 0 and _reaches(kind, called[:at], plugin.name):
            spellings.append(called[at + 1 :])

        def item_named(
            name: str, plugin: CallPlugin = plugin, spellings: list[str] = spellings
        ) -> bool:
            return unsure or any(
                _reaches(kind, s, name) or _reaches(kind, s, f"{plugin.name}:{name}")
                for s in spellings
            )

        def alias_named(key: str, spellings: list[str] = spellings) -> bool:
            return any(_reaches(kind, s, key) for s in spellings)

        direct = [i for i in plugin.items if i.startswith(prefix) and item_named(i[len(prefix) :])]
        aliased = [
            t
            for key, targets in plugin.aliases.items()
            if key.startswith(prefix) and alias_named(key[len(prefix) :])
            for t in targets
            if t in plugin.items
        ]
        reached = list(dict.fromkeys([*direct, *aliased]))
        if not reached:
            continue
        if is_off(plugin.source, SOURCE_ITEM):
            return OffCallItem(plugin.source, SOURCE_ITEM)
        item = next((i for i in reached if is_off(plugin.source, i)), None)
        if item is not None:
            return OffCallItem(plugin.source, item)
    return None


_STRICTNESS = ("read", "write", "outward", "off")


def builtin_access(name: str, packages: Sequence[str], rows: Sequence[AccessRow]) -> str:
    """A package built-in's access across the packages naming it (agent resolver.ts
    ``builtin``): each package's ``builtin:<name>`` row, ``outward`` without one (or with no
    tier), the strictest winning; ``outward`` when no package names it."""
    item = f"builtin:{name}"
    each = []
    for package in packages:
        source = f"package:{package}"
        found = next((r.access for r in rows if r.source == source and r.item == item), None)
        each.append(found if found in TOOL_ACCESS else "outward")
    return max(each, key=_STRICTNESS.index) if each else "outward"


#: A classic turn's words for a tool switched off (agent harness/permissions.ts decideLive).
TOOL_OFF = "{} is disabled in Settings → Tools & plugins; the user can turn it back on there"


def off_item_reason(off: OffCallItem) -> str:
    """The one sentence a refused call's result says (items.ts ``offItemReason``)."""
    owner = (
        "ScadBuddy's own plugin"
        if off.source == OWN_PLUGIN_SOURCE
        else f"plugin package {off.source[len('package:') :]}"
    )
    what = (
        owner
        if off.item == SOURCE_ITEM
        else f'{item_kind_of(off.item)} "{off.item[off.item.find(":") + 1 :]}" of {owner}'
    )
    return (
        f"{what} is switched off in Settings → Tools & plugins; the user can turn it back on there"
    )
