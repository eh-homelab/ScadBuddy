import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { getSpool, listSpoolEvents } from '../../../api/filament'
import { spoolId } from '../../../mocks/features/filament/seed'
import { renderPage } from '../../../test/utils'
import { WeightField } from './WeightField'

const lastWeighing = async (id: string) => (await listSpoolEvents(id)).filter((one) => one.kind === 'weighed').at(-1)

describe('WeightField (#7273)', () => {
  it('subtracts the empty spool from the gross weight', async () => {
    // #3: its type sits on the 250 g Bambu reusable spool.
    const spool = await getSpool(spoolId(3))
    const { user } = renderPage(<WeightField spool={spool} />)
    const field = await screen.findByLabelText('Gross weight (g)')
    await user.type(field, '1250')
    expect(screen.getByText('1 250 g − 250 g empty spool = 1 000 g of filament')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save weight' }))
    expect(await screen.findByText('Saved')).toBeInTheDocument()
    const event = await lastWeighing(spool.id)
    // #7682: the reading goes with the grams, so the event keeps its breakdown.
    expect(event).toMatchObject({ kind: 'weighed', source: 'person', grams_after: 1000, data: { reading_g: 1250, empty_g: 250 } })
  })

  it('sends a decimal reading alone, so the server takes the insert off it (#7682)', async () => {
    const spool = await getSpool(spoolId(3))
    const { user } = renderPage(<WeightField spool={spool} />)
    await user.type(await screen.findByLabelText('Gross weight (g)'), '1250.4')
    await user.click(screen.getByRole('button', { name: 'Save weight' }))
    expect(await screen.findByText('Saved')).toBeInTheDocument()
    const event = await lastWeighing(spool.id)
    expect(event).toMatchObject({ grams_after: expect.closeTo(1000.4, 5), data: { reading_g: 1250.4, empty_source: 'insert', empty_name: 'Bambu reusable spool' } })
  })

  it('takes the filament weight when the empty spool is unknown', async () => {
    // #6 has no type and no spool insert.
    const spool = await getSpool(spoolId(6))
    const { user } = renderPage(<WeightField spool={spool} />)
    const field = await screen.findByLabelText('Filament weight (g)')
    expect(screen.queryByLabelText('Gross weight (g)')).toBeNull()
    await user.type(field, '432')
    await user.click(screen.getByRole('button', { name: 'Save weight' }))
    await waitFor(async () => expect(await lastWeighing(spool.id)).toMatchObject({ grams_after: 432, source: 'person' }))
    expect((await lastWeighing(spool.id))?.data).not.toHaveProperty('reading_g')
  })

  it.each(['1e3', '0x10', '1,243', '-5', '12 kg'])('refuses %s as a weight', async (typed) => {
    const spool = await getSpool(spoolId(6))
    const before = (await listSpoolEvents(spool.id)).length
    const { user } = renderPage(<WeightField spool={spool} />)
    await user.type(await screen.findByLabelText('Filament weight (g)'), typed)
    await user.click(screen.getByRole('button', { name: 'Save weight' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter the weight in grams, such as 1243.')
    expect(await listSpoolEvents(spool.id)).toHaveLength(before)
  })

  it('hands the spool, read again, to the page after a save, and keeps its live region mounted', async () => {
    const spool = await getSpool(spoolId(3))
    const saved = vi.fn()
    const { user } = renderPage(<WeightField spool={spool} onSaved={saved} />)
    const field = await screen.findByLabelText('Gross weight (g)')
    const live = screen.getByRole('status', { name: 'Weight saved' })
    expect(live).toHaveTextContent('')
    await user.type(field, '1050')
    await user.click(screen.getByRole('button', { name: 'Save weight' }))
    await waitFor(() => expect(live).toHaveTextContent('Saved'))
    expect(screen.getByRole('status', { name: 'Weight saved' })).toBe(live)
    await waitFor(() => expect(saved).toHaveBeenCalledOnce())
    expect(saved.mock.calls[0]![0]).toMatchObject({ id: spool.id, grams_left: 800 })
  })

  it('refuses a weight below the empty spool', async () => {
    const spool = await getSpool(spoolId(3))
    const before = (await listSpoolEvents(spool.id)).length
    const { user } = renderPage(<WeightField spool={spool} />)
    await user.type(await screen.findByLabelText('Gross weight (g)'), '200')
    await user.click(screen.getByRole('button', { name: 'Save weight' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The weight is less than the empty spool.')
    expect(await listSpoolEvents(spool.id)).toHaveLength(before)
  })

  it('asks what is up when a new spool reads under its label, and saves the choice (#7683)', async () => {
    // #9 is new: a 1000 g label, 1000 g left, no use, on a 180 g cardboard spool.
    const spool = await getSpool(spoolId(9))
    const { user } = renderPage(<WeightField spool={spool} />)
    await user.type(await screen.findByLabelText('Gross weight (g)'), '950.5')
    const group = await screen.findByRole('group', { name: 'The reading is 49.5 g under the label weight. What is up?' })
    expect(within(group).getByRole('radio', { name: 'The scale was tared on the empty spool: 950.5 g of filament' })).toBeChecked()
    expect(within(group).getByRole('radio', { name: 'The spool is short: 770.5 g of filament, 49.5 g under its label' })).not.toBeChecked()
    await user.click(screen.getByRole('button', { name: 'Save weight' }))
    expect(await screen.findByText('Saved')).toBeInTheDocument()
    const event = await lastWeighing(spool.id)
    expect(event).toMatchObject({ source: 'person', grams_after: 950.5, data: { reading_g: 950.5, resolution: 'tared', shortfall_g: 49.5, empty_source: 'tared' } })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('saves the reading the person chose, even before the check answered (#7683)', async () => {
    const spool = await getSpool(spoolId(9))
    const { user } = renderPage(<WeightField spool={spool} />)
    await user.type(await screen.findByLabelText('Gross weight (g)'), '800')
    await user.click(await screen.findByRole('radio', { name: /The spool is short/ }))
    await user.click(screen.getByRole('button', { name: 'Save weight' }))
    expect(await screen.findByText('Saved')).toBeInTheDocument()
    expect(await lastWeighing(spool.id)).toMatchObject({ grams_after: 620, data: { resolution: 'short' } })
    expect((await getSpool(spool.id)).added_full).toBe(false)
  })
})
