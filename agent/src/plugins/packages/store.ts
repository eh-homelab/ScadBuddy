import type { Sql, TransactionSql } from 'postgres'
import { builtinItem, itemKindIn, skillItem, subagentItem } from '../../access/items.js'
import { packageSource } from '../../access/model.js'
import { notifyWarmEvict } from '../../sessions/warmEvict.js'
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
  /** Approved with `allow_refused`: loaded despite what the rules refuse (vet.ts). */
  allowRefused?: boolean
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
  /** Approved with what the review's `refused` lists allowed. */
  allow_refused: boolean
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
  allow_refused: boolean
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

/** A review stored before `refused` existed had none: it would have been refused outright. */
function withRefused(review: PackageReview): PackageReview & { refused: string[] } {
  return { ...review, refused: review.refused ?? [] }
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
    review: withRefused(row.review),
    approved: row.approved_at !== null,
    approved_at: row.approved_at?.toISOString() ?? null,
    allow_refused: row.allow_refused,
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
            review: withRefused(row.pending_review),
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
    allowRefused: row.allow_refused,
  }
}

/** Where `package:<name>`'s servers' sources start: compared with `starts_with`, never `LIKE` (a name may hold `_`). */
const serverPrefix = (name: string): string => `${packageSource(name)}/mcp:`

/** Every `ai_access` row and `ai_tool_listings` row of `package:<name>` and its servers (#4757). */
async function forgetPackage(tx: TransactionSql, name: string): Promise<void> {
  const source = packageSource(name)
  await tx`DELETE FROM ai_access WHERE source = ${source} OR starts_with(source, ${serverPrefix(name)})`
  await tx`DELETE FROM ai_tool_listings WHERE source = ${source} OR starts_with(source, ${serverPrefix(name)})`
}

/** The servers `review` declares, by name; undefined for a review stored without `local_servers`. */
function declaredServers(review: PackageReview): Set<string> | undefined {
  if (review.local_servers === undefined) return undefined
  return new Set([...review.mcp_servers.map((s) => s.name), ...review.local_servers])
}

/**
 * Whether `review` names a package's items as this code keys them: one written by an older vet
 * (no `described`, or a hook without an id) may name a root skill by its vet folder, not
 * `rootSkillName`, and cannot tell a kept hook from a lost one (#4757 Task 7 review I1).
 */
function namesItems(review: PackageReview): boolean {
  return review.described !== undefined && review.hooks.every((h) => h.id !== undefined)
}

/**
 * Deletes what `package:<name>` stores for items `review` (the pin current after the write)
 * no longer has (#4757): a skill or command (`skill:`), a subagent, a built-in and a hook
 * (by its id), and the rows and listings of the servers it no longer declares. A root skill
 * keyed by an old folder name (`skill:pkg`) goes too: the review names it `rootSkillName`.
 * What the review cannot tell is kept: no item row for an older vet's review (`namesItems`),
 * no built-in's without `builtin_tools`, no server's without `local_servers`. Runs in every
 * write's transaction, so rows an older replica's approval left go with the next write to the
 * package vetted by this code. Returns whether it deleted anything.
 *
 * An access write for a lost item that commits after the read below (it does not lock the
 * package row) leaves its row until the next write: harmless, since it is for an item the pin
 * no longer has, and a kept item's row is never deleted here, so no write is lost.
 */
