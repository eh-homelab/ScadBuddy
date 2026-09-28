import type { Sql } from 'postgres'
import { PluginError } from '../registry.js'
import { diffFiles, type FileDiff, type FileList } from './hash.js'
import type { PackageSource } from './source.js'
import type { PackageReview } from './vet.js'

// `ai_plugin_packages` (src/db/migrations/20260928T0750Z_plugin_packages.sql):
// the one authoritative record of an installed plugin package. What is on
// disk (cache.ts) is rebuilt from `fetch_url`, `fetch_path` and
// `commit_sha`, and used only when it hashes to `content_hash`.
//
// LIFECYCLE (issue #297 "Review before enable"; spec §8.2, installing is a
// settings write and outward):
//   install   fetched, vetted and hashed; stored UNAPPROVED and disabled
//   approve   the admin approves exactly the (commit, hash) the review showed;
//             anything else is a 409, so a pin cannot change under an approval
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
  commit: string
  contentHash: string
  files: FileList
  review: PackageReview
}

export type PackagePin = {
  name: string
  fetchUrl: string
  fetchPath: string
  commit: string
  contentHash: string
}

export type PendingPin = {
  ref: string
  /** Where the new commit is fetched from; replaces the pin's on approval. */
  plugin_url: string
  plugin_path: string
  commit_sha: string
  content_hash: string
  review: PackageReview
  diff: FileDiff
}

/** A package as routes show it. */
export type PackageView = {
  name: string
  source:
    | { kind: 'git'; url: string; ref: string; path: string }
    | { kind: 'marketplace'; url: string; ref: string; entry: string; plugin_url: string; plugin_path: string }
  commit_sha: string
  content_hash: string
  review: PackageReview
  approved: boolean
  approved_at: string | null
  enabled: boolean
  pending: PendingPin | null
  created_at: string
  updated_at: string
}

type Row = {
  name: string
  source_kind: 'git' | 'marketplace'
  source_url: string
  source_ref: string
  marketplace_entry: string | null
  fetch_url: string
  fetch_path: string
  commit_sha: string
  content_hash: string
  files: FileList
  review: PackageReview
  approved_at: Date | null
  enabled: boolean
  pending_ref: string | null
  pending_fetch_url: string | null
  pending_fetch_path: string | null
  pending_commit_sha: string | null
  pending_content_hash: string | null
  pending_files: FileList | null
  pending_review: PackageReview | null
  created_at: Date
  updated_at: Date
}

function view(row: Row): PackageView {
  return {
    name: row.name,
    source:
      row.source_kind === 'git'
        ? { kind: 'git', url: row.source_url, ref: row.source_ref, path: row.fetch_path }
        : {
            kind: 'marketplace',
            url: row.source_url,
            ref: row.source_ref,
            entry: row.marketplace_entry ?? '',
            plugin_url: row.fetch_url,
            plugin_path: row.fetch_path,
          },
    commit_sha: row.commit_sha,
    content_hash: row.content_hash,
    review: row.review,
    approved: row.approved_at !== null,
    approved_at: row.approved_at?.toISOString() ?? null,
    enabled: row.enabled,
    pending:
      row.pending_commit_sha && row.pending_content_hash && row.pending_review && row.pending_files && row.pending_ref &&
      row.pending_fetch_url !== null && row.pending_fetch_path !== null
        ? {
            ref: row.pending_ref,
            plugin_url: row.pending_fetch_url,
            plugin_path: row.pending_fetch_path,
            commit_sha: row.pending_commit_sha,
            content_hash: row.pending_content_hash,
            review: row.pending_review,
            diff: diffFiles(row.files, row.pending_files),
          }
        : null,
    created_at: row.created_at.toISOString(),
    updated_at: row.updated_at.toISOString(),
  }
}

