import { z } from 'zod'
import { PairingError } from '../bridge/pairings.js'
import type { BrowserTarget, HubErrorCode } from '../bridge/hub.js'
import { type BambuddyScope, defineTool, json, type Risk, type Tool, type ToolContext, ToolError } from './registry.js'

// The browser_* tools (#254, spec §5.2 and §8.5): the user's own open
// ScadBuddy tab, driven through the same handlers user input goes through
// (frontend/src/agent/, PR #339). Each tool here is one tool of the tab's
// catalogue (frontend/src/agent/catalog.ts `TOOLS`), named `browser_<name>`;
// its handler forwards the call over the paired tab's socket
// (bridge/hub.ts) and returns what the tab answered, or an error that says
// why it could not ("no browser attached", a timeout). The catalogue is the
// same on every route; a tool whose page is not open answers `unavailable`
// from the tab, so the list an agent sees never changes mid-session.
// test/browserTools.test.ts checks this list against the tab's catalogue:
// same tools, same arguments, and a tier no lower than the tab's.
//
// Tiers (spec §8.1). Reading the screen is `read`. Acting in the user's tab,
// which includes moving it to another page, is at least `write`, whatever
// the tab's own tier says: navigate and open_model are `read` in the tab's
// catalogue because they change no data, but here they change what the user
// is looking at. `open_print_dialog` is `outward` in both, so it goes through
// the approval gate of spec §8.2 like every other outward tool, and even then
// it only opens the dialog: the Print and Send confirmations are user-only
// (frontend dom.ts `USER_ONLY`), so a human presses them, exactly as in the UI.
//
// Not here: the `screenshot()` fallback of #254, which PR #339 left out of the
// tab on purpose. And not the headless browser (#349, harness/headlessBrowser.ts),
// a separate Chromium that never sees the user's tab.

/** Where a tab's answer comes from, for the untrusted-data envelope (#258). */
const SOURCE =
  "the user's open ScadBuddy tab: what the page shows, including model names, READMEs, parameters, " +
  'OpenSCAD source and Bambuddy data that others wrote'

/** Added to a tool's own wait (`timeout_ms`) for the round trip to the tab. */
export const ROUND_TRIP_MARGIN_MS = 10_000

/** The tab's own tool names (frontend catalog.ts `TOOLS`), forwarded as they are. */
export type TabTool =
  | 'navigate'
  | 'snapshot'
  | 'click'
  | 'fill'
  | 'search'
  | 'open_model'
  | 'get_params'
  | 'set_param'
  | 'set_params'
  | 'reset_param'
  | 'render'
  | 'generate'
  | 'select_plate'
  | 'open_print_dialog'
  | 'get_editor_text'
  | 'replace_range'
  | 'get_problems'
  | 'get_form'
  | 'set_field'
  | 'test_connection'

const paramValue = z.union([z.boolean(), z.number(), z.string()])
const waitMs = (max: number, fallback: number) =>
  z.number().int().min(0).max(max).default(fallback).describe('How long the tab waits, in milliseconds')

/** frontend catalog.ts `SETTINGS_FIELDS`. */
export const SETTINGS_FIELDS = [
  'bambuddy_url',
  'public_url',
  'library_folder_id',
  'printer_id',
  'default_plate',
  'display_unit',
] as const

type Forwarded<S extends z.ZodRawShape> = {
  tool: TabTool
  description: string
  input: z.ZodObject<S>
  risk: Risk
  bambuddyScope?: readonly BambuddyScope[]
  summarize?: (args: z.infer<z.ZodObject<S>>) => string
}

function target(ctx: ToolContext): BrowserTarget {
  return { principal: ctx.principal }
}

function tabs(ctx: ToolContext) {
  if (!ctx.browser) throw new ToolError('no browser attached: this agent service runs without the browser bridge')
  return ctx.browser
}

