import { EgressError } from '../../http/egress.js'
import { type OperationKind, OperationRefusal, refusal } from '../../operations/kinds.js'
import { PluginError } from '../registry.js'
import { PackageRefusedError, type PackageInstaller } from './install.js'
import { type PackageSource, validateRef, validateSource } from './source.js'
import type { PackageRepo } from './store.js'

// Installing and re-pinning a plugin package as commands (spec 2026-10-01 §4.1, §4.3
// `agent-tools`; §10 phase 4; #1055). Both are a git fetch, an effect outside the one
// Postgres write that stores the pin, so each is an AgentOperation kind: the check
// makes the refusals that need no fetch (a bad source or ref, an unknown package, the
// concurrency cap the route had), the run fetches, vets and stores the pin, and stops
// git when its activity is cancelled.
// The answers are the routes' own: 201/200 with the package, 400, 404, 409, 422 with
// every problem, 429.

export const INSTALL_KIND = 'plugin_package_install'
export const REPIN_KIND = 'plugin_package_repin'

export type PackageKindDeps = {
  packages: PackageRepo
  installer: Pick<PackageInstaller, 'prepare'>
  /** Fetches at once across all packages; more are refused with 429. */
  maxConcurrentFetches?: number
}

/** The route's refusals, in its words. */
function asRefusal(err: unknown): unknown {
  if (err instanceof PackageRefusedError) return refusal(422, 'the plugin package is refused', { problems: err.problems })
  if (err instanceof PluginError) return refusal(err.status, err.message)
  if (err instanceof EgressError) return refusal(400, err.message)
  return err
}

async function refusing<T>(work: () => Promise<T>): Promise<T> {
  try {
    return await work()
  } catch (err) {
    throw asRefusal(err)
  }
}

/** The source's host: what an install acts on, for its record and Search Attributes. */
function hostOf(url: unknown): string {
  try {
    return new URL(String(url)).host || 'plugin-packages'
  } catch {
    return 'plugin-packages'
  }
}

export function packageKinds(deps: PackageKindDeps): OperationKind[] {
  const maxFetches = deps.maxConcurrentFetches ?? 2
  let fetching = 0
  const busy = new Set<string>()
  const waiting = new Set<() => void>()

  /**
   * The cap, in the check: refused there, before the record, a re-send with the same
   * key starts again once a fetch is free (a 429 from the run would be recorded, and
   * answered to that key for good).
   */
  function refuseWhenTaken(key: string): void {
    if (busy.has(key)) throw refusal(429, `a fetch for ${key} is already running`)
    if (fetching >= maxFetches) throw refusal(429, 'too many plugin fetches are running; try again')
  }

  /** Resolves when a fetch ends; rejects when `signal` aborts first. */
  function freed(signal: AbortSignal | undefined): Promise<void> {
    return new Promise((resolve, reject) => {
      const abort = () => {
        waiting.delete(wake)
        reject(signal!.reason)
      }
      const wake = () => {
        signal?.removeEventListener('abort', abort)
        resolve()
      }
      waiting.add(wake)
      signal?.addEventListener('abort', abort, { once: true })
    })
  }

  /** A fetch under the cap, one per package at a time; a run that passed its check waits its turn. */
  async function withFetch<T>(key: string, signal: AbortSignal | undefined, work: () => Promise<T>): Promise<T> {
    while (busy.has(key) || fetching >= maxFetches) {
      signal?.throwIfAborted()
      await freed(signal)
    }
    busy.add(key)
    fetching++
    try {
      return await work()
    } finally {
      busy.delete(key)
      fetching--
      for (const wake of [...waiting]) {
        waiting.delete(wake)
        wake()
      }
    }
  }

  const installKey = (source: PackageSource) => `${source.url}#${source.kind === 'git' ? source.path : source.entry}`

  const install: OperationKind = {
    name: INSTALL_KIND,
    subject: (request) => hostOf((request.source as { url?: unknown } | undefined)?.url),
    check: (request) =>
      refusing(async () => {
        const source = validateSource(request.source as Parameters<typeof validateSource>[0])
        refuseWhenTaken(installKey(source))
        return { source }
      }),
    run: (_request, checked, signal) =>
      refusing(async () => {
        const { source } = checked as { source: PackageSource }
        return withFetch(installKey(source), signal, async () =>
          deps.packages.create(await deps.installer.prepare(source, signal)),
        )
      }),
    runAttempts: 1,
    runTimeoutS: 300,
  }

  const repin: OperationKind = {
    name: REPIN_KIND,
    subject: (request) => String(request.name),
    check: (request) =>
      refusing(async () => {
        const name = String(request.name)
        const current = await deps.packages.pinOf(name)
        if (!current) throw new OperationRefusal({ status: 404, title: 'Not Found', detail: `no plugin package named "${name}"` })
        const ref = validateRef((request.ref as string | undefined) ?? current.source.ref)
        refuseWhenTaken(name)
        return { source: { ...current.source, ref } }
      }),
    run: (request, checked, signal) =>
      refusing(async () => {
        const name = String(request.name)
        const { source } = checked as { source: PackageSource }
        return withFetch(name, signal, async () =>
          deps.packages.setPending(name, await deps.installer.prepare(source, signal)),
        )
      }),
    runAttempts: 1,
    runTimeoutS: 300,
  }

  return [install, repin]
}
