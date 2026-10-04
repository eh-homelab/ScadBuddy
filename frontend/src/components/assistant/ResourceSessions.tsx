import { useEffect, useId, useRef, useState } from 'react'
import { statusLabel } from '../../agent/chat/labels'
import { useAssistantOpener } from '../../agent/chat/opener'
import { api } from '../../api/client'
import type { ResourceRef } from '../../api/types'
import { useAsync } from '../../lib/useAsync'

interface Props {
  resource: ResourceRef
  /** The toggle's text, before the count. */
  label?: string
  /** The model the resource belongs to: the list reads again when that model changes. */
  model?: string
}

/**
 * #931 — the assistant sessions whose tool calls changed this resource
 * (`GET /api/v1/ai/resources/:type/:id/sessions`, the reverse of the panel's Touched
 * list), as a small menu on the resource's page; picking one opens it in the panel.
 * Shows nothing without the assistant, while it loads, when the agent cannot answer,
 * or when no session touched it: it is a pointer, not a status.
 */
export function ResourceSessions({ resource, label = 'Changed by assistant', model }: Props) {
  const opener = useAssistantOpener()
  const enabled = opener !== null
  const listed = useAsync(
    async () => (enabled ? (await api.listAiResourceSessions(resource)).sessions : null),
    [enabled, resource.type, resource.id],
    model ? [`model:${model}`] : undefined,
  )
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const menuId = useId()

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const sessions = listed.data
  if (!opener || !sessions || sessions.length === 0) return null

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        onClick={() => setOpen((was) => !was)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
      >
        {label} ({sessions.length})
      </button>
      {open && (
        <div
          id={menuId}
          role="menu"
          aria-label="Assistant sessions that changed this"
          className="absolute right-0 top-full z-30 mt-1 max-h-72 w-72 overflow-y-auto rounded-[6px] border border-line bg-surface py-1 shadow-xl"
        >
          {sessions.map((s) => (
            <button
              key={s.id}
              type="button"
              role="menuitem"
              title={s.id}
              className="block w-full px-3 py-1.5 text-left text-[13px] hover:bg-surface-2"
              onClick={() => {
                setOpen(false)
                opener.openSession(s.id)
              }}
            >
              <span className="block truncate">{s.title || 'Untitled session'}</span>
              <span className="block text-[11px] text-faint">
                {statusLabel(s.status)} · {new Date(s.updated_at).toLocaleString()}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
