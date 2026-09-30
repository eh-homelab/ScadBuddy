import { type Tier, TIERS } from '../auth/principal.js'
import type { Tool } from '../tools/registry.js'

// The `scadbuddy://` resources (issue #264; spec §5.4), as MCP resources
// (https://modelcontextprotocol.io/specification/2025-11-25/server/resources;
// the SDK in use, @modelcontextprotocol/sdk 1.30.1, speaks 2025-11-25 and
// negotiates down to 2025-06-18, whose resource messages are the same).
//
// Every resource is BACKED BY A READ TOOL of the registry: reading
// `scadbuddy://models/keychain/source` runs `get_source({ slug: 'keychain' })`
// through the same typed backend client, the same argument validation and the
// same redaction (`get_settings`) as the tool. There is no second copy of the
// API here, and the openapi coverage check (tools/coverage.ts) needs no
// resource entries: a resource adds no backend route.
//
// URIs. A custom scheme is allowed ("implementations are always free to use
// additional, custom URI schemes", same page, "Common URI Schemes") and must
// follow RFC 3986 (https://datatracker.ietf.org/doc/html/rfc3986). Templates
// use RFC 6570 level-1 `{var}` expressions only
// (https://datatracker.ietf.org/doc/html/rfc6570#section-3.2.2), one path
// segment each: a value is percent-encoded when expanded and decoded when
// matched, so `builtin:gridfinity-bin` is `builtin%3Agridfinity-bin` in a URI.
// `canonical()` is the one spelling subscriptions and notifications use.
//
// Sessions (#300) are backed by the `sessions_*` read tools, which answer
// only for sessions the caller may see (tools/sessions.ts).
//
// Not here yet (each needs a backend route or event source that is not on
// main): the Bambuddy printers, queue, inventory, history and stats resources
// (the print watcher, #268) and `scadbuddy://browser/{tab}/snapshot` (#254).
// `scadbuddy://docs/authoring` (#252) needs no backend route: its tool reads
// the authoring skill the agent ships with (tools/guide.ts).

export type ResourceDef = {
  /** RFC 6570 template, or a fixed URI when it has no `{var}`. */
  template: string
  name: string
  title: string
  description: string
  mimeType: string
  /** The registry tool that reads it. It must be a `read` tool. */
  tool: string
  /** Template variables → the tool's arguments (strings by default). */
  args?: (vars: Record<string, string>) => Record<string, unknown>
  /**
   * The tier a caller needs to read or subscribe, when it is higher than the
   * tool's. Settings need `write` (issue #264: "even redacted settings reveal
   * the environment").
   */
  tier?: Tier
  /**
   * Subscribing reads the resource first and refuses one the caller cannot
   * read: for resources whose tool also checks WHO may see them, beyond the
   * tier (a session, #300), so nobody can follow what it may not see.
   */
  readToSubscribe?: boolean
}

const JSON_TYPE = 'application/json'

