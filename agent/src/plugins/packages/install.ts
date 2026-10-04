import { randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import { mkdir, readdir, readFile, rename, rm } from 'node:fs/promises'
import path from 'node:path'
import { EgressError, type Resolver, systemResolver } from '../../http/egress.js'
import { assertEndpointAllowed, PluginError } from '../registry.js'
import { type Checkout, extractSubdir, FetchError, type RepoFetcher } from './git.js'
import { hashTree, PackageContentError } from './hash.js'
import {
  DEFAULT_REF,
  normaliseGitUrl,
  normaliseRepoPath,
  type PackageSource,
  validateRef,
} from './source.js'
import type { PackagePin, PackageRepo, PreparedPackage } from './store.js'
import { type Endpoint, vetPackage } from './vet.js'

// Installing a plugin package, and turning its pin back into a directory for a
// harness run (issue #297, "Installing a plugin"; the SDK "loads plugins by
// local path", https://code.claude.com/docs/en/agent-sdk/plugins: "To use a
// plugin distributed through a marketplace or remote repository, download it
// first and provide the local directory path").
//
// POSTGRES IS THE RECORD, DISK IS A CACHE. `ai_plugin_packages` holds the
// source, the pinned commit and the content hash (store.ts). A harness run
// asks `materialise` for each enabled pin: the cached directory
// `<cacheRoot>/<name>/<commit>-<hash prefix>` is used only when every file in
// it hashes to the pin; a missing, partial or altered copy is removed and
// fetched again from the pin (same commit; the new files must hash the same,
// or the package is not loaded). The cache can be an emptyDir.
//
// EVERY FETCH goes through the egress check first (src/http/egress.ts, via
// `assertEndpointAllowed`: https, or http to loopback; no link-local or cloud
// metadata host), and so does every URL the package itself declares (its
// MCP servers and http hooks, vet.ts `endpoints`), at install and again at
// every load, since a name may resolve differently later.
//
// Vetting (vet.ts, on top of src/harness/plugins.ts) runs at install, at
// re-pin and at every load; a package the current rules refuse is not
// loaded even if it was approved under older ones.

export class PackageRefusedError extends Error {
  override name = 'PackageRefusedError'
  readonly problems: readonly string[]
  constructor(problems: readonly string[]) {
    super(`the plugin package is refused: ${problems.join('; ')}`)
    this.problems = problems
  }
}

export type InstallerOptions = {
  fetcher: RepoFetcher
  /** Where materialised packages live; a `.tmp/` under it holds fetches in progress. */
  cacheRoot: string
  resolve?: Resolver
}

type MarketplaceTarget = { url: string; ref: string; path: string; sameRepo: boolean }

const ENTRY_COMPONENT_KEYS = ['commands', 'agents', 'skills', 'hooks', 'mcpServers', 'lspServers', 'outputStyles']

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * Where a marketplace entry's plugin lives
 * (https://code.claude.com/docs/en/plugin-marketplaces, "Choose a plugin
 * source"; fields in https://code.claude.com/docs/en/plugins/marketplace-reference#plugin-sources):
 * a relative path "from the marketplace root" (fetched at the same commit),
 * or a `github`, `url` or `git-subdir` source, each a git repository at an
 * optional `ref` or `sha` (`git-subdir`'s `url` may be an `owner/repo`
 * GitHub shorthand). `archive`, `npm` and `command` sources are refused: no
 * git commit to pin, or a command to run. An entry that declares components
 * of its own is refused too: the SDK loads the plugin by path and would not
 * apply them.
 */
export function marketplaceTarget(marketplace: unknown, entryName: string, marketplaceUrl: string): MarketplaceTarget {
  if (!isRecord(marketplace) || !Array.isArray(marketplace.plugins)) {
    throw new PluginError('.claude-plugin/marketplace.json has no "plugins" list', 422)
  }
  const entry = marketplace.plugins.find((p) => isRecord(p) && p.name === entryName)
  if (!isRecord(entry)) throw new PluginError(`the marketplace has no plugin named "${entryName}"`, 422)
  const declared = ENTRY_COMPONENT_KEYS.filter((k) => entry[k] !== undefined)
  if (declared.length) {
    throw new PluginError(
      `the marketplace entry declares ${declared.join(', ')} itself; ScadBuddy loads the plugin's own files only`,
      422,
    )
  }
  const source = entry.source
  if (typeof source === 'string') {
    return { url: marketplaceUrl, ref: '', path: normaliseRepoPath(source), sameRepo: true }
  }
  if (!isRecord(source)) throw new PluginError(`the marketplace entry "${entryName}" has no source`, 422)
  const at = (): string => {
    if (typeof source.sha === 'string') return validateRef(source.sha)
    if (typeof source.ref === 'string') return validateRef(source.ref)
    return DEFAULT_REF
  }
  switch (source.source) {
    case 'github': {
      if (typeof source.repo !== 'string' || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(source.repo)) {
        throw new PluginError(`the marketplace entry "${entryName}" has no valid github repo`, 422)
      }
      return { url: `https://github.com/${source.repo}.git`, ref: at(), path: '', sameRepo: false }
    }
    case 'url':
    case 'git-subdir': {
      if (typeof source.url !== 'string') throw new PluginError(`the marketplace entry "${entryName}" has no url`, 422)
      const sub = source.source === 'git-subdir' && typeof source.path === 'string' ? source.path : ''
      const shorthand = source.source === 'git-subdir' && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(source.url)
      const url = shorthand ? `https://github.com/${source.url}.git` : normaliseGitUrl(source.url)
      return { url, ref: at(), path: normaliseRepoPath(sub), sameRepo: false }
    }
    default:
      throw new PluginError(
        `the marketplace entry "${entryName}" has a ${JSON.stringify(source.source)} source; only git sources can be pinned`,
        422,
      )
  }
}

export class PackageInstaller {
  readonly cacheRoot: string
  private readonly fetcher: RepoFetcher
  private readonly resolve: Resolver
  /** Per package, the tail of its queue: materialising and pruning run one at a time. */
  private readonly queues = new Map<string, Promise<unknown>>()
  /** Turns using each cached directory; a leased directory is never pruned. */
  private readonly leases = new Map<string, number>()
  /** Per package, the cached directory most recently materialised: kept by prune. */
  private readonly current = new Map<string, string>()

  constructor(options: InstallerOptions) {
    this.fetcher = options.fetcher
    this.cacheRoot = path.resolve(options.cacheRoot)
    this.resolve = options.resolve ?? systemResolver
  }

  private async tempDir(): Promise<string> {
    const dir = path.join(this.cacheRoot, '.tmp', randomUUID())
    await mkdir(dir, { recursive: true })
    return dir
  }

  private async egress(url: string, what: string): Promise<void> {
    try {
      await assertEndpointAllowed(url, this.resolve)
    } catch (err) {
      if (err instanceof EgressError) throw new PluginError(`${what}: ${err.message}`, 400)
      throw err
    }
  }

  /** Problems with the URLs the package declares; empty when each passes the egress check. */
  private async endpointProblems(endpoints: readonly Endpoint[]): Promise<string[]> {
    const problems: string[] = []
    for (const { what, url } of endpoints) {
      try {
        const parsed = new URL(url)
        if (parsed.username || parsed.password) throw new EgressError('the URL carries credentials')
        await assertEndpointAllowed(url, this.resolve)
      } catch (err) {
        if (err instanceof EgressError) problems.push(`${what}: ${err.message}`)
        else if (err instanceof TypeError) problems.push(`${what}: ${url} is not a valid URL`)
        else throw err
      }
    }
    return problems
  }

  private async checkout(url: string, ref: string, into: string, signal?: AbortSignal): Promise<Checkout> {
    try {
      return await this.fetcher.checkout(url, ref, into, signal)
    } catch (err) {
      if (err instanceof FetchError) throw new PluginError(err.message, 502)
      throw err
    }
  }

  /**
   * Copies the plugin's directory out of a checkout into `dest`, refusing
   * symlinks and submodules, and hashes and vets it. Throws
   * PackageRefusedError with every problem found.
   */
  private async extractAndVet(checkout: Checkout, subpath: string, dest: string, fallbackName: string) {
    const special = await this.fetcher.specialPaths(checkout, subpath)
    if (special.length) {
      throw new PackageRefusedError(special.map((s) => `${s}: a package holds regular files only`))
    }
    try {
      await extractSubdir(checkout, subpath, dest)
    } catch (err) {
      if (err instanceof FetchError) throw new PluginError(err.message, 422)
      throw err
    }
    let tree
    try {
      tree = await hashTree(dest)
    } catch (err) {
      if (err instanceof PackageContentError) throw new PackageRefusedError([err.message])
      throw err
    }
    const vetting = vetPackage(dest, fallbackName)
    const problems = [...vetting.problems, ...(await this.endpointProblems(vetting.endpoints))]
    if (problems.length || !vetting.review) throw new PackageRefusedError(problems)
    return { tree, review: vetting.review }
  }

  /**
   * Fetches `source`, pins its commit, and vets and hashes the plugin. Nothing
   * is stored: the caller stores the result (unapproved) for the admin to
   * review. The fetched copy is kept in the cache for the first run. `signal` stops
   * the fetch (an AgentOperation run that is cancelled).
   */
  async prepare(source: PackageSource, signal?: AbortSignal): Promise<PreparedPackage> {
    await this.egress(source.url, 'source url')
    const tmp = await this.tempDir()
    try {
      const repo = await this.checkout(source.url, source.ref, path.join(tmp, 'repo'), signal)
      let checkout = repo
      let fetchUrl = source.url
      let fetchPath = source.kind === 'git' ? source.path : ''
      let fallbackName: string
      if (source.kind === 'marketplace') {
        const file = path.join(repo.dir, '.claude-plugin', 'marketplace.json')
        let marketplace: unknown
        try {
          marketplace = JSON.parse(await readFile(file, 'utf8'))
        } catch {
          throw new PluginError('the repository has no valid .claude-plugin/marketplace.json', 422)
        }
        const target = marketplaceTarget(marketplace, source.entry, source.url)
        fetchUrl = target.url
        fetchPath = target.path
        if (!target.sameRepo) {
          await this.egress(target.url, `marketplace entry "${source.entry}"`)
          checkout = await this.checkout(target.url, target.ref, path.join(tmp, 'plugin-repo'), signal)
        }
        fallbackName = source.entry
      } else {
        fallbackName = (fetchPath.split('/').pop() || new URL(source.url).pathname.split('/').pop() || '')
          .replace(/\.git$/, '')
          .toLowerCase()
      }
      const pkg = path.join(tmp, 'pkg')
      const { tree, review } = await this.extractAndVet(checkout, fetchPath, pkg, fallbackName)
      const prepared: PreparedPackage = {
        source,
        fetchUrl,
        fetchPath,
        commit: checkout.commit,
        contentHash: tree.hash,
        files: tree.files,
        review,
      }
      await this.place(
        { name: review.name, fetchUrl, fetchPath, commit: checkout.commit, contentHash: tree.hash },
        pkg,
      ).catch(() => undefined)
      return prepared
    } finally {
      await rm(tmp, { recursive: true, force: true })
    }
  }

  /** `<cacheRoot>/<name>/<commit>-<first 16 of the hash>`. */
  cacheDir(pin: PackagePin): string {
    return path.join(this.cacheRoot, pin.name, `${pin.commit}-${pin.contentHash.slice('sha256:'.length, 'sha256:'.length + 16)}`)
  }

  /** Moves a verified copy into the cache (no-op when a copy is already there). */
  private async place(pin: PackagePin, from: string): Promise<string> {
    const dir = this.cacheDir(pin)
    await mkdir(path.dirname(dir), { recursive: true })
    try {
      await rename(from, dir)
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code
      if (code !== 'ENOTEMPTY' && code !== 'EEXIST') throw err
    }
    return dir
  }

  /** True when the cached copy exists and every file hashes to the pin. */
  private async cacheMatches(dir: string, pin: PackagePin): Promise<boolean> {
    if (!existsSync(dir)) return false
    try {
      return (await hashTree(dir)).hash === pin.contentHash
    } catch (err) {
      if (err instanceof PackageContentError || (err as NodeJS.ErrnoException).code === 'ENOENT') return false
      throw err
    }
  }

  /**
   * The local directory for `pin`, verified against it: the cached copy when
   * it hashes to the pin, else fetched again at the pinned commit. Re-vetted
   * either way. Throws PluginError / PackageRefusedError when it cannot be
   * loaded.
   */
  async materialise(pin: PackagePin): Promise<string> {
    const { dir, release } = await this.acquire(pin)
    release()
    return dir
  }

  /**
   * As `materialise`, and leases the directory to the caller until `release`
   * (once the turn that loads it has ended): a newer version materialised
   * meanwhile does not delete it; it is pruned when its last lease goes.
   */
  async acquire(pin: PackagePin): Promise<{ dir: string; release: () => void }> {
    const dir = await this.serial(pin.name, async () => {
      const ready = await this.materialiseNow(pin)
      this.leases.set(ready, (this.leases.get(ready) ?? 0) + 1)
      this.current.set(pin.name, ready)
      await this.prune(pin.name)
      return ready
    })
    let released = false
    const release = () => {
      if (released) return
      released = true
      const left = (this.leases.get(dir) ?? 1) - 1
      if (left > 0) {
        this.leases.set(dir, left)
        return
      }
      this.leases.delete(dir)
      void this.serial(pin.name, () => this.prune(pin.name)).catch(() => undefined)
    }
    return { dir, release }
  }

  /** Runs `work` after every earlier queued step for package `name`. */
  private serial<T>(name: string, work: () => Promise<T>): Promise<T> {
    const next = (this.queues.get(name) ?? Promise.resolve()).catch(() => undefined).then(work)
    this.queues.set(name, next)
    void next
      .finally(() => {
        if (this.queues.get(name) === next) this.queues.delete(name)
      })
      .catch(() => undefined)
    return next
  }

  private async materialiseNow(pin: PackagePin): Promise<string> {
    const dir = this.cacheDir(pin)
    if (!(await this.cacheMatches(dir, pin))) {
      await rm(dir, { recursive: true, force: true })
      await this.egress(pin.fetchUrl, 'plugin url')
      const tmp = await this.tempDir()
      try {
        const checkout = await this.checkout(pin.fetchUrl, pin.commit, path.join(tmp, 'repo'))
        if (checkout.commit !== pin.commit) {
          throw new PluginError(`fetched commit ${checkout.commit}, the pin is ${pin.commit}`, 502)
        }
        const pkg = path.join(tmp, 'pkg')
        const { tree } = await this.extractAndVet(checkout, pin.fetchPath, pkg, pin.name)
        if (tree.hash !== pin.contentHash) {
          throw new PluginError(
            `the files at commit ${pin.commit} hash to ${tree.hash}, not the pinned ${pin.contentHash}`,
            502,
          )
        }
        await this.place(pin, pkg)
      } finally {
        await rm(tmp, { recursive: true, force: true })
      }
      if (!(await this.cacheMatches(dir, pin))) {
        throw new PluginError(`the cached copy of ${pin.name} does not match its pin after fetching`, 502)
      }
    }
    // Re-vetted at every load: the rules may be stricter than when it was approved.
    const vetting = vetPackage(dir, pin.name)
    const problems = [...vetting.problems, ...(await this.endpointProblems(vetting.endpoints))]
    if (vetting.review && vetting.review.name !== pin.name) {
      problems.push(`the package names itself "${vetting.review.name}", not "${pin.name}"`)
    }
    if (problems.length) throw new PackageRefusedError(problems)
    return dir
  }

  /**
   * Removes cached versions of package `name` that are neither the current one
   * nor leased to a running turn (best effort). Runs in the package's queue.
   */
  private async prune(name: string): Promise<void> {
    const parent = path.join(this.cacheRoot, name)
    const keep = this.current.get(name)
    const entries = await readdir(parent).catch(() => [] as string[])
    await Promise.all(
      entries
        .map((e) => path.join(parent, e))
        .filter((dir) => dir !== keep && !this.leases.has(dir))
        .map((dir) => rm(dir, { recursive: true, force: true })),
    )
  }

  /** Removes every cached version of a package (on delete); one a running turn uses goes when the turn ends. */
  async evict(name: string): Promise<void> {
    await this.serial(name, async () => {
      this.current.delete(name)
      await this.prune(name)
    })
  }
}

export type PackagesForRun = {
  paths: string[]
  problems: string[]
  /** Call when the turn has ended: its package directories may then be pruned. */
  release: () => void
}

/**
 * The enabled packages for one harness run, each materialised and verified.
 * A package that cannot be loaded is left out and named in `problems`; the run
 * goes ahead without it (as for remote MCP plugins, registry.ts).
 */
export async function loadPackagesForRun(
  store: Pick<PackageRepo, 'enabledPins'>,
  installer: Pick<PackageInstaller, 'acquire'>,
): Promise<PackagesForRun> {
  const paths: string[] = []
  const problems: string[] = []
  const releases: (() => void)[] = []
  for (const pin of await store.enabledPins()) {
    try {
      const { dir, release } = await installer.acquire(pin)
      paths.push(dir)
      releases.push(release)
    } catch (err) {
      if (err instanceof PackageRefusedError) {
        problems.push(`plugin package ${pin.name} was not loaded: ${err.problems.join('; ')}`)
      } else if (err instanceof PluginError) {
        problems.push(`plugin package ${pin.name} was not loaded: ${err.message}`)
      } else {
        problems.push(`plugin package ${pin.name} was not loaded: ${(err as Error).message}`)
      }
    }
  }
  return { paths, problems, release: () => releases.forEach((r) => r()) }
}
