import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { getSpool, listCatalog, upsertCatalog } from '../../api/filament'
import { SPOOL_ADVANCED_KEY } from '../../components/filament/SpoolSections'
import { IT, spoolId } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { rememberAdvanced } from '../../test/advanced'
import { renderPage } from '../../test/utils'
import { SpoolPage } from './SpoolPage'

// #7280 — a new spool's weighing offers its insert an empty weight. Spool #9 is new: a
// 1000 g label, 1000 g left, no use, on a cardboard spool of 180 g from the catalog.
// Spool #1 has been used. The weigh form is behind the page's Advanced toggle.

beforeEach(() => rememberAdvanced(SPOOL_ADVANCED_KEY))

function open(number: number) {
  return renderPage(
    <Routes>
      <Route path="/filament/spools/:id" element={<SpoolPage />} />
    </Routes>,
    { route: `/filament/spools/${spoolId(number)}` },
  )
}

const heading = (number: number) => screen.findByRole('heading', { level: 1, name: new RegExp(`Spool #${number}\\b`) })

/** Each request to a path that matches, with its body for a POST, as it leaves. */
function recorded(method: string, match: RegExp) {
  const seen: { url: string; body: unknown }[] = []
  server.events.on('request:start', ({ request }) => {
    const url = new URL(request.url)
    if (request.method !== method || !match.test(url.pathname)) return
    if (method === 'GET') seen.push({ url: url.pathname + url.search, body: null })
    else void request.clone().json().then((body) => seen.push({ url: url.pathname, body }), () => {})
  })
  return seen
}

const OVERFILL = "Makers overfill by about 10-30 g, so this estimate comes out slightly heavy. To correct it, edit the insert's empty weight in the catalog."

