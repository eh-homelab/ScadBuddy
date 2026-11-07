import { HttpResponse, http } from 'msw'
import type { Fulfilment, Order, OrderItem, Revision, RevisionAnswerInput, RevisionCreate } from '../../../api/types'
import { byCreated } from '../../../components/orders/price'
import { mockModels, setMockMedia } from '../../handlers'
import { printersState } from '../printers'
import { upload } from './attachments'
import type { Resolved } from './orders'
import { acceptQuote, checkQuote, event, itemOf, printInFlight, printRequestId, lock, notEnded, notOnHold, OPEN_FOR_EDITS, openForRequote, quotesOf, rederive, refused, resolveReference, sameDesign, SETTLED, TAKES_ITEMS, transition, withFilaments } from './orders'
import { BROWSER, fieldProblem, newId, now, problem, PROBLEMS, Refused, revisionsOf, state } from './state'

/**
 * Design revisions (#5280, storefront spec 2026-11-02 §4; `backend/scadbuddy/api/orders.py`,
 * `orders/lifecycle.py` `_revise`, `add_revision`, `send_revision`, `answer_revision`): an
 * item's designs, numbered per item. A new one supersedes a sent, unanswered newest one and
 * drops the item's quotes; only the newest is sent, answered or test-printed; a customer's
 * acceptance on a `together` item also accepts the quote that names it. An accepted item's
 * new design (#5350, `…/revisions/after-acceptance`) keeps its accepted quote until a quote
 * of the new design is accepted.
 */

/** `lifecycle.py` `REQUOTED` (#5350): the states a revised accepted item goes back to `draft` from. */
const REQUOTED = new Set(['quoted', 'approved', 'printed', 'failed'])

/** `Lifecycle._revise`: the item's next revision, and the item's design a copy of it. */
export function pushRevision(
  order: Order,
  item: OrderItem,
  reference: NonNullable<OrderItem['reference']>,
  colours: OrderItem['colours'],
  note: string,
  afterAcceptance = false,
): Revision {
  const requote = TAKES_ITEMS.has(order.status) && (afterAcceptance || item.revised_after_acceptance)
  const kept = requote ? (quotesOf(item.id).find((q) => q.accepted)?.id ?? null) : null
  const all = revisionsOf(item.id)
  const newest = all.at(-1)
  let superseded: string | null = null
  if (newest && newest.sent_at && newest.answer === null) {
    newest.answer = 'superseded'
    newest.answered_at = now()
    newest.answered_by = BROWSER
    superseded = newest.id
  }
  const revision: Revision = {
    id: newId('rev'),
    item_id: item.id,
    number: (newest?.number ?? 0) + 1,
    note,
    reference,
    colours,
    created_at: now(),
    created_by: BROWSER,
    answer: null,
    answer_note: null,
    answered_at: null,
    answered_by: null,
    sent_at: null,
  }
  state.revisions.push(revision)
  item.reference = reference
  item.colours = colours
  item.current_revision_id = revision.id
  item.updated_at = now()
  if (requote) {
    // #5350: the accepted quote stays; the item waits for a quote of the new design.
    item.revised_after_acceptance = true
    if (REQUOTED.has(item.status)) item.status = 'draft'
    event(order, 'revision.added_after_acceptance', { item_id: item.id, revision_id: revision.id, number: revision.number, superseded, order_status: order.status, accepted_quote_id: kept })
    return revision
  }
  // Every quote goes stale (§4.3), so a quoted or approved item is a draft again.
  for (const q of quotesOf(item.id)) q.accepted = false
  if (item.status === 'quoted' || item.status === 'approved') item.status = 'draft'
  event(order, 'revision.added', { item_id: item.id, revision_id: revision.id, number: revision.number, superseded })
  return revision
}

/** `Lifecycle._newest`: the revision of the item, which must be its newest. */
function newestOf(item: OrderItem, revisionId: string): Revision {
  const revisions = revisionsOf(item.id)
  const found = revisions.find((r) => r.id === revisionId)
  if (!found) throw new Refused('revision_not_found', `no revision ${revisionId} of item ${item.id}`, [], true)
  if (revisions.at(-1)!.id !== found.id) throw new Refused('revision_not_newest', `revision ${found.number} of item ${item.id} is not its newest`, [item.id])
  return found
}

