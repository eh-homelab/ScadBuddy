import { CrudReadError } from '../../api/crudRead.js'
import { type CrudCallInit, type CrudTarget, crudOf, qs, segment } from '../../api/crudStores.js'
import { PluginError } from '../registry.js'
import { diffFiles, type FileDiff, type FileList } from './hash.js'
import type { PackageSource, SourceKind } from './source.js'
import type { PackageReview } from './vet.js'

// `ai_plugin_packages` (backend/scadbuddy/migrations_agent/20260928T0750Z_plugin_packages.sql,
// 20261122T0323Z_plugin_package_pins.sql): the one authoritative record of an installed
// plugin package. What is on disk (install.ts) is rebuilt from the pin, and used only when
// it hashes to `content_hash`: a commit pin (`pin_kind` `commit`) from `fetch_url`,
// `fetch_path` and `commit_sha`; a bytes pin (`bytes`, #6935: an archive or npm package)
// from the mirrored file `bytes_sha256` (mirror.ts) and `fetch_path` inside it, never
// from upstream.
//
// LIFECYCLE (issue #297 "Review before enable"; spec §8.2, installing is a
// settings write and outward):
//   install   fetched, vetted and hashed; stored UNAPPROVED and disabled
//   approve   the admin approves exactly the (commit or bytes, hash) the review showed;
//             anything else is a 409, so a pin cannot change under an approval
//             A pin whose review lists refusals (vet.ts `refused`: a command
//             hook, a hooks module, a stdio MCP server, ...) is approved only
//             with `allow_refused`, which loads it as it is; the flag belongs
//             to that approval, so a re-pin is approved, and allowed, anew
//   enable    only an approved pin (also a CHECK in the table)
//   repin     a new commit is fetched and vetted into `pending_*`; the current
//             pin keeps loading until the admin approves the pending one, after
//             seeing the file diff. Never automatic ("Plugins are never
//             auto-updated").

/** A fetched, vetted and hashed package, ready to be stored as a pin. */
export type PreparedPackage = {
  source: PackageSource
  fetchUrl: string
  fetchPath: string
  /** The pinned commit; '' for a bytes pin. */
  commit: string
  /** A bytes pin's file's sha256 (mirrored before the pin is stored); absent for a commit pin. */
  bytesSha256?: string
  contentHash: string
  files: FileList
  review: PackageReview
}

export type PackagePin = {
  name: string
  fetchUrl: string
  fetchPath: string
  /** The pinned commit; '' for a bytes pin. */
  commit: string
  /** A bytes pin's file's sha256: the pin loads from its mirror (mirror.ts). */
  bytesSha256?: string
  contentHash: string
  /** Approved with `allow_refused`: loaded despite what the rules refuse (vet.ts). */
  allowRefused?: boolean
}

export type PinKind = 'commit' | 'bytes' | 'version'

export type PendingPin = {
  ref: string
  /** Where the new commit is fetched from; replaces the pin's on approval. */
  plugin_url: string
  plugin_path: string
  pin_kind: PinKind
  commit_sha: string | null
  bytes_sha256: string | null
  content_hash: string
  review: PackageReview
  diff: FileDiff
}

/** A package as routes show it. */
export type PackageView = {
  name: string
  source:
    | { kind: 'git'; url: string; ref: string; path: string }
    | { kind: 'marketplace' | 'marketplace-url'; url: string; ref: string; entry: string; plugin_url: string; plugin_path: string }
    | { kind: 'archive' | 'npm'; url: string; ref: string; plugin_path: string }
  /** `commit` (commit_sha), or `bytes` (bytes_sha256: the sha256 of the archive or tarball). */
  pin_kind: PinKind
  commit_sha: string | null
  bytes_sha256: string | null
  content_hash: string
  review: PackageReview
  approved: boolean
  approved_at: string | null
  /** Approved with what the review's `refused` lists allowed. */
  allow_refused: boolean
  enabled: boolean
  pending: PendingPin | null
  created_at: string
  updated_at: string
}

