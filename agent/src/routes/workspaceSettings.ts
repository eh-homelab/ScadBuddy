import type { Hono } from 'hono'
import { z } from 'zod'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import {
  DEFAULT_FILE_MAX_BYTES,
  DEFAULT_WORKSPACE_MAX_BYTES,
  WORKSPACE_FILE_MAX_SETTING,
  WORKSPACE_MAX_SETTING,
} from '../workspace/store.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from './httpRequest.js'
import type { SettingsWriter } from './mcpAuthMode.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/workspace (#2329): the caps on the assistant's workspace
// (workspace/store.ts), in bytes. `SessionFiles` reads them at every write, so
// a change applies to the next one; files already stored stay.
//
//   GET /api/v1/ai/settings/workspace   { file_max_bytes, workspace_max_bytes, min, file_max, workspace_max }
//                                       (the defaults, 25 MiB and 200 MiB, when unset)
//   PUT /api/v1/ai/settings/workspace   body { file_max_bytes, workspace_max_bytes }: integers, the
//                                       file cap no larger than the workspace's
//
// Bounds: 1 MiB at least for either. A file holds at most 100 MiB (every byte
// of an upload or a read is held in memory once, and a row is one bytea), a
// workspace at most 2 GiB. Guarded and audited like the other AI settings
// routes (routes/imageSettings.ts): the write passes `uiRequestProblem` and is a
// `settings` audit row per key, the read passes `uiReadProblem`. Both keys are
// written in one transaction (#2469), so no reader sees one cap without the other.

export const WORKSPACE_SETTINGS_PATH = '/api/v1/ai/settings/workspace'
export const MIN_WORKSPACE_BYTES = 1024 * 1024
export const MAX_FILE_BYTES = 100 * 1024 * 1024
export const MAX_WORKSPACE_BYTES = 2 * 1024 * 1024 * 1024

export type WorkspaceSettingsRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
  /** Writes both caps at once; undefined when there is no database. */
  writer: SettingsWriter | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type WorkspaceSettingsView = {
  file_max_bytes: number
  workspace_max_bytes: number
  min: number
  file_max: number
  workspace_max: number
}

const FileMax = z.number().int().min(MIN_WORKSPACE_BYTES).max(MAX_FILE_BYTES)
const WorkspaceMax = z.number().int().min(MIN_WORKSPACE_BYTES).max(MAX_WORKSPACE_BYTES)
const PutBody = z
  .strictObject({ file_max_bytes: FileMax, workspace_max_bytes: WorkspaceMax })
  .refine((b) => b.file_max_bytes <= b.workspace_max_bytes, {
    message: 'file_max_bytes may not be larger than workspace_max_bytes',
  })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'assistant workspace settings'

/**
 * The caps as stored, each the default for anything the PUT could not have
 * written. The store (`SessionFiles.limits`) takes any positive integer, so a
 * value written some other way still applies there; this shows what the
 * form can save.
 */
export function workspaceSettings(file: unknown, total: unknown): WorkspaceSettingsView {
  const f = FileMax.safeParse(file)
  const w = WorkspaceMax.safeParse(total)
  return {
    file_max_bytes: f.success ? f.data : DEFAULT_FILE_MAX_BYTES,
    workspace_max_bytes: w.success ? w.data : DEFAULT_WORKSPACE_MAX_BYTES,
    min: MIN_WORKSPACE_BYTES,
    file_max: MAX_FILE_BYTES,
    workspace_max: MAX_WORKSPACE_BYTES,
  }
}

export function registerWorkspaceSettingsRoutes(app: Hono, deps: WorkspaceSettingsRouteDeps): void {
  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }
  async function writer(): Promise<SettingsWriter | string> {
    if (!deps.writer) return NO_DATABASE
    return (await deps.ready()) ? deps.writer : NOT_READY
  }

  app.get(WORKSPACE_SETTINGS_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    const [file, total] = await Promise.all([
      settings.get<unknown>(WORKSPACE_FILE_MAX_SETTING),
      settings.get<unknown>(WORKSPACE_MAX_SETTING),
    ])
    return c.json(workspaceSettings(file, total))
  })

  app.put(WORKSPACE_SETTINGS_PATH, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await writer()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    let body: z.infer<typeof PutBody>
    try {
      body = PutBody.parse(await c.req.json())
    } catch (err) {
      const detail =
        err instanceof z.ZodError
          ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
          : 'body is not valid JSON'
      return c.json({ detail }, 400)
    }
    const context = { actor: UI_ACTOR, surface: 'http' as const, clientIp: deps.remoteAddress(c) }
    await settings.setMany(
      { [WORKSPACE_FILE_MAX_SETTING]: body.file_max_bytes, [WORKSPACE_MAX_SETTING]: body.workspace_max_bytes },
      () => Promise.resolve(true),
      context,
    )
    return c.json(workspaceSettings(body.file_max_bytes, body.workspace_max_bytes))
  })
}

/** The workspace caps. `deps.settings` is declared in routes/httpRequest.ts, `deps.aiSettings` in routes/mcpAuthMode.ts. */
export const route: RouteModule = {
  register(app, deps) {
    registerWorkspaceSettingsRoutes(app, {
      settings: deps.settings,
      writer: deps.database ? deps.aiSettings : undefined,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
