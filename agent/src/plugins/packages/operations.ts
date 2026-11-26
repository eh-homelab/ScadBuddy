import { EgressError } from '../../http/egress.js'
import { type OperationKind, OperationRefusal, refusal } from '../../operations/kinds.js'
import { PluginError } from '../registry.js'
import { BuiltInPluginError, PackageRefusedError, type PackageInstaller, recordPreparedMirror } from './install.js'
import { type PackageSource, REPIN_FIXED, RepinFixedError, validateSource, withRepinRef } from './source.js'
import type { PackageRepo, PackageView, PreparedPackage } from './store.js'

/**
 * A hosted plugin's latest version offered to the package model: installed when no package
 * has its name (stored unapproved), re-pinned into `pending_*` when the hosted package is
 * installed, chosen by whether the row exists, never by a version number (spec §6.3 step 5).
 * A package of another kind under the name is refused 409.
 */
export async function offerHosted(
  packages: Pick<PackageRepo, 'get' | 'create' | 'setPending'>,
  installer: Pick<PackageInstaller, 'prepare'>,
  name: string,
  signal?: AbortSignal,
): Promise<PackageView> {
  const source: PackageSource = { kind: 'hosted', name }
  const current = await packages.get(name)
  if (current && current.source.kind !== 'hosted') {
    throw new PluginError(`a plugin package named "${name}" is installed from a ${current.source.kind} source, not this hosted plugin`, 409)
  }
  const prepared = await installer.prepare(source, signal)
  return current ? packages.setPending(name, prepared) : packages.create(prepared)
}

// Installing and re-pinning a plugin package as commands (spec 2026-10-01 §4.1, §4.3
// `agent-tools`; §10 phase 4; #1055). Both are a fetch (git, or HTTP for an archive or npm
// package, #6935), an effect outside the one Postgres write that stores the pin, so each
// is an AgentOperation kind: the check makes the refusals that need no fetch (a bad source
// or ref, an unknown package, the concurrency cap the route had), the run fetches, vets
// and stores the pin, and stops the fetch when its activity is cancelled.
// The answers are the routes' own: 201/200 with the package, 400, 404, 409, 422 with
// every problem, 429.
//
// A hosted source (#6934, spec 2026-11-21 §6.3 step 5) is OFFERED: its latest version is
// installed when no package has its name, and re-pinned into `pending_*` when the hosted
// package is installed (`offerHosted`), so the UI's Offer and the publish tool's in-process
// call are one command whichever it turns out to be. Either way nothing is approved: the
// admin approves the exact (version, content hash).

export const INSTALL_KIND = 'plugin_package_install'
export const REPIN_KIND = 'plugin_package_repin'

export type PackageKindDeps = {
  packages: PackageRepo
  /** `wantMirror` starts a commit pin's mirror job when the install's own mirror was not recorded (#7907). */
  installer: Pick<PackageInstaller, 'prepare'> & Partial<Pick<PackageInstaller, 'wantMirror'>>
  /** Fetches at once across all packages; more are refused with 429. */
  maxConcurrentFetches?: number
}

/** The route's refusals, in its words. */
function asRefusal(err: unknown): unknown {
  if (err instanceof BuiltInPluginError) return refusal(409, err.message, { built_in: true })
  if (err instanceof PackageRefusedError) return refusal(422, 'the plugin package is refused', { problems: err.problems })
  if (err instanceof RepinFixedError) return refusal(409, err.message, { reason: REPIN_FIXED })
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

  /** `prepared` stored by `store`, then its commit mirror recorded on the row, or its mirror job started (#7907). */
  async function stored(prepared: PreparedPackage, store: (prepared: PreparedPackage) => Promise<PackageView>): Promise<PackageView> {
    const view = await store(prepared)
    await recordPreparedMirror(deps.packages, deps.installer, prepared, { name: view.name })
    return view
  }

  const installKey = (source: PackageSource) => {
    // A hosted package's install and re-pin share the package's name: one fetch at a time.
    if (source.kind === 'hosted') return source.name
    if (source.kind === 'git') return `${source.url}#${source.path}`
    if (source.kind === 'marketplace' || source.kind === 'marketplace-url') return `${source.url}#${source.entry}`
    return `${source.url}#${source.ref}`
  }

  const install: OperationKind = {
    name: INSTALL_KIND,
    label: 'Install plugin package',
    subject: (request) => {
      const source = request.source as { kind?: unknown; url?: unknown; name?: unknown } | undefined
      return source?.kind === 'hosted' ? String(source.name) : hostOf(source?.url)
    },
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
          source.kind === 'hosted'
            ? offerHosted(deps.packages, deps.installer, source.name, signal)
            : stored(await deps.installer.prepare(source, signal), (prepared) => deps.packages.create(prepared)),
        )
      }),
    runAttempts: 1,
    runTimeoutS: 300,
  }

  const repin: OperationKind = {
    name: REPIN_KIND,
    label: 'Re-pin plugin package',
    subject: (request) => String(request.name),
    check: (request) =>
      refusing(async () => {
        const name = String(request.name)
        const current = await deps.packages.pinOf(name)
        if (!current) throw new OperationRefusal({ status: 404, title: 'Not Found', detail: `no plugin package named "${name}"` })
        const source = withRepinRef(current.source, request.ref as string | undefined)
        refuseWhenTaken(name)
        return { source }
      }),
    run: (request, checked, signal) =>
      refusing(async () => {
        const name = String(request.name)
        const { source } = checked as { source: PackageSource }
        return withFetch(name, signal, async () =>
          stored(await deps.installer.prepare(source, signal), (prepared) => deps.packages.setPending(name, prepared)),
        )
      }),
    runAttempts: 1,
    runTimeoutS: 300,
  }

  return [install, repin]
}
