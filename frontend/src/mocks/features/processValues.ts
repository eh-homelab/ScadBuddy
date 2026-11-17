import { HttpResponse, http } from 'msw'
import type { ProcessValues } from '../../api/types'

/**
 * #2998 — a few keys of a flattened Bambu H2C process preset (`0.20mm Standard @BBL H2C`),
 * as `POST /print/process-values` answers them: a string, or a list of strings for a
 * per-extruder-variant option.
 */
export const PROCESS_VALUES: Record<string, string | string[]> = {
  brim_type: 'auto_brim',
  enable_prime_tower: '1',
  layer_height: '0.2',
  outer_wall_speed: ['200', '200'],
  print_sequence: 'by layer',
  skirt_loops: '0',
  sparse_infill_density: '15%',
  sparse_infill_pattern: 'grid',
  top_shell_layers: '5',
  wall_loops: '2',
  wipe_tower_no_sparse_layers: '0',
}

function initial(): ProcessValues {
  return { process: { source: 'cloud', id: 'GP004' }, resolved: true, values: PROCESS_VALUES }
}

let answer: ProcessValues = initial()

export function reset(): void {
  answer = initial()
}

/** A test's own answer, e.g. one that could not be read. */
export function setProcessValues(next: ProcessValues): void {
  answer = next
}

export const handlers = [http.post('/api/v1/print/process-values', () => HttpResponse.json(answer))]
