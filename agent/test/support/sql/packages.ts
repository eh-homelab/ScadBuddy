import type { Sql, TransactionSql } from 'postgres'
import { builtinItem, itemKindIn, skillItem, subagentItem } from '../../../src/access/items.js'
import { packageSource } from '../../../src/access/model.js'
import {
  filesOfRow,
  type PackagePin,
  type PackageRepo,
  type PackageRow,
  type PackageView,
  pin,
  type PreparedPackage,
  sourceOf,
  view,
  withRefused,
} from '../../../src/plugins/packages/store.js'
import type { FileList } from '../../../src/plugins/packages/hash.js'
import type { PackageSource } from '../../../src/plugins/packages/source.js'
import type { PackageReview } from '../../../src/plugins/packages/vet.js'
import { PluginError } from '../../../src/plugins/registry.js'
import { notifyWarmEvict } from './publisher.js'

// Test-only: the package store over SQL, with the prune of stale access rows the API runs
// (backend api/crud_agent_packages.py). The agent itself goes through the API
// (src/plugins/packages/store.ts CrudPackageStore).

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

export class PackageStore implements PackageRepo {
  private readonly sql: Sql
  constructor(sql: Sql) {
    this.sql = sql
  }

  async list(): Promise<PackageView[]> {
    const rows = await this.sql<PackageRow[]>`SELECT * FROM ai_plugin_packages ORDER BY name`
    return rows.map(view)
  }

  async get(name: string): Promise<PackageView | undefined> {
    const [row] = await this.sql<PackageRow[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name}`
    return row ? view(row) : undefined
  }

  async pinOf(name: string): Promise<(PackagePin & { source: PackageSource }) | undefined> {
    const [row] = await this.sql<PackageRow[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name}`
    return row ? { ...pin(row), source: sourceOf(row) } : undefined
  }

  async create(p: PreparedPackage): Promise<PackageView> {
    return this.sql.begin(async (tx) => {
      const rows = await tx<PackageRow[]>`
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
      const [current] = await tx<PackageRow[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name} FOR UPDATE`
      if (!current) throw new PluginError(`no plugin package named "${name}"`, 404)
      if (
        current.commit_sha === p.commit &&
        current.content_hash === p.contentHash &&
        current.fetch_url === p.fetchUrl &&
        current.fetch_path === p.fetchPath
      ) {
        throw new PluginError(`"${name}" is already pinned to ${p.commit}`, 409)
      }
      const [row] = await tx<PackageRow[]>`
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
      const [row] = await tx<PackageRow[]>`
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
      const [current] = await tx<PackageRow[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name} FOR UPDATE`
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
        const [row] = await tx<PackageRow[]>`
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
      const [row] = await tx<PackageRow[]>`
        UPDATE ai_plugin_packages SET approved_at = now(), allow_refused = ${allow}, updated_at = now()
        WHERE name = ${name} RETURNING *`
      await pruneStaleRows(tx, name, row!.review)
      await notifyWarmEvict(tx, '*')
      return view(row!)
    })
  }

  async setEnabled(name: string, enabled: boolean): Promise<PackageView> {
    return this.sql.begin(async (tx) => {
      const [current] = await tx<PackageRow[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name} FOR UPDATE`
      if (!current) throw new PluginError(`no plugin package named "${name}"`, 404)
      if (enabled && current.approved_at === null) {
        throw new PluginError('approve the pin before enabling the package (issue #297, "Review before enable")', 409)
      }
      const [row] = await tx<PackageRow[]>`
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
    const rows = await this.sql<PackageRow[]>`SELECT * FROM ai_plugin_packages WHERE enabled ORDER BY name`
    return rows.map(pin)
  }

  async filesOf(name: string, pending: boolean): Promise<{ pin: PackagePin; files: FileList } | undefined> {
    const [row] = await this.sql<PackageRow[]>`SELECT * FROM ai_plugin_packages WHERE name = ${name}`
    return row ? filesOfRow(row, pending) : undefined
  }
}