/** A row of `ai_plugin_packages`. */
export type PackageRow = {
  name: string
  source_kind: SourceKind | 'hosted'
  source_url: string
  source_ref: string
  marketplace_entry: string | null
  fetch_url: string
  fetch_path: string
  pin_kind: PinKind
  commit_sha: string | null
  bytes_sha256: string | null
  version: number | null
  content_hash: string
  files: FileList
  review: PackageReview
  approved_at: Date | null
  allow_refused: boolean
  enabled: boolean
  pending_ref: string | null
  pending_fetch_url: string | null
  pending_fetch_path: string | null
  pending_pin_kind: PinKind | null
  pending_commit_sha: string | null
  pending_bytes_sha256: string | null
  pending_version: number | null
  pending_content_hash: string | null
  pending_files: FileList | null
  pending_review: PackageReview | null
  created_at: Date
  updated_at: Date
}

/** A review stored before `refused` existed had none: it would have been refused outright. */
export function withRefused(review: PackageReview): PackageReview & { refused: string[] } {
  return { ...review, refused: review.refused ?? [] }
}

function viewSource(row: PackageRow): PackageView['source'] {
  switch (row.source_kind) {
    case 'git':
      return { kind: 'git', url: row.source_url, ref: row.source_ref, path: row.fetch_path }
    case 'marketplace':
    case 'marketplace-url':
      return {
        kind: row.source_kind,
        url: row.source_url,
        ref: row.source_ref,
        entry: row.marketplace_entry ?? '',
        plugin_url: row.fetch_url,
        plugin_path: row.fetch_path,
      }
    default:
      return { kind: row.source_kind === 'npm' ? 'npm' : 'archive', url: row.source_url, ref: row.source_ref, plugin_path: row.fetch_path }
  }
}

/** A row as routes see it. */
export function view(row: PackageRow): PackageView {
  return {
    name: row.name,
    source: viewSource(row),
    pin_kind: row.pin_kind,
    commit_sha: row.commit_sha,
    bytes_sha256: row.bytes_sha256,
    content_hash: row.content_hash,
    review: withRefused(row.review),
    approved: row.approved_at !== null,
    approved_at: row.approved_at?.toISOString() ?? null,
    allow_refused: row.allow_refused,
    enabled: row.enabled,
    pending:
      row.pending_pin_kind && row.pending_content_hash && row.pending_review && row.pending_files && row.pending_ref !== null &&
      row.pending_fetch_url !== null && row.pending_fetch_path !== null
        ? {
            ref: row.pending_ref,
            plugin_url: row.pending_fetch_url,
            plugin_path: row.pending_fetch_path,
            pin_kind: row.pending_pin_kind,
            commit_sha: row.pending_commit_sha,
            bytes_sha256: row.pending_bytes_sha256,
            content_hash: row.pending_content_hash,
            review: withRefused(row.pending_review),
            diff: diffFiles(row.files, row.pending_files),
          }
        : null,
    created_at: row.created_at.toISOString(),
    updated_at: row.updated_at.toISOString(),
  }
}

/** A row's pin, for a harness run. */
export function pin(row: PackageRow): PackagePin {
  return {
    name: row.name,
    fetchUrl: row.fetch_url,
    fetchPath: row.fetch_path,
    commit: row.commit_sha ?? '',
    ...(row.bytes_sha256 !== null ? { bytesSha256: row.bytes_sha256 } : {}),
    contentHash: row.content_hash,
    allowRefused: row.allow_refused,
  }
}

