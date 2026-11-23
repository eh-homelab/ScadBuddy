import { createHash } from 'node:crypto'
import { CrudReadError } from '../api/crudRead.js'
import { crudCall, type CrudTarget, crudOf, segment } from '../api/crudStores.js'
import { archiveFormat, entryPath, readArchive } from './packages/unpack.js'
import { storedZip } from './packages/zip.js'

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
// - a commit pin (a git URL, or a git entry of a marketplace repository or of a bare
//   `marketplace.json` URL) as a `url` source, or a `git-subdir` source when the plugin is a
//   folder of its repository, with `sha` set to the pinned commit. Claude Code's `sha` takes a
//   40-character commit only, so a SHA-256 repository's pin is not listed (`unpinnable`). No
//   `version` is given: the `sha` pins it, and a re-pin then reaches clients
//   ([marketplace reference, plugin sources](https://code.claude.com/docs/en/plugins/marketplace-reference.md#plugin-sources));
// - a bytes pin (an archive or npm package, directly or as a marketplace entry; #6935, #6936,
//   spec §6.7) as an `archive` of ScadBuddy's own mirrored copy, served by this agent under the
//   feed's URL with its sha256, so a client gets the bytes that were approved, even after
//   upstream changed or deleted them. Claude Code's `archive` is a zip whose plugin is "at the
//   top of the zip or one directory down"
//   ([archive plugin source](https://code.claude.com/docs/en/plugins/marketplace-reference.md#archive-plugin-source)),
//   the rule the installer found the pin's root by (unpack.ts `pluginRoot`). So a mirrored zip
//   is served as it is; a tarball (an npm package) is served as a zip of the pin's root
//   (`storedZip`, the same bytes on every replica), whose files are the approved ones. Its
//   sha256 is known only once it is made, so `ArchiveMaker` makes it from the mirror once per
//   process and keeps the digest; a mirror that cannot be read leaves the package unlisted
//   (`no-mirror`) until it can.

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
  /** `commit`, `bytes` or `version`; absent from an API older than #6936. */
  pin_kind?: string
  /** A bytes pin's file (#6935); absent from an API older than #6936. */
  bytes_sha256?: string | null
  approved: boolean
  /** Approved with its refused items allowed (hooks, MCP servers); absent from an API older than #6936. */
  allow_refused?: boolean
  description: string | null
  /** For a hosted package, the version its pin is. */
  hosted: { version: number; zip_sha256: string } | null
}

export type FeedRead = { feed: FeedRow | null; packages: FeedPackageRow[] }

/** A version's zip, as published. */
export type FeedZip = { bytes: Uint8Array }

/** What the feed routes read and write. */
export interface PluginFeedReader {
  /** The feed and its packages; with `name`, only that package (a zip request's read). */
  read(feed: string, name?: string): Promise<FeedRead>
  /** The zip of a hosted plugin's version; undefined when there is none. */
  zip(name: string, version: number): Promise<FeedZip | undefined>
  /** The mirrored file of `name`'s bytes pin `sha256`, checked against it; undefined when none is kept. */
  mirror(name: string, sha256: string): Promise<Buffer | undefined>
  /** Lists `name` in the feed; false when it already was. FeedWriteError when the feed or the package is not there. */
  add(feed: string, name: string): Promise<boolean>
  /** Takes `name` out of the feed; false when it was not in it. FeedWriteError when the feed is not there. */
  remove(feed: string, name: string): Promise<boolean>
}

/** A feed write the API refused: the feed or the package is not there. */
export class FeedWriteError extends Error {
  override name = 'FeedWriteError'
  readonly reason: 'feed-not-found' | 'package-not-found'
  constructor(message: string, reason: 'feed-not-found' | 'package-not-found') {
    super(message)
    this.reason = reason
  }
}

