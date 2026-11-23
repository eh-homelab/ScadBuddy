import { z } from 'zod'
import type { components } from '../api/schema.js'
import { BuiltInPluginError, PackageRefusedError } from '../plugins/packages/install.js'
import type { PackageView } from '../plugins/packages/store.js'
import { ok } from './call.js'
import { defineTool, json, type Tool, type ToolContext, ToolError } from './registry.js'

// #6938 (spec 2026-11-21 §6.2, §6.3): the assistant writes and publishes a hosted plugin, a
// Claude plugin kept in ScadBuddy as a draft of files (Settings → Tools & plugins → Hosted
// plugins). The tools call the API's `api` routes `/api/v1/hosted-plugins` with the agent's
// service credential, as every tool calls the API (`ctx.backend`); those routes take it
// (backend auth/routes.py `would_refuse`: a Service with scope `api`).
//
// A draft write is `write`: nothing loads from a draft. hosted_plugin_publish is `outward`:
// it also offers the new version to the package model (§6.3 step 5), an install or a re-pin,
// which is an outward settings write. After a person approves the call, confirm_action runs
// it once: the publish, then the offer in-process (`ToolServices.hostedPackages`, over
// `PackageInstaller`), never through the UI-guarded package routes. Nothing loads until a
// person also approves that version in Settings, as for every package.
//
// File content crosses the tool boundary as UTF-8 text: a plugin's files are Markdown and
// scripts. A file that is not UTF-8 is read as its size and sha256 only (#2479: no bytes in JSON).

type HostedPlugin = components['schemas']['HostedPlugin']

/**
 * A hosted plugin's name, as the API takes it (backend hosted_plugins/rules.py `NAME_RE`, and
 * no `--`). The API also refuses a reserved name; that refusal comes back from it.
 */
const name = z
  .string()
  .regex(/^[a-z][a-z0-9-]{0,30}[a-z0-9]$/)
  .refine((n) => !n.includes('--'), 'a name has no "--"')
  .describe("The hosted plugin's name, which is also its package's name: 2-32 lower-case letters, digits and single hyphens")

/** The top folders a draft may hold (backend hosted_plugins/rules.py `TOP_LEVEL`). */
const TOP_LEVEL = ['skills', 'agents', 'commands']
/** The longest path and the longest segment, in UTF-8 bytes (rules.py `MAX_PATH_BYTES`, `MAX_SEGMENT_BYTES`). */
const MAX_PATH_BYTES = 1024
const MAX_SEGMENT_BYTES = 255
const utf8Bytes = (text: string) => Buffer.byteLength(text, 'utf8')

/**
 * Why a draft path is refused, or undefined: the API's own rules (backend hosted_plugins/rules.py
 * `check_path`). Checked here too, before the URL is built: a path of `.` or `..` is not
 * percent-encoded, so `files/..` would resolve to the plugin's own route (`DELETE /{name}/`, then
 * a 307 to `DELETE /{name}`), and the API's check would never run (#6938 review).
 */
export function draftPathProblem(path: string): string | undefined {
  if (path === '') return 'the path is empty'
  if (utf8Bytes(path) > MAX_PATH_BYTES) return `the path is too long: at most ${MAX_PATH_BYTES} bytes`
  if (path.includes('\\')) return 'the path holds a backslash; separate folders with /'
  // eslint-disable-next-line no-control-regex -- the API refuses control characters too
  if (/[\x00-\x1f\x7f]/.test(path)) return 'the path holds a control character'
  if (path.startsWith('/')) return "the path must be relative to the plugin's root"
  const segments = path.split('/')
  for (const segment of segments) {
    if (segment === '') return 'the path has an empty segment'
    if (segment.startsWith('.')) return `"${segment}" starts with "."; no ".", "..", hidden files or folders`
    if (utf8Bytes(segment) > MAX_SEGMENT_BYTES) return `a segment of the path is too long: at most ${MAX_SEGMENT_BYTES} bytes`
  }
  if (segments.length < 2 || !TOP_LEVEL.includes(segments[0] ?? '')) return 'a file must be under skills/, agents/ or commands/'
  return undefined
}

const filePath = z
  .string()
  .min(1)
  .max(MAX_PATH_BYTES)
  .superRefine((path, check) => {
    const problem = draftPathProblem(path)
    if (problem) check.addIssue({ code: 'custom', message: problem })
  })
  .describe('The path in the draft, under skills/, agents/ or commands/ (for example skills/my-skill/SKILL.md)')
const tag = z.string().regex(/^[a-z0-9][a-z0-9-]{0,31}$/)
const category = z.string().regex(/^[a-z0-9][a-z0-9-]{0,63}$/)

const SOURCE = 'a hosted plugin draft, written by a person or the assistant in ScadBuddy'
const NOT_HERE = 'publishing needs the package installer, which this agent process does not have; nothing was published'