describe('the weigh form offers an insert weight from a new spool (#7280)', () => {
  it('shows the offer with its numbers, unchecked over a catalog weight', async () => {
    const { user } = open(9)
    await heading(9)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '1200')
    const panel = await screen.findByRole('group', { name: 'Empty spool weight from this new spool' })
    const row = (term: string) => within(panel).getByText(term, { selector: 'dt' }).nextElementSibling
    expect(row('Scale reading')).toHaveTextContent('1200 g')
    expect(row('Label weight')).toHaveTextContent('1000 g')
    expect(row('This empty spool')).toHaveTextContent('200 g')
    expect(row('Now')).toHaveTextContent('180 g, from the catalog')
    expect(row('New weight')).toHaveTextContent('200 g')
    expect(row('Readings used')).toHaveTextContent('1')
    expect(within(panel).getByText(OVERFILL)).toBeInTheDocument()
    expect(within(panel).getByRole('checkbox', { name: 'Save 200 g as the empty weight of Cardboard spool' })).not.toBeChecked()
  })

  it('records the weighing as today when the offer stays unchecked', async () => {
    const accepts = recorded('POST', /\/insert-weight$/)
    const events = recorded('POST', /\/events$/)
    const { user } = open(9)
    await heading(9)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '1200')
    await screen.findByRole('group', { name: 'Empty spool weight from this new spool' })
    await user.click(screen.getByRole('button', { name: 'Record weighing' }))
    await waitFor(() => expect(events.map((one) => one.body)).toEqual([{ kind: 'weighed', grams: 1020, source: 'scale', reading_g: 1200 }]))
    expect(accepts).toEqual([])
    expect((await listCatalog('insert-types')).find((one) => one.id === IT.CARDBOARD)?.empty_weight_g).toBe(180)
  })

  it('starts checked over a placeholder, accepts first, then weighs with the new empty weight', async () => {
    await upsertCatalog('insert-types', { note: 'Placeholder weight, not measured.' }, IT.CARDBOARD)
    const accepts = recorded('POST', /\/insert-weight$/)
    const events = recorded('POST', /\/events$/)
    const { user } = open(9)
    await heading(9)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '1200')
    const panel = await screen.findByRole('group', { name: 'Empty spool weight from this new spool' })
    expect(within(panel.querySelector('dl')!).getByText('180 g, a placeholder')).toBeInTheDocument()
    expect(within(panel).getByRole('checkbox', { name: /Save 200 g/ })).toBeChecked()
    await user.click(screen.getByRole('button', { name: 'Record weighing' }))
    await waitFor(() => expect(events.map((one) => one.body)).toEqual([{ kind: 'weighed', grams: 1000, source: 'scale', reading_g: 1200 }]))
    expect(accepts.map((one) => one.body)).toEqual([{ reading_g: 1200, new_empty_weight_g: 200 }])
    expect((await getSpool(spoolId(9))).grams_left).toBe(1000)
    const insert = (await listCatalog('insert-types')).find((one) => one.id === IT.CARDBOARD)
    expect(insert).toMatchObject({ empty_weight_g: 200, empty_weight_basis: 'derived', derived_count: 1 })
    expect(await screen.findByText('Empty spool 200 g (Cardboard spool), from 1 new spool.')).toBeInTheDocument()
  })

  it('accepts when a person checks the offer', async () => {
    const accepts = recorded('POST', /\/insert-weight$/)
    const events = recorded('POST', /\/events$/)
    const { user } = open(9)
    await heading(9)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '1210')
    await user.click(await screen.findByRole('checkbox', { name: /Save 210 g/ }))
    await user.click(screen.getByRole('button', { name: 'Record weighing' }))
    await waitFor(() => expect(events.map((one) => one.body)).toEqual([{ kind: 'weighed', grams: 1000, source: 'scale', reading_g: 1210 }]))
    expect(accepts.map((one) => one.body)).toEqual([{ reading_g: 1210, new_empty_weight_g: 210 }])
  })

  it('shows no offer for a used spool, and weighs as today', async () => {
    const checks = recorded('GET', /\/insert-weight-offer$/)
    const accepts = recorded('POST', /\/insert-weight$/)
    const events = recorded('POST', /\/events$/)
    const { user } = open(1)
    await heading(1)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '900')
    await waitFor(() => expect(checks.map((one) => one.url)).toContain(`/api/v1/filament/spools/${spoolId(1)}/insert-weight-offer?reading_g=900`))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Record weighing' })).toBeEnabled())
    expect(screen.queryByRole('group', { name: 'Empty spool weight from this new spool' })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Record weighing' }))
    await waitFor(() => expect(events.map((one) => one.body)).toEqual([{ kind: 'weighed', grams: 650, source: 'scale', reading_g: 900 }]))
    expect(accepts).toEqual([])
  })

  it('waits for the check of the reading before it records the weighing', async () => {
    const events = recorded('POST', /\/events$/)
    const { user } = open(9)
    await heading(9)
    const input = screen.getByLabelText('Scale reading, with the spool (g)')
    await user.type(input, '1200')
    const button = screen.getByRole('button', { name: 'Record weighing' })
    expect(button).toBeDisabled()
    await user.type(input, '{Enter}')
    await screen.findByRole('group', { name: 'Empty spool weight from this new spool' })
    expect(button).toBeEnabled()
    expect(events).toEqual([])
  })

  it('says that the offer replaces a weight a person set (#7450-3)', async () => {
    await upsertCatalog('insert-types', { empty_weight_g: 185 }, IT.CARDBOARD)
    const { user } = open(9)
    await heading(9)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '1200')
    const panel = await screen.findByRole('group', { name: 'Empty spool weight from this new spool' })
    expect(within(panel).getByText('This replaces the 185 g a person set.')).toBeInTheDocument()
  })

  it('says why a spool weighed into a mean gets no later offer (#7451-3)', async () => {
    server.use(
      http.get('*/api/v1/filament/spools/:id/insert-weight-offer', () =>
        HttpResponse.json({
          offer: { insert_type_id: IT.CARDBOARD, insert_name: 'Cardboard spool', spool_id: spoolId(9), reading_g: 1200, label_weight_g: 1000, derived_g: 200, current_empty_weight_g: 190, current_basis: 'derived', placeholder: false, new_empty_weight_g: 195, sample_count: 2, checked_by_default: true },
          reason: null,
          insert_type_id: IT.CARDBOARD,
          empty_weight_g: 190,
          filament_g: 1010,
        }),
      ),
    )
    const { user } = open(9)
    await heading(9)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '1200')
    const panel = await screen.findByRole('group', { name: 'Empty spool weight from this new spool' })
    expect(within(panel).getByText("The new weight is a mean, so this spool's grams left can end a few grams off its label weight. It then gets no new offer.")).toBeInTheDocument()
  })

  it('asks for a new check when the weight changed before the accept (#7450-2)', async () => {
    await upsertCatalog('insert-types', { note: 'Placeholder weight, not measured.' }, IT.CARDBOARD)
    const checks = recorded('GET', /\/insert-weight-offer$/)
    const events = recorded('POST', /\/events$/)
    const { user } = open(9)
    await heading(9)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '1200')
    await screen.findByRole('group', { name: 'Empty spool weight from this new spool' })
    server.use(
      http.post(
        '*/api/v1/filament/spools/:id/insert-weight',
        () => HttpResponse.json({ title: 'Conflict', status: 409, detail: 'the insert weight is now 190 g; check again', code: 'insert_weight_changed', new_empty_weight_g: 190 }, { status: 409 }),
        { once: true },
      ),
    )
    const before = checks.length
    await user.click(screen.getByRole('button', { name: 'Record weighing' }))
    expect(await screen.findByText("The insert's empty weight changed since the check. Check the new numbers, then record the weighing again.")).toBeInTheDocument()
    await waitFor(() => expect(checks.length).toBeGreaterThan(before))
    expect(events).toEqual([])
  })

  it('records the weighing when the check does not answer (#7452)', async () => {
    server.use(http.get('*/api/v1/filament/spools/:id/insert-weight-offer', () => new Promise<never>(() => {})))
    const { user } = open(9)
    await heading(9)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '1200')
    expect(await screen.findByText('The empty weight check did not answer. You can record the weighing without it.', undefined, { timeout: 15_000 })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Record weighing' })).toBeEnabled()
  }, 20_000)
})

