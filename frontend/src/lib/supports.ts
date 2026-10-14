import type { SupportsChoice } from '../api/types'

/**
 * The process keys a choice here sets: a copy of the backend's `SUPPORT_KEYS`
 * (`bambuddy/supports.py`), which `backend/tests/api/test_print_supports.py` pins.
 * Keep the two in step; `supports.test.ts` pins this one.
 */
export const SUPPORT_KEYS = [
  'enable_support',
  'support_type',
  'support_threshold_angle',
  'support_on_build_plate_only',
] as const

/**
 * The template keys `choice` replaces, as the backend's `wanted_settings` does: none for
 * Default, all of them with supports off, and with supports on only the fields it sets,
 * so an empty angle still shows the template's as applying (#2837 review).
 */
export function overriddenSupportKeys(choice: SupportsChoice | null): string[] {
  if (choice === null) return []
  if (!choice.enabled) return [...SUPPORT_KEYS]
  return [
    'enable_support',
    ...(choice.type == null ? [] : ['support_type']),
    ...(choice.threshold_angle == null ? [] : ['support_threshold_angle']),
    ...(choice.on_build_plate_only == null ? [] : ['support_on_build_plate_only']),
  ]
}
