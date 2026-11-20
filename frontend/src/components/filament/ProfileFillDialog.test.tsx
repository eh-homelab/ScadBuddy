import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setMockBambuddyKValues } from '../../mocks/features/filament'
import { T } from '../../mocks/features/filament/seed'
import { resetMockState } from '../../mocks/handlers'
import { renderPage } from '../../test/utils'
import { ProfileFillDialog } from './ProfileFillDialog'

const K = { printer_id: '00000000-0000-4000-8000-000000006747', extruder: 0, nozzle_diameter: '0.4', k_value: '0.02' }

function show(onApplied = vi.fn()) {
  renderPage(<ProfileFillDialog typeId={T.PINK} open onClose={() => {}} onApplied={onApplied} />)
  return onApplied
}

describe('ProfileFillDialog (#6714)', () => {
  beforeEach(() => resetMockState())

  it('writes the K values the refresh showed', async () => {
    setMockBambuddyKValues(T.PINK, [K])
    const onApplied = show()
    await userEvent.click(await screen.findByRole('button', { name: /^Apply \d+ changes?$/ }))
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1))
    expect(onApplied.mock.calls[0]?.[0].k_values).toHaveLength(1)
  })

  it('shows the refusal when Bambuddy gives other K values since the refresh (R-6714-k)', async () => {
    setMockBambuddyKValues(T.PINK, [K])
    const onApplied = show()
    const apply = await screen.findByRole('button', { name: /^Apply \d+ changes?$/ })
    expect(screen.getByText('Add 1 K value from its Bambuddy spools')).toBeInTheDocument()
    setMockBambuddyKValues(T.PINK, [{ ...K, k_value: '0.03' }])
    await userEvent.click(apply)
    expect(await screen.findByRole('alert')).toHaveTextContent('the K values now are not the ones shown: refresh again')
    expect(onApplied).not.toHaveBeenCalled()
  })
})
