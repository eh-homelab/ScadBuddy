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