export async function pruneStaleRows(tx: TransactionSql, name: string, review: PackageReview): Promise<boolean> {
  let deleted = false
  if (namesItems(review)) {
    const source = packageSource(name)
    const bare = (qualified: string) => (qualified.startsWith(`${name}:`) ? qualified.slice(name.length + 1) : qualified)
    const kept = new Set([
      ...[...review.skills, ...review.commands].map((s) => skillItem(bare(s))),
      ...review.agents.map((a) => subagentItem(bare(a))),
      ...(review.builtin_tools ?? []).map(builtinItem),
    ])
    const hookIds = new Set(review.hooks.map((h) => h.id!))
    const stale = (item: string): boolean => {
      const kind = itemKindIn(source, item)
      if (kind === 'tool') return false
      if (kind === 'hook') return !hookIds.has(item.slice(item.lastIndexOf(':') + 1))
      if (kind === 'builtin' && review.builtin_tools === undefined) return false
      return !kept.has(item)
    }
    const rows = await tx<{ item: string }[]>`SELECT item FROM ai_access WHERE source = ${source}`
    const items = rows.map((r) => r.item).filter(stale)
    if (items.length) {
      await tx`DELETE FROM ai_access WHERE source = ${source} AND item IN ${tx(items)}`
      deleted = true
    }
  }

  const declared = declaredServers(review)
  if (declared === undefined) return deleted
  const prefix = serverPrefix(name)
  const sources = await tx<{ source: string }[]>`
    SELECT source FROM ai_access WHERE starts_with(source, ${prefix})
    UNION SELECT source FROM ai_tool_listings WHERE starts_with(source, ${prefix})`
  const gone = sources.map((r) => r.source).filter((s) => !declared.has(s.slice(prefix.length)))
  if (gone.length) {
    await tx`DELETE FROM ai_access WHERE source IN ${tx(gone)}`
    await tx`DELETE FROM ai_tool_listings WHERE source IN ${tx(gone)}`
    deleted = true
  }
  return deleted
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
  /** `allowRefused` must be true when the reviewed pin's `refused` is not empty. */
  approve(name: string, commit: string, contentHash: string, allowRefused?: boolean): Promise<PackageView>
  setEnabled(name: string, enabled: boolean): Promise<PackageView>
  delete(name: string): Promise<boolean>
  /** Every enabled (so approved) pin, for a harness run. */
  enabledPins(): Promise<PackagePin[]>
  /** The pin (or its pending re-pin) with its file list, for reading its files (#1029). */
  filesOf(name: string, pending: boolean): Promise<{ pin: PackagePin; files: FileList } | undefined>
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
    return this.sql.begin(async (tx) => {
      const rows = await tx<Row[]>`
      INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref, marketplace_entry, fetch_url,
                                      fetch_path, commit_sha, content_hash, files, review)
      VALUES (${p.review.name}, ${p.source.kind}, ${p.source.url}, ${p.source.ref},
              ${p.source.kind === 'marketplace' ? p.source.entry : null}, ${p.fetchUrl}, ${p.fetchPath},
              ${p.commit}, ${p.contentHash}, ${tx.json(p.files)}, ${tx.json(p.review)})
      ON CONFLICT (name) DO NOTHING
      RETURNING *`
      const row = rows[0]
      if (!row) {
        throw new PluginError(`a plugin package named "${p.review.name}" is already installed; re-pin it instead`, 409)
      }
      // What an older replica's remove left: a re-install starts at the defaults.
      await forgetPackage(tx, row.name)
      // A warm Claude Code loaded the packages of its time (spec 2026-10-13 §5.3): every write here tells the caches (sessions/warmEvict.ts).
      await notifyWarmEvict(tx, '*')
      return view(row)
    })
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
      await pruneStaleRows(tx, name, row!.review)
      await notifyWarmEvict(tx, '*')
      return view(row!)
    })
  }

  async discardPending(name: string): Promise<PackageView> {
    return this.sql.begin(async (tx) => {
      const [row] = await tx<Row[]>`
        UPDATE ai_plugin_packages SET pending_ref = NULL, pending_fetch_url = NULL, pending_fetch_path = NULL,
          pending_commit_sha = NULL, pending_content_hash = NULL,
          pending_files = NULL, pending_review = NULL, updated_at = now()
        WHERE name = ${name}
        RETURNING *`
      if (!row) throw new PluginError(`no plugin package named "${name}"`, 404)
      await pruneStaleRows(tx, name, row.review)
      await notifyWarmEvict(tx, '*')
      return view(row)
    })
  }

  async approve(name: string, commit: string, contentHash: string, allowRefused = false): Promise<PackageView> {
    return this.sql.begin(async (tx) => {
      const [current] = await tx<Row[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name} FOR UPDATE`
      if (!current) throw new PluginError(`no plugin package named "${name}"`, 404)
      const isPending = current.pending_commit_sha === commit && current.pending_content_hash === contentHash
      const isCurrent = current.commit_sha === commit && current.content_hash === contentHash
      if (!isPending && !isCurrent) {
        throw new PluginError(
          'the commit and content hash do not match the pin under review; reload the review and approve what it shows',
          409,
        )
      }
      // Only what the admin was shown is allowed: a pin with nothing refused
      // is stored without the flag, so a later rule still refuses it.
      const refused = withRefused(isPending ? current.pending_review! : current.review).refused
      if (refused.length && !allowRefused) {
        throw new PluginError(
          `the review lists what the rules refuse (${refused.length}); approve with allow_refused to load the package as it is`,
          409,
        )
      }
      const allow = allowRefused && refused.length > 0
      if (isPending) {
        // The re-pin under review becomes the pin. A new commit from the same
        // place keeps `enabled`; one fetched from another repository or path
        // (a marketplace entry that moved) is a new source to trust, so it is
        // approved disabled and loads only once an admin enables it again.
        const moved = current.pending_fetch_url !== current.fetch_url || current.pending_fetch_path !== current.fetch_path
        const [row] = await tx<Row[]>`
          UPDATE ai_plugin_packages SET
            enabled = enabled AND NOT ${moved}, source_ref = pending_ref, fetch_url = pending_fetch_url, fetch_path = pending_fetch_path,
            commit_sha = pending_commit_sha, content_hash = pending_content_hash,
            files = pending_files, review = pending_review, approved_at = now(), allow_refused = ${allow},
            pending_ref = NULL, pending_fetch_url = NULL, pending_fetch_path = NULL, pending_commit_sha = NULL, pending_content_hash = NULL,
            pending_files = NULL, pending_review = NULL, updated_at = now()
          WHERE name = ${name}
          RETURNING *`
        await pruneStaleRows(tx, name, row!.review)
        await notifyWarmEvict(tx, '*')
        return view(row!)
      }
      if (current.approved_at !== null && current.allow_refused === allow) {
        if (await pruneStaleRows(tx, name, current.review)) await notifyWarmEvict(tx, '*')
        return view(current)
      }
      const [row] = await tx<Row[]>`
        UPDATE ai_plugin_packages SET approved_at = now(), allow_refused = ${allow}, updated_at = now()
        WHERE name = ${name} RETURNING *`
      await pruneStaleRows(tx, name, row!.review)
      await notifyWarmEvict(tx, '*')
      return view(row!)
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
      await pruneStaleRows(tx, name, row!.review)
      await notifyWarmEvict(tx, '*')
      return view(row!)
    })
  }

  async delete(name: string): Promise<boolean> {
    return this.sql.begin(async (tx) => {
      const rows = await tx`DELETE FROM ai_plugin_packages WHERE name = ${name}`
      if (rows.count === 0) return false
      await forgetPackage(tx, name)
      await notifyWarmEvict(tx, '*')
      return true
    })
  }

  async enabledPins(): Promise<PackagePin[]> {
    const rows = await this.sql<Row[]>`SELECT * FROM ai_plugin_packages WHERE enabled ORDER BY name`
    return rows.map(pin)
  }

  async filesOf(name: string, pending: boolean): Promise<{ pin: PackagePin; files: FileList } | undefined> {
    const [row] = await this.sql<Row[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name}`
    if (!row) return undefined
    if (!pending) return { pin: pin(row), files: row.files }
    if (!row.pending_commit_sha || !row.pending_content_hash || !row.pending_files) return undefined
    if (row.pending_fetch_url === null || row.pending_fetch_path === null) return undefined
    return {
      pin: {
        name: row.name,
        fetchUrl: row.pending_fetch_url,
        fetchPath: row.pending_fetch_path,
        commit: row.pending_commit_sha,
        contentHash: row.pending_content_hash,
      },
      files: row.pending_files,
    }
  }
}
