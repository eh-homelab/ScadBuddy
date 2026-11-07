import { useState } from 'react'
import { ImagePlusIcon, PaperclipIcon } from 'lucide-react'
import { api } from '../../api/client'
import type { Approval, Fulfilment, Order, OrderAttachment, OrderAttachmentRole, OrderItemDetail, Revision } from '../../api/types'
import { timeAgo } from '../../lib/format'
import { failure } from '../../lib/mediaFiles'
import { AddItemDialog } from './AddItemDialog'
import { itemOpen } from './afterAcceptance'
import { EndAttempt } from './EndAttempt'
import { AttachmentCard, FilePick } from './OrderAttachments'
import { PrintItem } from './PrintItem'
import { refusalSlug, refusalText } from './refusal'
import { ROLE_LABELS } from './attachmentMedia'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'
import { Textarea } from '@/components/ui/textarea'

/** The order states a person's decision has ended: an item's approval no longer changes. */
const ENDED = new Set(['closed', 'cancelled'])

const APPROVALS: Record<Approval, string> = { design_first: 'Design first', together: 'Design and price together' }
/** What a revision can be filed under here; the plain upload dropdown offers neither. */
const REVISION_ROLES: OrderAttachmentRole[] = ['render', 'test_print']
const ANSWER_LABELS = { accepted: 'Accepted', changes_requested: 'Changes requested', superseded: 'Superseded' } as const
const OUTCOMES: Record<Fulfilment['outcome'], string> = { unknown: 'in progress', succeeded: 'succeeded', failed: 'failed', partial: 'partial' }

/** Saying why a send is refused, in the shop's words; any other refusal is the backend's. */
function sendRefusal(error: unknown): string {
  switch (refusalSlug(error)) {
    case 'revision_empty':
      return 'Add a render or photo first: a revision is sent with at least one attachment.'
    case 'revision_not_quoted':
      return 'This item is approved together: quote it before sending its design.'
    default:
      return refusalText(error)
  }
}

/**
 * #5280 (storefront spec 2026-11-02 §4.7) — an item's design revisions, newest first: the
 * note, whether it was sent and the customer's answer, its renders and photos, and its test
 * prints. The newest takes a render or photo, is sent to the customer, and has the
 * customer's answer recorded by the shop (the customer answers outside ScadBuddy). Not for
 * a custom item, which has no design.
 */
