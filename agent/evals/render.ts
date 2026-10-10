import { type Rendered3mf, readBambu3mf } from './threemf.js'

// Real-render checks (issue #1924, docs/ai/evals.md "Real-render checks"): after
// a scenario's run, every render the model started is followed on the backend
// the run used, the way a person would check it: the job settled, the result
// was saved as an output, and that output's 3MF is what models/<name>/verify.sh
// checks a render for. Live, with SCADBUDDY_EVAL_BACKEND_URL, that backend is the
// image, so OpenSCAD really rendered it; in the scripted run (test/evals.test.ts)
// it is the recorded backend's 3MF of the same shape (evals/backend.ts).
//
// The scorer's own requests go through `peek`, never the logged `fetch`, so a
// check over the log (an outward request, a write) never counts them.

export type LoggedRequest = {
  method: string
  /** Decoded per segment: `/api/v1/models/builtin:name-keychain/render`. */
  path: string
  body: unknown
  status?: number
  /** The parsed JSON body of the answer, when it was JSON. */
  response?: unknown
}

/** The backend a scenario runs against: the recorded one, or the image behind a recorder. */
export interface ScenarioBackend {
  readonly log: LoggedRequest[]
  requests(pattern: RegExp): LoggedRequest[]
  /** The `fetch` the agent's backend client is given; every request is logged. */
  readonly fetch: typeof fetch
  /** A request the scorer makes, not logged. `path` starts with `/api/v1/`. */
  peek(path: string, init?: RequestInit): Promise<Response>
  /** Undoes what the run left behind (a real backend's outputs). */
  cleanup?(): Promise<void>
}

export type RenderedJob = {
  jobId: string
  slug: string
  params: Record<string, unknown>
  /** The job's last status, or `unknown` when it could not be read. */
  status: string
  error?: string | null
  /** The output saved from the job, if any. */
  outputId?: string
  model?: Rendered3mf
  /** Why the job, its output or the 3MF could not be read. */
  problem?: string
}

const SETTLED = new Set(['done', 'failed', 'cancelled'])
const RENDER = /^POST \/api\/v1\/models\/([^/]+)\/render$/

export type CollectOptions = { pollMs?: number; deadlineMs?: number }

async function json(res: Response): Promise<unknown> {
  return res.ok ? res.json() : undefined
}

/** Follows every render the run started: the job to its end, then its saved output's 3MF. */
export async function collectRenders(backend: ScenarioBackend, options: CollectOptions = {}): Promise<RenderedJob[]> {
  const started = new Map<string, { slug: string; params: Record<string, unknown> }>()
  for (const r of backend.log) {
    const slug = RENDER.exec(`${r.method} ${r.path}`)?.[1]
    const jobId = (r.response as { job_id?: unknown } | undefined)?.job_id
    if (slug === undefined || typeof jobId !== 'string' || started.has(jobId)) continue
    const body = (r.body ?? {}) as { params?: Record<string, unknown>; inputs?: { params?: Record<string, unknown> } }
    started.set(jobId, { slug, params: body.inputs?.params ?? body.params ?? {} })
  }
  const deadline = Date.now() + (options.deadlineMs ?? 180_000)
  const rendered: RenderedJob[] = []
  for (const [jobId, { slug, params }] of started) {
    const job: RenderedJob = { jobId, slug, params, status: 'unknown' }
    rendered.push(job)
    try {
      for (;;) {
        const status = (await json(await backend.peek(`/api/v1/jobs/${jobId}`))) as
          | { status?: string; error?: string | null }
          | undefined
        if (!status?.status) throw new Error(`GET /api/v1/jobs/${jobId} did not answer a status`)
        job.status = status.status
        job.error = status.error ?? null
        if (SETTLED.has(status.status) || Date.now() > deadline) break
        await new Promise((resolve) => setTimeout(resolve, options.pollMs ?? 1000))
      }
      if (job.status !== 'done') continue
      const outputs = (await json(await backend.peek(`/api/v1/models/${encodeURIComponent(slug)}/outputs`))) as
        | { id: string; job_id: string }[]
        | undefined
      const output = outputs?.find((o) => o.job_id === jobId)
      if (!output) continue
      job.outputId = output.id
      const file = await backend.peek(`/api/v1/outputs/${encodeURIComponent(output.id)}/model.3mf`)
      if (!file.ok) throw new Error(`GET the output's model.3mf answered ${file.status}`)
      job.model = readBambu3mf(Buffer.from(await file.arrayBuffer()))
    } catch (err) {
      job.problem = err instanceof Error ? err.message : String(err)
    }
  }
  return rendered
}

