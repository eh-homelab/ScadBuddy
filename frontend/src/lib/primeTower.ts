import type { PrimeTowerChoice } from '../api/types'

/**
 * The process keys a prime tower choice sets: a copy of the backend's `PRIME_TOWER_KEYS`
 * (`bambuddy/prime_tower.py`), Bambu Studio's names, which
 * `backend/tests/api/test_print_prime_tower.py` pins. `primeTower.test.ts` pins this one.
 */
export const PRIME_TOWER_KEYS = ['enable_prime_tower', 'wipe_tower_no_sparse_layers'] as const

/**
 * The template keys `choice` replaces, as the backend's `prime_tower_overrides` does:
 * none for Default, both with the tower off (No sparse layers then means nothing), and
 * with it on, "No sparse layers" only when the choice sets it.
 */
export function overriddenTowerKeys(choice: PrimeTowerChoice | null): string[] {
  if (choice === null) return []
  if (!choice.enabled) return [...PRIME_TOWER_KEYS]
  return ['enable_prime_tower', ...(choice.no_sparse_layers == null ? [] : ['wipe_tower_no_sparse_layers'])]
}

/**
 * #6721 — the print's prime tower as the analyzers take it (`AnalysisRequest.prime_tower`),
 * as the run gives it to them (`api/accepted_fixes.py` `_tower_choice`): an
 * `enable_prime_tower` setting in `processOverrides` wins over `choice`, as it does at the
 * slice. A list is read by its first value. A value is off when the backend stores it as
 * `"0"` (`process_overrides.py` `scalar_text`). `null` leaves the tower to the file.
 */
export function analysisTower(
  choice: PrimeTowerChoice | null,
  processOverrides: Readonly<Record<string, unknown>> | null,
): PrimeTowerChoice | null {
  let value = processOverrides?.['enable_prime_tower']
  if (Array.isArray(value)) value = value[0]
  if (value === undefined || value === null) return choice
  const enabled = !(value === false || value === 0 || value === '0')
  return choice !== null && choice.enabled === enabled ? choice : { enabled }
}
