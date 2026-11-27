import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { archiveSpool, getSpool, moveSpool, upsertLocation } from '../../api/filament'
import { L, LK, spoolId } from '../../mocks/features/filament/seed'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { SwapPage } from './SwapPage'

type User = ReturnType<typeof renderPage>['user']
type Sent = { spool: string; body: Record<string, unknown> }

/** The bodies of every move request, kept before the mock answers (it falls through to it). */
function moves() {
  const bodies: Sent[] = []
  server.use(
    http.post('/api/v1/filament/spools/:id/move', async ({ params, request }) => {
      bodies.push({ spool: String(params.id), body: (await request.clone().json()) as Record<string, unknown> })
      return undefined
    }),
  )
  return bodies
}

/** Two dry boxes of one spool each: S-6 in Dry box A, S-9 in Dry box B. */
async function seedBoxes() {
  const box = (name: string) => upsertLocation({ name, kind_id: LK.DRY_BOX, parent_id: L.WORKSHOP, capacity: 1 })
  const a = await box('Dry box A')
  const b = await box('Dry box B')
  await moveSpool(spoolId(6), a.id)
  await moveSpool(spoolId(9), b.id)
  return { a: a.id, b: b.id }
}

async function pickSpool(user: User, code: string) {
  const ways = await screen.findByRole('radiogroup', { name: 'How do you name the spool?' })
  await user.click(within(ways).getByRole('radio', { name: 'Pick by hand' }))
  const list = await screen.findByRole('radiogroup', { name: 'Spools' })
  await user.click(await within(list).findByRole('radio', { name: new RegExp(`^${code}\\b`) }))
}

async function pickPlace(user: User, name: string) {
  const ways = await screen.findByRole('radiogroup', { name: 'How do you name the place?' })
  await user.click(within(ways).getByRole('radio', { name: 'Pick by hand' }))
  const list = await screen.findByRole('radiogroup', { name: 'Places' })
  await user.click(await within(list).findByRole('radio', { name: new RegExp(`^L-\\d+ ${name}\\b`) }))
}

const logOf = () => screen.getByRole('list', { name: 'Moves done' })