export const RESOURCES: readonly ResourceDef[] = [
  // ── catalogue ───────────────────────────────────────────────────────────
  {
    template: 'scadbuddy://models',
    name: 'models',
    title: 'Model catalogue',
    description: 'Every model (bundled templates and user models): name, slug, tags, version, origin, upstream.',
    mimeType: JSON_TYPE,
    tool: 'list_models',
  },
  {
    template: 'scadbuddy://models/{slug}',
    name: 'model',
    title: 'Model',
    description: "One model's metadata: name, description, tags, current version, libraries and upstream.",
    mimeType: JSON_TYPE,
    tool: 'get_model',
  },
  {
    template: 'scadbuddy://models/{slug}/source',
    name: 'model-source',
    title: 'Model source',
    description: "A model's current OpenSCAD source.",
    mimeType: 'text/x-openscad',
    tool: 'get_source',
  },
  {
    template: 'scadbuddy://models/{slug}/schema',
    name: 'model-schema',
    title: 'Customizer schema',
    description: "A model's customizer schema: every parameter's type, default, range, options and group.",
    mimeType: JSON_TYPE,
    tool: 'get_schema',
  },
  {
    template: 'scadbuddy://models/{slug}/readme',
    name: 'model-readme',
    title: 'Model README',
    description: "A model's README.",
    mimeType: 'text/markdown',
    tool: 'get_readme',
  },
  {
    template: 'scadbuddy://models/{slug}/thumbnail',
    name: 'model-thumbnail',
    title: 'Model thumbnail',
    description: "A model's thumbnail image (its own, or its first output's plate image).",
    mimeType: 'image/png',
    tool: 'get_model_thumbnail',
  },
  {
    template: 'scadbuddy://models/{slug}/diagnostics',
    name: 'model-diagnostics',
    title: 'Render diagnostics',
    description: "OpenSCAD's warnings and errors from the model's most recently settled render.",
    mimeType: JSON_TYPE,
    tool: 'get_render_diagnostics',
  },
  {
    template: 'scadbuddy://models/{slug}/outputs',
    name: 'model-outputs',
    title: 'Model outputs',
    description: "A model's saved outputs, newest first.",
    mimeType: JSON_TYPE,
    tool: 'list_outputs',
  },
  // ── history ─────────────────────────────────────────────────────────────
  {
    template: 'scadbuddy://models/{slug}/versions',
    name: 'model-versions',
    title: 'Revision history',
    description: "A model's revision history, newest first: commit id, message and time.",
    mimeType: JSON_TYPE,
    tool: 'list_versions',
  },
  {
    template: 'scadbuddy://models/{slug}/versions/{commit}',
    name: 'model-version-source',
    title: 'Source at a revision',
    description: "A model's OpenSCAD source at an earlier revision.",
    mimeType: 'text/x-openscad',
    tool: 'get_version_source',
  },
  {
    template: 'scadbuddy://models/{slug}/versions/{commit}/diff',
    name: 'model-version-diff',
    title: 'Revision diff',
    description: 'The unified diff of a revision against its parent.',
    mimeType: JSON_TYPE,
    tool: 'diff_version',
  },
  {
    template: 'scadbuddy://models/{slug}/versions/{commit}/schema',
    name: 'model-version-schema',
    title: 'Schema at a revision',
    description: "A model's customizer schema at an earlier revision.",
    mimeType: JSON_TYPE,
    tool: 'get_schema',
    args: ({ slug, commit }) => ({ slug, version: commit }),
  },
  {
    template: 'scadbuddy://models/{slug}/upstream',
    name: 'model-upstream',
    title: 'Upstream status',
    description: "A duplicate's upstream template: whether it has moved on, and a merge preview.",
    mimeType: JSON_TYPE,
    tool: 'get_upstream',
  },
  // ── renders and outputs ─────────────────────────────────────────────────
  {
    template: 'scadbuddy://jobs/{job_id}',
    name: 'job',
    title: 'Render job',
    description: "A render job's state: status, error, warnings, bounding box, colours, parts and log tail.",
    mimeType: JSON_TYPE,
    tool: 'get_render_job',
  },
  {
    template: 'scadbuddy://outputs/{output_id}',
    name: 'output',
    title: 'Output',
    description: 'One saved output: its model, version, parameters, parts and colours.',
    mimeType: JSON_TYPE,
    tool: 'get_output',
  },
  {
    template: 'scadbuddy://outputs/{output_id}/plates',
    name: 'output-plates',
    title: 'Output plates',
    description: "An output's build plates and what is on each.",
    mimeType: JSON_TYPE,
    tool: 'get_output_plates',
  },
  {
    template: 'scadbuddy://outputs/{output_id}/thumbnail',
    name: 'output-thumbnail',
    title: 'Output image',
    description: "An output's cover image.",
    mimeType: 'image/png',
    tool: 'get_output_image',
  },
  {
    template: 'scadbuddy://outputs/{output_id}/plates/{plate}/thumbnail',
    name: 'output-plate-thumbnail',
    title: 'Plate image',
    description: "One plate's image (plates count from 1).",
    mimeType: 'image/png',
    tool: 'get_output_image',
    args: ({ output_id, plate }) => ({ output_id, plate: /^\d+$/.test(plate ?? '') ? Number(plate) : plate }),
  },
  {
    template: 'scadbuddy://outputs/{output_id}/model.3mf',
    name: 'output-3mf',
    title: 'Output 3MF',
    description: "An output's multi-colour 3MF. Over the inline limit the content is a JSON note saying where to fetch it.",
    mimeType: 'model/3mf',
    tool: 'download_3mf',
  },
  {
    template: 'scadbuddy://print/outputs/{output_id}/progress',
    name: 'print-progress',
    title: 'Print progress',
    description: "The progress of an output's latest print in Bambuddy.",
    mimeType: JSON_TYPE,
    tool: 'get_print_progress',
  },
  {
    template: 'scadbuddy://plates',
    name: 'plates',
    title: 'Plate profiles',
    description: 'The build plates ScadBuddy knows, with their sizes.',
    mimeType: JSON_TYPE,
    tool: 'list_plates',
  },
  // ── dependencies and settings ───────────────────────────────────────────
  {
    template: 'scadbuddy://libraries',
    name: 'libraries',
    title: 'Libraries',
    description: 'OpenSCAD libraries pinned by models, with their commits.',
    mimeType: JSON_TYPE,
    tool: 'list_libraries',
  },
  {
    template: 'scadbuddy://fonts',
    name: 'fonts',
    title: 'Fonts',
    description: 'Font families installed for OpenSCAD.',
    mimeType: JSON_TYPE,
    tool: 'list_fonts',
  },
  {
    template: 'scadbuddy://settings',
    name: 'settings',
    title: 'Settings',
    description: 'ScadBuddy settings with secrets redacted. Needs the write tier.',
    mimeType: JSON_TYPE,
    tool: 'get_settings',
    tier: 'write',
  },
  // ── agent sessions (#300; spec §6) ──────────────────────────────────────
  {
    template: 'scadbuddy://sessions',
    name: 'sessions',
    title: 'Agent sessions',
    description: 'The agent sessions this caller may see, newest first: title, origin, owner and status.',
    mimeType: JSON_TYPE,
    tool: 'sessions_list',
  },
  {
    template: 'scadbuddy://sessions/{session_id}',
    name: 'session',
    title: 'Agent session',
    description:
      "One agent session: status, owner, pending approvals and its transcript's first page. Subscribe for live " +
      'updates; page the rest with sessions_get.',
    mimeType: JSON_TYPE,
    tool: 'sessions_get',
    readToSubscribe: true,
  },
  // ── docs (#252) ─────────────────────────────────────────────────────────
  {
    template: 'scadbuddy://docs/authoring',
    name: 'docs-authoring',
    title: 'Authoring guide',
    description:
      "ScadBuddy's template conventions: customizer comments, colours per extruder, installed fonts, " +
      'open preview parts, verify.sh, and the edit loop. Read it before writing a template.',
    mimeType: 'text/markdown',
    tool: 'get_authoring_guide',
  },
]

