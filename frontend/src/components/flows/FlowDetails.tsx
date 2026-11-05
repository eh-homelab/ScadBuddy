import { useId, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { FlowDetails, FlowDetailsUpdate, FlowSummary } from '../../api/types'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Spinner } from '../ui/spinner'

/** The longest commit subject the backend keeps (`core/gitrepo.py` `MAX_SUBJECT`). */
const MAX_SUBJECT = 200
/** The longest description the backend takes (`api/flows.py` `MAX_DESCRIPTION`). */
const MAX_DESCRIPTION = 20_000

/**
 * #3186 — a save's commit message: its subject and an optional body. Both go into the flow's git history.
 */
export function CommitFields({
  message,
  body,
  onMessage,
  onBody,
  placeholder,
}: {
  message: string
  body: string
  onMessage: (next: string) => void
  onBody: (next: string) => void
  placeholder?: string
}) {
  const id = useId()
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={`${id}-message`}>Commit message</Label>
      <Input
        id={`${id}-message`}
        value={message}
        maxLength={MAX_SUBJECT}
        placeholder={placeholder}
        onChange={(event) => onMessage(event.target.value)}
      />
      <Label htmlFor={`${id}-body`} className="mt-1">
        Commit details (optional)
      </Label>
      <Textarea
        id={`${id}-body`}
        rows={2}
        className="text-[12.5px]"
        value={body}
        onChange={(event) => onBody(event.target.value)}
      />
    </div>
  )
}

/** A commit message as the routes take it: blank fields left out. */
export function commitMessage(message: string, body: string): { message?: string; body?: string } {
  return {
    ...(message.trim() ? { message: message.trim() } : {}),
    ...(body.trim() ? { body: body.trim() } : {}),
  }
}

/**
 * #3186 — "Edit details": a flow's display name (shown in place of its name, which stays
 * its id) and its Markdown description. A save sends only the fields that changed, as one
 * commit of `flow.json` in the flow's history, never a new version.
 */
export function EditFlowDetails({
  flow,
  onSaved,
}: {
  flow: Pick<FlowSummary, 'name' | 'display_name' | 'description'>
  onSaved: (details: FlowDetails) => void
}) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [message, setMessage] = useState('')
  const [body, setBody] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string>()

  function show(next: boolean) {
    if (saving) return
    if (next) {
      setDisplayName(flow.display_name ?? '')
      setDescription(flow.description ?? '')
      setMessage('')
      setBody('')
      setError(undefined)
    }
    setOpen(next)
  }

  const change: FlowDetailsUpdate = {
    ...(displayName.trim() !== (flow.display_name ?? '') ? { display_name: displayName.trim() } : {}),
    ...(description.trim() !== (flow.description ?? '') ? { description: description.trim() } : {}),
  }
  const changed = Object.keys(change).length > 0

  async function save() {
    if (!changed || saving) return
    setSaving(true)
    setError(undefined)
    try {
      // Left blank, the subject says what changed (`Rename …`).
      const saved = await api.updateFlowDetails(flow.name, { ...change, ...commitMessage(message, body) })
      setOpen(false)
      onSaved(saved)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={show}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          Edit details
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit details</DialogTitle>
          <DialogDescription>
            What the flow is called and what it does. Its name, <code className="font-mono">{flow.name}</code>, stays its
            id. Saving is one commit in its history, not a new version.
          </DialogDescription>
        </DialogHeader>
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault()
            void save()
          }}
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${id}-name`}>Display name</Label>
            <Input
              id={`${id}-name`}
              value={displayName}
              maxLength={120}
              placeholder={flow.name}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${id}-description`}>Description</Label>
            <Textarea
              id={`${id}-description`}
              rows={5}
              maxLength={MAX_DESCRIPTION}
              aria-describedby={`${id}-description-help`}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
            <p id={`${id}-description-help`} className="text-[12px] text-muted-foreground">
              Markdown.
            </p>
          </div>
          <CommitFields
            message={message}
            body={body}
            onMessage={setMessage}
            onBody={setBody}
            placeholder="Blank: says what changed"
          />
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline" disabled={saving}>
                Cancel
              </Button>
            </DialogClose>
            <Button type="submit" disabled={!changed || saving} aria-busy={saving}>
              {saving && <Spinner />}
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
