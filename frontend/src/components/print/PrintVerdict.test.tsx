import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PrintCheck } from '../../api/types'
import { seedUnknownTray } from '../../mocks/features/filament/unknownTrays'
import { resetMockState } from '../../mocks/handlers'
import { NeedsYouHarness } from '../../test/needsYou'
import { renderPage } from '../../test/utils'
import { PrintVerdict } from './PrintVerdict'

describe('PrintVerdict · unknown spool (#7143)', () => {
  beforeEach(() => resetMockState())

  it('offers to link the spool the block names', async () => {
    const tray = seedUnknownTray()
    const verdict = {
      errors: ['A spool in AMS-A slot 2 is not known.'],
      warnings: [],
      unknown_trays: [{ id: tray.id, slot: 'AMS-A slot 2' }],
    } as unknown as PrintCheck
    const { user } = renderPage(
      <NeedsYouHarness>
        <PrintVerdict verdict={verdict} onRetry={vi.fn()} />
      </NeedsYouHarness>,
    )
    await user.click(await screen.findByRole('button', { name: 'Link the spool in AMS-A slot 2' }))
    expect(await screen.findByRole('dialog', { name: 'Unknown spool in 3DP-31B-598 AMS-A slot 2' })).toBeInTheDocument()
  })

  it('checks again once the spool is linked', async () => {
    const tray = seedUnknownTray()
    const onRetry = vi.fn()
    const verdict = { errors: ['x'], warnings: [], unknown_trays: [{ id: tray.id, slot: 'AMS-A slot 2' }] } as unknown as PrintCheck
    const { user } = renderPage(
      <NeedsYouHarness>
        <PrintVerdict verdict={verdict} onRetry={onRetry} />
      </NeedsYouHarness>,
    )
    await user.click(await screen.findByRole('button', { name: 'Link the spool in AMS-A slot 2' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('radio', { name: /S-9\b/ }))
    await user.type(within(dialog).getByLabelText('Net weight (g)'), '980')
    await user.click(within(dialog).getByRole('button', { name: 'Link spool' }))
    await waitFor(() => expect(onRetry).toHaveBeenCalledTimes(1))
  })
})
