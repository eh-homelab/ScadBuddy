import { act, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { seedUnknownTray } from '../../mocks/features/filament/unknownTrays'
import { resetMockState } from '../../mocks/handlers'
import { needsYouItems, type NeedsYouItem } from '../../lib/needsYou'
import { renderPage } from '../../test/utils'
import { useUnknownSpoolDialog } from './useUnknownSpoolDialog'

function Host({ items, refresh, tray }: { items: readonly NeedsYouItem[] | null; refresh: () => void; tray: ReturnType<typeof seedUnknownTray> }) {
  const { open, dialog } = useUnknownSpoolDialog(items, refresh)
  return (
    <>
      <button onClick={() => open(tray)}>open</button>
      {dialog}
    </>
  )
}

describe('useUnknownSpoolDialog (#7143)', () => {
  beforeEach(() => resetMockState())

  it('reads the shell list again on open, and is not gone before the shell has held the tray', async () => {
    const tray = seedUnknownTray()
    const refresh = vi.fn()
    // The shell's last poll is older than the row: its items lack the tray.
    const { user, rerender } = renderPage(<Host items={[]} refresh={refresh} tray={tray} />)
    await user.click(screen.getByRole('button', { name: 'open' }))
    await screen.findByRole('dialog')
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/not waiting any more/)).toBeNull()
    // The poll then holds the tray; a later poll without it says it is gone.
    rerender(<Host items={needsYouItems([], null, [], [tray])} refresh={refresh} tray={tray} />)
    await act(async () => {})
    expect(screen.queryByText(/not waiting any more/)).toBeNull()
    rerender(<Host items={[]} refresh={refresh} tray={tray} />)
    expect(await screen.findByText(/not waiting any more/)).toBeInTheDocument()
  })
})
