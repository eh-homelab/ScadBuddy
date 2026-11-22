import { CrudReadError } from '../api/crudRead.js'
import { crudCall, type CrudTarget, crudOf, segment } from '../api/crudStores.js'

// A marketplace feed of plugin packages (#6933, spec
// docs/superpowers/specs/2026-11-21-hosted-plugin-feeds-design.md §6.4): the
// `marketplace.json` Claude Code adds with `/plugin marketplace add <url>`, built from
// `ai_plugin_feed_packages` (backend api/crud_agent_packages.py, area `plugin-feeds`).
//
// Only a package's current APPROVED pin is listed: a pending re-pin is not listed until it is
// approved, and a package with no approved pin is not listed. So a client installs only the
// bytes an admin reviewed:
//
// - a hosted package (`source_kind` `hosted`, #6934) as an `archive` of the version its pin
//   is, served by this agent under the feed's own URL (the origin the feed was asked on), with
//   the zip's sha256;
// - a git package (a git URL or a marketplace entry) as a `url` source, or a `git-subdir`
//   source when the plugin is a folder of its repository, with `sha` set to the pinned commit.
//   Claude Code's `sha` takes a 40-character commit only, so a SHA-256 repository's pin is not
//   listed. No `version` is given: the `sha` pins it, and a re-pin then reaches clients
//   ([marketplace reference, plugin sources](https://code.claude.com/docs/en/plugins/marketplace-reference.md#plugin-sources)).
//
// A bytes pin (`archive`, `npm`; #6935) is emitted with #6936, as an `archive` of ScadBuddy's
// own mirrored copy.

/** `ai_plugin_feeds.slug`'s CHECK. */
export const FEED_SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/
/** `ai_plugin_packages.name`'s alphabet. */
export const PACKAGE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/

export type FeedRow = { slug: string; name: string; owner: string }

/** A package the feed names, at its current pin (backend `FeedPackage`). */
export type FeedPackageRow = {
  name: string
  source_kind: string
  fetch_url: string | null
  fetch_path: string | null
  commit_sha: string | null
  content_hash: string
  approved: boolean
  description: string | null
  /** For a hosted package, the version its pin is. */
  hosted: { version: number; zip_sha256: string } | null
}

export type FeedRead = { feed: FeedRow | null; packages: FeedPackageRow[] }

/** A version's zip, as published. */
export type FeedZip = { bytes: Uint8Array }

/** What the feed routes read. */
export interface PluginFeedReader {
  /** The feed and its packages; with `name`, only that package (a zip request's read). */
  read(feed: string, name?: string): Promise<FeedRead>
  /** The zip of a hosted plugin's version; undefined when there is none. */
  zip(name: string, version: number): Promise<FeedZip | undefined>
}

/** The feed reads over the API's CRUD routes (area `plugin-feeds`; the zip is area `hosted-plugins`'s). */
export class CrudPluginFeeds implements PluginFeedReader {
  readonly #crud: ReturnType<typeof crudOf>
  readonly #target: CrudTarget

  constructor(target: CrudTarget) {
    this.#target = target
    this.#crud = crudOf(target)
  }

  read(feed: string, name?: string): Promise<FeedRead> {
    const path = `/plugin-feeds/${segment(feed, FEED_SLUG, 'a feed')}`
    const only = name === undefined ? '' : `?package=${segment(name, PACKAGE_NAME, 'a package name')}`
    return this.#crud<FeedRead>('a plugin feed read', `${path}${only}`)
  }

  async zip(name: string, version: number): Promise<FeedZip | undefined> {
    const path = `/hosted-plugins/${segment(name, PACKAGE_NAME, 'a package name')}/versions/${version}.zip`
    try {
      const response = await crudCall(this.#target.fetch ?? globalThis.fetch, this.#target.baseUrl, 'a hosted plugin zip read', path)
      return { bytes: new Uint8Array(await response.arrayBuffer()) }
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.status === 404) return undefined
      throw err
    }
  }
}

/** Why a package the feed names is not listed. */
export type Unlisted = 'not-approved' | 'no-version' | 'unpinnable'

export type FeedEntry =
  | { name: string; listed: true; kind: 'hosted'; version: number; entry: Record<string, unknown> }
  | { name: string; listed: true; kind: 'git'; commit: string; entry: Record<string, unknown> }
  | { name: string; listed: false; reason: Unlisted }

const GIT_SHA = /^[0-9a-f]{40}$/

/** `<origin>/api/v1/ai/plugin-feeds/<feed>`: the feed's own base. */
export function feedBase(origin: string, feed: string): string {
  return `${origin.replace(/\/+$/, '')}/api/v1/ai/plugin-feeds/${encodeURIComponent(feed)}`
}

/** The marketplace's address: what `/plugin marketplace add` takes. */
export function marketplaceUrl(origin: string, feed: string): string {
  return `${feedBase(origin, feed)}/marketplace.json`
}

/**
 * A hosted version's zip, as the feed lists it, on `origin`: the origin the `marketplace.json`
 * request came on. Claude Code sends a marketplace's `headers` only to the marketplace URL's own
 * origin (scheme, host and port), so a zip on another host (the SSO host SCADBUDDY_PUBLIC_URL
 * names in prod) would get no token
 * ([Authenticate archive downloads](https://code.claude.com/docs/en/plugins/host-marketplace.md#authenticate-archive-downloads)).
 */
export function zipUrl(origin: string, feed: string, name: string, version: number): string {
  return `${feedBase(origin, feed)}/plugins/${encodeURIComponent(name)}/versions/${version}.zip`
}

/** How the feed shows `row`: its marketplace entry (zip URLs on `origin`), or why it has none. */
export function feedEntry(row: FeedPackageRow, origin: string, feed: string): FeedEntry {
  if (!row.approved) return { name: row.name, listed: false, reason: 'not-approved' }
  const described = row.description ? { description: row.description } : {}
  if (row.source_kind === 'hosted') {
    if (!row.hosted) return { name: row.name, listed: false, reason: 'no-version' }
    const { version, zip_sha256 } = row.hosted
    return {
      name: row.name,
      listed: true,
      kind: 'hosted',
      version,
      entry: {
        name: row.name,
        ...described,
        version: `${version}.0.0`,
        source: { source: 'archive', url: zipUrl(origin, feed, row.name, version), sha256: zip_sha256 },
      },
    }
  }
  const gitKind = row.source_kind === 'git' || row.source_kind === 'marketplace'
  if (!gitKind || !row.fetch_url || !row.commit_sha || !GIT_SHA.test(row.commit_sha)) {
    return { name: row.name, listed: false, reason: 'unpinnable' }
  }
  const source = row.fetch_path
    ? { source: 'git-subdir', url: row.fetch_url, path: row.fetch_path, sha: row.commit_sha }
    : { source: 'url', url: row.fetch_url, sha: row.commit_sha }
  return { name: row.name, listed: true, kind: 'git', commit: row.commit_sha, entry: { name: row.name, ...described, source } }
}

/** The feed's `marketplace.json` (spec §6.4), its packages in name order, zip URLs on `origin`. */
export function marketplaceOf(feed: FeedRow, packages: readonly FeedPackageRow[], origin: string): Record<string, unknown> {
  const plugins = packages
    .map((p) => feedEntry(p, origin, feed.slug))
    .flatMap((e) => (e.listed ? [e.entry] : []))
  return { name: feed.name, owner: { name: feed.owner }, plugins }
}
