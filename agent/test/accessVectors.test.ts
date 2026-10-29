import { readFileSync, writeFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  type AccessRow,
  MIGRATED_BY,
  registrySource,
  type StoredOverrides,
  TOOL_ACCESS,
} from "../src/access/model.js";
import {
  ALIAS_KEY_CAP,
  boundedAliases,
  type CallPlugin,
  type ItemKind,
  itemKindIn,
  offCallItem,
  offItemReason,
} from "../src/access/items.js";
import { UNICODE_DOUBT } from "../src/access/unicodeDoubt.js";
import { registryOverrides, sourceEnabled } from "../src/access/resolve.js";
import { AccessResolver } from "../src/access/resolver.js";
import type { AccessReader } from "../src/access/store.js";
import type { RiskTier } from "../src/harness/permissions.js";

// The tool-access rules (#2171, spec 2026-10-18 §4.1–§4.3). The vectors are the one source of
// truth for them: agent-durable's tests open this file and apply the same rules in Python.
// Regenerate: UPDATE_ACCESS_VECTORS=1 pnpm vitest run test/accessVectors.test.ts
const FILE = new URL("./fixtures/access-vectors.json", import.meta.url);

type VectorTool = {
  name: string;
  risk: RiskTier | "?";
  hitl: "approval" | "answer" | null;
};
type RegistryVector = {
  name: string;
  durable_only?: true;
  tools: VectorTool[];
  rows: AccessRow[];
  expect: StoredOverrides;
};
type SourceVector = {
  name: string;
  source: string;
  rows: AccessRow[];
  expect: boolean;
};
type ItemKindVector = { source: string; item: string; expect: ItemKind };
/** One Skill, Agent or Task call (#4757): the item it names that is off, or null. */
type CallVector = {
  name: string;
  tool: string;
  input: unknown;
  plugins: CallPlugin[];
  rows: AccessRow[];
  expect: { source: string; item: string; reason: string } | null;
};
type Vectors = {
  tool_access: string[];
  item_kinds: ItemKindVector[];
  migrated_by: string;
  registry_source: Record<string, string>;
  registry: RegistryVector[];
  sources: SourceVector[];
  calls: CallVector[];
  /** Node's own unassigned (`\\p{Cn}`) code point ranges and `UNICODE_DOUBT` (Task 6 re-review R6): agent-durable's `Cn` must be exactly their union. */
  unicode: { node: string; cn: [number, number][]; doubt: [number, number][] };
};

const S = "builtin:scadbuddy";
const B = "builtin:browser";
const BY = "browser:browser";

const GET_MODEL: VectorTool = { name: "get_model", risk: "read", hitl: null };
const SEND: VectorTool = {
  name: "send_to_bambuddy",
  risk: "outward",
  hitl: "approval",
};
const CLICK: VectorTool = { name: "browser_click", risk: "write", hitl: null };
const TOOLS = [GET_MODEL, SEND, CLICK];

const row = (
  source: string,
  item: string,
  access: string,
  updated_by = BY,
): AccessRow => ({ source, item, access, updated_by });
const none: StoredOverrides = { tool_tiers: {}, disabled_tools: [] };

const PA = "package:pa";
const PB = "package:pb";
const OWN = "builtin:own-plugin";
/**
 * Two packages and the own plugin, each with the items it has. `pa`'s `agents/review/deep.md`
 * declares `name: deep-review`; its skills `sweep` and `dust` both declare `name: tidy`.
 */
