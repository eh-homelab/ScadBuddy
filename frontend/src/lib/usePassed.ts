import { useEffect, useState } from 'react'

/**
 * Whether `at` has passed, re-rendering when it does (#3350); false for no time. By the
 * browser's clock, which the agent no longer trusts for the hop itself (#3411): a fast one
 * hides the "you may decide it now" notice early. It says nothing the run page's decision
 * does not, so that is accepted.
 */
export function usePassed(at: string | null | undefined): boolean {
  const due = at ? Date.parse(at) : Number.NaN
  const [passed, setPassed] = useState(() => Number.isFinite(due) && due <= Date.now())
  useEffect(() => {
    if (!Number.isFinite(due)) return setPassed(false)
    let timer: ReturnType<typeof setTimeout> | undefined
    const look = () => {
      const left = due - Date.now()
      setPassed(left <= 0)
      // The cap keeps setTimeout's 32-bit delay from overflowing; it then looks again.
      if (left > 0) timer = setTimeout(look, Math.min(left, 2 ** 31 - 1))
    }
    look()
    return () => clearTimeout(timer)
  }, [due])
  return passed
}