/** `Lifecycle._quote_of`: the newest quote that names the revision. */
const quoteOf = (item: OrderItem, revision: Revision) => quotesOf(item.id).filter((q) => q.revision_id === revision.id).sort(byCreated).at(-1)

const IDEMPOTENCY = 'https://scadbuddy.dev/problems/idempotency-key-required'
const ANSWERS = new Set(['accepted', 'changes_requested'])

function unknownKeys(request: Request, body: Record<string, unknown>, allowed: string[]) {
  for (const key of Object.keys(body)) if (!allowed.includes(key)) return fieldProblem(request, key, 'Extra inputs are not permitted')
  return null
}

const base = '/api/v1/orders/:id/items/:item/revisions'

/** `RevisionInput`'s `note`: a string of at most 4000 characters, as the backend's 422 says. */
function badNote(request: Request, body: Record<string, unknown>) {
  if (!('note' in body)) return null
  if (typeof body.note !== 'string') return fieldProblem(request, 'note', 'Input should be a valid string')
  if (body.note.length > 4000) return fieldProblem(request, 'note', 'String should have at most 4000 characters')
  return null
}

/** `Lifecycle._revisable_after_acceptance` (#5350): the checks past the item's own. */
function revisableAfterAcceptance(order: Order, item: OrderItem) {
  if (OPEN_FOR_EDITS.has(order.status)) {
    throw new Refused('order_not_accepted', `order ${order.number} is ${order.status}, not accepted: revise the item with POST /orders/${order.id}/items/${item.id}/revisions`, [item.id])
  }
  if (!TAKES_ITEMS.has(order.status)) throw new Refused('item_locked', `order ${order.number} is ${order.status}: its items' designs are final once shipped`, [item.id])
  notOnHold(order)
  if (!quotesOf(item.id).some((q) => q.accepted)) {
    throw new Refused('item_not_priced', `item ${item.id} has no accepted quote: revise it with POST /orders/${order.id}/items/${item.id}/revisions`, [item.id])
  }
  // #5592: accepted units are real goods of the agreed design. Permanent, so before the
  // transient `print_in_flight` (#5598).
  const accepted = state.fulfilments.filter((f) => f.order_item_id === item.id && f.purpose === 'production' && f.accepted_at)
  if (accepted.length > 0) {
    const made = accepted.reduce((sum, f) => sum + f.quantity, 0)
    throw new Refused('item_partly_fulfilled', `item ${item.id} has accepted attempts (${made} of ${item.quantity} made of the agreed design): cancel the item and add one with the new design`, [item.id])
  }
  if (item.status === 'queued' || item.status === 'printing') throw new Refused('print_in_flight', `item ${item.id} is ${item.status}: settle its print first`, [item.id])
  const open = printInFlight(item.id)
  if (open) throw new Refused('print_in_flight', `item ${item.id}'s attempt ${open.attempt} (${open.purpose}) is still printing: settle it first`, [item.id])
}

