import type { Context, Hono } from 'hono'
import { z } from 'zod'
import { EgressError } from '../http/egress.js'
import type { OriginPolicy } from '../http/origins.js'
import type { Commands } from '../operations/run.js'
import { BuiltInPluginError, PackageRefusedError, type PackageInstaller } from '../plugins/packages/install.js'
import {
  BuiltInPackages,
  type BuiltInPackageView,
  builtInFiles,
  builtInNamed,
  readBuiltInFile,
} from '../plugins/packages/builtins.js'
import { fileContent } from '../plugins/packages/files.js'
import { INSTALL_KIND, REPIN_KIND } from '../plugins/packages/operations.js'
import { UI_ACTOR } from '../audit/writes.js'
import type { PackageRepo } from '../plugins/packages/store.js'
import { type PackageSource, validateRef, validateSource } from '../plugins/packages/source.js'
import { PluginError } from '../plugins/registry.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'
import { commandResponse, NO_COMMANDS } from './operations.js'

// /api/v1/ai/plugin-packages (issue #297, "Installing a plugin"): Claude plugin
// packages (skills, subagents, hooks, .mcp.json) from a git URL or a
// marketplace entry, pinned to a commit in Postgres (plugins/packages/).
//
//   GET    /api/v1/ai/plugin-packages                  list
//   GET    /api/v1/ai/plugin-packages/:name            one, with its review and any pending re-pin + diff
//   GET    /api/v1/ai/plugin-packages/:name/files      { files: [{ path, size }] } (?pending=true: the re-pin's)
//   GET    /api/v1/ai/plugin-packages/:name/file       ?path=…[&pending=true][&full=true]: one file's content (files.ts)
//   POST   /api/v1/ai/plugin-packages                  install: fetch, pin, vet → stored UNAPPROVED (201), a command
//   POST   /api/v1/ai/plugin-packages/:name/approve    approve { commit_sha, content_hash, allow_refused? } as reviewed
//   PATCH  /api/v1/ai/plugin-packages/:name            { enabled } (an approved pin only)
//   POST   /api/v1/ai/plugin-packages/:name/repin      fetch { ref } → pending pin + file diff, a command
//   DELETE /api/v1/ai/plugin-packages/:name/pending    drop the pending re-pin
//   DELETE /api/v1/ai/plugin-packages/:name            uninstall (and evict the cache)
//
// Installing is an outward settings write (spec §8.1) and needs a human
// approval in the UI (§8.2). Two steps give it one: the install (or re-pin)
// only fetches, vets and stores the pin with its review, and nothing loads
// until the admin approves that exact commit and content hash, which the
// review showed them. Every write, and the fetches, go through the UI guard
// the other Settings writes use (guard.ts: HTTPS through a trusted proxy or
// loopback, the UI's origin, JSON bodies).
//
// A package whose review lists refusals (vet.ts `refused`) installs, and is
// approved only with `allow_refused: true`, the admin's decision to load it
// as it is. Fatal problems (vet.ts `fatal`, egress, symlinks) still refuse
// the install with a 422. ScadBuddy's own plugin is answered 409 with
// `built_in: true`: the harness already loads it (install.ts BuiltInPluginError).
//
// The built-in plugins (builtins.ts: ScadBuddy's own and the headless browser's)
// are listed first, with `built_in: true`. PATCH turns one on or off (its
// setting); approve, re-pin and DELETE answer 409, since it ships with the agent,
// and an install that names one answers 409 with `built_in: true`. A package
// stored under a built-in's name before then is never loaded (install.ts
// `loadPackagesForRun`) and DELETE removes it.
//
// The two fetches are commands (spec 2026-10-01 §4.2, #1055): AgentOperation runs them
// on `agent-tools` (plugins/packages/operations.ts), keyed by the client's
// `Idempotency-Key`, so a re-send after a lost answer fetches nothing twice. Within the
// deadline they answer as before; past it, 202 with the operation to follow at
// GET /api/v1/ai/operations/{id}.

export type PackageRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  packages: PackageRepo | undefined
  /** The plugins that ship with the agent (builtins.ts), listed first; undefined without the database. */
  builtIns: BuiltInPackages | undefined
  installer: Pick<PackageInstaller, 'prepare' | 'evict' | 'readFile'> | undefined
  /** Install and re-pin run here; undefined without Temporal (503). */
  commands: Commands | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

