import type { SessionMode } from '../../agent/chat/protocol'
import { api } from '../../api/client'
import { useAsync } from '../../lib/useAsync'

const LABEL: Record<SessionMode, string> = { classic: 'Classic', durable: 'Durable' }

/**
 * Plan 5d — the composer's choice of a new chat's mode (spec §6.1), offered only while
 * no session is open: the mode is set when a session starts. "Default" sends none, so
 * the agent's default (Settings → Assistant) applies, and falls back to classic, with
 * a note, when durable cannot run; a picked mode is sent and never falls back. Named
 * "Session mode" rather than the spec's "Advanced": the header has an Advanced switch.
 */
export function SessionModePicker({
  value,
  onChange,
}: {
  value: SessionMode | ''
  onChange: (mode: SessionMode | '') => void
}) {
  // What "Default" means now; a failed read leaves the option unnamed.
  const fallback = useAsync(() => api.getSessionMode(), [])
  const current = fallback.data?.mode
  return (
    <details className="mb-1.5 text-[11.5px] text-muted">
      <summary className="cursor-pointer select-none">Session mode</summary>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <select
          aria-label="Session mode"
          value={value}
          onChange={(event) => onChange(event.target.value as SessionMode | '')}
          className="sb-field h-7 text-[12px]"
        >
          <option value="">Default{current ? ` (${LABEL[current]})` : ''}</option>
          <option value="classic">Classic</option>
          <option value="durable">Durable</option>
        </select>
        <span>Durable chats keep running through a restart of the assistant, and cannot be forked.</span>
      </div>
    </details>
  )
}
