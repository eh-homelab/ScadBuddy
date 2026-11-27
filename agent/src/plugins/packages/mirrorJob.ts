import { Context } from '@temporalio/activity'
import type { Client, WorkflowHandleWithFirstExecutionRunId } from '@temporalio/client'
import { ApplicationFailure } from '@temporalio/common'
import { TASK_QUEUE } from '../../temporal/names.js'
import {
  MIRROR_REFUSED,
  PACKAGE_MIRROR_ACTIVITY,
  PACKAGE_MIRROR_WORKFLOW,
  type PackageMirrorInput,
  type PackageMirrorResult,
} from '../../temporal/packageMirrorNames.js'
import { type MirrorJob, type MirrorJobs, MirrorRefusedError, type PackageInstaller } from './install.js'
import type { PackageRepo } from './store.js'

// The job that makes a commit pin's mirror (#7907; the owner's decision of 2026-11-26 on the
// issue: no git fetch on the turn path). A load that finds no usable mirror starts it and goes
// on without the package (install.ts `fromCommitMirror`); so do install, re-pin and approve
// when no mirror was recorded. The job reads the pin from the row and runs
// `PackageInstaller.makeMirror`: a good recorded mirror ends it, else it fetches the commit,
// checks the content hash, and writes and records the zip.
//
// On Temporal it is the PackageMirror workflow (temporal/packageMirror.ts) on `agent-tools`,
// one per pin across the cluster: `temporalMirrorJobs` starts it with the conflict policy
// USE_EXISTING, so a start while one runs joins it, and the reuse policy ALLOW_DUPLICATE, so a
// start after a finished run begins a new one. That is what a load needs when the mirror is
// missing or fails its check after a run that succeeded (the blob gone, a bad record): the new
// run reads the recorded mirror first and fetches only when it is bad. Without Temporal (a
// test, or a deployment with no SCADBUDDY_TEMPORAL_ADDRESS) `localMirrorJobs` runs the same job
// in this process, one per pin at a time, never awaited by the load that started it.

/** The workflow id of `job`'s PackageMirror: one per pin, the content hash's first 16 hex digits beside the commit. */
export function mirrorWorkflowId(job: MirrorJob): string {
  return `package-mirror-${job.name}-${job.commit}-${job.contentHash.slice('sha256:'.length, 'sha256:'.length + 16)}`
}

export type MirrorJobDeps = {
  packages: Pick<PackageRepo, 'filesOf'>
  installer: Pick<PackageInstaller, 'makeMirror'>
}

/** The job: the row's pin (or re-pin) that is `job`'s commit and content hash, mirrored; `not-pinned` when neither is. */
export async function runMirrorJob(deps: MirrorJobDeps, job: PackageMirrorInput, signal?: AbortSignal): Promise<PackageMirrorResult> {
  for (const pending of [false, true]) {
    const found = await deps.packages.filesOf(job.name, pending)
    const pin = found?.pin
    if (pin && pin.commit === job.commit && pin.contentHash === job.contentHash && pin.bytesSha256 === undefined && pin.version === undefined) {
      return await deps.installer.makeMirror(pin, signal)
    }
  }
  return { outcome: 'not-pinned' }
}

/** The PackageMirror workflow's activity, on the agent-tools worker. A refusal is never retried. */
export function packageMirrorActivities(deps: MirrorJobDeps): Record<string, (input: PackageMirrorInput) => Promise<PackageMirrorResult>> {
  return {
    [PACKAGE_MIRROR_ACTIVITY]: async (input) => {
      try {
        return await runMirrorJob(deps, input, Context.current().cancellationSignal)
      } catch (err) {
        if (err instanceof MirrorRefusedError) throw ApplicationFailure.nonRetryable(err.message, MIRROR_REFUSED)
        throw err
      }
    },
  }
}

/** Starts `job`'s PackageMirror, or joins the run of its id that is still running. */
export async function startMirrorWorkflow(client: Client, job: MirrorJob): Promise<WorkflowHandleWithFirstExecutionRunId> {
  return await client.workflow.start(PACKAGE_MIRROR_WORKFLOW, {
    taskQueue: TASK_QUEUE,
    workflowId: mirrorWorkflowId(job),
    args: [job],
    workflowIdConflictPolicy: 'USE_EXISTING',
    workflowIdReusePolicy: 'ALLOW_DUPLICATE',
  })
}

/** Starts PackageMirror workflows through `client`; a start that fails is told to `warn`. */
export function temporalMirrorJobs(client: Client, warn: (message: string) => void = (m) => console.warn(m)): MirrorJobs {
  return {
    start(job) {
      startMirrorWorkflow(client, job)
        .then(() => undefined)
        .catch((err: unknown) => warn(`plugin package ${job.name}: the mirror job of commit ${job.commit} did not start: ${(err as Error).message}`))
    },
  }
}

/** The job in this process (no Temporal): one run per pin at a time; `settled` waits for every run started. */
export function localMirrorJobs(
  deps: MirrorJobDeps,
  warn: (message: string) => void = (m) => console.warn(m),
): MirrorJobs & { settled(): Promise<void> } {
  const running = new Map<string, Promise<void>>()
  return {
    start(job) {
      const id = mirrorWorkflowId(job)
      if (running.has(id)) return
      const run = runMirrorJob(deps, job)
        .then(() => undefined)
        .catch((err: unknown) => warn(`plugin package ${job.name}: the mirror of commit ${job.commit} was not made: ${(err as Error).message}`))
        .finally(() => running.delete(id))
      running.set(id, run)
    },
    async settled() {
      while (running.size) await Promise.all([...running.values()])
    },
  }
}
