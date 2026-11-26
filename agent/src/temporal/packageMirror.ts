import { proxyActivities } from '@temporalio/workflow'
import { MIRROR_REFUSED, PACKAGE_MIRROR_ACTIVITY, type PackageMirrorInput, type PackageMirrorResult } from './packageMirrorNames.js'

// PackageMirror (#7907, the owner's decision of 2026-11-26 on the issue): makes one commit
// pin's mirror, once per pin across the cluster. Its workflow id is
// `package-mirror-<name>-<commit>-<first 16 of the content hash>` on `agent-tools`
// (plugins/packages/mirrorJob.ts `mirrorWorkflowId`), started by install, re-pin and approve
// when no mirror was recorded, and by a load that finds none it can use. A start joins a run
// of the same id (conflict policy USE_EXISTING) and may follow a finished one
// (ALLOW_DUPLICATE): a load starts one only when the mirror is missing or bad, and the
// activity reads a recorded mirror back first, so a run after a good one fetches nothing.
//
// The activity (`package_mirror`, mirrorJob.ts `packageMirrorActivities`) reads the pin from
// the row, ends when its recorded mirror reads back and hashes to the pin, else fetches the
// commit with git, checks the content hash, and writes and records the zip. A fetch is the
// slow part: a depth-1 fetch of the hindsight monorepo took 132 s on a worker pod (#7907),
// so the activity's own limit is far above the fetcher's (main.ts `MIRROR_FETCH_TIMEOUT_MS`).
// It is tried again with back-off on any failure but a refusal (`MIRROR_REFUSED`: the commit
// gave other files, or files that cannot be mirrored), which no later try would change.
//
// A new workflow type: no `patched()` (nothing replays it before this build). A change to
// this function goes behind `patched()`.

const acts = proxyActivities<{ [PACKAGE_MIRROR_ACTIVITY]: (input: PackageMirrorInput) => Promise<PackageMirrorResult> }>({
  startToCloseTimeout: '20 minutes',
  // The worker heartbeats every activity every 10 s (worker.ts ACTIVITY_HEARTBEAT_MS): a lost pod's fetch is tried again soon.
  heartbeatTimeout: '1 minute',
  retry: {
    initialInterval: '30s',
    backoffCoefficient: 2,
    maximumInterval: '10m',
    maximumAttempts: 6,
    nonRetryableErrorTypes: [MIRROR_REFUSED],
  },
})

export async function PackageMirror(input: PackageMirrorInput): Promise<PackageMirrorResult> {
  return await acts[PACKAGE_MIRROR_ACTIVITY](input)
}