export function RevisionsPanel({
  order,
  item,
  attachments,
  onChanged,
}: {
  order: Order
  item: OrderItemDetail
  attachments: OrderAttachment[]
  onChanged: () => void
}) {
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [note, setNote] = useState('')
  const [picking, setPicking] = useState(false)
  const [busy, setBusy] = useState(false)
  if (item.kind === 'custom') return null

  const revisions = [...item.revisions].sort((a, b) => b.number - a.number)
  const newest = revisions[0]
  const settled = item.status === 'fulfilled' || item.status === 'cancelled'
  const ended = ENDED.has(order.status)
  // #5349: an item added after acceptance has no accepted price, and still takes revisions.
  const canRevise = itemOpen(order, item.quotes) && !settled

  async function run(action: () => Promise<unknown>, describe: (error: unknown) => string = refusalText): Promise<boolean> {
    setError(null)
    setBusy(true)
    try {
      await action()
      onChanged()
      return true
    } catch (caught) {
      setError(describe(caught))
      return false
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="flex flex-col gap-3" aria-label="Revisions" data-testid="revisions-panel">
      <div className="flex flex-wrap items-end gap-2">
        <h3 className="mr-auto text-sm font-medium">Revisions</h3>
        <div className="flex items-center gap-2">
          <Label htmlFor={`approval-${item.id}`} className="text-[13px] text-muted-foreground">
            Approval
          </Label>
          <Select
            value={item.approval}
            disabled={ended || settled || busy}
            onValueChange={(value) => void run(() => api.updateOrderItem(order.id, item.id, { approval: value as Approval }))}
          >
            <SelectTrigger id={`approval-${item.id}`} size="sm" aria-label="Approval" className="w-56">
              <SelectValue />
            </SelectTrigger>
            <SelectContent position="popper">
              {(Object.keys(APPROVALS) as Approval[]).map((approval) => (
                <SelectItem key={approval} value={approval}>
                  {APPROVALS[approval]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {canRevise && (
          <Button variant="outline" size="sm" aria-expanded={adding} onClick={() => setAdding((now) => !now)}>
            New revision
          </Button>
        )}
      </div>
      {adding && canRevise && (
        <div className="flex flex-col gap-2 rounded-xl border p-3" data-testid="new-revision">
          <Label htmlFor={`revision-note-${item.id}`}>Revision note</Label>
          <Textarea id={`revision-note-${item.id}`} rows={2} value={note} onChange={(event) => setNote(event.target.value)} />
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={busy || item.reference === null}
              title={item.reference === null ? 'This item has no design yet: change the design.' : undefined}
              onClick={() =>
                void run(() => api.addOrderItemRevision(order.id, item.id, { note })).then((ok) => {
                  if (ok) {
                    setNote('')
                    setAdding(false)
                  }
                })
              }
            >
              {busy && <Spinner />}
              Add revision (same design)
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setPicking(true)}>
              Change the design…
            </Button>
          </div>
          {picking && (
            <AddItemDialog
              open
              orderId={order.id}
              revise={{ itemId: item.id, slug: item.reference?.slug ?? null }}
              onAdded={() => {
                setNote('')
                setAdding(false)
                onChanged()
              }}
              onClose={() => setPicking(false)}
            />
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      {revisions.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No revisions yet. Add one to give this request a design.</p>
      ) : (
        <ol aria-label="Revision list" className="flex flex-col gap-3">
          {revisions.map((revision) => (
            <RevisionRow
              key={revision.id}
              order={order}
              item={item}
              revision={revision}
              newest={revision.id === newest?.id}
              canAct={!ended && !settled}
              files={attachments.filter((a) => a.revision_id === revision.id)}
              busy={busy}
              run={run}
              onChanged={onChanged}
            />
          ))}
        </ol>
      )}
    </section>
  )
}

function RevisionRow({
  order,
  item,
  revision,
  newest,
  canAct,
  files,
  busy,
  run,
  onChanged,
}: {
  order: Order
  item: OrderItemDetail
  revision: Revision
  newest: boolean
  canAct: boolean
  files: OrderAttachment[]
  busy: boolean
  run: (action: () => Promise<unknown>, describe?: (error: unknown) => string) => Promise<boolean>
  onChanged: () => void
}) {
  const [role, setRole] = useState<OrderAttachmentRole>('render')
  const [attempt, setAttempt] = useState('')
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [answering, setAnswering] = useState(false)
  const [answer, setAnswer] = useState<'accepted' | 'changes_requested'>('accepted')
  const [answerNote, setAnswerNote] = useState('')
  const [answerError, setAnswerError] = useState<string | null>(null)
  const [added, setAdded] = useState<string | null>(null)
  const tests = item.fulfilments.filter((f) => f.purpose === 'test' && f.revision_id === revision.id)
  const hasSliceQuote = item.quotes.some((q) => q.revision_id === revision.id && q.source === 'slice')
  const photos = revision.answer === 'accepted' ? files.filter((a) => a.role === 'test_print' && a.kind === 'image') : []

  async function upload(picked: File[]) {
    setUploadError(null)
    if (role === 'test_print' && !attempt) {
      setUploadError('Pick the test print this photo shows.')
      return
    }
    for (const file of picked) {
      try {
        await api.addRevisionAttachment(order.id, item.id, revision.id, file, { role, fulfilmentId: role === 'test_print' ? attempt : null })
        onChanged()
      } catch (caught) {
        setUploadError(`${file.name}: ${failure(caught)}`)
      }
    }
  }

  function record() {
    setAnswerError(null)
    if (answer === 'changes_requested' && !answerNote.trim()) {
      setAnswerError('Say what the customer wants changed.')
      return
    }
    void run(() => api.answerOrderItemRevision(order.id, item.id, revision.id, { answer, note: answerNote.trim() })).then((ok) => {
      if (ok) {
        setAnswering(false)
        setAnswerNote('')
      }
    })
  }

  return (
    <li className="flex flex-col gap-2 rounded-xl border p-3" data-testid="revision" data-number={revision.number}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[13px]">
        <span className="font-medium">Revision {revision.number}</span>
        <span className="text-xs text-muted-foreground">Created {timeAgo(revision.created_at)}</span>
        <span className="text-xs text-muted-foreground">{revision.sent_at ? `Sent ${timeAgo(revision.sent_at)}` : 'Not sent'}</span>
        {revision.answer && (
          <span className="text-xs font-medium" data-testid="revision-answer">
            {ANSWER_LABELS[revision.answer]}
          </span>
        )}
      </div>
      {revision.note && <p className="whitespace-pre-wrap text-[13px]">{revision.note}</p>}
      {revision.answer_note && <p className="whitespace-pre-wrap text-[13px] text-muted-foreground">Customer: {revision.answer_note}</p>}
      {files.length > 0 && (
        <div aria-label={`Files of revision ${revision.number}`} role="group" className="grid gap-3 sm:grid-cols-2">
          {files.map((attachment) => (
            <div key={attachment.id} className="flex min-w-0 flex-col gap-1">
              <ul>
                <AttachmentCard
                  orderId={order.id}
                  itemId={null}
                  attachment={attachment}
                  delivered={false}
                  locked={revision.sent_at != null || revision.answer != null}
                  onChanged={onChanged}
                />
              </ul>
              {photos.some((p) => p.id === attachment.id) && (
                <Button
                  size="sm"
                  variant="outline"
                  className="self-start"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => {
                      const model = await api.revisionPhotoToModel(order.id, attachment.id)
                      setAdded(attachment.id)
                      return model
                    })
                  }
                >
                  <ImagePlusIcon aria-hidden="true" />
                  Add to model photos
                </Button>
              )}
              {added === attachment.id && <span role="status" className="text-xs text-muted-foreground">Added to the template's photos.</span>}
            </div>
          ))}
        </div>
      )}
      {tests.length > 0 && (
        <ul aria-label={`Test prints of revision ${revision.number}`} className="text-xs text-muted-foreground">
          {tests.map((test) => (
            <li key={test.id} className="flex flex-wrap items-center gap-2">
              Test print, attempt {test.attempt}: {OUTCOMES[test.outcome]}
              {/* #5316 — a test print is ended on any order not ended or on hold (`update_fulfilment`). */}
              {!ENDED.has(order.status) && !order.on_hold && <EndAttempt orderId={order.id} itemId={item.id} attempt={test} made={false} onChanged={onChanged} />}
            </li>
          ))}
        </ul>
      )}
      {newest && canAct && (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Select value={role} onValueChange={(value) => setRole(value as OrderAttachmentRole)}>
              <SelectTrigger size="sm" aria-label="Add revision files as" className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                {REVISION_ROLES.map((r) => (
                  <SelectItem key={r} value={r}>
                    {ROLE_LABELS[r]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {role === 'test_print' && (
              <Select value={attempt} onValueChange={setAttempt}>
                <SelectTrigger size="sm" aria-label="Photo of test print" className="w-40">
                  <SelectValue placeholder="Which test print" />
                </SelectTrigger>
                <SelectContent position="popper">
                  {tests.map((test) => (
                    <SelectItem key={test.id} value={test.id}>
                      Attempt {test.attempt}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            <FilePick label="Add render or photo" onPick={(picked) => void upload(picked)} icon={<PaperclipIcon aria-hidden="true" />} accept="image/*,video/*,application/pdf" multiple />
            <Button size="sm" disabled={busy || revision.sent_at !== null || revision.answer !== null} onClick={() => void run(() => api.sendOrderItemRevision(order.id, item.id, revision.id), sendRefusal)}>
              Send to customer
            </Button>
            {revision.sent_at && revision.answer === null && (
              <Button size="sm" variant="outline" aria-expanded={answering} onClick={() => setAnswering((now) => !now)}>
                Record answer
              </Button>
            )}
            {hasSliceQuote && <PrintItem order={order} item={item} onChanged={onChanged} testRevision={revision} />}
          </div>
          {uploadError && (
            <p role="alert" className="text-xs text-warn">
              {uploadError}
            </p>
          )}
          {answering && (
            <div className="flex flex-col gap-2 rounded-lg border border-dashed p-2" data-testid="record-answer">
              <p className="text-xs text-muted-foreground">The customer answers outside ScadBuddy: the shop records their answer for them.</p>
              <div className="flex flex-wrap items-center gap-2">
                <Select value={answer} onValueChange={(value) => setAnswer(value as typeof answer)}>
                  <SelectTrigger size="sm" aria-label="The customer's answer" className="w-48">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent position="popper">
                    <SelectItem value="accepted">Accepted</SelectItem>
                    <SelectItem value="changes_requested">Changes requested</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <Label htmlFor={`answer-note-${revision.id}`}>{answer === 'changes_requested' ? 'What should change' : 'Note (optional)'}</Label>
              <Textarea id={`answer-note-${revision.id}`} rows={2} value={answerNote} onChange={(event) => setAnswerNote(event.target.value)} />
              {answerError && (
                <p role="alert" className="text-xs text-warn">
                  {answerError}
                </p>
              )}
              <Button size="sm" className="self-start" disabled={busy} onClick={record}>
                Record the customer's answer
              </Button>
            </div>
          )}
        </div>
      )}
    </li>
  )
}
