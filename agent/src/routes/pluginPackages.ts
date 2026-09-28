import type { Context, Hono } from 'hono'
import { z } from 'zod'
import { EgressError } from '../http/egress.js'
import type { OriginPolicy } from '../http/origins.js'
import { PackageRefusedError, type PackageInstaller } from '../plugins/packages/install.js'
import { validateRef, validateSource } from '../plugins/packages/source.js'
import type { PackageRepo } from '../plugins/packages/store.js'
import { PluginError } from '../plugins/registry.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'

// /api/v1/ai/plugin-packages (issue #297, "Installing a plugin"): Claude plugin
// packages (skills, subagents, hooks, .mcp.json) from a git URL or a
// marketplace entry, pinned to a commit in Postgres (plugins/packages/).
//
//   GET    /api/v1/ai/plugin-packages                  list
//   GET    /api/v1/ai/plugin-packages/:name            one, with its review and any pending re-pin + diff
//   POST   /api/v1/ai/plugin-packages                  install: fetch, pin, vet → stored UNAPPROVED (201)
//   POST   /api/v1/ai/plugin-packages/:name/approve    approve { commit_sha, content_hash } as reviewed
//   PATCH  /api/v1/ai/plugin-packages/:name            { enabled } (an approved pin only)
//   POST   /api/v1/ai/plugin-packages/:name/repin      fetch { ref } → pending pin + file diff
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

export type PackageRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  packages: PackageRepo | undefined
  installer: Pick<PackageInstaller, 'prepare' | 'evict'> | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
  /** Fetches at once across all packages; more get a 429. */
  maxConcurrentFetches?: number
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
  const maxFetches = deps.maxConcurrentFetches ?? 2
  let fetching = 0
  const busy = new Set<string>()

  async function store(): Promise<PackageRepo | string> {
    if (!deps.packages) return NO_DATABASE
    return (await deps.ready()) ? deps.packages : NOT_READY
  }

  function refusal(c: Context, err: unknown) {
    if (err instanceof PackageRefusedError) {
      return c.json({ detail: 'the plugin package is refused', problems: err.problems }, 422)
    }
    if (err instanceof PluginError) return c.json({ detail: err.message }, err.status)
    if (err instanceof EgressError) return c.json({ detail: err.message }, 400)
    throw err
  }

  /** Runs a fetch under the concurrency cap, one per package name. */
  async function withFetch<T>(c: Context, key: string, work: () => Promise<T>): Promise<T | Response> {
    if (!deps.installer) return c.json({ detail: 'plugin packages are not available: no package cache' }, 503)
    if (busy.has(key)) return c.json({ detail: `a fetch for ${key} is already running` }, 429)
    if (fetching >= maxFetches) return c.json({ detail: 'too many plugin fetches are running; try again' }, 429)
    busy.add(key)
    fetching++
    try {
      return await work()
    } finally {
      busy.delete(key)
      fetching--
    }
  }

  app.on(['POST', 'PATCH', 'PUT', 'DELETE'], [base, `${base}/*`], async (c, next) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, {
      subject: 'plugin package changes',
      jsonMethods: ['PUT', 'PATCH', 'POST'],
    })
    if (problem) return c.json({ detail: problem }, 403)
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
    return c.json(await repo.list())
  })

  app.get(`${base}/:name`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const found = await repo.get(c.req.param('name'))
    if (!found) return c.json({ detail: `no plugin package named "${c.req.param('name')}"` }, 404)
    return c.json(found)
  })

  app.post(base, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const body = await parseBody(c, InstallBody)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    try {
      const source = validateSource(body.source)
      const result = await withFetch(c, `${source.url}#${source.kind === 'git' ? source.path : source.entry}`, async () => {
        const prepared = await deps.installer!.prepare(source)
        return repo.create(prepared)
      })
      return result instanceof Response ? result : c.json(result, 201)
    } catch (err) {
      return refusal(c, err)
    }
  })

  app.post(`${base}/:name/approve`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const body = await parseBody(c, ApproveBody)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    try {
      return c.json(await repo.approve(c.req.param('name'), body.commit_sha, body.content_hash))
    } catch (err) {
      return refusal(c, err)
    }
  })

  app.patch(`${base}/:name`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const body = await parseBody(c, PatchBody)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    try {
      return c.json(await repo.setEnabled(c.req.param('name'), body.enabled))
    } catch (err) {
      return refusal(c, err)
    }
  })

  app.post(`${base}/:name/repin`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const body = await parseBody(c, RepinBody)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    const name = c.req.param('name')
    try {
      const current = await repo.pinOf(name)
      if (!current) return c.json({ detail: `no plugin package named "${name}"` }, 404)
      const ref = validateRef(body.ref ?? current.source.ref)
      const result = await withFetch(c, name, async () => {
        const prepared = await deps.installer!.prepare({ ...current.source, ref })
        return repo.setPending(name, prepared)
      })
      return result instanceof Response ? result : c.json(result)
    } catch (err) {
      return refusal(c, err)
    }
  })

  app.delete(`${base}/:name/pending`, async (c) => {
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
    if (!(await repo.delete(name))) return c.json({ detail: `no plugin package named "${name}"` }, 404)
    await deps.installer?.evict(name).catch(() => undefined)
    return c.body(null, 204)
  })
}