const plain = (p: HostedPlugin) => ({
  name: p.name,
  description: p.description,
  category: p.category,
  tags: p.tags,
  latest_version: p.latest_version,
  file_count: p.file_count,
  total_bytes: p.total_bytes,
  updated_at: p.updated_at,
})

const pathParams = (plugin: string, path: string) => ({ params: { path: { name: plugin, path } } })

/** The package's pin and pending re-pin, as the model needs them after an offer. */
function packageState(view: PackageView) {
  return {
    version: view.version,
    approved: view.approved,
    enabled: view.enabled,
    pending_version: view.pending?.version ?? null,
    not_offered: view.not_offered,
  }
}

/** The backend's refusal of a publish, with its `reason` and every `problems` line, or undefined for a 2xx. */
function refusalOf(error: unknown): { reason?: string; detail?: string; problems?: string[] } {
  if (typeof error !== 'object' || error === null) return {}
  const body = error as { reason?: unknown; detail?: unknown; problems?: unknown }
  return {
    ...(typeof body.reason === 'string' ? { reason: body.reason } : {}),
    ...(typeof body.detail === 'string' ? { detail: body.detail } : {}),
    ...(Array.isArray(body.problems) ? { problems: body.problems.filter((p): p is string => typeof p === 'string') } : {}),
  }
}

async function readPlugin(plugin: string, { backend }: ToolContext): Promise<HostedPlugin> {
  return ok(backend.GET('/api/v1/hosted-plugins/{name}', { params: { path: { name: plugin } } }), `read hosted plugin ${plugin}`)
}

async function draftDiff(plugin: string, { backend }: ToolContext) {
  return ok(backend.GET('/api/v1/hosted-plugins/{name}/draft-diff', { params: { path: { name: plugin } } }), `compare hosted plugin ${plugin}'s draft`)
}

function diffLine(diff: { added: string[]; removed: string[]; changed: string[]; manifest_changed: boolean }): string {
  const parts = [
    diff.added.length > 0 ? `${diff.added.length} added` : '',
    diff.changed.length > 0 ? `${diff.changed.length} changed` : '',
    diff.removed.length > 0 ? `${diff.removed.length} removed` : '',
    diff.manifest_changed ? 'a new description' : '',
  ].filter(Boolean)
  return parts.length > 0 ? parts.join(', ') : 'no change'
}