describe('the weigh form asks what is up when a new spool reads under its label (#7683)', () => {
  const GROUP = 'The reading is 100 g under the label weight. What is up?'

  it('shows the three readings with the default checked and records the weighing with it', async () => {
    const events = recorded('POST', /\/events$/)
    const { user } = open(9)
    await heading(9)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '900')
    const group = await screen.findByRole('group', { name: GROUP })
    expect(within(group).getByRole('radio', { name: 'The scale was tared on the empty spool: 900 g of filament' })).toBeChecked()
    expect(within(group).getByRole('radio', { name: 'The spool is short: 720 g of filament, 100 g under its label' })).not.toBeChecked()
    expect(within(group).getByRole('radio', { name: 'The label weight is wrong: 720 g of filament, and the label becomes 720 g' })).not.toBeChecked()
    expect(screen.getByText('900 g of filament.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Record weighing' }))
    await waitFor(() => expect(events.map((one) => one.body)).toEqual([{ kind: 'weighed', source: 'scale', reading_g: 900, resolution: 'tared' }]))
    expect((await getSpool(spoolId(9))).grams_left).toBe(900)
  })

  it('records the reading the person chose', async () => {
    const events = recorded('POST', /\/events$/)
    const { user } = open(9)
    await heading(9)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '900')
    await user.click(await screen.findByRole('radio', { name: /The spool is short/ }))
    expect(screen.getByText('720 g of filament.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Record weighing' }))
    await waitFor(() => expect(events.map((one) => one.body)).toEqual([{ kind: 'weighed', source: 'scale', reading_g: 900, resolution: 'short' }]))
    expect(await getSpool(spoolId(9))).toMatchObject({ grams_left: 720, added_full: false })
  })
})
