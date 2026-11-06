import type { FulfilmentInput } from '../../api/types'

/** The backend's `Outcome` (`orders/models.py`), in the order the attempt forms offer it;
 * `unknown` ends an attempt whose result nobody knows. */
export const OUTCOMES: readonly FulfilmentInput['outcome'][] = ['succeeded', 'partial', 'failed', 'unknown']