function forwarded<S extends z.ZodRawShape>(spec: Forwarded<S>): Tool {
  return defineTool({
    name: `browser_${spec.tool}`,
    description: spec.description,
    input: spec.input,
    risk: spec.risk,
    ...(spec.bambuddyScope ? { bambuddyScope: spec.bambuddyScope } : {}),
    ...(spec.summarize ? { summarize: spec.summarize } : {}),
    routes: [],
    source: SOURCE,
    handler: async (args, ctx) => {
      const input = args as Record<string, unknown>
      const wait = typeof input.timeout_ms === 'number' ? input.timeout_ms + ROUND_TRIP_MARGIN_MS : undefined
      const call = () =>
        tabs(ctx).call(target(ctx), spec.tool, input, { signal: ctx.signal, ...(wait === undefined ? {} : { timeoutMs: wait }) })
      let outcome = await call()
      // #815 §2: in a session the user owns, a call that finds no tab waits for
      // it as an attention request, and runs once more when the tab is back.
      // Once only: a tab that came back on another replica is still not here.
      if (!outcome.ok && outcome.error.code === 'no_browser' && ctx.waitForTab) {
        const waited = await ctx.waitForTab({ tool: `browser_${spec.tool}`, toolUseId: ctx.toolUseId, signal: ctx.signal })
        if (waited.back) outcome = await call()
        else throw new ToolError(`${outcome.error.message} ${waited.message}`)
      }
      if (outcome.ok) return json(outcome.result ?? null)
      const { code, message, issues } = outcome.error
      // The tab's answers can quote the page, so they go in the untrusted envelope (#258).
      if (HUB_CODES.has(code)) throw new ToolError(message)
      const detail = issues?.length ? `${message} ${issues.map((i) => `${i.path || '(root)'}: ${i.message}`).join('; ')}` : message
      throw new ToolError(`the tab answered ${spec.tool} with ${code}`, undefined, detail)
    },
  })
}

/** bridge/hub.ts `HubErrorCode`: the hub's own answers, in ScadBuddy's words. */
const HUB_CODES: ReadonlySet<string> = new Set<HubErrorCode>(['no_browser', 'no_answer', 'disconnected', 'busy'])

const PREFIX = "In the user's own open ScadBuddy tab, which they watch as it happens: "

