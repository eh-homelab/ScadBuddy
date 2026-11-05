import { beforeEach, describe, expect, it } from 'vitest'
import { api, type ApiError } from '../../../api/client'
import type { PrintRunRequest } from '../../../api/types'
import { resetMockState } from '../../handlers'
import { MAZE_SLUG } from '../../fixtures'
import { ITEM_APPROVED, ITEM_DRAFT, ORDER_ACCEPTED, ORDER_DRAFT, OUTPUT_REAGAN, PROBLEMS, state } from './state'

// Design revisions (#5280): the mock answers the lifecycle's rules and refusals as
// `orders/lifecycle.py` does (`_revise`, `send_revision`, `answer_revision`).

const failure = (promise: Promise<unknown>) => promise.then(() => undefined, (e: unknown) => e as ApiError)
// The mock reads none of the dialog's choices.
const NO_CHOICES = {} as PrintRunRequest
const PNG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
const photo = () => new File([new Uint8Array([...PNG, 0, 0, 0, 0])], 'render.png', { type: 'image/png' })
const revisionsOf = async (orderId: string, itemId: string) => (await api.order(orderId)).items.find((i) => i.id === itemId)!.revisions
const type = (slug: string) => `${PROBLEMS}${slug}`

/** A revision with a file and a quote that names it, ready to send. */
async function ready(note = 'next') {
  const revision = await api.addOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, { note })
  await api.addRevisionAttachment(ORDER_DRAFT, ITEM_DRAFT, revision.id, photo(), { role: 'render' })
  const quote = await api.quoteOrderItem(ORDER_DRAFT, ITEM_DRAFT, { unit_price_minor: 1200 })
  return { revision, quote }
}