/** `POST …/revisions` and, with `afterAcceptance`, `POST …/revisions/after-acceptance` (#5350). */
function addRevision(afterAcceptance: boolean) {
  return async ({ request, params }: { request: Request; params: Record<string, string | readonly string[] | undefined> }) => {
    const body = (await request.json()) as RevisionCreate & Record<string, unknown>
    const extra = unknownKeys(request, body, ['note', 'reference', 'output_id', 'filaments'])
    if (extra) return extra
    const note = badNote(request, body)
    if (note) return note
    let resolved: Resolved | undefined
    if (body.reference || body.output_id) {
      const found = resolveReference({ ...(body.reference ?? {}), output_id: body.output_id ?? null })
      if (Array.isArray(found)) return fieldProblem(request, found[0], found[1])
      resolved = found
    }
    try {
      const order = lock(String(params.id))
      notEnded(order)
      const found = itemOf(order, String(params.item))
      if (found.kind === 'custom') throw new Refused('item_custom', `item ${found.id} is a custom item`)
      if (SETTLED.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`, [found.id])
      if (afterAcceptance) revisableAfterAcceptance(order, found)
      else openForRequote(order, found, "an item's design")
      let reference = found.reference
      let colours = found.colours
      if (resolved) ({ reference, colours } = resolved)
      if (reference === null) throw new Refused('item_has_no_design', `item ${found.id} has no design yet`)
      if (body.filaments) colours = withFilaments({ ...found, colours }, body.filaments).colours
      const output = 'output_id' in body ? (body.output_id ?? null) : sameDesign(reference, found.reference) ? found.output_id : null
      const revision = pushRevision(order, found, reference, colours, body.note ?? '', afterAcceptance)
      found.output_id = output ?? null
      rederive(order)
      return HttpResponse.json(revision, { status: 201 })
    } catch (error) {
      return refused(request, error)
    }
  }
}

export const handlers = [
  http.post(base, addRevision(false)),
  http.post(`${base}/after-acceptance`, addRevision(true)),

  http.post(`${base}/:revision/send`, ({ request, params }) =>
    transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      const found = itemOf(order, String(params.item))
      if (SETTLED.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      const revision = newestOf(found, String(params.revision))
      if (revision.answer !== null) throw new Refused('revision_answered', `revision ${revision.number} is ${revision.answer}`)
      if (revision.sent_at) return revision
      if (!state.attachments.some((a) => a.subject === `order_item_revision:${revision.id}`)) {
        throw new Refused('revision_empty', `revision ${revision.number} has no file to send`)
      }
      if (found.approval === 'together' && !quoteOf(found, revision)) {
        throw new Refused('revision_not_quoted', `revision ${revision.number} has no quote: this item is approved together`)
      }
      revision.sent_at = now()
      event(order, 'revision.sent', { item_id: found.id, revision_id: revision.id })
      return revision
    }),
  ),

  http.post(`${base}/:revision/answer`, async ({ request, params }) => {
    const body = (await request.json()) as RevisionAnswerInput & Record<string, unknown>
    const extra = unknownKeys(request, body, ['answer', 'note'])
    if (extra) return extra
    if (!ANSWERS.has(body.answer)) return fieldProblem(request, 'answer', "Input should be 'accepted' or 'changes_requested'")
    const note = body.note ?? ''
    if (body.answer === 'changes_requested' && !note.trim()) return fieldProblem(request, 'note', 'say what should change')
    return transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      const found = itemOf(order, String(params.item))
      if (SETTLED.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      const revision = newestOf(found, String(params.revision))
      if (revision.answer !== null) throw new Refused('revision_answered', `revision ${revision.number} is ${revision.answer}`)
      if (!revision.sent_at) throw new Refused('revision_not_sent', `revision ${revision.number} was not sent`)
      let quote
      if (body.answer === 'accepted' && found.approval === 'together') {
        quote = quoteOf(found, revision)
        if (!quote) throw new Refused('revision_not_quoted', `revision ${revision.number} has no quote: this item is approved together`)
        openForRequote(order, found, "an item's price")
        notOnHold(order)
      }
      // Every check that can throw comes first (acceptQuote's too); the stamp follows.
      if (quote) checkQuote(found, quote)
      revision.answer = body.answer
      revision.answer_note = note
      revision.answered_at = now()
      revision.answered_by = BROWSER
      event(order, 'revision.answered', { item_id: found.id, revision_id: revision.id, answer: body.answer, note })
      if (quote) acceptQuote(order, found, quote)
      return revision
    })
  }),

  http.post(`${base}/:revision/attachments`, ({ request, params }) => upload(request, String(params.id), String(params.item), null, String(params.revision))),

  // `test_print_item`: one copy of the item's output, an attempt of purpose `test` that
  // counts for nothing (the item, its made quantity and the order stay as they were).
  http.post(`${base}/:revision/test-print`, async ({ request, params }) => {
    if (!request.headers.get('Idempotency-Key')) return problem(request, 428, 'This request needs an Idempotency-Key header', IDEMPOTENCY)
    const body = (await request.json()) as { printer?: string | null }
    try {
      const order = lock(String(params.id))
      notEnded(order)
      notOnHold(order)
      const found = itemOf(order, String(params.item))
      if (SETTLED.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`, [found.id])
      if (found.kind === 'custom') throw new Refused('item_custom', `item ${found.id} is a custom item`)
      if (found.current_revision_id === null) throw new Refused('item_has_no_design', `item ${found.id} has no design yet`)
      const revision = newestOf(found, String(params.revision))
      const attempts = state.fulfilments.filter((f) => f.order_item_id === found.id)
      const open = printInFlight(found.id)
      if (open) throw new Refused('print_in_flight', `item ${found.id}'s attempt ${open.attempt} (${open.purpose}) is still printing: settle it first`, [found.id])
      const quote = quotesOf(found.id).filter((q) => q.revision_id === revision.id).sort(byCreated).at(-1)
      if (!quote || found.output_id === null) throw new Refused('requote_needed', `item ${found.id} needs a new quote before it prints`, [found.id])
      const printer = body.printer == null ? undefined : printersState().find((row) => row.id === body.printer)
      const attempt: Fulfilment = {
        id: newId('f'),
        order_item_id: found.id,
        attempt: attempts.length + 1,
        quantity: 0,
        outcome: 'unknown',
        failure_reason: null,
        started_at: now(),
        finished_at: null,
        notes: '',
        accepted_at: null,
        created_by: BROWSER,
        created_at: now(),
        purpose: 'test',
        revision_id: revision.id,
        print_request_id: printRequestId(found.id, request.headers.get('Idempotency-Key')!),
        print_run_id: newId('run'),
        copies: 1,
        sliced_sha256: quote.sliced_sha256,
        sliced_blob_key: quote.sliced_blob_key,
        printer: printer?.id ?? null,
        printer_serial: printer?.serial ?? null,
        printer_name: printer?.name ?? null,
        bambuddy_printer_id: printer?.bambuddy_printer_id ?? null,
      }
      state.fulfilments.push(attempt)
      event(order, 'print.claimed', { item_id: found.id, fulfilment_id: attempt.id, attempt: attempt.attempt, copies: 1, purpose: 'test', revision_id: revision.id })
      return HttpResponse.json(attempt)
    } catch (error) {
      return refused(request, error)
    }
  }),

  // `attachment_to_model`: a test-print photo of an accepted revision joins its template's photos.
  http.post('/api/v1/orders/:id/attachments/:a/to-model', ({ request, params }) => {
    if (!request.headers.get('Idempotency-Key')) return problem(request, 428, 'This request needs an Idempotency-Key header', IDEMPOTENCY)
    try {
      lock(String(params.id))
      const attachment = state.attachments.find((a) => a.id === params.a && a.order_id === params.id)
      if (!attachment) throw new Refused('attachment_not_found', `no attachment ${String(params.a)}`, [], true)
      if (attachment.role !== 'test_print' || !attachment.revision_id) {
        return fieldProblem(request, 'role', 'not a test-print photo of a revision')
      }
      const revision = state.revisions.find((r) => r.id === attachment.revision_id)
      if (!revision) return problem(request, 404, `no revision '${attachment.revision_id}' of that photo`)
      if (revision.answer !== 'accepted') return problem(request, 409, 'the customer has not accepted this design revision', PROBLEMS + 'revision_not_accepted')
      const model = mockModels().find((m) => m.slug === revision.reference.slug)
      if (!model) return problem(request, 409, `template '${revision.reference.slug}' was deleted; restore it first`, PROBLEMS + 'template_deleted', { slug: revision.reference.slug })
      const extension = attachment.media_type.split('/')[1] ?? 'bin'
      const file = `${attachment.sha256}.${extension}`
      // A copy of this photo already in the gallery is kept, not added again (#5318).
      const held = (model.media ?? []).some((m) => m.source_revision_id === revision.id && m.file === file)
      if (held) return HttpResponse.json(model)
      setMockMedia(model.slug, [
        ...(model.media ?? []),
        {
          id: newId('media'),
          kind: attachment.kind === 'video' ? 'video' : 'image',
          file,
          content_type: attachment.media_type,
          caption: attachment.caption,
          size: attachment.size_bytes,
          missing: false,
          readonly: false,
          source_commit: revision.reference.commit,
          source_revision_id: revision.id,
        },
      ])
      return HttpResponse.json(mockModels().find((m) => m.slug === model.slug))
    } catch (error) {
      return refused(request, error)
    }
  }),
]

/** The revisions live in `state.ts`, which puts the fixtures back; nothing here holds any. */
export function reset(): void {
  // nothing of its own
}