export const hostedPluginTools: readonly Tool[] = [
  defineTool({
    name: 'hosted_plugin_list',
    label: 'List hosted plugins',
    description:
      'List the hosted plugins (Claude plugins written in ScadBuddy, Settings → Tools & plugins → Hosted plugins), or, ' +
      "with `name`, read one: its details, its draft's files (path, size, sha256, executable) with the caps, and its " +
      'draft against its latest published version (`draft_diff`). A plugin holds only skills/, agents/ and commands/; ' +
      'its .claude-plugin/plugin.json is generated at publish. Read a file with hosted_plugin_file_read.',
    input: z.object({ name: name.optional() }),
    risk: 'read',
    routes: [
      'GET /api/v1/hosted-plugins',
      'GET /api/v1/hosted-plugins/{name}',
      'GET /api/v1/hosted-plugins/{name}/files',
      'GET /api/v1/hosted-plugins/{name}/draft-diff',
    ],
    source: SOURCE,
    handler: async ({ name: plugin }, ctx) => {
      if (plugin === undefined) {
        const all = await ok(ctx.backend.GET('/api/v1/hosted-plugins'), 'list the hosted plugins')
        return json({ plugins: all.map(plain) })
      }
      const found = await readPlugin(plugin, ctx)
      const files = await ok(
        ctx.backend.GET('/api/v1/hosted-plugins/{name}/files', { params: { path: { name: plugin } } }),
        `list hosted plugin ${plugin}'s draft`,
      )
      return json({
        ...plain(found),
        files: files.files.map((f) => ({ path: f.path, size: f.size, sha256: f.sha256, executable: f.executable })),
        caps: { max_files: files.max_files, max_file_bytes: files.max_file_bytes, max_total_bytes: files.max_total_bytes },
        draft_diff: await draftDiff(plugin, ctx),
      })
    },
  }),
  defineTool({
    name: 'hosted_plugin_create',
    label: 'Create a hosted plugin',
    description:
      'Create a hosted plugin with an empty draft. Its name is also its package name: 409 `package-name-taken` when an ' +
      'installed or built-in package has it, 409 `hosted-plugin-exists` when a hosted plugin does. Then write its files ' +
      'with hosted_plugin_file_write and publish it with hosted_plugin_publish.',
    input: z.object({
      name,
      description: z.string().max(1024).optional().describe('What the plugin is for; it goes into the generated plugin.json'),
      category: category.optional(),
      tags: z.array(tag).max(20).optional(),
    }),
    risk: 'write',
    routes: ['POST /api/v1/hosted-plugins'],
    summarize: ({ name: plugin }) => `Create hosted plugin ${plugin}`,
    handler: async ({ name: plugin, description, category: cat, tags }, { backend }) => {
      const made = await ok(
        backend.POST('/api/v1/hosted-plugins', {
          body: { name: plugin, description: description ?? '', category: cat ?? null, tags: tags ?? [] },
        }),
        `create hosted plugin ${plugin}`,
      )
      return json(plain(made))
    },
  }),
  defineTool({
    name: 'hosted_plugin_update',
    label: 'Change a hosted plugin',
    description:
      "Change a hosted plugin's description, category or tags (the name never changes). A field left out stays; " +
      '`category` null clears it. A new description reaches the next published version.',
    input: z.object({
      name,
      description: z.string().max(1024).optional(),
      category: category.nullable().optional(),
      tags: z.array(tag).max(20).optional(),
    }),
    risk: 'write',
    routes: ['PATCH /api/v1/hosted-plugins/{name}'],
    summarize: ({ name: plugin }) => `Change hosted plugin ${plugin}`,
    handler: async ({ name: plugin, description, category: cat, tags }, { backend }) => {
      const body = {
        ...(description === undefined ? {} : { description }),
        ...(cat === undefined ? {} : { category: cat }),
        ...(tags === undefined ? {} : { tags }),
      }
      if (Object.keys(body).length === 0) throw new ToolError('name at least one of description, category and tags')
      const changed = await ok(
        backend.PATCH('/api/v1/hosted-plugins/{name}', { params: { path: { name: plugin } }, body }),
        `change hosted plugin ${plugin}`,
      )
      return json(plain(changed))
    },
  }),
  defineTool({
    name: 'hosted_plugin_file_read',
    label: 'Read a hosted plugin file',
    description:
      "Read one file of a hosted plugin's draft as text. A file that is not UTF-8 answers its size and sha256 only.",
    input: z.object({ name, path: filePath }),
    risk: 'read',
    routes: ['GET /api/v1/hosted-plugins/{name}/files/{path}'],
    source: SOURCE,
    handler: async ({ name: plugin, path }, { backend }) => {
      const { data, error, response } = await backend.GET('/api/v1/hosted-plugins/{name}/files/{path}', {
        ...pathParams(plugin, path),
        parseAs: 'arrayBuffer',
      })
      if (!response.ok || !(data instanceof ArrayBuffer)) {
        await ok(Promise.resolve({ error, response }), `read ${path} of hosted plugin ${plugin}`)
        throw new ToolError(`read ${path} of hosted plugin ${plugin} failed: no body`)
      }
      const bytes = new Uint8Array(data)
      const executable = response.headers.get('x-scadbuddy-executable') === 'true'
      try {
        const content = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
        return json({ path, size: bytes.byteLength, executable, content })
      } catch {
        const sha256 = Buffer.from(await crypto.subtle.digest('SHA-256', bytes)).toString('hex')
        return json({ path, size: bytes.byteLength, executable, binary: true, sha256 })
      }
    },
  }),
  defineTool({
    name: 'hosted_plugin_file_write',
    label: 'Write a hosted plugin file',
    description:
      "Write one file of a hosted plugin's draft, as UTF-8 text, replacing any file at that path. Paths are relative, " +
      'under skills/, agents/ or commands/, with no .. and no segment that starts with a dot; .claude-plugin/plugin.json ' +
      'is generated at publish and refused. Each skills/<skill>/SKILL.md needs `name` and `description` frontmatter. ' +
      "`executable` marks a script under skills/. Caps: 2 MiB a file, 500 files and 20 MiB a draft. Nothing loads from " +
      'a draft: publish it with hosted_plugin_publish.',
    input: z.object({
      name,
      path: filePath,
      content: z.string().max(2 * 1024 * 1024).describe("The file's text"),
      executable: z.boolean().optional().describe('A script under skills/ that may run'),
    }),
    risk: 'write',
    routes: ['PUT /api/v1/hosted-plugins/{name}/files/{path}'],
    summarize: ({ name: plugin, path }) => `Write ${path} in hosted plugin ${plugin}`,
    handler: async ({ name: plugin, path, content, executable }, { backend }) => {
      const params = pathParams(plugin, path)
      const written = await ok(
        backend.PUT('/api/v1/hosted-plugins/{name}/files/{path}', {
          params: { ...params.params, query: { executable: executable ?? false } },
          body: content,
          bodySerializer: (text: string) => new TextEncoder().encode(text),
          headers: { 'Content-Type': 'application/octet-stream' },
        }),
        `write ${path} of hosted plugin ${plugin}`,
      )
      return json({ path: written.path, size: written.size, sha256: written.sha256, executable: written.executable })
    },
  }),
  defineTool({
    name: 'hosted_plugin_file_delete',
    label: 'Delete a hosted plugin file',
    description: "Delete one file of a hosted plugin's draft. Published versions keep their files.",
    input: z.object({ name, path: filePath }),
    risk: 'write',
    routes: ['DELETE /api/v1/hosted-plugins/{name}/files/{path}'],
    summarize: ({ name: plugin, path }) => `Delete ${path} from hosted plugin ${plugin}`,
    handler: async ({ name: plugin, path }, { backend }) => {
      await ok(backend.DELETE('/api/v1/hosted-plugins/{name}/files/{path}', pathParams(plugin, path)), `delete ${path} of hosted plugin ${plugin}`)
      return json({ deleted: path })
    },
  }),
  defineTool({
    name: 'hosted_plugin_publish',
    label: 'Publish a hosted plugin',
    description:
      "Publish a hosted plugin's draft as its next numbered version, then offer that version to the assistant: an " +
      'install when no package has its name, else a re-pin into the pending pin. A person approves this call first, and ' +
      'nothing loads until a person also approves the version in Settings → Tools & plugins. The draft is checked: 422 ' +
      '`draft-invalid` lists every problem. A draft equal to the latest version publishes nothing, and that version is ' +
      'offered when no pin names it yet (so a call again after a failed offer offers it).',
    input: z.object({ name }),
    risk: 'outward',
    routes: ['POST /api/v1/hosted-plugins/{name}/publish'],
    summarize: ({ name: plugin }) => `Publish hosted plugin ${plugin} and offer it to the assistant`,
    prepareApproval: async (args, ctx, needs) => {
      if (!needs.summary) return { input: args, summary: `Publish hosted plugin ${args.name} and offer it to the assistant` }
      const found = await readPlugin(args.name, ctx)
      const diff = await draftDiff(args.name, ctx)
      const changes = diffLine(diff)
      // A draft equal to the latest version publishes nothing: the run offers that version.
      if (changes === 'no change' && found.latest_version !== null && found.latest_version !== undefined) {
        return { input: args, summary: `Offer version ${found.latest_version} of hosted plugin ${args.name} to the assistant (the draft has no change)` }
      }
      const next = (found.latest_version ?? 0) + 1
      return {
        input: args,
        summary: `Publish hosted plugin ${args.name} as version ${next} (${changes}) and offer it to the assistant`,
      }
    },
    handler: async ({ name: plugin }, ctx) => {
      const offers = ctx.hostedPackages
      if (!offers) throw new ToolError(NOT_HERE)
      const { data, error, response } = await ctx.backend.POST('/api/v1/hosted-plugins/{name}/publish', {
        params: { path: { name: plugin } },
      })
      const refused = refusalOf(error)
      let published: number | null = null
      if (response.ok && data) {
        published = data.version
      } else if (!(response.status === 409 && refused.reason === 'nothing-to-publish')) {
        const why = [refused.detail, ...(refused.problems ?? []).map((p) => `- ${p}`)].filter(Boolean).join('\n')
        throw new ToolError(`publish hosted plugin ${plugin} failed (HTTP ${response.status})`, response.status, why || undefined, refused.reason)
      }
      const current = await offers.get(plugin)
      if (published === null && current && current.not_offered === null) {
        return json({
          name: plugin,
          published: null,
          note: 'The draft is the same as the latest version, which a pin or pending pin already names: nothing to offer.',
          package: packageState(current),
        })
      }
      let offered: PackageView
      try {
        offered = await offers.offer(plugin, ctx.signal)
      } catch (err) {
        // The version stands; a call again publishes nothing and offers it (§6.3 step 5).
        const what = published === null ? 'the latest version is published, but' : `version ${published} is published, but`
        const status = (err as { status?: number }).status
        // A refusal fails again on a retry, and each retry costs a person's approval.
        const refusal =
          err instanceof PackageRefusedError || err instanceof BuiltInPluginError || status === 400 || status === 409 || status === 422
        const advice = refusal
          ? 'the package was refused, so a call again fails the same way: fix the draft (or, for a name another package has, ' +
            'tell the person) before you publish again'
          : 'call hosted_plugin_publish again, or use Offer in Settings → Tools & plugins → Hosted plugins'
        throw new ToolError(`${what} offering it to the assistant failed; ${advice}`, status, (err as Error).message)
      }
      return json({
        name: plugin,
        published,
        offered: current ? 're-pinned' : 'installed',
        package: packageState(offered),
        next: 'A person approves the version in Settings → Tools & plugins before it loads.',
      })
    },
  }),
]