describe('SwapPage (#7168)', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it('moves a named spool into a named place, carrying where it was read', async () => {
    const sent = moves()
    const { user } = renderPage(<SwapPage />)
    expect(screen.getByRole('heading', { level: 1, name: 'Swap spools' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Which spool?' })).toBeInTheDocument()
    await pickSpool(user, 'S-3')
    expect(await screen.findByRole('heading', { name: 'Where does it go?' })).toBeInTheDocument()
    await pickPlace(user, 'Dry box 1')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    await waitFor(() => expect(within(logOf()).getByText('S-3 → Dry box 1')).toBeInTheDocument())
    expect(sent).toEqual([{ spool: spoolId(3), body: { location_id: L.DRY_BOX, if_occupied: 'refuse', expected_location_id: L.SHELF_A } }])
    expect(await screen.findByRole('heading', { name: 'Which spool?' })).toBeInTheDocument()
  })

  it('sends null for a spool that was in no place', async () => {
    const sent = moves()
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-6')
    await pickPlace(user, 'Shelf A')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    await waitFor(() => expect(within(logOf()).getByText('S-6 → Shelf A')).toBeInTheDocument())
    expect(sent).toEqual([{ spool: spoolId(6), body: { location_id: L.SHELF_A, if_occupied: 'refuse', expected_location_id: null } }])
  })

  it('shows where a spool went meanwhile, and moves it only when the person confirms', async () => {
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-3')
    // Another wizard moves S-3 between the steps.
    await moveSpool(spoolId(3), L.DRY_BOX)
    const sent = moves()
    await pickPlace(user, 'Workshop')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    expect(await screen.findByText('S-3 is now in Dry box 1.')).toBeInTheDocument()
    const confirm = screen.getByRole('button', { name: 'Move S-3 to Workshop' })
    expect(sent).toHaveLength(1)
    await user.click(confirm)
    await waitFor(() => expect(within(logOf()).getByText('S-3 → Workshop')).toBeInTheDocument())
    expect(sent.map((one) => one.body.expected_location_id)).toEqual([L.SHELF_A, L.DRY_BOX])
  })

  it('goes back to "Which spool?" when the spool was archived meanwhile', async () => {
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-3')
    await archiveSpool(spoolId(3))
    const sent = moves()
    await pickPlace(user, 'Workshop')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    expect(await screen.findByText('S-3 was archived.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Which spool?' })).toBeInTheDocument()
    expect(sent).toHaveLength(1)
  })

  it('archives the box’s spool only with the occupant the person saw', async () => {
    const { a, b } = await seedBoxes()
    const sent = moves()
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-6')
    await pickPlace(user, 'Dry box B')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    expect(await screen.findByText(/S-9 is in Dry box B/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Choose another place' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Archive S-9' }))
    await waitFor(() => expect(within(logOf()).getByText('S-6 → Dry box B')).toBeInTheDocument())
    expect(sent.at(-1)).toEqual({ spool: spoolId(6), body: { location_id: b, if_occupied: 'replace', expected_occupant_id: spoolId(9), expected_location_id: a } })
    expect((await getSpool(spoolId(9))).archived_at).not.toBeNull()
  })

  it('moves the box’s spool first, then sends the waiting move by itself', async () => {
    const { a, b } = await seedBoxes()
    const sent = moves()
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-6')
    await pickPlace(user, 'Dry box B')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    await user.click(await screen.findByRole('button', { name: 'Move S-9 first' }))
    expect(await screen.findByRole('heading', { name: 'Where does it go?' })).toBeInTheDocument()
    expect(screen.getByText(/S-6 waits for Dry box B/)).toBeInTheDocument()
    await pickPlace(user, 'Shelf A')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    await waitFor(() => expect(within(logOf()).getByText('S-6 → Dry box B')).toBeInTheDocument())
    expect(within(logOf()).getByText('S-9 → Shelf A')).toBeInTheDocument()
    expect(sent.slice(1)).toEqual([
      { spool: spoolId(9), body: { location_id: L.SHELF_A, if_occupied: 'refuse', expected_location_id: b } },
      { spool: spoolId(6), body: { location_id: b, if_occupied: 'refuse', expected_location_id: a } },
    ])
  })

  it('breaks a cycle by taking the waiting spool out first', async () => {
    const { a, b } = await seedBoxes()
    const sent = moves()
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-6')
    await pickPlace(user, 'Dry box B')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    await user.click(await screen.findByRole('button', { name: 'Move S-9 first' }))
    await pickPlace(user, 'Dry box A')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    const out = await screen.findByRole('button', { name: 'Take S-6 out first' })
    expect(sent).toHaveLength(2)
    await user.click(out)
    await waitFor(() => expect(within(logOf()).getByText('S-6 → Dry box B')).toBeInTheDocument())
    expect(sent.slice(2)).toEqual([
      { spool: spoolId(6), body: { location_id: null, if_occupied: 'refuse', expected_location_id: a } },
      { spool: spoolId(9), body: { location_id: a, if_occupied: 'refuse', expected_location_id: b } },
      { spool: spoolId(6), body: { location_id: b, if_occupied: 'refuse', expected_location_id: null } },
    ])
  })

  it('offers "Try again" after a move conflict, which sends the same move', async () => {
    // The recorder goes on after the conflict, so it sees the first move too (msw tries the newest handler first).
    server.use(
      http.post(
        '/api/v1/filament/spools/:id/move',
        () => HttpResponse.json({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'the move met another move; try again', code: 'move_conflict' }, { status: 409, headers: { 'Content-Type': 'application/problem+json' } }),
        { once: true },
      ),
    )
    const sent = moves()
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-3')
    await pickPlace(user, 'Dry box 1')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    await user.click(await screen.findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(within(logOf()).getByText('S-3 → Dry box 1')).toBeInTheDocument())
    expect(sent).toHaveLength(2)
    expect(sent[1]).toEqual(sent[0])
  })

  it('in a frame, offers its own tab and starts no NFC and no camera', async () => {
    const top = window.top
    Object.defineProperty(window, 'top', { value: {}, configurable: true })
    const reader = vi.fn()
    const getUserMedia = vi.fn()
    ;(window as unknown as { NDEFReader: unknown }).NDEFReader = reader
    const media = navigator.mediaDevices
    Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true })
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    try {
      const { user } = renderPage(<SwapPage />)
      await user.click(screen.getByRole('button', { name: 'Open the swap page in its own tab' }))
      expect(open).toHaveBeenCalledWith(window.location.href, '_blank', 'noopener')
      const ways = screen.getByRole('radiogroup', { name: 'How do you name the spool?' })
      expect(within(ways).queryByRole('radio', { name: 'Scan tag' })).toBeNull()
      await user.click(within(ways).getByRole('radio', { name: 'QR code' }))
      expect(screen.queryByRole('button', { name: 'Use camera' })).toBeNull()
      await pickSpool(user, 'S-3')
      await screen.findByRole('heading', { name: 'Where does it go?' })
      expect(screen.queryByRole('button', { name: 'Write tag' })).toBeNull()
      expect(reader).not.toHaveBeenCalled()
      expect(getUserMedia).not.toHaveBeenCalled()
    } finally {
      Object.defineProperty(window, 'top', { value: top, configurable: true })
      Object.defineProperty(navigator, 'mediaDevices', { value: media, configurable: true })
      delete (window as unknown as { NDEFReader?: unknown }).NDEFReader
    }
  })

  it('without Web NFC, says where tags work and keeps the QR and hand ways', () => {
    renderPage(<SwapPage />)
    expect(screen.getByText('Tags need Chrome on Android or the ScadBuddy app.')).toBeInTheDocument()
    const ways = screen.getByRole('radiogroup', { name: 'How do you name the spool?' })
    expect(within(ways).queryByRole('radio', { name: 'Scan tag' })).toBeNull()
    expect(within(ways).getByRole('radio', { name: 'QR code' })).toBeInTheDocument()
    expect(within(ways).getByRole('radio', { name: 'Pick by hand' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Open the swap page in its own tab' })).toBeNull()
  })
})
