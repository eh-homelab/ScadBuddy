import * as z from 'zod'
import type { Risk, Scope } from './types'

/**
 * Every browser tool, declared once: name, description, argument schema, tier and the
 * part of the app that provides it. The implementations live in the pages and
 * components that own the state they act on (`useAgentHandlers`), so a tool can only
 * run while its page is mounted — and the list here stays the same on every route, as
 * the AI design spec §5.2 asks ("Unavailable handlers return an error instead of
 * disappearing, so the session's tool list stays stable").
 *
 * This module is loaded lazily by the bridge, so zod stays out of the entry chunk
 * until something actually asks for a tool.
 */
interface ToolSpec<I extends z.ZodType> {
  description: string
  risk: Risk
  scope: Scope
  input: I
}

function tool<I extends z.ZodType>(spec: ToolSpec<I>): ToolSpec<I> {
  return spec
}

const none = z.object({}).strict()
const paramValue = z.union([z.string(), z.number(), z.boolean()])

export const SETTINGS_FIELDS = [
  'bambuddy_url',
  'public_url',
  'library_folder_id',
  'printer_id',
  'default_plate',
  'display_unit',
] as const

export const TOOLS = {
  // ── Global ────────────────────────────────────────────────────────────────────────
  navigate: tool({
    description:
      'Go to a route in the app, e.g. "/", "/settings", "/m/name-keychain", "/m/name-keychain/source". ' +
      'The catalogue ("/") takes its search, filters, sort and view from the query: ' +
      '"/?q=keychain&tag=a&tag=b&origin=builtin|mine&sort=name&view=list" (tags are ANDed). ' +
      'Returns the route that ended up on screen (an unknown path lands on "/").',
    risk: 'read',
    scope: 'global',
    input: z.object({ route: z.string().min(1).describe('An in-app path starting with "/"') }).strict(),
  }),
  snapshot: tool({
    description:
      "The agent's view of the screen: the route, open dialogs, form values, validation errors, " +
      "the page's own state, and a compact list of the interactive elements (role and name).",
    risk: 'read',
    scope: 'global',
    input: none,
  }),
  click: tool({
    description:
      'Fallback for UI no other tool covers: click the visible element with this ARIA role and ' +
      'accessible name. Refuses confirmation buttons that send, print, delete or save settings — ' +
      'only the user can press those.',
    risk: 'write',
    scope: 'global',
    input: z
      .object({
        role: z.string().min(1).describe('ARIA role, e.g. "button", "link", "tab", "checkbox"'),
        name: z.string().describe('Accessible name, matched exactly (case-insensitive)'),
        index: z.number().int().min(0).optional().describe('Which match, when several share the name'),
      })
      .strict(),
  }),
  fill: tool({
    description:
      'Fallback for UI no other tool covers: type a value into the visible field with this label ' +
      '(a text box, number box, select or slider). Never fills a password or API key.',
    risk: 'write',
    scope: 'global',
    input: z
      .object({
        label: z.string().min(1).describe("The field's accessible name"),
        value: z.string(),
      })
      .strict(),
  }),

  // ── Catalogue ─────────────────────────────────────────────────────────────────────
  search: tool({
    description:
      'Find models in the catalogue whose name, slug, description or tags contain the query. ' +
      'An empty query lists every model.',
    risk: 'read',
    scope: 'catalogue',
    input: z.object({ query: z.string() }).strict(),
  }),
  open_model: tool({
    description: 'Open a model from the catalogue in the customizer.',
    risk: 'read',
    scope: 'catalogue',
    input: z.object({ slug: z.string().min(1) }).strict(),
  }),

  // ── Customize ─────────────────────────────────────────────────────────────────────
  get_params: tool({
    description:
      "The open model's parameters: each one's type, group, limits, options, default and current value.",
    risk: 'read',
    scope: 'customize',
    input: none,
  }),
  set_param: tool({
    description:
      'Set one parameter, exactly as editing its field does: the preview re-renders after the ' +
      'same debounce. The value is checked against the parameter first.',
    risk: 'write',
    scope: 'customize',
    input: z.object({ name: z.string().min(1), value: paramValue }).strict(),
  }),
  set_params: tool({
    description:
      'Set several parameters at once (one render). Every value is checked before any is applied.',
    risk: 'write',
    scope: 'customize',
    input: z.object({ values: z.record(z.string(), paramValue) }).strict(),
  }),
  reset_param: tool({
    description: 'Put one parameter back to its default, or all of them when no name is given.',
    risk: 'write',
    scope: 'customize',
    input: z.object({ name: z.string().min(1).optional() }).strict(),
  }),
  render: tool({
    description:
      'Wait for the preview render of the current values to settle and report it: status, ' +
      'bounding box, colours, warnings, the notes the template echoed and, on failure, the OpenSCAD log.',
    risk: 'read',
    scope: 'customize',
    input: z
      .object({ timeout_ms: z.number().int().min(0).max(120_000).default(30_000) })
      .strict(),
  }),
  generate: tool({
    description:
      'Save the settled render as an output (the Generate button). Needed before the print or send dialog can open.',
    risk: 'write',
    scope: 'customize',
    input: z
      .object({ timeout_ms: z.number().int().min(0).max(120_000).default(30_000) })
      .strict(),
  }),
  select_plate: tool({
    description:
      "Choose the printer model whose plate the preview draws and checks the fit against; null for the configured default plate.",
    risk: 'write',
    scope: 'customize',
    input: z.object({ printer_model: z.string().min(1).nullable() }).strict(),
  }),
  open_print_dialog: tool({
    description:
      'Open the Print or Send to Bambuddy dialog for the generated output. It only opens the ' +
      'dialog: the user reviews it and presses the confirmation themselves.',
    risk: 'outward',
    scope: 'customize',
    input: z.object({ kind: z.enum(['print', 'send']).default('print') }).strict(),
  }),

  // ── Source editor ─────────────────────────────────────────────────────────────────
  get_editor_text: tool({
    description: "The source in the editor, as it stands (saved or not), and whether it's read-only.",
    risk: 'read',
    scope: 'source',
    input: none,
  }),
  replace_range: tool({
    description:
      'Replace a range of the source (1-based lines and columns, end exclusive) with text, as one ' +
      'undoable edit. start = end inserts. The source is not saved.',
    risk: 'write',
    scope: 'source',
    input: z
      .object({
        start_line: z.number().int().min(1),
        start_column: z.number().int().min(1),
        end_line: z.number().int().min(1),
        end_column: z.number().int().min(1),
        text: z.string(),
      })
      .strict(),
  }),
  get_problems: tool({
    description:
      "OpenSCAD's parse check of the source: ok, diagnostics by line, or the log. With wait, " +
      'waits for the check of the current text first.',
    risk: 'read',
    scope: 'source',
    input: z
      .object({
        wait: z.boolean().default(true),
        timeout_ms: z.number().int().min(0).max(60_000).default(15_000),
      })
      .strict(),
  }),

  // ── Settings ──────────────────────────────────────────────────────────────────────
  get_form: tool({
    description:
      'The settings form as it stands, saved or not, and the choices each picker offers. The ' +
      'API key is never returned; only whether one is stored.',
    risk: 'read',
    scope: 'settings',
    input: none,
  }),
  set_field: tool({
    description:
      'Change one settings field in the form. Nothing is saved: the user presses Save. The API ' +
      'key is not a field an agent can set.',
    risk: 'write',
    scope: 'settings',
    input: z.object({ field: z.enum(SETTINGS_FIELDS), value: z.string() }).strict(),
  }),
  test_connection: tool({
    description:
      'Test the stored Bambuddy connection. Refused while the form has unsaved changes, because ' +
      'the test saves the form first and that is for the user to do.',
    // `read`, though it reaches Bambuddy: AI spec §8.1 tiers by effect, and `outward` is
    // "send, print, delete, settings or credential writes". This changes nothing:
    // `POST /settings/test` only runs `GET /printers/` on Bambuddy with the stored settings
    // (backend/scadbuddy/api/settings.py `test_settings`); the handler skips the
    // button's save and refuses while the form is dirty, and the stored key never
    // reaches the browser or the result.
    risk: 'read',
    scope: 'settings',
    input: none,
  }),
} as const

export const SCOPE_ROUTES: Record<Scope, string> = {
  global: 'any page',
  catalogue: 'the model catalogue ("/")',
  customize: 'the customizer ("/m/<slug>")',
  source: 'the source editor ("/m/<slug>/source" or "/new")',
  settings: 'settings ("/settings")',
}