const PLUGINS: CallPlugin[] = [
  {
    name: "pa",
    source: PA,
    items: [
      "skill:jot",
      "skill:wave",
      "skill:sweep",
      "skill:dust",
      "subagent:reviewer",
      "subagent:review:deep",
    ],
    aliases: {
      "subagent:deep-review": ["subagent:review:deep"],
      "skill:tidy": ["skill:sweep", "skill:dust"],
    },
  },
  {
    name: "pb",
    source: PB,
    items: ["skill:jot", "skill:dust", "subagent:reviewer"],
    aliases: {},
  },
  {
    name: "scadbuddy",
    source: OWN,
    items: ["skill:print", "subagent:print-analyst"],
    aliases: {},
  },
];
/** A frontmatter name that fits the 128-character cap, but not once called as `pa:<name>`. */
const LONG = `long-${"x".repeat(122)}`;
/** A frontmatter name over the cap: a stored review keeps it cut, the loaded pin whole. */
const OVER = "deep-".repeat(40);
/** `pa` as the loaded pin gives it, with the uncut alias keys (vet.ts `PackageItems.declared`). */
const PA_LONG: CallPlugin = {
  name: "pa",
  source: PA,
  items: [
    "skill:jot",
    "subagent:reviewer",
    "subagent:re-viewer",
    "subagent:review:deep",
    "subagent:long",
  ],
  aliases: {
    [`subagent:${LONG}`]: ["subagent:long"],
    [`skill:${LONG}`]: ["skill:jot"],
    [`subagent:${OVER}`]: ["subagent:review:deep"],
  },
};
/** A frontmatter name over the alias key bound (items.ts `ALIAS_KEY_CAP`): listed cut to it. */
const HUGE = "h".repeat(1500);
const PA_HUGE: CallPlugin = {
  name: "pa",
  source: PA,
  items: ["subagent:huge"],
  aliases: boundedAliases([[`subagent:${HUGE}`, ["subagent:huge"]]]),
};
const off = (source: string, item: string): CallVector["expect"] => ({
  source,
  item,
  reason: offItemReason({ source, item }),
});

/** Every unassigned code point, as Node's `\\p{Cn}` has it, as inclusive ranges (surrogates are Cs). */
function cnRanges(): [number, number][] {
  const out: [number, number][] = [];
  let start = -1;
  for (let cp = 0; cp <= 0x110000; cp++) {
    const cn =
      cp <= 0x10ffff &&
      (cp < 0xd800 || cp > 0xdfff) &&
      /\p{Cn}/u.test(String.fromCodePoint(cp));
    if (cn && start < 0) start = cp;
    if (!cn && start >= 0) {
      out.push([start, cp - 1]);
      start = -1;
    }
  }
  return out;
}

