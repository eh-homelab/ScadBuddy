import { execFile } from 'node:child_process'
import { cp, mkdir, stat } from 'node:fs/promises'
import path from 'node:path'
import { promisify } from 'node:util'
import { COMMIT_RE } from './source.js'

const run = promisify(execFile)

// Fetching a plugin package's repository at one commit, with the `git` CLI
// (installed in the agent image for this; Dockerfile `agent` stage).
//
// The caller has already put the URL through the egress check
// (install.ts: https, or http to loopback only; no link-local or cloud
// metadata host). git then resolves the name again itself, so, as for the
// gateway base URL (src/http/egress.ts), a name re-pointed in between is not
// caught here; the pod's egress NetworkPolicy is the boundary.
//
// What git gets:
//   - an environment built here, never the service's own: no database URL,
//     no key file path, no Claude credential. PATH, a throwaway HOME, the
//     proxy and CA variables, and the switches below.
//   - GIT_ALLOW_PROTOCOL=https (https and http in the loopback case), so a
//     redirect or a submodule URL cannot move it to ssh, file or ext::
//     (git-config(1) `protocol.allow`; GIT_ALLOW_PROTOCOL in git(1)).
//   - no system or global config (GIT_CONFIG_NOSYSTEM, GIT_CONFIG_GLOBAL=/dev/null),
//     so no credential helper, filter (LFS) or hook from the host applies;
//     `core.hooksPath=/dev/null` besides.
//   - `http.followRedirects=false`: the checked host is the one fetched from.
//   - GIT_TERMINAL_PROMPT=0: a repository that wants a password fails
//     instead of waiting.
//   - `transfer.fsckObjects`: malformed objects are refused.
//   - `core.symlinks=false`, and every symlink or submodule in the plugin's
//     directory is refused from the tree listing (`specialPaths`), before
//     any file is used.
//   - a shallow fetch (`--depth 1`) of exactly the ref, no tags, no submodules,
//     under a time limit.

export type Checkout = { commit: string; dir: string }

/** The network half of fetching, so tests can point it at a local repository. */
export interface RepoFetcher {
  /** Fetches `ref` of `url` into the empty directory `into`; resolves to the commit. `signal` kills git. */
  checkout(url: string, ref: string, into: string, signal?: AbortSignal): Promise<Checkout>
  /** Paths under `subpath` that are symlinks or submodules in the checked-out commit. */
  specialPaths(checkout: Checkout, subpath: string): Promise<string[]>
}

export class FetchError extends Error {
  override name = 'FetchError'
}

const PASSTHROUGH_ENV = [
  'HTTPS_PROXY',
  'https_proxy',
  'HTTP_PROXY',
  'http_proxy',
  'NO_PROXY',
  'no_proxy',
  'GIT_SSL_CAINFO',
  'SSL_CERT_FILE',
  'SSL_CERT_DIR',
]

export type GitFetcherOptions = {
  /** The git binary; `git` on PATH by default. */
  git?: string
  timeoutMs?: number
  /** GIT_ALLOW_PROTOCOL. https (and http, which the egress check limits to loopback) by default. */
  protocols?: readonly string[]
}

export class GitFetcher implements RepoFetcher {
  private readonly git: string
  private readonly timeoutMs: number
  private readonly protocols: readonly string[]

  constructor(options: GitFetcherOptions = {}) {
    this.git = options.git ?? 'git'
    this.timeoutMs = options.timeoutMs ?? 120_000
    this.protocols = options.protocols ?? ['https', 'http']
  }

  private env(home: string): NodeJS.ProcessEnv {
    const env: NodeJS.ProcessEnv = {
      HOME: home,
      LC_ALL: 'C',
      GIT_TERMINAL_PROMPT: '0',
      GIT_CONFIG_NOSYSTEM: '1',
      GIT_CONFIG_GLOBAL: '/dev/null',
      GIT_ALLOW_PROTOCOL: this.protocols.join(':'),
    }
    if (process.env.PATH !== undefined) env.PATH = process.env.PATH
    for (const name of PASSTHROUGH_ENV) if (process.env[name] !== undefined) env[name] = process.env[name]
    return env
  }