/** A mirrored file is at most the installer's download cap (download.ts); the CRUD default of seconds is for rows. */
const MIRROR_TIMEOUT_MS = 120_000
const SHA256 = /^[0-9a-f]{64}$/

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

  async mirror(name: string, sha256: string): Promise<Buffer | undefined> {
    const path = `/package-mirrors/${segment(name, PACKAGE_NAME, 'a package name')}/${segment(sha256, SHA256, 'a sha256')}`
    let bytes: Buffer
    try {
      const response = await crudCall(this.#target.fetch ?? globalThis.fetch, this.#target.baseUrl, "a feed package's mirrored file", path, {
        timeoutMs: MIRROR_TIMEOUT_MS,
      })
      bytes = Buffer.from(await response.arrayBuffer())
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.status === 404) return undefined
      throw err
    }
    if (createHash('sha256').update(bytes).digest('hex') !== sha256) {
      throw new Error(`the mirrored copy of ${sha256} does not hash to it`)
    }
    return bytes
  }

  add(feed: string, name: string): Promise<boolean> {
    return this.#write('PUT', feed, name)
  }

  remove(feed: string, name: string): Promise<boolean> {
    return this.#write('DELETE', feed, name)
  }

  async #write(method: 'PUT' | 'DELETE', feed: string, name: string): Promise<boolean> {
    const path = `/plugin-feeds/${segment(feed, FEED_SLUG, 'a feed')}/packages/${segment(name, PACKAGE_NAME, 'a package name')}`
    const step = method === 'PUT' ? 'a plugin feed addition' : 'a plugin feed removal'
    try {
      return (await this.#crud<{ done: boolean }>(step, path, { method })).done
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.status === 404) {
        if (err.reason === 'feed-not-found') throw new FeedWriteError(`no plugin feed "${feed}"`, 'feed-not-found')
        if (err.reason === 'package-not-found') throw new FeedWriteError(`no plugin package named "${name}"`, 'package-not-found')
      }
      throw err
    }
  }
}

/**
 * Why a package the feed names is not listed: no approved pin, a hosted pin that is no
 * published version, a pin Claude Code cannot take (a SHA-256 repository's commit), or a bytes
 * pin whose mirrored copy cannot be read now.
 */
export type Unlisted = 'not-approved' | 'no-version' | 'unpinnable' | 'no-mirror'

export type FeedEntry =
  | { name: string; listed: true; kind: 'hosted'; version: number; entry: Record<string, unknown> }
  | { name: string; listed: true; kind: 'git'; commit: string; entry: Record<string, unknown> }
  /** `pin` is the bytes pin (the mirrored file's sha256), `sha256` the served zip's. */
  | { name: string; listed: true; kind: 'archive'; pin: string; sha256: string; entry: Record<string, unknown> }
  | { name: string; listed: false; reason: Unlisted }

/** Each bytes pin's served zip by `archiveKey`: its sha256, or null when its mirror could not be read. */
export type FeedArchives = ReadonlyMap<string, string | null>

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

/** A bytes pin's served zip, on `origin`, named by the pin (the mirrored file's sha256). */
export function archiveUrl(origin: string, feed: string, name: string, pin: string): string {
  return `${feedBase(origin, feed)}/plugins/${encodeURIComponent(name)}/archives/${pin}.zip`
}

/** The row's pin kind; an API older than #6936 sends none, so it is read from the columns. */
export function pinKindOf(row: FeedPackageRow): string {
  if (row.pin_kind) return row.pin_kind
  if (row.source_kind === 'hosted') return 'version'
  return row.bytes_sha256 ? 'bytes' : 'commit'
}

/** The key a bytes pin's served zip is kept under. */
export function archiveKey(row: Pick<FeedPackageRow, 'name' | 'bytes_sha256' | 'fetch_path'>): string {
  return `${row.name}\u0000${row.bytes_sha256 ?? ''}\u0000${row.fetch_path ?? ''}`
}

/**
 * How the feed shows `row`: its marketplace entry (URLs on `origin`), or why it has none. A bytes
 * pin is listed only with its served zip's sha256 in `archives` (`ArchiveMaker.resolve`).
 */
export function feedEntry(row: FeedPackageRow, origin: string, feed: string, archives: FeedArchives = new Map()): FeedEntry {
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
  const kind = pinKindOf(row)
  if (kind === 'bytes') {
    const pin = row.bytes_sha256
    if (!pin || !SHA256.test(pin)) return { name: row.name, listed: false, reason: 'unpinnable' }
    const sha256 = archives.get(archiveKey(row))
    if (!sha256) return { name: row.name, listed: false, reason: 'no-mirror' }
    return {
      name: row.name,
      listed: true,
      kind: 'archive',
      pin,
      sha256,
      entry: { name: row.name, ...described, source: { source: 'archive', url: archiveUrl(origin, feed, row.name, pin), sha256 } },
    }
  }
  if (kind !== 'commit' || !row.fetch_url || !row.commit_sha || !GIT_SHA.test(row.commit_sha)) {
    return { name: row.name, listed: false, reason: 'unpinnable' }
  }
  const source = row.fetch_path
    ? { source: 'git-subdir', url: row.fetch_url, path: row.fetch_path, sha: row.commit_sha }
    : { source: 'url', url: row.fetch_url, sha: row.commit_sha }
  return { name: row.name, listed: true, kind: 'git', commit: row.commit_sha, entry: { name: row.name, ...described, source } }
}