function pin(row: Row): PackagePin {
  return {
    name: row.name,
    fetchUrl: row.fetch_url,
    fetchPath: row.fetch_path,
    commit: row.commit_sha,
    contentHash: row.content_hash,
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
  approve(name: string, commit: string, contentHash: string): Promise<PackageView>
  setEnabled(name: string, enabled: boolean): Promise<PackageView>
  delete(name: string): Promise<boolean>
  /** Every enabled (so approved) pin, for a harness run. */
  enabledPins(): Promise<PackagePin[]>
}

export class PackageStore implements PackageRepo {
  private readonly sql: Sql
  constructor(sql: Sql) {
    this.sql = sql
  }

  async list(): Promise<PackageView[]> {
    const rows = await this.sql<Row[]>`SELECT * FROM ai_plugin_packages ORDER BY name`
    return rows.map(view)
  }

  async get(name: string): Promise<PackageView | undefined> {
    const [row] = await this.sql<Row[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name}`
    return row ? view(row) : undefined
  }

  async pinOf(name: string): Promise<(PackagePin & { source: PackageSource }) | undefined> {
    const [row] = await this.sql<Row[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name}`
    if (!row) return undefined
    const source: PackageSource =
      row.source_kind === 'git'
        ? { kind: 'git', url: row.source_url, ref: row.source_ref, path: row.fetch_path }
        : { kind: 'marketplace', url: row.source_url, ref: row.source_ref, entry: row.marketplace_entry ?? '' }
    return { ...pin(row), source }
  }

  async create(p: PreparedPackage): Promise<PackageView> {
    const rows = await this.sql<Row[]>`
      INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref, marketplace_entry, fetch_url,
                                      fetch_path, commit_sha, content_hash, files, review)
      VALUES (${p.review.name}, ${p.source.kind}, ${p.source.url}, ${p.source.ref},
              ${p.source.kind === 'marketplace' ? p.source.entry : null}, ${p.fetchUrl}, ${p.fetchPath},
              ${p.commit}, ${p.contentHash}, ${this.sql.json(p.files)}, ${this.sql.json(p.review)})
      ON CONFLICT (name) DO NOTHING
      RETURNING *`
    const row = rows[0]
    if (!row) {
      throw new PluginError(`a plugin package named "${p.review.name}" is already installed; re-pin it instead`, 409)
    }
    return view(row)
  }

  async setPending(name: string, p: PreparedPackage): Promise<PackageView> {
    if (p.review.name !== name) {
      throw new PluginError(`the new commit names the plugin "${p.review.name}", not "${name}"`, 409)
    }
    return this.sql.begin(async (tx) => {
      const [current] = await tx<Row[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name} FOR UPDATE`
      if (!current) throw new PluginError(`no plugin package named "${name}"`, 404)
      if (
        current.commit_sha === p.commit &&
        current.content_hash === p.contentHash &&
        current.fetch_url === p.fetchUrl &&
        current.fetch_path === p.fetchPath
      ) {
        throw new PluginError(`"${name}" is already pinned to ${p.commit}`, 409)
      }
      const [row] = await tx<Row[]>`
        UPDATE ai_plugin_packages SET
          pending_ref = ${p.source.ref}, pending_fetch_url = ${p.fetchUrl}, pending_fetch_path = ${p.fetchPath},
          pending_commit_sha = ${p.commit}, pending_content_hash = ${p.contentHash},
          pending_files = ${tx.json(p.files)}, pending_review = ${tx.json(p.review)}, updated_at = now()
        WHERE name = ${name}
        RETURNING *`
      return view(row!)
    })
  }

  async discardPending(name: string): Promise<PackageView> {
    const [row] = await this.sql<Row[]>`
      UPDATE ai_plugin_packages SET pending_ref = NULL, pending_fetch_url = NULL, pending_fetch_path = NULL,
        pending_commit_sha = NULL, pending_content_hash = NULL,
        pending_files = NULL, pending_review = NULL, updated_at = now()
      WHERE name = ${name}
      RETURNING *`
    if (!row) throw new PluginError(`no plugin package named "${name}"`, 404)
    return view(row)
  }

  async approve(name: string, commit: string, contentHash: string): Promise<PackageView> {
    return this.sql.begin(async (tx) => {
      const [current] = await tx<Row[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name} FOR UPDATE`
      if (!current) throw new PluginError(`no plugin package named "${name}"`, 404)
      if (current.pending_commit_sha === commit && current.pending_content_hash === contentHash) {
        // The re-pin under review becomes the pin; enabled stays as it was.
        const [row] = await tx<Row[]>`
          UPDATE ai_plugin_packages SET
            source_ref = pending_ref, fetch_url = pending_fetch_url, fetch_path = pending_fetch_path,
            commit_sha = pending_commit_sha, content_hash = pending_content_hash,
            files = pending_files, review = pending_review, approved_at = now(),
            pending_ref = NULL, pending_fetch_url = NULL, pending_fetch_path = NULL, pending_commit_sha = NULL, pending_content_hash = NULL,
            pending_files = NULL, pending_review = NULL, updated_at = now()
          WHERE name = ${name}
          RETURNING *`
        return view(row!)
      }
      if (current.commit_sha === commit && current.content_hash === contentHash) {
        if (current.approved_at !== null) return view(current)
        const [row] = await tx<Row[]>`
          UPDATE ai_plugin_packages SET approved_at = now(), updated_at = now() WHERE name = ${name} RETURNING *`
        return view(row!)
      }
      throw new PluginError(
        'the commit and content hash do not match the pin under review; reload the review and approve what it shows',
        409,
      )
    })
  }

  async setEnabled(name: string, enabled: boolean): Promise<PackageView> {
    return this.sql.begin(async (tx) => {
      const [current] = await tx<Row[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name} FOR UPDATE`
      if (!current) throw new PluginError(`no plugin package named "${name}"`, 404)
      if (enabled && current.approved_at === null) {
        throw new PluginError('approve the pin before enabling the package (issue #297, "Review before enable")', 409)
      }
      const [row] = await tx<Row[]>`
        UPDATE ai_plugin_packages SET enabled = ${enabled}, updated_at = now() WHERE name = ${name} RETURNING *`
      return view(row!)
    })
  }

  async delete(name: string): Promise<boolean> {
    const rows = await this.sql`DELETE FROM ai_plugin_packages WHERE name = ${name}`
    return rows.count > 0
  }

  async enabledPins(): Promise<PackagePin[]> {
    const rows = await this.sql<Row[]>`SELECT * FROM ai_plugin_packages WHERE enabled ORDER BY name`
    return rows.map(pin)
  }
}