  private async git_(dir: string, args: string[], signal?: AbortSignal): Promise<string> {
    const config = [
      '-c', 'http.followRedirects=false',
      '-c', 'core.hooksPath=/dev/null',
      '-c', 'core.symlinks=false',
      '-c', 'transfer.fsckObjects=true',
      '-c', 'fetch.recurseSubmodules=false',
      '-c', 'credential.helper=',
      '-c', 'advice.detachedHead=false',
    ]
    try {
      const { stdout } = await run(this.git, ['-C', dir, ...config, ...args], {
        env: this.env(dir),
        timeout: this.timeoutMs,
        maxBuffer: 16 * 1024 * 1024,
        encoding: 'utf8',
        ...(signal ? { signal } : {}),
      })
      return stdout
    } catch (err) {
      if ((err as Error).name === 'AbortError') throw err
      const e = err as { stderr?: string; killed?: boolean; code?: unknown; message: string }
      if (e.code === 'ENOENT') throw new FetchError('git is not installed in the agent image')
      if (e.killed) throw new FetchError(`git ${args[0]} timed out after ${this.timeoutMs} ms`)
      const detail = (e.stderr ?? e.message).trim().split('\n').slice(-3).join(' ').slice(0, 500)
      throw new FetchError(`git ${args[0]} failed: ${detail}`)
    }
  }

  async checkout(url: string, ref: string, into: string, signal?: AbortSignal): Promise<Checkout> {
    await mkdir(into, { recursive: true })
    await this.git_(into, ['init', '-q'], signal)
    await this.git_(into, ['fetch', '-q', '--depth', '1', '--no-tags', '--no-recurse-submodules', url, ref], signal)
    const commit = (await this.git_(into, ['rev-parse', '--verify', 'FETCH_HEAD^{commit}'], signal)).trim()
    if (!COMMIT_RE.test(commit)) throw new FetchError(`git returned "${commit.slice(0, 80)}" as the commit`)
    if (COMMIT_RE.test(ref) && ref !== commit) {
      throw new FetchError(`asked for commit ${ref}, the remote returned ${commit}`)
    }
    await this.git_(into, ['checkout', '-q', '--detach', commit], signal)
    return { commit, dir: into }
  }

  async specialPaths(checkout: Checkout, subpath: string): Promise<string[]> {
    const out = await this.git_(checkout.dir, ['ls-tree', '-r', '-z', '--full-tree', checkout.commit])
    const prefix = subpath ? `${subpath}/` : ''
    const special: string[] = []
    for (const entry of out.split('\0')) {
      const match = /^(\d{6}) \w+ [0-9a-f]+\t(.*)$/s.exec(entry)
      if (!match) continue
      const [, mode, file] = match as unknown as [string, string, string]
      if (!file.startsWith(prefix)) continue
      if (mode === '120000' || mode === '160000') special.push(`${file} (${mode === '120000' ? 'symlink' : 'submodule'})`)
    }
    return special
  }
}

/**
 * Copies `subpath` of a checkout (its root when '') to `dest`, without `.git`.
 * Throws FetchError when it is not a directory in the commit.
 */
export async function extractSubdir(checkout: Checkout, subpath: string, dest: string): Promise<void> {
  const src = path.join(checkout.dir, subpath)
  let isDir = false
  try {
    isDir = (await stat(src)).isDirectory()
  } catch {
    // below
  }
  if (!isDir || subpath.split('/').includes('.git')) {
    throw new FetchError(`${subpath || 'the repository root'} is not a directory at commit ${checkout.commit}`)
  }
  const gitDir = path.join(checkout.dir, '.git')
  await cp(src, dest, {
    recursive: true,
    verbatimSymlinks: true,
    filter: (source) => source !== gitDir && !source.startsWith(gitDir + path.sep),
  })
}
