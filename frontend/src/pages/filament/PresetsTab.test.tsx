import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { listTypes } from '../../api/filament'
import { listPresets } from '../../api/filamentPresets'
import { presetsOff } from '../../mocks/features/filament/presets'
import { T } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { http, HttpResponse } from 'msw'
import { renderPage } from '../../test/utils'
import { PresetsTab } from './PresetsTab'

const presets = () => screen.findByRole('table', { name: 'Presets' })
const presetRows = (table: HTMLElement) => within(table).getAllByRole('row').filter((row) => row.dataset.preset)
const rowOf = (rows: HTMLElement[], id: number) => rows.find((one) => one.dataset.preset === String(id))!

describe('the Presets tab', () => {
  it('lists each preset with its source, version and mirror state', async () => {
    renderPage(<PresetsTab />)
    const rows = presetRows(await presets())
    const { rows: all } = await listPresets()
    expect(rows).toHaveLength(all.length)
    const failed = all.find((row) => row.mirror === 'failed')!
    const row = rowOf(rows, failed.id)
    expect(within(row).getByText('Manual')).toBeInTheDocument()
    expect(within(row).getByText(`v${failed.version}`)).toBeInTheDocument()
    expect(within(row).getByText('Failed')).toHaveAttribute('title', failed.mirror_error)
    expect(within(row).getByText(failed.mirror_error!)).toBeVisible()
    const sent = rowOf(rows, all.find((one) => one.mirror === 'sent')!.id)
    expect(within(sent).getByText('3DFP')).toBeInTheDocument()
    expect(within(sent).getByText('Sent')).toBeInTheDocument()
  })

  it('filters by source', async () => {
    const { user } = renderPage(<PresetsTab />)
    await presets()
    await user.click(screen.getByRole('button', { name: 'Bambuddy' }))
    const rows = presetRows(await presets())
    expect(rows).toHaveLength(1)
    expect(within(rows[0]!).getByText('Bambuddy')).toBeInTheDocument()
  })

  it('filters by type from the URL', async () => {
    const { user } = renderPage(<PresetsTab />, { route: `/?type_id=${T.ORANGE}` })
    const types = await listTypes({ includeArchived: true })
    const code = types.find((type) => type.id === T.ORANGE)!.code
    const chip = await screen.findByRole('button', { name: `Clear the type filter (Type: ${code})` })
    const rows = presetRows(await presets())
    expect(rows).toHaveLength(1)
    expect(within(rows[0]!).getByRole('link', { name: code })).toHaveAttribute('href', `/filament/types/${T.ORANGE}`)
    await user.click(chip)
    expect(presetRows(await presets()).length).toBeGreaterThan(1)
    expect(screen.queryByRole('button', { name: /^Clear the type filter/ })).not.toBeInTheDocument()
  })

  it('says no preset has the type when the type filter matches nothing', async () => {
    renderPage(<PresetsTab />, { route: '/?type_id=00000000-0000-4000-8000-000000000000' })
    expect(await screen.findByText('No preset has this type.')).toBeInTheDocument()
    expect(screen.queryByText(/^No presets yet/)).not.toBeInTheDocument()
  })

  it('keeps the type filter\'s clear button when the list fails', async () => {
    server.use(http.get('/api/v1/filament/presets', () => HttpResponse.json({ detail: 'bad type', code: 'invalid_type_id' }, { status: 422 })))
    const { user } = renderPage(<PresetsTab />, { route: '/?type_id=not-a-uuid' })
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    server.resetHandlers()
    await user.click(screen.getByRole('button', { name: /^Clear the type filter/ }))
    expect(presetRows(await presets()).length).toBeGreaterThan(1)
  })

  it('shows no rows of the last type while the new type loads', async () => {
    let release: () => void = () => {}
    const { user } = renderPage(<PresetsTab />, { route: `/?type_id=${T.ORANGE}` })
    expect(presetRows(await presets())).toHaveLength(1)
    const gate = new Promise<void>((resolve) => (release = resolve))
    server.use(
      http.get('/api/v1/filament/presets', async () => {
        await gate
        return undefined
      }),
    )
    await user.click(screen.getByRole('button', { name: /^Clear the type filter/ }))
    expect(await screen.findByText('Loading presets…')).toBeInTheDocument()
    expect(screen.queryByRole('table', { name: 'Presets' })).not.toBeInTheDocument()
    release()
    expect(presetRows(await presets()).length).toBeGreaterThan(1)
  })

  it('uses the singular for a count of one', async () => {
    renderPage(<PresetsTab />)
    await presets()
    expect(await screen.findByText('1 type, 2 spools')).toBeInTheDocument()
  })

  it('names a type by the start of its id when the types cannot be loaded', async () => {
    server.use(http.get('/api/v1/filament/types', () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
    renderPage(<PresetsTab />)
    const row = presetRows(await presets()).find((one) => one.dataset.preset === '1')!
    const link = within(row).getByRole('link', { name: `Type ${T.JADE}` })
    expect(link).toHaveTextContent(T.JADE.slice(0, 8))
  })

  it("opens a preset's page from its name", async () => {
    renderPage(<PresetsTab />)
    const { rows: all } = await listPresets()
    const first = all[0]!
    const row = rowOf(presetRows(await presets()), first.id)
    expect(within(row).getByRole('link', { name: first.name })).toHaveAttribute('href', `/filament/presets/${first.id}`)
  })

  it('shows the Bambuddy id, or none', async () => {
    renderPage(<PresetsTab />)
    const { rows: all } = await listPresets()
    const rows = presetRows(await presets())
    const withId = all.find((row) => row.bb_preset_id !== null)!
    const without = all.find((row) => row.bb_preset_id === null)!
    expect(within(rowOf(rows, withId.id)).getByText(String(withId.bb_preset_id))).toBeInTheDocument()
    expect(within(rowOf(rows, without.id)).getByText('None')).toBeInTheDocument()
  })

  it('says presets are not on when the area is off', async () => {
    presetsOff()
    renderPage(<PresetsTab />)
    expect(await screen.findByText('Presets are not on yet on this server.')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Manual' })).not.toBeInTheDocument()
  })
})
