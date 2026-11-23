import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { getSpool, listCatalog, upsertCatalog } from '../../api/filament'
import { IT, spoolId } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { SpoolPage } from './SpoolPage'

// #7280 — a new spool's weighing offers its insert an empty weight. Spool #9 is new: a
// 1000 g label, 1000 g left, no use, on a cardboard spool of 180 g from the catalog.
// Spool #1 has been used.

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

const OVERFILL = 'Makers overfill by about 10-30 g, so this estimate comes out slightly heavy. A later weighing of an empty spool replaces it.'

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
    await waitFor(() => expect(events.map((one) => one.body)).toEqual([{ kind: 'weighed', grams: 1020, source: 'scale' }]))
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
    await waitFor(() => expect(events.map((one) => one.body)).toEqual([{ kind: 'weighed', grams: 1000, source: 'scale' }]))
    expect(accepts.map((one) => one.body)).toEqual([{ reading_g: 1200 }])
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
    await waitFor(() => expect(events.map((one) => one.body)).toEqual([{ kind: 'weighed', grams: 1000, source: 'scale' }]))
    expect(accepts.map((one) => one.body)).toEqual([{ reading_g: 1210 }])
  })

  it('shows no offer for a used spool, and weighs as today', async () => {
    const checks = recorded('GET', /\/insert-weight-offer$/)
    const accepts = recorded('POST', /\/insert-weight$/)
    const events = recorded('POST', /\/events$/)
    const { user } = open(1)
    await heading(1)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '900')
    await waitFor(() => expect(checks.map((one) => one.url)).toContain(`/api/v1/filament/spools/${spoolId(1)}/insert-weight-offer?reading_g=900`))
    expect(screen.queryByRole('group', { name: 'Empty spool weight from this new spool' })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Record weighing' }))
    await waitFor(() => expect(events.map((one) => one.body)).toEqual([{ kind: 'weighed', grams: 650, source: 'scale' }]))
    expect(accepts).toEqual([])
  })
})
