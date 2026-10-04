import { EgressError } from '../../http/egress.js'
import { type OperationKind, OperationRefusal, refusal } from '../../operations/kinds.js'
import { PluginError } from '../registry.js'
import { PackageRefusedError, type PackageInstaller } from './install.js'
import { type PackageSource, validateRef, validateSource } from './source.js'
import type { PackageRepo } from './store.js'

// Installing and re-pinning a plugin package as commands (spec 2026-10-01 §4.1, §4.3
// `agent-tools`; §10 phase 4; #1055). Both are a git fetch, an effect outside the one
// Postgres write that stores the pin, so each is an AgentOperation kind: the check
// makes the refusals that need no fetch (a bad source or ref, an unknown package), the
// run fetches, vets and stores the pin, under the same concurrency cap the route had.
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

  /** A fetch under the cap, one per package at a time. */
  async function withFetch<T>(key: string, work: () => Promise<T>): Promise<T> {
    if (busy.has(key)) throw refusal(429, `a fetch for ${key} is already running`)
    if (fetching >= maxFetches) throw refusal(429, 'too many plugin fetches are running; try again')
    busy.add(key)
    fetching++
    try {
      return await work()
    } finally {
      busy.delete(key)
      fetching--
    }
  }

  const install: OperationKind = {
    name: INSTALL_KIND,
    subject: (request) => hostOf((request.source as { url?: unknown } | undefined)?.url),
    check: (request) => refusing(async () => ({ source: validateSource(request.source as Parameters<typeof validateSource>[0]) })),
    run: (_request, checked) =>
      refusing(async () => {
        const { source } = checked as { source: PackageSource }
        return withFetch(`${source.url}#${source.kind === 'git' ? source.path : source.entry}`, async () =>
          deps.packages.create(await deps.installer.prepare(source)),
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
        return { source: { ...current.source, ref } }
      }),
    run: (request, checked) =>
      refusing(async () => {
        const name = String(request.name)
        const { source } = checked as { source: PackageSource }
        return withFetch(name, async () => deps.packages.setPending(name, await deps.installer.prepare(source)))
      }),
    runAttempts: 1,
    runTimeoutS: 300,
  }

  return [install, repin]
}
