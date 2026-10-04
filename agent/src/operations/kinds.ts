import type { OperationProblem } from '../temporal/names.js'

// A kind of agent command (spec 2026-10-01 §4.2, #1055), as the backend's `OperationKind`
// (backend/scadbuddy/operations/kinds.py): a check that makes the route's refusals and
// writes nothing, and a run that is the effect. Both throw `OperationRefusal` for an
// answer the route would have given; anything else is unexpected.

export type OperationKind = {
  name: string
  /** What the command acts on, for the record and `ScadbuddySubject`. */
  subject(request: Record<string, unknown>): string
  /** The route's refusals; its result is handed to `run`. */
  check(request: Record<string, unknown>): Promise<unknown>
  run(request: Record<string, unknown>, checked: unknown): Promise<unknown>
  /** 1 unless the effect dedupes itself (§4.2: a repeat never repeats the effect). */
  runAttempts: number
  runTimeoutS: number
}

/** A refusal or failure in the route's own words: its status and body. */
export class OperationRefusal extends Error {
  override name = 'OperationRefusal'
  readonly problem: OperationProblem
  constructor(problem: OperationProblem) {
    super(problem.detail)
    this.problem = problem
  }
}

const TITLES: Record<number, string> = {
  400: 'Bad Request',
  403: 'Forbidden',
  404: 'Not Found',
  409: 'Conflict',
  422: 'Unprocessable Content',
  429: 'Too Many Requests',
  502: 'Bad Gateway',
  503: 'Service Unavailable',
}

export function refusal(status: number, detail: string, extensions?: Record<string, unknown>): OperationRefusal {
  return new OperationRefusal({
    status,
    title: TITLES[status] ?? 'Error',
    detail,
    ...(extensions ? { extensions } : {}),
  })
}
