import { useId } from 'react'
import type { SessionMode } from '../../agent/chat/protocol'

/** The browser's last choice of session mode, per browser (like the panel's Advanced switch). */
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

const DESCRIPTION: Record<SessionMode, string> = {
  classic: 'Runs in the assistant service and ends if it restarts. Plugins are available.',
  durable: 'Survives restarts; approvals wait as long as needed. Plugins are not available.',
}

interface Props {
  value: SessionMode
  onChange: (mode: SessionMode) => void
}

/**
 * #1056 — how a new chat runs, chosen before its first message (a session's mode is
 * fixed when it starts). Tucked under "Advanced" so the usual chat is one box.
 */
export function ModePicker({ value, onChange }: Props) {
  const selectId = useId()
  const helpId = useId()
  return (
    <details className="mt-1.5 text-[12px]">
      <summary className="cursor-pointer text-muted">Advanced</summary>
      <div className="mt-1.5 flex flex-col gap-1">
        <label htmlFor={selectId} className="text-muted">
          Session mode
        </label>
        <select
          id={selectId}
          value={value}
          aria-describedby={helpId}
          onChange={(event) => onChange(event.target.value as SessionMode)}
          className="sb-field w-40"
        >
          <option value="classic">Classic</option>
          <option value="durable">Durable</option>
        </select>
        <p id={helpId} className="text-faint">
          {DESCRIPTION[value]}
        </p>
      </div>
    </details>
  )
}
