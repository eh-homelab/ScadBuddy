import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getSpool, moveSpool } from '../../api/filament'
import { seedHomeReturn } from '../../mocks/features/filament'
import { spoolId } from '../../mocks/features/filament/seed'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { NeedsYouHarness } from '../../test/needsYou'
import { renderPage } from '../../test/utils'
import { NeedsYouList } from '../activity/NeedsYou'
import { HomeReturnDialog } from './HomeReturnDialog'

const TITLE = 'S-11 came out of 3DP-31B-598 H2C AMS-A slot 1. Which spool goes into Dry box 2?'

/** The bodies of every move request, read as they leave. */
function moves() {
  const bodies: unknown[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === 'POST' && /\/filament\/spools\/[^/]+\/move$/.test(new URL(request.url).pathname)) {
      void request.clone().json().then((body) => bodies.push({ spool: new URL(request.url).pathname.split('/').at(-2), body }), () => {})
    }
  })
  return bodies
}

async function show(options: { empty?: boolean } = {}) {
  const { box } = seedHomeReturn(options)
  const spool = await getSpool(spoolId(11))
  const onDone = vi.fn()
  const onOpenChange = vi.fn()
  const view = renderPage(<HomeReturnDialog spool={spool} open onOpenChange={onOpenChange} onDone={onDone} />)
  const dialog = await screen.findByRole('dialog', { name: TITLE })
  return { ...view, box, dialog, onDone, onOpenChange }
}

async function pick(user: ReturnType<typeof renderPage>['user'], dialog: HTMLElement, code: string) {
  await user.click(within(dialog).getByRole('radio', { name: 'Pick by hand' }))
  const list = await within(dialog).findByRole('radiogroup', { name: 'Spools' })
  await user.click(await within(list).findByRole('radio', { name: new RegExp(`^${code}\\b`) }))
}

describe('HomeReturnDialog (#7167)', () => {
  beforeEach(() => resetMockState())

  it('names where the spool came from and the box, with no spool chosen', async () => {
    const { dialog } = await show()
    expect(within(dialog).getByRole('button', { name: 'Put in box' })).toBeDisabled()
    for (const way of within(within(dialog).getByRole('radiogroup', { name: 'How do you name the spool?' })).getAllByRole('radio')) {
      expect(way).toHaveAttribute('aria-checked', 'false')
    }
    expect(within(dialog).getByText(/S-7 is in Dry box 2 now/)).toBeInTheDocument()
  })

  it('puts the box’s own spool back with refuse, answering the prompt', async () => {
    const sent = moves()
    const { user, dialog, box, onDone } = await show()
    await pick(user, dialog, 'S-7')
    expect(within(dialog).getByText('S-7 stays in Dry box 2.')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Put in box' }))
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    expect(sent).toEqual([{ spool: spoolId(7), body: { location_id: box, if_occupied: 'refuse', answers: spoolId(11) } }])
  })

  it('replaces the box’s spool, saying what it books', async () => {
    const sent = moves()
    const { user, dialog, box, onDone } = await show()
    await pick(user, dialog, 'S-11')
    expect(within(dialog).getByText('S-7 comes out of Dry box 2 for good: it is archived at 0 g, and its 300 g are booked as non-print waste.')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Put in box' }))
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    expect(sent).toEqual([{ spool: spoolId(11), body: { location_id: box, if_occupied: 'replace', answers: spoolId(11) } }])
    const replaced = await getSpool(spoolId(7))
    expect(replaced.archived_at).not.toBeNull()
    expect((await getSpool(spoolId(11))).home_asked_at).toBeNull()
  })

  it('puts a spool into an empty box with refuse', async () => {
    const sent = moves()
    const { user, dialog, box, onDone } = await show({ empty: true })
    expect(within(dialog).getByText('Dry box 2 is empty.')).toBeInTheDocument()
    await pick(user, dialog, 'S-9')
    await user.click(within(dialog).getByRole('button', { name: 'Put in box' }))
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    expect(sent).toEqual([{ spool: spoolId(9), body: { location_id: box, if_occupied: 'refuse', answers: spoolId(11) } }])
  })

  it('reads the box again after a 409 box_full and shows the new occupant', async () => {
    const { user, dialog, box, onDone } = await show({ empty: true })
    await pick(user, dialog, 'S-11')
    // Someone else fills the box meanwhile.
    await moveSpool(spoolId(9), box)
    await user.click(within(dialog).getByRole('button', { name: 'Put in box' }))
    expect(await within(dialog).findByText('S-9 went into Dry box 2 meanwhile. Check which spool goes in, then try again.')).toBeInTheDocument()
    expect(within(dialog).getByText('S-9 comes out of Dry box 2 for good: it is archived at 0 g, and its 1000 g are booked as non-print waste.')).toBeInTheDocument()
    expect(onDone).not.toHaveBeenCalled()
    expect((await getSpool(spoolId(9))).archived_at).toBeNull()
  })

  it('opens from its Needs-you row, and the row goes once the box is answered', async () => {
    seedHomeReturn()
    const { user } = renderPage(
      <NeedsYouHarness>
        <NeedsYouList />
      </NeedsYouHarness>,
    )
    const row = await waitFor(() => {
      const found = screen.getAllByTestId('needs-you-item').find((one) => one.dataset.kind === 'home-return')
      if (!found) throw new Error('no home-return row')
      return found
    })
    expect(within(row).getByText('S-11 came out of its tray. Which spool goes into its dry box?')).toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: 'Choose the spool…' }))
    const dialog = await screen.findByRole('dialog', { name: TITLE })
    await pick(user, dialog, 'S-7')
    await user.click(within(dialog).getByRole('button', { name: 'Put in box' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(screen.queryAllByTestId('needs-you-item').filter((one) => one.dataset.kind === 'home-return')).toHaveLength(0))
  })

  it('keeps the focus inside, and Escape closes it with nothing sent', async () => {
    const sent = moves()
    const { user, dialog, onOpenChange } = await show()
    for (let at = 0; at < 8; at += 1) {
      await user.tab()
      expect(dialog.contains(document.activeElement)).toBe(true)
    }
    await user.keyboard('{Escape}')
    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(sent).toEqual([])
  })
})
