import type { SessionMode } from './protocol'

/** The browser's last choice of session mode (#1056), per browser like the panel's Advanced switch. */
export const MODE_KEY = 'scadbuddy.assistant.mode'

export function readMode(): SessionMode | null {
  try {
    const stored = window.localStorage.getItem(MODE_KEY)
    return stored === 'classic' || stored === 'durable' ? stored : null
  } catch {
    return null
  }
}

export function writeMode(mode: SessionMode): void {
  try {
    window.localStorage.setItem(MODE_KEY, mode)
  } catch {
    // Private mode or blocked storage: the choice still holds for this page.
  }
}