const Source = z.discriminatedUnion('kind', [
  z.strictObject({
    kind: z.literal('git'),
    url: z.string().max(2048),
    ref: z.string().max(200).optional(),
    path: z.string().max(512).optional(),
  }),
  z.strictObject({
    kind: z.literal('marketplace'),
    url: z.string().max(2048),
    ref: z.string().max(200).optional(),
    entry: z.string().max(64),
  }),
])
const InstallBody = z.strictObject({ source: Source })
const ApproveBody = z.strictObject({
  commit_sha: z.string().max(64),
  content_hash: z.string().max(80),
  /** Load the pin despite what its review's `refused` lists (store.ts). */
  allow_refused: z.boolean().optional(),
})
const PatchBody = z.strictObject({ enabled: z.boolean() })
const RepinBody = z.strictObject({ ref: z.string().max(200).optional() })

async function parseBody<T extends z.ZodType>(c: Context, schema: T): Promise<z.infer<T> | string> {
  try {
    return schema.parse(await c.req.json())
  } catch (err) {
    return err instanceof z.ZodError
      ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
      : 'body is not valid JSON'
  }
}

export function registerPluginPackageRoutes(app: Hono, deps: PackageRouteDeps): void {
  const base = '/api/v1/ai/plugin-packages'
  async function store(): Promise<PackageRepo | string> {
    if (!deps.packages) return NO_DATABASE
    return (await deps.ready()) ? deps.packages : NOT_READY
  }

  function refusal(c: Context, err: unknown) {
    if (err instanceof BuiltInPluginError) return c.json({ detail: err.message, built_in: true }, 409)
    if (err instanceof PackageRefusedError) {
      return c.json({ detail: 'the plugin package is refused', problems: err.problems }, 422)
    }
    if (err instanceof PluginError) return c.json({ detail: err.message }, err.status)
    if (err instanceof EgressError) return c.json({ detail: err.message }, 400)
    throw err
  }

  /** Why a fetch cannot start here, before any command. */
  function cannotFetch(c: Context): Response | undefined {
    if (!deps.installer) return c.json({ detail: 'plugin packages are not available: no package cache' }, 503)
    if (!deps.commands) return c.json({ detail: NO_COMMANDS }, 503)
    return undefined
  }

  /** The built-in plugin `name` names, or undefined; a store that is not ready says so. */
  async function builtIn(name: string): Promise<BuiltInPackageView | string | undefined> {
    if (!builtInNamed(name)) return undefined
    const repo = await store()
    if (typeof repo === 'string') return repo
    return deps.builtIns?.get(name)
  }
  const isBuiltIn = (c: Context, what: string): Response | undefined => {
    const name = c.req.param('name') ?? ''
    if (!builtInNamed(name)) return undefined
    return c.json({ detail: `"${name}" is built in: it cannot be ${what}. Disable it instead.`, built_in: true }, 409)
  }

  const key = (c: Context): string | undefined => c.req.header('idempotency-key')?.slice(0, 128) || undefined

  app.on(['POST', 'PATCH', 'PUT', 'DELETE'], [base, `${base}/*`], async (c, next) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'plugin package changes')
    if (problem) return c.json({ detail: problem }, 403)
    // guard.ts checks the body type of PUT only; every POST and PATCH here
    // reads a JSON body, so a cross-origin HTML form (which cannot send JSON
    // without a preflight) is refused as well.
    const readsBody = c.req.method === 'POST' || c.req.method === 'PATCH'
    const type = c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase()
    if (readsBody && type !== 'application/json') return c.json({ detail: 'request body must be application/json' }, 403)
    await next()
  })

  app.on('GET', [base, `${base}/*`], async (c, next) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'plugin package reads')
    if (problem) return c.json({ detail: problem }, 403)
    await next()
  })

  app.get(base, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    return c.json([...((await deps.builtIns?.list()) ?? []), ...(await repo.list())])
  })

  app.get(`${base}/:name`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const builtin = await builtIn(c.req.param('name'))
    if (builtin) return typeof builtin === 'string' ? c.json({ detail: builtin }, 503) : c.json(builtin)
    const found = await repo.get(c.req.param('name'))
    if (!found) return c.json({ detail: `no plugin package named "${c.req.param('name')}"` }, 404)
    return c.json(found)
  })

  // A package's files for review (#1029). Only a path in the pin's own file list
  // is read, from the copy that hashes to the pin (install.ts `readFile`), so a
  // request never names a path of its own on disk.
  const truthy = (c: Context, name: string) => c.req.query(name) === 'true'

  app.get(`${base}/:name/files`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const name = c.req.param('name')
    const pending = truthy(c, 'pending')
    if (builtInNamed(name)) {
      if (pending) return c.json({ detail: `"${name}" is built in: it has no re-pin` }, 404)
      return c.json({ files: builtInFiles(name) })
    }
    const found = await repo.filesOf(name, pending)
    if (!found) return c.json({ detail: `no ${pending ? 'pending re-pin of a ' : ''}plugin package named "${name}"` }, 404)
    // Sorted here: jsonb keeps an object's keys in its own order.
    const paths = Object.keys(found.files).sort()
    return c.json({ files: paths.map((path) => ({ path, size: found.files[path]!.size })) })
  })

  app.get(`${base}/:name/file`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const name = c.req.param('name')
    const rel = c.req.query('path') ?? ''
    const pending = truthy(c, 'pending')
    const full = truthy(c, 'full')
    const notFound = () => c.json({ detail: `"${name}" has no file "${rel}"${pending ? ' in its pending re-pin' : ''}` }, 404)
    if (builtInNamed(name)) {
      const bytes = pending ? undefined : readBuiltInFile(name, rel)
      return bytes ? c.json(fileContent(rel, bytes, full)) : notFound()
    }
    const found = await repo.filesOf(name, pending)
    if (!found || !Object.hasOwn(found.files, rel)) return notFound()
    if (!deps.installer) return c.json({ detail: 'plugin packages are not available: no package cache' }, 503)
    try {
      return c.json(fileContent(rel, await deps.installer.readFile(found.pin, rel), full))
    } catch (err) {
      return refusal(c, err)
    }
  })

  app.post(base, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const body = await parseBody(c, InstallBody)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    // Validated here, before anything crosses: the request is the command's workflow
    // input, in Temporal history, and a URL with a token in it is refused (source.ts).
    let source: PackageSource
    try {
      source = validateSource(body.source)
    } catch (err) {
      return refusal(c, err)
    }
    const blocked = cannotFetch(c)
    if (blocked) return blocked
    return commandResponse(c, await deps.commands!.run(INSTALL_KIND, { source }, key(c)), 201)
  })

  app.post(`${base}/:name/approve`, async (c) => {
    const refused = isBuiltIn(c, 'approved: it ships with the agent, so there is no pin')
    if (refused) return refused
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const body = await parseBody(c, ApproveBody)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    try {
      return c.json(await repo.approve(c.req.param('name'), body.commit_sha, body.content_hash, body.allow_refused ?? false))
    } catch (err) {
      return refusal(c, err)
    }
  })

  app.patch(`${base}/:name`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const body = await parseBody(c, PatchBody)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    // Always the built-in: a package stored under its name never loads, so it has no switch.
    if (builtInNamed(c.req.param('name'))) {
      if (!deps.builtIns) return c.json({ detail: NO_DATABASE }, 503)
      const context = { actor: UI_ACTOR, surface: 'http' as const, clientIp: deps.remoteAddress(c) }
      return c.json(await deps.builtIns.setEnabled(c.req.param('name'), body.enabled, context))
    }
    try {
      return c.json(await repo.setEnabled(c.req.param('name'), body.enabled))
    } catch (err) {
      return refusal(c, err)
    }
  })

  app.post(`${base}/:name/repin`, async (c) => {
    const refused = isBuiltIn(c, 're-pinned: it ships with the agent')
    if (refused) return refused
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const body = await parseBody(c, RepinBody)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    let ref: string | undefined
    try {
      ref = body.ref === undefined ? undefined : validateRef(body.ref)
    } catch (err) {
      return refusal(c, err)
    }
    const blocked = cannotFetch(c)
    if (blocked) return blocked
    const request = { name: c.req.param('name'), ...(ref === undefined ? {} : { ref }) }
    return commandResponse(c, await deps.commands!.run(REPIN_KIND, request, key(c)), 200)
  })

  app.delete(`${base}/:name/pending`, async (c) => {
    const refused = isBuiltIn(c, 'given a re-pin to discard: it ships with the agent')
    if (refused) return refused
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    try {
      return c.json(await repo.discardPending(c.req.param('name')))
    } catch (err) {
      return refusal(c, err)
    }
  })

  app.delete(`${base}/:name`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const name = c.req.param('name')
    // An installed package under a built-in's name (stored before builtins.ts) is
    // removed; the built-in itself never is.
    if (builtInNamed(name) && !(await repo.get(name))) return isBuiltIn(c, 'removed')!
    if (!(await repo.delete(name))) return c.json({ detail: `no plugin package named "${name}"` }, 404)
    await deps.installer?.evict(name).catch(() => undefined)
    return c.body(null, 204)
  })
}

declare module '../app.js' {
  interface AppDeps {
    /** Installed plugin packages (#297, plugins/packages/); undefined when there is no database. */
    pluginPackages?: PackageRepo | undefined
    /** Fetches and caches plugin packages; undefined disables installing. */
    packageInstaller?: Pick<PackageInstaller, 'prepare' | 'evict' | 'readFile'> | undefined
  }
}

/** The plugin package routes (#297). */
export const route: RouteModule = {
  register(app, deps) {
    registerPluginPackageRoutes(app, {
      packages: deps.pluginPackages,
      builtIns: deps.settings ? new BuiltInPackages(deps.settings) : undefined,
      installer: deps.packageInstaller,
      commands: deps.commands,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
