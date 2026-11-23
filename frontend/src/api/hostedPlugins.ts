/**
 * #6937 — the hosted plugin editor's client for `/api/v1/hosted-plugins/*`
 * (`backend/scadbuddy/api/hosted_plugins.py`, spec 2026-11-21 §6.2 and §6.3), typed from
 * the generated `schema.d.ts`. A draft file's bytes go up as the raw body of a `PUT` and come
 * back as `application/octet-stream`, never inside JSON (#2479).
 */
import { ApiError, request, requestRaw } from './client'
import type { components } from './schema'

type Schemas = components['schemas']

export type HostedPlugin = Schemas['HostedPlugin']
export type HostedPluginCreate = Schemas['HostedPluginCreate']
export type HostedPluginUpdate = Schemas['HostedPluginUpdate']
export type HostedPluginFile = Schemas['HostedPluginFile']
export type HostedPluginFiles = Required<Schemas['HostedPluginFiles']>
export type HostedPluginDraftDiff = Schemas['HostedPluginDraftDiff']
export type HostedPluginVersion = Schemas['HostedPluginVersion']

/** The folders a draft file may sit under (`hosted_plugins/rules.py` `TOP_LEVEL`). */
export const TOP_LEVEL = ['skills', 'agents', 'commands'] as const

/** One draft file read back: its bytes and whether it is executable. */
export interface DraftFileBytes {
  bytes: Uint8Array<ArrayBuffer>
  executable: boolean
}

/** The `problems` a refused publish (422 `draft-invalid`) lists, or none. */
export function draftProblems(error: unknown): string[] {
  if (!(error instanceof ApiError)) return []
  const problems = error.problem.problems
  return Array.isArray(problems) ? problems.filter((p): p is string => typeof p === 'string') : []
}

/** The route's `reason` for a refusal (`path-refused`, `nothing-to-publish` …), or null. */
export function refusalReason(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  const reason = error.problem.reason
  return typeof reason === 'string' ? reason : null
}

const seg = encodeURIComponent
/** A draft path in the URL: each segment encoded, the slashes kept (the route takes `{path:path}`). */
const filePath = (path: string) => path.split('/').map(seg).join('/')
const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })

export const hostedPlugins = {
  list: () => request<HostedPlugin[]>('/hosted-plugins'),
  get: (name: string) => request<HostedPlugin>(`/hosted-plugins/${seg(name)}`),
  create: (body: HostedPluginCreate) => request<HostedPlugin>('/hosted-plugins', json('POST', body)),
  update: (name: string, body: HostedPluginUpdate) => request<HostedPlugin>(`/hosted-plugins/${seg(name)}`, json('PATCH', body)),
  remove: (name: string) => request<void>(`/hosted-plugins/${seg(name)}`, { method: 'DELETE' }),

  files: (name: string) => request<HostedPluginFiles>(`/hosted-plugins/${seg(name)}/files`),
  readFile: async (name: string, path: string): Promise<DraftFileBytes> => {
    const response = await requestRaw(`/hosted-plugins/${seg(name)}/files/${filePath(path)}`, {
      headers: { Accept: 'application/octet-stream' },
    })
    return {
      bytes: new Uint8Array(await response.arrayBuffer()),
      executable: response.headers.get('X-ScadBuddy-Executable') === 'true',
    }
  },
  writeFile: (name: string, path: string, data: BlobPart, executable = false) =>
    request<HostedPluginFile>(`/hosted-plugins/${seg(name)}/files/${filePath(path)}${executable ? '?executable=true' : ''}`, {
      method: 'PUT',
      body: new Blob([data], { type: 'application/octet-stream' }),
    }),
  deleteFile: (name: string, path: string) =>
    request<void>(`/hosted-plugins/${seg(name)}/files/${filePath(path)}`, { method: 'DELETE' }),
  moveFile: (name: string, from: string, to: string) =>
    request<HostedPluginFile>(`/hosted-plugins/${seg(name)}/files/moves`, json('POST', { from, to })),

  draftDiff: (name: string) => request<HostedPluginDraftDiff>(`/hosted-plugins/${seg(name)}/draft-diff`),
  publish: (name: string) => request<HostedPluginVersion>(`/hosted-plugins/${seg(name)}/publish`, { method: 'POST' }),
  versions: (name: string) => request<HostedPluginVersion[]>(`/hosted-plugins/${seg(name)}/versions`),
}