/** The package store as routes and the run loader see it. */
export type PackageRepo = {
  list(): Promise<PackageView[]>
  get(name: string): Promise<PackageView | undefined>
  /** The pin to fetch for a re-pin or a cache rebuild. */
  pinOf(name: string): Promise<(PackagePin & { source: PackageSource }) | undefined>
  create(prepared: PreparedPackage): Promise<PackageView>
  setPending(name: string, prepared: PreparedPackage): Promise<PackageView>
  discardPending(name: string): Promise<PackageView>
  /**
   * Approves the pin the review showed: `pin` is its commit, or with `kind` `bytes` its
   * file's sha256. `allowRefused` must be true when the reviewed pin's `refused` is not empty.
   */
  approve(name: string, pin: string, contentHash: string, allowRefused?: boolean, kind?: 'commit' | 'bytes'): Promise<PackageView>
  setEnabled(name: string, enabled: boolean): Promise<PackageView>
  delete(name: string): Promise<boolean>
  /** Every enabled (so approved) pin, for a harness run. */
  enabledPins(): Promise<PackagePin[]>
  /** The pin (or its pending re-pin) with its file list, for reading its files (#1029). */
  filesOf(name: string, pending: boolean): Promise<{ pin: PackagePin; files: FileList } | undefined>
}

/** Where a row's package was fetched from. */
export function sourceOf(row: PackageRow): PackageSource {
  switch (row.source_kind) {
    case 'git':
      return { kind: 'git', url: row.source_url, ref: row.source_ref, path: row.fetch_path }
    case 'marketplace':
      return { kind: 'marketplace', url: row.source_url, ref: row.source_ref, entry: row.marketplace_entry ?? '' }
    case 'marketplace-url':
      return { kind: 'marketplace-url', url: row.source_url, ref: '', entry: row.marketplace_entry ?? '' }
    case 'archive':
    case 'npm':
      return { kind: row.source_kind, url: row.source_url, ref: row.source_ref }
    default:
      throw new PluginError(`plugin package ${row.name} has a ${row.source_kind} source, which this installer does not fetch`, 409)
  }
}

/** The entry a marketplace source names, for its column; null for the other kinds. */
export function entryOf(source: PackageSource): string | null {
  return source.kind === 'marketplace' || source.kind === 'marketplace-url' ? source.entry : null
}

/** The name a package may have (the table's CHECK): any other is never sent to the API. */
export const PACKAGE_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/

/** A row as the API answers it: every column, its times as ISO text. */
type WireRow = Omit<PackageRow, 'approved_at' | 'created_at' | 'updated_at'> & {
  approved_at: string | null
  created_at: string
  updated_at: string
}

function fromWire(row: WireRow): PackageRow {
  return {
    ...row,
    approved_at: row.approved_at === null ? null : new Date(row.approved_at),
    created_at: new Date(row.created_at),
    updated_at: new Date(row.updated_at),
  }
}

/**
 * A prepared pin as the routes take it. A commit pin is sent as before #6935 (no
 * `pin_kind`), so an API pod of that time still takes it during a roll.
 */
function pinBody(p: PreparedPackage) {
  const pinned = p.bytesSha256 === undefined ? { commit_sha: p.commit } : { pin_kind: 'bytes', bytes_sha256: p.bytesSha256 }
  return { fetch_url: p.fetchUrl, fetch_path: p.fetchPath, ...pinned, content_hash: p.contentHash, files: p.files, review: p.review }
}

/**
 * The packages through the API's CRUD routes, area `plugin-packages` (backend
 * api/crud_agent_packages.py): each method one route and one transaction there, the row lock,
 * the prune of stale access rows and listings, and the warm eviction included. The API answers
 * raw rows, so `view` and `pin` shape them here. A route's 404 or 409 is a PluginError, with the
 * API's text.
 */
