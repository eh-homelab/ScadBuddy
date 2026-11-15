import { useState, type FormEvent, type KeyboardEvent } from 'react'
import { PencilIcon, XIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import type { QueuedEntry } from '../../agent/chat/protocol'
import { SentImages } from './SentImages'
import { Tip } from './Tip'

/**
 * #6386 — the messages sent while a turn runs, below it, oldest first: each waits to run
 * as its own turn once the turns before it end. Drawn as a user message not yet sent (a
 * dashed outline, not the filled bubble), marked "Queued". Until it starts, its sender
 * may give it new words or remove it; the agent's next `session.queue` event is what
 * changes the list, and a message that starts leaves it for the transcript.
 */
export function QueuedMessages({
  entries,
  sessionId,
  canChange,
  onRemove,
  onEdit,
}: {
  entries: QueuedEntry[]
  sessionId: string
  /** The viewer may send to the session, so may change its queue. */
  canChange: boolean
  onRemove: (id: string) => void
  onEdit: (id: string, text: string) => void
}) {
  if (entries.length === 0) return null
  return (
    <ol aria-label="Queued messages" className="space-y-2">
      {entries.map((entry, index) => (
        <QueuedMessage
          key={entry.id}
          entry={entry}
          next={index === 0}
          sessionId={sessionId}
          canChange={canChange}
          onRemove={() => onRemove(entry.id)}
          onEdit={(text) => onEdit(entry.id, text)}
        />
      ))}
    </ol>
  )
}

function QueuedMessage({
  entry,
  next,
  sessionId,
  canChange,
  onRemove,
  onEdit,
}: {
  entry: QueuedEntry
  next: boolean
  sessionId: string
  canChange: boolean
  onRemove: () => void
  onEdit: (text: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(entry.text)
  /** Sent, until the next `session.queue` drops the entry: the buttons wait for it. */
  const [removing, setRemoving] = useState(false)
  const label = next ? 'Queued, runs next' : 'Queued'

  const startEditing = () => {
    setDraft(entry.text)
    setEditing(true)
  }
  const save = (event?: FormEvent) => {
    event?.preventDefault()
    const text = draft.trim()
    if (!text) return
    if (text !== entry.text) onEdit(text)
    setEditing(false)
  }
  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      setEditing(false)
    } else if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      save(event)
    }
  }

  return (
    <li
      data-feed-item="queued"
      aria-label={`${label}: ${entry.text}`}
      className={`ml-8 rounded-lg border border-dashed border-border px-2.5 py-1.5 text-[13px] ${removing ? 'opacity-50' : ''}`}
    >
      <div className="mb-0.5 flex items-center gap-1 text-[11px] text-muted-foreground">
        <span className="min-w-0 flex-1">
          {entry.author.kind !== 'browser' && `${entry.author.label}, `}
          {label}
        </span>
        {canChange && !editing && (
          <>
            <Tip content="Edit before it runs">
              <Button variant="ghost" size="icon-xs" aria-label="Edit queued message" disabled={removing} onClick={startEditing}>
                <PencilIcon />
              </Button>
            </Tip>
            <Tip content="Remove from the queue">
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Remove queued message"
                disabled={removing}
                onClick={() => {
                  setRemoving(true)
                  onRemove()
                }}
              >
                <XIcon />
              </Button>
            </Tip>
          </>
        )}
      </div>
      {entry.images && <SentImages images={entry.images} sessionId={sessionId} />}
      {editing ? (
        <form onSubmit={save} className="space-y-1.5">
          <Textarea
            aria-label="Queued message"
            value={draft}
            autoFocus
            rows={2}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKey}
            className="max-h-48 min-h-14 resize-none text-[13px] md:text-[13px]"
          />
          <div className="flex justify-end gap-1.5">
            <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button type="submit" size="sm" disabled={!draft.trim()}>
              Save
            </Button>
          </div>
        </form>
      ) : (
        <p className="whitespace-pre-wrap text-foreground/80">{entry.text}</p>
      )}
    </li>
  )
}
