import { useMediaQuery } from './useMediaQuery'

/** Whether the user asked for less motion, following the setting as it changes. */
export function useReducedMotion(): boolean {
  return useMediaQuery('(prefers-reduced-motion: reduce)')
}
