import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { listPresets } from '../../api/filamentPresets'
import { presetsOff } from '../../mocks/features/filament/presets'
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
