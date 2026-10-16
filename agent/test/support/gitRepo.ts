import { execFileSync } from 'node:child_process'
import { chmodSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { type Checkout, FetchError, GitFetcher, type RepoFetcher } from '../../src/plugins/packages/git.js'

// Local git repositories for the plugin package tests (#297). The installer
// only fetches https URLs that pass the egress check; `localFetcher` maps
// such a URL to a file:// repository here and fetches it with the real
// GitFetcher (so the real git commands run), allowed the file protocol.

export const gitMissing: string | undefined = (() => {
  try {
    execFileSync('git', ['--version'], { stdio: 'ignore' })
    return undefined
  } catch {
    return 'git is not on PATH'
  }
})()

const ENV = {
  ...process.env,
  GIT_AUTHOR_NAME: 'Test',
  GIT_AUTHOR_EMAIL: 'test@example.invalid',
  GIT_COMMITTER_NAME: 'Test',
  GIT_COMMITTER_EMAIL: 'test@example.invalid',
  GIT_CONFIG_NOSYSTEM: '1',
  GIT_CONFIG_GLOBAL: '/dev/null',
}

function git(dir: string, ...args: string[]): string {
  return execFileSync('git', ['-C', dir, ...args], { env: ENV, encoding: 'utf8' }).trim()
}

export type Files = Record<string, string>

export type TestRepo = {
  dir: string
  commit: string
  /** Writes `files` (null deletes), commits, and returns the new commit. */
  commitFiles(files: Record<string, string | null>, options?: { symlinks?: Files; executable?: string[] }): string
  remove(): void
}

export function gitRepo(files: Files, options: { symlinks?: Files; executable?: string[] } = {}): TestRepo {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'pkg-repo-'))
  git(dir, 'init', '-q', '-b', 'main')
  const commitFiles = (next: Record<string, string | null>, opts: { symlinks?: Files; executable?: string[] } = {}) => {
    for (const [rel, content] of Object.entries(next)) {
      const abs = path.join(dir, rel)
      if (content === null) {
        rmSync(abs, { force: true })
        continue
      }
      mkdirSync(path.dirname(abs), { recursive: true })
      writeFileSync(abs, content)
    }
    for (const [rel, target] of Object.entries(opts.symlinks ?? {})) {
      mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true })
      symlinkSync(target, path.join(dir, rel))
    }
    for (const rel of opts.executable ?? []) chmodSync(path.join(dir, rel), 0o755)
    git(dir, 'add', '-A')
    git(dir, 'commit', '-q', '--allow-empty', '-m', 'commit')
    return git(dir, 'rev-parse', 'HEAD')
  }
  const commit = commitFiles(files, options)
  return { dir, commit, commitFiles, remove: () => rmSync(dir, { recursive: true, force: true }) }
}

/** A RepoFetcher that serves `https://git.test/<key>.git` from the local repositories in `repos`. */
export function localFetcher(repos: Record<string, TestRepo>): RepoFetcher & { checkouts: string[] } {
  const real = new GitFetcher({ protocols: ['file'] })
  const checkouts: string[] = []
  return {
    checkouts,
    checkout(url: string, ref: string, into: string, signal?: AbortSignal): Promise<Checkout> {
      checkouts.push(`${url}@${ref}`)
      const key = /^https:\/\/git\.test\/(.+)\.git$/.exec(url)?.[1]
      const repo = key === undefined ? undefined : repos[key]
      if (!repo) return Promise.reject(new FetchError(`no test repository for ${url}`))
      return real.checkout(pathToFileURL(repo.dir).href, ref, into, signal)
    },
    specialPaths: (checkout, subpath) => real.specialPaths(checkout, subpath),
  }
}

/** Resolves every name to a public documentation address, except those listed. */
export function resolver(overrides: Record<string, string[]> = {}) {
  return (host: string) => Promise.resolve(overrides[host] ?? ['203.0.113.10'])
}

/** A small plugin that passes vetting: a skill, a command, an agent, a prompt hook and a remote MCP server. */
export const GREETER: Files = {
  '.claude-plugin/plugin.json': JSON.stringify({ name: 'greeter', version: '1.0.0', description: 'Says hello.' }),
  'skills/hello/SKILL.md': [
    '---',
    'name: hello',
    'description: Greets the user by name.',
    'allowed-tools: mcp__scadbuddy__list_models',
    '---',
    '',
    'Say hello to the user, then list the models.',
    '',
  ].join('\n'),
  'commands/wave.md': '---\ndescription: Waves.\n---\n\nWave at the user.\n',
  'agents/helper.md': '---\nname: helper\ndescription: Helps.\ntools:\n  - mcp__plugin_greeter_mem__recall\n---\n\nHelp.\n',
  'hooks/hooks.json': JSON.stringify({
    hooks: { Stop: [{ hooks: [{ type: 'prompt', prompt: 'Did the user get greeted? $ARGUMENTS' }] }] },
  }),
  '.mcp.json': JSON.stringify({ mcpServers: { mem: { type: 'http', url: 'https://mcp.example/mcp/' } } }),
  'README.md': '# greeter\n\nA fixture.\n',
}