/** The feed's `marketplace.json` (spec §6.4), its packages in name order, zip URLs on `origin`. */
export function marketplaceOf(
  feed: FeedRow,
  packages: readonly FeedPackageRow[],
  origin: string,
  archives: FeedArchives = new Map(),
): Record<string, unknown> {
  const plugins = packages
    .map((p) => feedEntry(p, origin, feed.slug, archives))
    .flatMap((e) => (e.listed ? [e.entry] : []))
  return { name: feed.name, owner: { name: feed.owner }, plugins }
}

/**
 * The zip the feed serves for a bytes pin's mirrored `bytes`: the file itself when it is a zip
 * (its plugin root is the top or the one directory there, as Claude Code reads it), else a
 * stored zip of the files under the pin's root `fetchPath` (an npm tarball's `package/`).
 */
export function servedArchive(bytes: Buffer, fetchPath: string): Buffer {
  if (archiveFormat(bytes) === 'zip') return bytes
  const root = entryPath(fetchPath)
  const prefix = root === '' ? '' : `${root}/`
  const files = readArchive(bytes)
    .filter((e) => !e.dir && e.path.startsWith(prefix))
    .map((e) => ({ path: e.path.slice(prefix.length), data: e.data!, executable: e.executable }))
  if (files.length === 0) throw new Error(`the mirrored file has no files under ${JSON.stringify(fetchPath)}`)
  return storedZip(files)
}

const ARCHIVES_KEPT = 256

/**
 * The served zips of bytes pins (#6936): made from the mirror once per process and pin, and their
 * sha256 kept (only the digest, at most `ARCHIVES_KEPT`), so a feed read fetches a mirror only the
 * first time it lists a pin. A read that fails is not kept, so the next one tries again.
 */
export class ArchiveMaker {
  readonly #reader: Pick<PluginFeedReader, 'mirror'>
  readonly #known = new Map<string, Promise<string | null>>()

  constructor(reader: Pick<PluginFeedReader, 'mirror'>) {
    this.#reader = reader
  }

  /** The served zip of `row`'s bytes pin; undefined when no mirror is kept for it. */
  async make(row: FeedPackageRow): Promise<{ bytes: Buffer; sha256: string } | undefined> {
    const mirrored = await this.#reader.mirror(row.name, row.bytes_sha256!)
    if (!mirrored) return undefined
    const bytes = servedArchive(mirrored, row.fetch_path ?? '')
    const sha256 = createHash('sha256').update(bytes).digest('hex')
    this.#keep(archiveKey(row), Promise.resolve(sha256))
    return { bytes, sha256 }
  }

  /** Each approved bytes pin of `rows` with its served zip's sha256, or null when its mirror cannot be read. */
  async resolve(rows: readonly FeedPackageRow[]): Promise<FeedArchives> {
    const wanted = rows.filter((r) => r.approved && pinKindOf(r) === 'bytes' && r.bytes_sha256 && SHA256.test(r.bytes_sha256))
    const out = new Map<string, string | null>()
    await Promise.all(
      wanted.map(async (row) => {
        const key = archiveKey(row)
        let known = this.#known.get(key)
        if (!known) {
          known = this.make(row).then(
            (made) => made?.sha256 ?? null,
            (err: unknown) => {
              console.error(`plugin feed: the mirrored file of ${row.name} could not be served: ${(err as Error).message}`)
              return null
            },
          )
          this.#keep(key, known)
        }
        const sha256 = await known
        // A failure is not kept: the next read tries again.
        if (sha256 === null && this.#known.get(key) === known) this.#known.delete(key)
        out.set(key, sha256)
      }),
    )
    return out
  }

  #keep(key: string, value: Promise<string | null>): void {
    this.#known.delete(key)
    this.#known.set(key, value)
    if (this.#known.size > ARCHIVES_KEPT) this.#known.delete(this.#known.keys().next().value!)
  }
}