// ── templates ──────────────────────────────────────────────────────────────

const VAR = /\{([a-z_]+)\}/g

export function isTemplate(def: ResourceDef): boolean {
  return def.template.includes('{')
}

export function variablesOf(template: string): string[] {
  return [...template.matchAll(VAR)].map((m) => m[1]!)
}

/** RFC 6570 simple expansion: each value percent-encoded (reserved characters included). */
export function expand(template: string, vars: Record<string, string>): string {
  return template.replace(VAR, (_, name: string) => encodeURIComponent(vars[name] ?? ''))
}

function matcher(template: string): RegExp {
  const escaped = template.split(VAR).map((part, i) => (i % 2 === 1 ? '([^/?#]+)' : part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  return new RegExp(`^${escaped.join('')}$`)
}

const COMPILED = RESOURCES.map((def) => ({ def, re: matcher(def.template), names: variablesOf(def.template) }))

export type Match = { def: ResourceDef; vars: Record<string, string>; uri: string }

/** The resource a URI names, with its variables decoded and the URI in canonical form. */
export function matchUri(uri: string): Match | undefined {
  for (const { def, re, names } of COMPILED) {
    const m = re.exec(uri)
    if (!m) continue
    const vars: Record<string, string> = {}
    try {
      names.forEach((name, i) => (vars[name] = decodeURIComponent(m[i + 1]!)))
    } catch {
      return undefined
    }
    return { def, vars, uri: expand(def.template, vars) }
  }
  return undefined
}

/** The canonical spelling of a URI, or undefined when it names no ScadBuddy resource. */
export function canonical(uri: string): string | undefined {
  return matchUri(uri)?.uri
}

/** The higher of two tiers. */
export function maxTier(a: Tier, b: Tier): Tier {
  return TIERS.indexOf(a) >= TIERS.indexOf(b) ? a : b
}

/** The tier needed to read `def`: its tool's, raised by its own `tier`. */
export function tierFor(def: ResourceDef, tool: Tool): Tier {
  return def.tier ? maxTier(def.tier, tool.risk) : tool.risk
}