export const browserTools: Tool[] = [
  defineTool({
    name: 'browser_status',
    description:
      "Whether a ScadBuddy tab is attached for the browser_* tools: the user's tab this chat runs in, or one the " +
      'user paired with this caller (browser_pair). When attached, its route and which browser_* tools its page ' +
      'has live now (the others answer "unavailable" until you navigate to their page).',
    input: z.object({}),
    risk: 'read',
    routes: [],
    source: SOURCE,
    handler: async (_args, ctx) => {
      const status = await tabs(ctx).status(target(ctx))
      if (!status.attached) return json({ attached: false, reason: status.reason })
      return json({
        attached: true,
        via: status.via,
        route: status.route,
        live_tools: status.live.map((name) => `browser_${name}`),
        ...(status.pairing ? { paired_until: status.pairing.expiresAt.toISOString() } : {}),
      })
    },
  }),

  defineTool({
    name: 'browser_pair',
    description:
      "Ask to drive the user's open ScadBuddy tab with the browser_* tools. Returns a short code: tell the user " +
      'to type it into the prompt that appears in their ScadBuddy tab. Once they have, browser_status says ' +
      'attached. The code works once, for five minutes. Not needed in a chat the user started from ScadBuddy: ' +
      'that pairs with their tab on its own.',
    input: z.object({}),
    // Pairing is only ever for acting in the tab, which is write.
    risk: 'write',
    routes: [],
    handler: async (_args, ctx) => {
      if (ctx.principal.kind === 'browser') {
        throw new ToolError(
          "this is the user's own chat: it pairs with the tab they send messages from, so there is nothing to pair",
        )
      }
      const browser = tabs(ctx)
      const status = await browser.status(target(ctx))
      if (status.attached) {
        return json({
          status: 'paired',
          route: status.route,
          ...(status.pairing ? { paired_until: status.pairing.expiresAt.toISOString() } : {}),
        })
      }
      try {
        const request = await browser.pair(ctx.principal)
        return json({
          status: 'pending',
          code: request.code,
          expires_at: request.expiresAt.toISOString(),
          shown_to_user_as: request.label,
          next:
            `Tell the user: "Type ${request.code} into the pairing prompt in your ScadBuddy tab." Their tab shows ` +
            `the request as ${request.label}. Then call browser_status until it says attached.`,
        })
      } catch (err) {
        if (err instanceof PairingError) throw new ToolError(err.message)
        throw err
      }
    },
  }),

  // ── Global ──────────────────────────────────────────────────────────────────
  forwarded({
    tool: 'navigate',
    description:
      PREFIX +
      'go to a route in the app, e.g. "/", "/settings", "/m/name-keychain", "/m/name-keychain/source". The ' +
      'catalogue ("/") takes its search, filters, sort and view from the query: ' +
      '"/?q=keychain&tag=a&tag=b&origin=builtin|mine&sort=name&view=list" (tags are ANDed). Returns the route ' +
      'that ended up on screen (an unknown path lands on "/").',
    input: z.object({ route: z.string().min(1).describe('An in-app path starting with "/"') }),
    risk: 'write',
  }),
  forwarded({
    tool: 'snapshot',
    description:
      PREFIX +
      "the agent's view of the screen: the route, open dialogs, form values, validation errors, the page's own " +
      'state, and a compact list of the interactive elements (role and name). Passwords are never read back.',
    input: z.object({}),
    risk: 'read',
  }),
  forwarded({
    tool: 'click',
    description:
      PREFIX +
      'fallback for UI no other browser_* tool covers: click the visible element with this ARIA role and ' +
      'accessible name. Refuses the confirmation buttons that send, print, delete or save settings: only the ' +
      'user can press those.',
    input: z.object({
      role: z.string().min(1).describe('ARIA role, e.g. "button", "link", "tab", "checkbox"'),
      name: z.string().describe('Accessible name, matched exactly (case-insensitive)'),
      index: z.number().int().min(0).optional().describe('Which match, when several share the name'),
    }),
    risk: 'write',
  }),
  forwarded({
    tool: 'fill',
    description:
      PREFIX +
      'fallback for UI no other browser_* tool covers: type a value into the visible field with this label (a ' +
      "text box, number box, select or slider). A select takes an option's value or its visible label. Never " +
      'fills a password or API key.',
    input: z.object({
      label: z.string().min(1).describe("The field's accessible name"),
      value: z.string(),
    }),
    risk: 'write',
  }),

  // ── Catalogue ("/") ───────────────────────────────────────────────────────────
  forwarded({
    tool: 'search',
    description:
      PREFIX +
      'on the catalogue ("/"), find models whose name, slug, description or tags contain the query. An empty ' +
      'query lists every model.',
    input: z.object({ query: z.string() }),
    risk: 'read',
  }),
  forwarded({
    tool: 'open_model',
    description: PREFIX + 'on the catalogue ("/"), open a model in the customizer.',
    input: z.object({ slug: z.string().min(1) }),
    risk: 'write',
  }),

  // ── Customizer ("/m/<slug>") ──────────────────────────────────────────────────
  forwarded({
    tool: 'get_params',
    description:
      PREFIX +
      "in the customizer, the open model's parameters: each one's type, group, limits, options, default and " +
      'current value.',
    input: z.object({}),
    risk: 'read',
  }),
  forwarded({
    tool: 'set_param',
    description:
      PREFIX +
      'in the customizer, set one parameter exactly as editing its field does: the preview re-renders after the ' +
      'same debounce. The value is checked against the parameter first.',
    input: z.object({ name: z.string().min(1), value: paramValue }),
    risk: 'write',
  }),
  forwarded({
    tool: 'set_params',
    description:
      PREFIX + 'in the customizer, set several parameters at once (one render). Every value is checked before any is applied.',
    input: z.object({
      // `catchall`, not `z.record`: see `params` in common.ts.
      values: z.object({}).catchall(paramValue).describe('New values by parameter name'),
    }),
    risk: 'write',
  }),
  forwarded({
    tool: 'reset_param',
    description: PREFIX + 'in the customizer, put one parameter back to its default, or all of them when no name is given.',
    input: z.object({ name: z.string().min(1).optional() }),
    risk: 'write',
  }),
  forwarded({
    tool: 'render',
    description:
      PREFIX +
      'in the customizer, wait for the preview render of the current values to settle and report it: status, ' +
      'bounding box, colours, warnings, the notes the template echoed and, on failure, the OpenSCAD log.',
    input: z.object({ timeout_ms: waitMs(120_000, 30_000) }),
    risk: 'read',
  }),
  forwarded({
    tool: 'generate',
    description:
      PREFIX +
      'in the customizer, save the settled render as an output (the Generate button). Needed before the print ' +
      'or send dialog can open.',
    input: z.object({ timeout_ms: waitMs(120_000, 30_000) }),
    risk: 'write',
  }),
  forwarded({
    tool: 'select_plate',
    description:
      PREFIX +
      'in the customizer, choose the printer model whose plate the preview draws and checks the fit against; ' +
      'null for the configured default plate.',
    input: z.object({ printer_model: z.string().min(1).nullable() }),
    risk: 'write',
  }),
  forwarded({
    tool: 'open_print_dialog',
    description:
      PREFIX +
      'in the customizer, open the Print or Send to Bambuddy dialog for the generated output. It only opens the ' +
      'dialog: the user reviews it and presses the confirmation themselves.',
    input: z.object({ kind: z.enum(['print', 'send']).default('print') }),
    risk: 'outward',
    summarize: ({ kind }) => `Open the ${kind === 'send' ? 'Send to Bambuddy' : 'Print'} dialog in your ScadBuddy tab`,
  }),

  // ── Source editor ("/m/<slug>/source", "/new") ────────────────────────────────
  forwarded({
    tool: 'get_editor_text',
    description: PREFIX + "in the source editor, the source as it stands (saved or not), and whether it's read-only.",
    input: z.object({}),
    risk: 'read',
  }),
  forwarded({
    tool: 'replace_range',
    description:
      PREFIX +
      'in the source editor, replace a range of the source (1-based lines and columns, end exclusive) with text, ' +
      'as one undoable edit. start = end inserts. The source is not saved.',
    input: z.object({
      start_line: z.number().int().min(1),
      start_column: z.number().int().min(1),
      end_line: z.number().int().min(1),
      end_column: z.number().int().min(1),
      text: z.string(),
    }),
    risk: 'write',
  }),
  forwarded({
    tool: 'get_problems',
    description:
      PREFIX +
      "in the source editor, OpenSCAD's parse check of the source: ok, diagnostics by line, or the log. With " +
      'wait, waits for the check of the current text first.',
    input: z.object({ wait: z.boolean().default(true), timeout_ms: waitMs(60_000, 15_000) }),
    risk: 'read',
  }),

  // ── Settings ("/settings") ────────────────────────────────────────────────────
  forwarded({
    tool: 'get_form',
    description:
      PREFIX +
      'on Settings, the form as it stands, saved or not, and the choices each picker offers. The API key is ' +
      'never returned; only whether one is stored.',
    input: z.object({}),
    risk: 'read',
  }),
  forwarded({
    tool: 'set_field',
    description:
      PREFIX +
      'on Settings, change one field in the form. Nothing is saved: the user presses Save. The API key is not a ' +
      'field an agent can set.',
    input: z.object({ field: z.enum(SETTINGS_FIELDS), value: z.string() }),
    risk: 'write',
  }),
  forwarded({
    tool: 'test_connection',
    description:
      PREFIX +
      'on Settings, test the stored Bambuddy connection. Refused while the form has unsaved changes, because the ' +
      'test saves the form first and that is for the user to do.',
    // `read` as in the tab's catalogue: POST /settings/test only reads Bambuddy's
    // printers with the stored settings (catalog.ts explains).
    input: z.object({}),
    risk: 'read',
    bambuddyScope: ['Read Status'],
  }),
]