function build(): Vectors {
  return {
    tool_access: [...TOOL_ACCESS],
    // #4757 review I1: only a package's own source and the own plugin hold named items.
    item_kinds: [
      { source: "package:pk", item: "skill:a", expect: "skill" },
      { source: "package:pk", item: "subagent:a", expect: "subagent" },
      {
        source: "package:pk",
        item: "hook:Stop:0123456789abcdef",
        expect: "hook",
      },
      { source: "package:pk", item: "builtin:Bash", expect: "builtin" },
      { source: "package:pk", item: "search", expect: "tool" },
      { source: "builtin:own-plugin", item: "skill:print", expect: "skill" },
      { source: "remote:hs", item: "skill:x", expect: "tool" },
      { source: "package:pk/mcp:srv", item: "skill:x", expect: "tool" },
      { source: "builtin:scadbuddy", item: "builtin:x", expect: "tool" },
      {
        source: "builtin:http",
        item: "hook:Stop:0123456789abcdef",
        expect: "tool",
      },
    ],
    migrated_by: MIGRATED_BY,
    registry_source: {
      get_model: S,
      send_to_bambuddy: S,
      get_printer_camera: S,
      order_accept: S,
      browser_click: B,
      browser_navigate: B,
    },
    registry: [
      {
        name: "no rows: every tool at its own tier",
        tools: TOOLS,
        rows: [],
        expect: none,
      },
      {
        name: "a raise of a read tool",
        tools: TOOLS,
        rows: [row(S, "get_model", "outward")],
        expect: { tool_tiers: { get_model: "outward" }, disabled_tools: [] },
      },
      {
        name: "a lowering of an outward tool",
        tools: TOOLS,
        rows: [row(S, "send_to_bambuddy", "write")],
        expect: {
          tool_tiers: { send_to_bambuddy: "write" },
          disabled_tools: [],
        },
      },
      {
        name: "off disables",
        tools: TOOLS,
        rows: [
          row(S, "send_to_bambuddy", "off"),
          row(B, "browser_click", "off"),
          row(S, "get_model", "off"),
        ],
        expect: {
          tool_tiers: {},
          disabled_tools: ["browser_click", "get_model", "send_to_bambuddy"],
        },
      },
      {
        name: "a tool's own tier is no override",
        tools: TOOLS,
        rows: [row(S, "get_model", "read"), row(B, "browser_click", "write")],
        expect: none,
      },
      {
        name: "a row naming no tool is ignored",
        tools: TOOLS,
        rows: [row(S, "no_such_tool", "off"), row(S, "*", "off")],
        expect: none,
      },
      {
        name: "an unknown access value is ignored",
        tools: TOOLS,
        rows: [row(S, "get_model", "none"), row(S, "send_to_bambuddy", "on")],
        expect: none,
      },
      {
        name: "a migrated row that lowers is ignored",
        tools: TOOLS,
        rows: [row(S, "send_to_bambuddy", "write", MIGRATED_BY)],
        expect: none,
      },
      {
        name: "a migrated row that raises counts",
        tools: TOOLS,
        rows: [row(S, "get_model", "write", MIGRATED_BY)],
        expect: { tool_tiers: { get_model: "write" }, disabled_tools: [] },
      },
      {
        name: "a migrated off counts",
        tools: TOOLS,
        rows: [row(S, "send_to_bambuddy", "off", MIGRATED_BY)],
        expect: { tool_tiers: {}, disabled_tools: ["send_to_bambuddy"] },
      },
      {
        name: "browser tools come from builtin:browser",
        tools: TOOLS,
        rows: [
          row(S, "browser_click", "off"),
          row(B, "browser_click", "outward"),
          row(B, "get_model", "off"),
        ],
        expect: {
          tool_tiers: { browser_click: "outward" },
          disabled_tools: [],
        },
      },
      {
        name: "a row of another source is ignored",
        tools: TOOLS,
        rows: [
          row("remote:hs", "get_model", "off"),
          row("builtin:http", "send_to_bambuddy", "read"),
        ],
        expect: none,
      },
      {
        name: "an answer tool is in no source",
        tools: [...TOOLS, { name: "ask_user", risk: "read", hitl: "answer" }],
        rows: [row(S, "ask_user", "off")],
        expect: none,
      },
      {
        // An own key in both languages: a JS object literal would take it as its prototype.
        name: "a tool named __proto__ is an own key",
        tools: [{ name: "__proto__", risk: "read", hitl: null }],
        rows: [row(S, "__proto__", "write")],
        expect: {
          tool_tiers: JSON.parse('{"__proto__":"write"}') as Record<
            string,
            RiskTier
          >,
          disabled_tools: [],
        },
      },
      {
        name: "durable_only: a migrated raise of a tool whose tier is no tier counts",
        durable_only: true,
        tools: [{ name: "t", risk: "?", hitl: null }],
        rows: [row(S, "t", "outward", MIGRATED_BY)],
        expect: { tool_tiers: { t: "outward" }, disabled_tools: [] },
      },
      {
        // #4757: the browser's `*` switch, folded into every gate's overrides.
        name: "browser switched off disables the browser tools",
        tools: [
          ...TOOLS,
          { name: "browser_navigate", risk: "read", hitl: null },
        ],
        rows: [
          row(B, "*", "off"),
          row(B, "browser_click", "outward"),
          row(S, "get_model", "write"),
        ],
        expect: {
          tool_tiers: { get_model: "write" },
          disabled_tools: ["browser_click", "browser_navigate"],
        },
      },
      {
        name: "browser switched on keeps their rows",
        tools: TOOLS,
        rows: [row(B, "*", "on"), row(B, "browser_click", "outward")],
        expect: {
          tool_tiers: { browser_click: "outward" },
          disabled_tools: [],
        },
      },
    ],
    sources: [
      {
        name: "http is on by default",
        source: "builtin:http",
        rows: [],
        expect: true,
      },
      {
        name: "http off",
        source: "builtin:http",
        rows: [row("builtin:http", "*", "off")],
        expect: false,
      },
      {
        name: "the own plugin off",
        source: "builtin:own-plugin",
        rows: [row("builtin:own-plugin", "*", "off")],
        expect: false,
      },
      {
        name: "a remote plugin is off by default",
        source: "remote:hs",
        rows: [],
        expect: false,
      },
      {
        name: "a remote plugin on",
        source: "remote:hs",
        rows: [row("remote:hs", "*", "on")],
        expect: true,
      },
      {
        name: "a '*' row holding a tier is ignored",
        source: "remote:hs",
        rows: [row("remote:hs", "*", "outward")],
        expect: false,
      },
      {
        name: "a package server is on by default",
        source: "package:pk/mcp:srv",
        rows: [],
        expect: true,
      },
      {
        name: "a package server off",
        source: "package:pk/mcp:srv",
        rows: [row("package:pk/mcp:srv", "*", "off")],
        expect: false,
      },
      {
        name: "a package is off by default",
        source: "package:pk",
        rows: [],
        expect: false,
      },
      {
        name: "a source with a newline is no package server",
        source: "package:pk\n/mcp:srv",
        rows: [],
        expect: false,
      },
    ],
    unicode: {
      node: process.versions.unicode ?? "",
      cn: cnRanges(),
      doubt: UNICODE_DOUBT.map(([a, b]) => [a, b]),
    },
    calls: [
      {
        name: "a qualified skill off",
        tool: "Skill",
        input: { skill: "pa:jot" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off")],
        expect: off(PA, "skill:jot"),
      },
      {
        name: "a qualified skill of another plugin",
        tool: "Skill",
        input: { skill: "pb:jot" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off")],
        expect: null,
      },
      {
        name: "an unqualified skill off in one plugin",
        tool: "Skill",
        input: { skill: "jot" },
        plugins: PLUGINS,
        rows: [row(PB, "skill:jot", "off")],
        expect: off(PB, "skill:jot"),
      },
      {
        name: "an unqualified subagent off in one plugin",
        tool: "Agent",
        input: { subagent_type: "reviewer", prompt: "p" },
        plugins: PLUGINS,
        rows: [row(PB, "subagent:reviewer", "off")],
        expect: off(PB, "subagent:reviewer"),
      },
      {
        name: "a subagent by its frontmatter name",
        tool: "Agent",
        input: { subagent_type: "pa:deep-review" },
        plugins: PLUGINS,
        rows: [row(PA, "subagent:review:deep", "off")],
        expect: off(PA, "subagent:review:deep"),
      },
      {
        name: "a nested subagent named without its plugin",
        tool: "Agent",
        input: { subagent_type: "review:deep" },
        plugins: PLUGINS,
        rows: [row(PA, "subagent:review:deep", "off")],
        expect: off(PA, "subagent:review:deep"),
      },
      {
        name: "Task as Agent",
        tool: "Task",
        input: { subagent_type: "pa:reviewer" },
        plugins: PLUGINS,
        rows: [row(PA, "subagent:reviewer", "off")],
        expect: off(PA, "subagent:reviewer"),
      },
      {
        name: "an alias with several targets, one off",
        tool: "Skill",
        input: { skill: "pa:tidy" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:dust", "off")],
        expect: off(PA, "skill:dust"),
      },
      {
        name: "an alias is its own plugin's only",
        tool: "Skill",
        input: { skill: "pb:tidy" },
        plugins: PLUGINS,
        rows: [row(PB, "skill:dust", "off")],
        expect: null,
      },
      {
        name: "the own plugin's skill off",
        tool: "Skill",
        input: { skill: "scadbuddy:print" },
        plugins: PLUGINS,
        rows: [row(OWN, "skill:print", "off")],
        expect: off(OWN, "skill:print"),
      },
      {
        name: "an on row refuses nothing",
        tool: "Skill",
        input: { skill: "pa:jot" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "on")],
        expect: null,
      },
      {
        name: "a skill row does not refuse a subagent",
        tool: "Agent",
        input: { subagent_type: "pa:jot" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off")],
        expect: null,
      },
      {
        name: "a row of an unloaded source refuses nothing",
        tool: "Skill",
        input: { skill: "pc:jot" },
        plugins: PLUGINS,
        rows: [row("package:pc", "skill:jot", "off")],
        expect: null,
      },
      {
        name: "a tool that is not Skill, Agent or Task",
        tool: "Bash",
        input: { skill: "pa:jot", subagent_type: "pa:jot" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off"), row(PA, "subagent:jot", "off")],
        expect: null,
      },
      {
        name: "an input without the field",
        tool: "Skill",
        input: { subagent_type: "pa:jot" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off")],
        expect: null,
      },
      {
        name: "an input that is not an object",
        tool: "Agent",
        input: "pa:reviewer",
        plugins: PLUGINS,
        rows: [row(PA, "subagent:reviewer", "off")],
        expect: null,
      },
      // Claude Code 2.1.310's Skill tool trims the name and drops one leading `/`, then matches exactly (items.ts `callNames`).
      {
        name: "a skill name with spaces around it",
        tool: "Skill",
        input: { skill: "  pa:jot\n" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off")],
        expect: off(PA, "skill:jot"),
      },
      {
        name: "a skill name with a leading slash",
        tool: "Skill",
        input: { skill: "/pa:jot" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off")],
        expect: off(PA, "skill:jot"),
      },
      {
        name: "a command named as its slash command, spaced",
        tool: "Skill",
        input: { skill: " /wave " },
        plugins: PLUGINS,
        rows: [row(PA, "skill:wave", "off")],
        expect: off(PA, "skill:wave"),
      },
      {
        name: "only one leading slash is dropped",
        tool: "Skill",
        input: { skill: "//pa:jot" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off")],
        expect: null,
      },
      {
        name: "a skill name in other case is another name",
        tool: "Skill",
        input: { skill: "PA:Jot" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off")],
        expect: null,
      },
      // Its Agent tool takes the exact agent type, else the one whose NFKC, lower-cased form without spaces, dashes and underscores is the same.
      {
        name: "a subagent type in other case and spacing",
        tool: "Agent",
        input: { subagent_type: " PA:Re_viewer " },
        plugins: PLUGINS,
        rows: [row(PA, "subagent:reviewer", "off")],
        expect: off(PA, "subagent:reviewer"),
      },
      {
        name: "a frontmatter name normalised",
        tool: "Agent",
        input: { subagent_type: "pa:Deep_Review" },
        plugins: PLUGINS,
        rows: [row(PA, "subagent:review:deep", "off")],
        expect: off(PA, "subagent:review:deep"),
      },
      {
        name: "a subagent type in full-width letters",
        tool: "Task",
        input: { subagent_type: "ｒｅｖｉｅｗｅｒ" },
        plugins: PLUGINS,
        rows: [row(PB, "subagent:reviewer", "off")],
        expect: off(PB, "subagent:reviewer"),
      },
      {
        name: "a subagent type keeps its slash",
        tool: "Agent",
        input: { subagent_type: "/reviewer" },
        plugins: PLUGINS,
        rows: [row(PB, "subagent:reviewer", "off")],
        expect: null,
      },
      // #4757 review M2: a row refuses only an item its plugin has.
      {
        name: "a built-in subagent type with no item is left alone (a stale off row)",
        tool: "Agent",
        input: { subagent_type: "general-purpose" },
        plugins: PLUGINS,
        rows: [row(PB, "subagent:general-purpose", "off")],
        expect: null,
      },
      {
        name: "a stale off skill row of a plugin without it",
        tool: "Skill",
        input: { skill: "jot" },
        plugins: PLUGINS,
        rows: [row(OWN, "skill:jot", "off")],
        expect: null,
      },
      // #4757 review M1: a plugin switched off as a whole refuses every call that reaches one of its items.
      {
        name: "a package switched off",
        tool: "Skill",
        input: { skill: "pa:jot" },
        plugins: PLUGINS,
        rows: [row(PA, "*", "off")],
        expect: off(PA, "*"),
      },
      {
        name: "the own plugin switched off",
        tool: "Agent",
        input: { subagent_type: "print-analyst" },
        plugins: PLUGINS,
        rows: [row(OWN, "*", "off")],
        expect: off(OWN, "*"),
      },
      {
        name: "a switched-off plugin does not refuse another's item",
        tool: "Skill",
        input: { skill: "pb:jot" },
        plugins: PLUGINS,
        rows: [row(PA, "*", "off")],
        expect: null,
      },
      // #4757 review round 2: the `<plugin>:` prefix comes off before the rest is cut and compared.
      {
        name: "a 127-character frontmatter name called qualified",
        tool: "Agent",
        input: { subagent_type: `pa:${LONG}` },
        plugins: [PA_LONG],
        rows: [row(PA, "subagent:long", "off")],
        expect: off(PA, "subagent:long"),
      },
      {
        name: "a 127-character skill alias called qualified",
        tool: "Skill",
        input: { skill: `/pa:${LONG}` },
        plugins: [PA_LONG],
        rows: [row(PA, "skill:jot", "off")],
        expect: off(PA, "skill:jot"),
      },
      {
        name: "a frontmatter name over the cap, called in another spelling",
        tool: "Agent",
        input: { subagent_type: `PA:${"DEEP".repeat(40)}` },
        plugins: [PA_LONG],
        rows: [row(PA, "subagent:review:deep", "off")],
        expect: off(PA, "subagent:review:deep"),
      },
      {
        name: "an ambiguous agent type is refused when any match is off",
        tool: "Agent",
        input: { subagent_type: "pa:REVIEWER" },
        plugins: [PA_LONG],
        rows: [row(PA, "subagent:re-viewer", "off")],
        expect: off(PA, "subagent:re-viewer"),
      },
      {
        name: "an exact match that is on beside an off one its spelling reaches is refused (fail closed)",
        tool: "Agent",
        input: { subagent_type: "pa:reviewer" },
        plugins: [PA_LONG],
        rows: [row(PA, "subagent:re-viewer", "off")],
        expect: off(PA, "subagent:re-viewer"),
      },
      // JS trim() also removes U+FEFF, which Python's str.strip() keeps: a port trims by the JS set.
      {
        name: "a skill name with a byte order mark before it",
        tool: "Skill",
        input: { skill: "\uFEFFpa:jot\u3000" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off")],
        expect: off(PA, "skill:jot"),
      },
      {
        name: "a switch reaches only a name the plugin has",
        tool: "Agent",
        input: { subagent_type: "general-purpose" },
        plugins: PLUGINS,
        rows: [row(PA, "*", "off")],
        expect: null,
      },
      // Task 6 review I1: the plugins in the turn's order (packages, then the own plugin), the first that refuses names the reason.
      {
        name: "one name off in a package and in the own plugin: the package is named",
        tool: "Skill",
        input: { skill: "print" },
        plugins: [
          { name: "pa", source: PA, items: ["skill:print"], aliases: {} },
          PLUGINS[2]!,
        ],
        rows: [row(OWN, "skill:print", "off"), row(PA, "skill:print", "off")],
        expect: off(PA, "skill:print"),
      },
      // Task 6 review m1: an alias key over the cap is compared whole; a call that differs from it past the cap names another.
      {
        name: "a name that differs from an alias over the cap only past it",
        tool: "Agent",
        input: { subagent_type: `pa:${OVER.slice(0, 128)}zzz` },
        plugins: [PA_LONG],
        rows: [row(PA, "subagent:review:deep", "off")],
        expect: null,
      },
      // Task 6 review m7: JS trim() trims U+2028, not U+0085 nor \x1c (Python's str.strip() differs on all three).
      {
        name: "a skill name between line separators",
        tool: "Skill",
        input: { skill: " pa:jot " },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off")],
        expect: off(PA, "skill:jot"),
      },
      {
        name: "a skill name after a next line is another name",
        tool: "Skill",
        input: { skill: "\u0085pa:jot" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off")],
        expect: null,
      },
      {
        name: "a skill name after a file separator is another name",
        tool: "Skill",
        input: { skill: "\u001Cpa:jot" },
        plugins: PLUGINS,
        rows: [row(PA, "skill:jot", "off")],
        expect: null,
      },
      {
        name: "a subagent type with a hyphen and a next line in it",
        tool: "Agent",
        input: { subagent_type: "pa:re‐view\u0085er" },
        plugins: PLUGINS,
        rows: [row(PA, "subagent:reviewer", "off")],
        expect: off(PA, "subagent:reviewer"),
      },
      // Task 6 review m3: case is folded only where both runtimes' Unicode agree (the BMP); an agent type holding a letter
      // past it, or a code point either one has unassigned, reaches every subagent of each plugin (fail closed).
      {
        name: "an agent type with a letter past the BMP reaches every subagent",
        tool: "Agent",
        input: { subagent_type: "pb:\u{10400}" },
        plugins: PLUGINS,
        rows: [row(PB, "subagent:reviewer", "off")],
        expect: off(PB, "subagent:reviewer"),
      },
      {
        name: "an agent type with a Unicode 17 letter reaches every subagent",
        tool: "Agent",
        input: { subagent_type: "x\u{16EA0}" },
        plugins: PLUGINS,
        rows: [row(OWN, "subagent:print-analyst", "off")],
        expect: off(OWN, "subagent:print-analyst"),
      },
      {
        name: "an agent type with an unassigned code point reaches every subagent",
        tool: "Agent",
        input: { subagent_type: "x\u{E0080}" },
        plugins: PLUGINS,
        rows: [row(PA, "subagent:review:deep", "off")],
        expect: off(PA, "subagent:review:deep"),
      },
      {
        name: "a final sigma is a sigma (its context differs between Unicode versions)",
        tool: "Agent",
        input: { subagent_type: "\u0295\u03A3" },
        plugins: [
          {
            name: "pz",
            source: "package:pz",
            items: ["subagent:\u0295\u03C3"],
            aliases: {},
          },
        ],
        rows: [row("package:pz", "subagent:\u0295\u03C3", "off")],
        expect: off("package:pz", "subagent:\u0295\u03C3"),
      },
      // Task 6 re-review R1: an alias key is bounded (ALIAS_KEY_CAP); a name over it is a prefix key, reached by any call cut the same way.
      {
        name: "a name over the alias bound, called whole",
        tool: "Agent",
        input: { subagent_type: `pa:${HUGE}` },
        plugins: [PA_HUGE],
        rows: [row(PA, "subagent:huge", "off")],
        expect: off(PA, "subagent:huge"),
      },
      {
        name: "a name that differs from one over the alias bound only past it (fail closed)",
        tool: "Agent",
        input: { subagent_type: `pa:${HUGE.slice(0, ALIAS_KEY_CAP)}zzz` },
        plugins: [PA_HUGE],
        rows: [row(PA, "subagent:huge", "off")],
        expect: off(PA, "subagent:huge"),
      },
      // Task 6 re-review R6: a code point Unicode 17 assigned and agent-durable's Unicode 16 has not is unsure in both.
      {
        name: "an agent type with a BMP code point new in Unicode 17 reaches every subagent",
        tool: "Agent",
        input: { subagent_type: "x౜" },
        plugins: PLUGINS,
        rows: [row(PB, "subagent:reviewer", "off")],
        expect: off(PB, "subagent:reviewer"),
      },
      {
        name: "an agent type with an emoji is compared as it is",
        tool: "Agent",
        input: { subagent_type: "\u{1F916}" },
        plugins: PLUGINS,
        rows: [row(PA, "subagent:reviewer", "off")],
        expect: null,
      },
    ],
  };
}

describe("access vectors", () => {
  const built = build();

  it("match the committed file", () => {
    if (process.env.UPDATE_ACCESS_VECTORS === "1")
      writeFileSync(FILE, `${JSON.stringify(built, null, 2)}\n`);
    expect(JSON.parse(readFileSync(FILE, "utf8"))).toEqual(built);
  });

  it.each(built.registry.filter((v) => !v.durable_only))(
    "registry: $name",
    (v) => {
      const tools = v.tools.filter((t) => t.hitl !== "answer") as {
        name: string;
        risk: RiskTier;
      }[];
      expect(registryOverrides(tools, v.rows)).toEqual(v.expect);
    },
  );

  // Review of #4838: a call's tool is resolved from its one row, not the whole registry.
  it.each(built.registry.filter((v) => !v.durable_only))(
    "one tool per call, as the whole registry: $name",
    async (v) => {
      const tools = v.tools.filter((t) => t.hitl !== "answer") as {
        name: string;
        risk: RiskTier;
      }[];
      const reads = { rows: 0, row: 0 };
      const reader: AccessReader = {
        rows: async (sources) => {
          reads.rows += 1;
          return v.rows.filter((r) => sources.includes(r.source));
        },
        row: async (source, item) => {
          reads.row += 1;
          return v.rows.find((r) => r.source === source && r.item === item);
        },
      };
      const overrides = new AccessResolver(reader, tools).registryOverrides();
      for (const tool of tools) {
        const tier = v.expect.tool_tiers[tool.name];
        expect(await overrides.of(tool.name), tool.name).toEqual({
          ...(tier === undefined ? {} : { tier }),
          disabled: v.expect.disabled_tools.includes(tool.name),
        });
      }
      // A browser tool's call reads its source's rows once, for the `*` switch too (#4757).
      const browser = tools.filter((t) => registrySource(t.name) === B).length;
      expect(reads).toEqual({ rows: browser, row: tools.length - browser });
    },
  );

  it.each(built.item_kinds)("item kind: $item in $source", (v) =>
    expect(itemKindIn(v.source, v.item)).toBe(v.expect),
  );

  it.each(built.sources)("source: $name", (v) =>
    expect(sourceEnabled(v.source, v.rows)).toBe(v.expect),
  );

  it.each(built.calls)("call: $name", (v) => {
    const found = offCallItem(v.tool, v.input, v.plugins, v.rows);
    expect(found ? { ...found, reason: offItemReason(found) } : null).toEqual(
      v.expect,
    );
  });

  it("names each tool's registry source", () => {
    for (const [tool, source] of Object.entries(built.registry_source))
      expect(registrySource(tool)).toBe(source);
  });
});