/** True for an OpenSCAD colour a person would call red: the name, or a hex that is mostly red. */
export function isRed(value: unknown): boolean {
  if (typeof value !== 'string') return false
  const v = value.trim().toLowerCase()
  if (['red', 'crimson', 'firebrick', 'darkred', 'orangered'].includes(v)) return true
  const hex = /^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/.exec(v)?.[1]
  if (!hex) return false
  const full = hex.length <= 4 ? [...hex].map((c) => c + c).join('') : hex
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16)) as [number, number, number]
  return r >= 0xb0 && g <= 0x60 && b <= 0x60
}

/**
 * name-keychain's default heights: `base_thickness = 4` and `letter_height = 2.8`
 * in models/name-keychain/model.scad, which models/name-keychain/verify.sh
 * checks a render against (BASE_THICKNESS, LETTER_HEIGHT; 6.8 mm in all).
 */
export const KEYCHAIN_HEIGHTS = { base: 4, letters: 2.8 } as const

/** verify.sh's tolerance on heights is 0.001 mm; the Bambu writer rounds to 1 µm (bambu3mf.py `_number`). */
const TOLERANCE_MM = 0.01

export type KeychainExpectation = { base: number; letters: number; lettersRed: boolean }

/**
 * The checks models/name-keychain/verify.sh makes of a keychain render, on the
 * output's 3MF: exactly two non-empty parts, the base `base` mm thick from the
 * bottom, the letters standing `letters` mm proud on top of it, so the whole is
 * `base + letters` tall; and, for a scenario that asked for it, red letters.
 * Heights are relative to the model's own bottom, since the writer may move the
 * whole model on the plate. Empty when it passes.
 */
export function keychainProblems(model: Rendered3mf, expected: KeychainExpectation): string[] {
  const parts = model.parts.filter((p) => p.triangles > 0).sort((a, b) => a.zMin - b.zMin)
  if (parts.length !== 2) return [`${parts.length} non-empty part(s), expected 2 (base and letters)`]
  const [base, letters] = parts as [(typeof parts)[0], (typeof parts)[0]]
  const off = (got: number, want: number) => Math.abs(got - want) > TOLERANCE_MM
  const mm = (n: number) => n.toFixed(3)
  const problems: string[] = []
  if (off(base.zMax - base.zMin, expected.base))
    problems.push(`the base is ${mm(base.zMax - base.zMin)} mm thick, expected ${expected.base}`)
  if (off(letters.zMin - model.zMin, expected.base))
    problems.push(`the letters start ${mm(letters.zMin - model.zMin)} mm up, expected ${expected.base}`)
  if (off(letters.zMax - letters.zMin, expected.letters))
    problems.push(`the letters are ${mm(letters.zMax - letters.zMin)} mm proud, expected ${expected.letters}`)
  const total = expected.base + expected.letters
  if (off(model.zMax - model.zMin, total))
    problems.push(`the model is ${mm(model.zMax - model.zMin)} mm tall, expected ${Number(total.toFixed(3))}`)
  if (expected.lettersRed && !isRed(letters.colour)) problems.push(`the letters are ${letters.colour}, not red`)
  return problems
}