export class CrudPackageStore implements PackageRepo {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async #row(step: string, path: string, init?: CrudCallInit): Promise<PackageRow> {
    try {
      return fromWire(await this.#call<WireRow>(step, path, init))
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && (err.status === 404 || err.status === 409)) {
        throw new PluginError(err.detail, err.status)
      }
      throw err
    }
  }

  async #rowOf(name: string): Promise<PackageRow | undefined> {
    if (!PACKAGE_NAME_RE.test(name)) return undefined
    const { package: row } = await this.#call<{ package: WireRow | null }>('a plugin package', `/plugin-packages/${segment(name, PACKAGE_NAME_RE, 'a package name')}`)
    return row ? fromWire(row) : undefined
  }

  #path(name: string, action: string): string {
    if (!PACKAGE_NAME_RE.test(name)) throw new PluginError(`no plugin package named "${name}"`, 404)
    return `/plugin-packages/${segment(name, PACKAGE_NAME_RE, 'a package name')}/${action}`
  }

  async list(): Promise<PackageView[]> {
    const { packages } = await this.#call<{ packages: WireRow[] }>('the plugin packages', '/plugin-packages')
    return packages.map((row) => view(fromWire(row)))
  }

  async get(name: string): Promise<PackageView | undefined> {
    const row = await this.#rowOf(name)
    return row ? view(row) : undefined
  }

  async pinOf(name: string): Promise<(PackagePin & { source: PackageSource }) | undefined> {
    const row = await this.#rowOf(name)
    return row ? { ...pin(row), source: sourceOf(row) } : undefined
  }

  async create(p: PreparedPackage): Promise<PackageView> {
    const body = {
      name: p.review.name,
      source_kind: p.source.kind,
      source_url: p.source.url,
      source_ref: p.source.ref,
      marketplace_entry: entryOf(p.source),
      ...pinBody(p),
    }
    return view(await this.#row('a plugin package installed', '/plugin-packages', { body }))
  }

  async setPending(name: string, p: PreparedPackage): Promise<PackageView> {
    if (p.review.name !== name) {
      throw new PluginError(`the new commit names the plugin "${p.review.name}", not "${name}"`, 409)
    }
    const body = { ref: p.source.ref, ...pinBody(p) }
    return view(await this.#row('a plugin package re-pinned', this.#path(name, 'pending'), { body }))
  }

  async discardPending(name: string): Promise<PackageView> {
    return view(await this.#row("a plugin package's re-pin discarded", this.#path(name, 'discard-pending'), { method: 'POST' }))
  }

  async approve(name: string, commit: string, contentHash: string, allowRefused = false, kind: 'commit' | 'bytes' = 'commit'): Promise<PackageView> {
    const pinned = kind === 'bytes' ? { bytes_sha256: commit } : { commit_sha: commit }
    const body = { ...pinned, content_hash: contentHash, allow_refused: allowRefused }
    return view(await this.#row('a plugin package approved', this.#path(name, 'approve'), { body }))
  }

  async setEnabled(name: string, enabled: boolean): Promise<PackageView> {
    return view(await this.#row('a plugin package switched', this.#path(name, 'enabled'), { body: { enabled } }))
  }

  async delete(name: string): Promise<boolean> {
    if (!PACKAGE_NAME_RE.test(name)) return false
    return (await this.#call<{ done: boolean }>('a plugin package removed', this.#path(name, 'delete'), { method: 'POST' })).done
  }

  async enabledPins(): Promise<PackagePin[]> {
    const { packages } = await this.#call<{ packages: WireRow[] }>('the enabled plugin packages', `/plugin-packages${qs({ enabled: 'true' })}`)
    return packages.map((row) => pin(fromWire(row)))
  }

  async filesOf(name: string, pending: boolean): Promise<{ pin: PackagePin; files: FileList } | undefined> {
    const row = await this.#rowOf(name)
    return row ? filesOfRow(row, pending) : undefined
  }
}

export function filesOfRow(row: PackageRow, pending: boolean): { pin: PackagePin; files: FileList } | undefined {
  if (!pending) return { pin: pin(row), files: row.files }
  if (!row.pending_pin_kind || !row.pending_content_hash || !row.pending_files) return undefined
  if (row.pending_fetch_url === null || row.pending_fetch_path === null) return undefined
  return {
    pin: {
      name: row.name,
      fetchUrl: row.pending_fetch_url,
      fetchPath: row.pending_fetch_path,
      commit: row.pending_commit_sha ?? '',
      ...(row.pending_bytes_sha256 !== null ? { bytesSha256: row.pending_bytes_sha256 } : {}),
      contentHash: row.pending_content_hash,
    },
    files: row.pending_files,
  }
}
