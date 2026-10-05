import { useId } from 'react'
import type { SessionMode } from '../../agent/chat/protocol'

const LABEL: Record<SessionMode, string> = { classic: 'Classic', durable: 'Durable' }

const DESCRIPTION: Record<SessionMode, string> = {
  classic: 'Runs in the assistant service and ends if it restarts. Plugins are available.',
  durable: 'Survives restarts; approvals wait as long as needed. Plugins are not available.',
}

interface Props {
  /** This browser's choice; null while it has made none, so the server's default applies. */
  value: SessionMode | null
  /** The server's default, once known (null while loading or when it could not be read). */
  defaultMode: SessionMode | null
  onChange: (mode: SessionMode) => void
}

/**
 * #1056 — how a new chat runs, chosen before its first message (a session's mode is
 * fixed when it starts). Tucked under "Advanced" so the usual chat is one box.
 */
export function ModePicker({ value, defaultMode, onChange }: Props) {
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
          value={value ?? ''}
          aria-describedby={helpId}
          onChange={(event) => onChange(event.target.value as SessionMode)}
          className="sb-field w-40"
        >
          {value === null && (
            <option value="">
              {defaultMode ? `Default (${LABEL[defaultMode]})` : 'Server default'}
            </option>
          )}
          <option value="classic">Classic</option>
          <option value="durable">Durable</option>
        </select>
        <p id={helpId} className="text-faint">
          {value ? DESCRIPTION[value] : defaultMode ? DESCRIPTION[defaultMode] : 'Uses the default set in Settings.'}
        </p>
      </div>
    </details>
  )
}