describe('revisions mock (#5280)', () => {
  beforeEach(() => resetMockState())

  it('starts every model item at revision 1, and its quotes name it', async () => {
    const [first] = await revisionsOf(ORDER_ACCEPTED, ITEM_APPROVED)
    const approved = (await api.order(ORDER_ACCEPTED)).items.find((i) => i.id === ITEM_APPROVED)!
    expect(first).toMatchObject({ number: 1, answer: null, sent_at: null })
    expect(approved.current_revision_id).toBe(first!.id)
    expect(approved.approval).toBe('together')
    expect(approved.quotes[0]?.revision_id).toBe(first!.id)
  })

  it('a new revision supersedes a sent one and drops the quotes', async () => {
    const { revision, quote } = await ready()
    await api.acceptOrderQuote(ORDER_DRAFT, ITEM_DRAFT, quote.id)
    await api.sendOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id)
    await api.addOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, { note: 'again' })
    const item = (await api.order(ORDER_DRAFT)).items[0]!
    expect(item.revisions.map((r) => [r.number, r.answer])).toEqual([[1, null], [2, 'superseded'], [3, null]])
    expect(item.status).toBe('draft')
    expect(item.quotes.every((q) => !q.accepted)).toBe(true)
    expect(item.current_revision_id).toBe(item.revisions[2]!.id)
  })

  it('answers only the newest revision', async () => {
    const [first] = await revisionsOf(ORDER_DRAFT, ITEM_DRAFT)
    await api.addOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, { note: 'newer' })
    const res = await fetch(`/api/v1/orders/${ORDER_DRAFT}/items/${ITEM_DRAFT}/revisions/${first!.id}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer: 'accepted', note: '' }),
    })
    expect(res.status).toBe(409)
    expect(((await res.json()) as { type: string }).type).toBe(type('revision_not_newest'))
    const missing = await failure(api.sendOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, 'nope'))
    expect(missing?.status).toBe(404)
  })

  it('refuses to send an empty or unquoted revision, and to answer one not sent or already answered', async () => {
    const revision = await api.addOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, { note: 'x' })
    expect((await failure(api.sendOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id)))?.problem.type).toBe(type('revision_empty'))
    await api.addRevisionAttachment(ORDER_DRAFT, ITEM_DRAFT, revision.id, photo(), { role: 'render' })
    expect((await failure(api.sendOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id)))?.problem.type).toBe(type('revision_not_quoted'))
    expect((await failure(api.answerOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id, { answer: 'accepted', note: '' })))?.problem.type).toBe(type('revision_not_sent'))
    await api.quoteOrderItem(ORDER_DRAFT, ITEM_DRAFT, { unit_price_minor: 900 })
    await api.sendOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id)
    expect((await failure(api.answerOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id, { answer: 'changes_requested', note: '' })))?.status).toBe(422)
    await api.answerOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id, { answer: 'changes_requested', note: 'bigger' })
    expect((await failure(api.answerOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id, { answer: 'accepted', note: '' })))?.problem.type).toBe(type('revision_answered'))
    expect((await failure(api.sendOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id)))?.problem.type).toBe(type('revision_answered'))
  })

  it('together accepts the revision and its quote', async () => {
    const { revision, quote } = await ready()
    await api.sendOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id)
    const answered = await api.answerOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id, { answer: 'accepted', note: 'lovely' })
    expect(answered).toMatchObject({ answer: 'accepted', answer_note: 'lovely' })
    const item = (await api.order(ORDER_DRAFT)).items[0]!
    expect(item.status).toBe('approved')
    expect(item.quotes.find((q) => q.id === quote.id)?.accepted).toBe(true)
  })

  it('design first needs the revision accepted before a price is', async () => {
    await api.updateOrderItem(ORDER_DRAFT, ITEM_DRAFT, { approval: 'design_first' })
    const { revision, quote } = await ready()
    expect((await failure(api.acceptOrderQuote(ORDER_DRAFT, ITEM_DRAFT, quote.id)))?.problem.type).toBe(type('revision_not_accepted'))
    await api.sendOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id)
    await api.answerOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id, { answer: 'accepted', note: '' })
    expect((await api.order(ORDER_DRAFT)).items[0]!.status).toBe('quoted')
    expect((await api.acceptOrderQuote(ORDER_DRAFT, ITEM_DRAFT, quote.id)).status).toBe('approved')
  })

  it('a changed reference or filament is a revision, the same one is not', async () => {
    const before = await revisionsOf(ORDER_DRAFT, ITEM_DRAFT)
    const item = (await api.order(ORDER_DRAFT)).items[0]!
    await api.updateOrderItem(ORDER_DRAFT, ITEM_DRAFT, { reference: { slug: item.reference!.slug, commit: item.reference!.commit, inputs: item.reference!.inputs } })
    expect(await revisionsOf(ORDER_DRAFT, ITEM_DRAFT)).toHaveLength(before.length)
    await api.updateOrderItem(ORDER_DRAFT, ITEM_DRAFT, { filaments: { '0': (await api.priceBook()).filaments[0]!.id } })
    const after = await revisionsOf(ORDER_DRAFT, ITEM_DRAFT)
    expect(after).toHaveLength(before.length + 1)
    expect(after.at(-1)!.colours[0]!.filament_id).not.toBeNull()
    await api.updateOrderItem(ORDER_DRAFT, ITEM_DRAFT, { quantity: 5 })
    expect(await revisionsOf(ORDER_DRAFT, ITEM_DRAFT)).toHaveLength(after.length)
  })

  it('a request item has no design until its first revision names one', async () => {
    const request = await api.addOrderItem(ORDER_DRAFT, { kind: 'request', title: 'A thing', quantity: 1 })
    expect(request).toMatchObject({ kind: 'request', approval: 'design_first', reference: null, current_revision_id: null })
    expect((await failure(api.addOrderItem(ORDER_DRAFT, { kind: 'request', quantity: 1 })))?.status).toBe(422)
    expect((await failure(api.addOrderItemRevision(ORDER_DRAFT, request.id, {})))?.problem.type).toBe(type('item_has_no_design'))
    const model = await api.getModel(MAZE_SLUG)
    const first = await api.addOrderItemRevision(ORDER_DRAFT, request.id, { reference: { slug: MAZE_SLUG, commit: model.version ?? '', inputs: { params: {}, v: 0 } } })
    expect(first.number).toBe(1)
    const custom = await api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'Fee', quantity: 1, unit_price_minor: 100 })
    expect((await failure(api.addOrderItemRevision(ORDER_DRAFT, custom.id, {})))?.problem.type).toBe(type('item_custom'))
  })

  it('test-prints the newest revision, which counts for nothing, then adds its accepted photo to the model', async () => {
    const { revision, quote } = await ready()
    expect((await failure(api.testPrintRevision(ORDER_DRAFT, ITEM_DRAFT, 'nope', NO_CHOICES)))?.status).toBe(404)
    await api.acceptOrderQuote(ORDER_DRAFT, ITEM_DRAFT, quote.id).catch(() => undefined)
    const attempt = await api.testPrintRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id, NO_CHOICES)
    expect(attempt).toMatchObject({ purpose: 'test', revision_id: revision.id, copies: 1 })
    expect((await failure(api.testPrintRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id, NO_CHOICES)))?.problem.type).toBe(type('print_in_flight'))
    const detail = await api.order(ORDER_DRAFT)
    expect(detail.items[0]!.status).not.toBe('queued')
    expect(detail.order.status).not.toBe('in_production')
    const shot = await api.addRevisionAttachment(ORDER_DRAFT, ITEM_DRAFT, revision.id, photo(), { role: 'test_print', fulfilmentId: attempt.id })
    expect(shot).toMatchObject({ revision_id: revision.id, fulfilment_id: attempt.id, role: 'test_print' })
    expect((await failure(api.revisionPhotoToModel(ORDER_DRAFT, shot.id)))?.problem.type).toBe(type('revision_not_accepted'))
    await api.sendOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id)
    await api.answerOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id, { answer: 'accepted', note: '' })
    const model = await api.revisionPhotoToModel(ORDER_DRAFT, shot.id)
    expect(model.media?.at(-1)).toMatchObject({ source_revision_id: revision.id })
  })

  it('refuses a production print while a test print is open', async () => {
    const [first] = await revisionsOf(ORDER_ACCEPTED, ITEM_APPROVED)
    await api.testPrintRevision(ORDER_ACCEPTED, ITEM_APPROVED, first!.id, NO_CHOICES)
    const refused = await failure(api.printItem(ORDER_ACCEPTED, ITEM_APPROVED, NO_CHOICES as never))
    expect(refused?.status).toBe(409)
    expect(refused?.problem.type).toBe(type('print_in_flight'))
  })

  it('fills the colours of an item that has none by amending its revision in place', async () => {
    const made = await api.addOrderItem(ORDER_DRAFT, { output_id: OUTPUT_REAGAN, quantity: 1 })
    const quote = await api.quoteOrderItem(ORDER_DRAFT, made.id, { unit_price_minor: 700 })
    await api.acceptOrderQuote(ORDER_DRAFT, made.id, quote.id)
    // An item made by hand has no slots until its first render brings them.
    state.items.find((i) => i.id === made.id)!.colours = []
    state.revisions.find((r) => r.id === made.current_revision_id)!.colours = []
    await api.updateOrderItem(ORDER_DRAFT, made.id, { output_id: OUTPUT_REAGAN })
    const detail = (await api.order(ORDER_DRAFT)).items.find((i) => i.id === made.id)!
    expect(detail.revisions).toHaveLength(1)
    expect(detail.revisions[0]!.colours.length).toBeGreaterThan(0)
    expect(detail.colours).toEqual(detail.revisions[0]!.colours)
    expect(detail.status).toBe('approved')
    expect(detail.quotes.find((q) => q.id === quote.id)?.accepted).toBe(true)
    const events = await api.orderEvents(ORDER_DRAFT)
    expect(events.some((e) => e.kind === 'revision.amended')).toBe(true)
  })

  it('leaves the revision unanswered when its quote has gone stale', async () => {
    const { revision } = await ready()
    await api.sendOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id)
    await api.updateOrderItem(ORDER_DRAFT, ITEM_DRAFT, { quantity: 5 })
    const refused = await failure(api.answerOrderItemRevision(ORDER_DRAFT, ITEM_DRAFT, revision.id, { answer: 'accepted', note: '' }))
    expect(refused?.status).toBe(409)
    expect(refused?.problem.type).toBe(type('quote_stale'))
    expect((await revisionsOf(ORDER_DRAFT, ITEM_DRAFT)).at(-1)).toMatchObject({ answer: null, answered_at: null })
  })
})
