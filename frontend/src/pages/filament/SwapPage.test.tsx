import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { archiveSpool, getSpool, moveSpool, upsertLocation } from '../../api/filament'
import { L, LK, spoolId } from '../../mocks/features/filament/seed'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { installNdefStub } from '../../test/ndefStub'
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
const box = (name: string) => upsertLocation({ name, kind_id: LK.DRY_BOX, parent_id: L.WORKSHOP, capacity: 1 })

async function seedBoxes() {
  const a = await box('Dry box A')
  const b = await box('Dry box B')
  await moveSpool(spoolId(6), a.id)
  await moveSpool(spoolId(9), b.id)
  return { a: a.id, b: b.id }
}

/** The move answers 409 `move_conflict` once; the handlers added before it see nothing of that answer. */
function conflictOnce() {
  server.use(
    http.post(
      '/api/v1/filament/spools/:id/move',
      () => HttpResponse.json({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'the move met another move; try again', code: 'move_conflict' }, { status: 409, headers: { 'Content-Type': 'application/problem+json' } }),
      { once: true },
    ),
  )
}

/** S-6 in Dry box A waits for Dry box B; S-9, in Dry box B, is named for Dry box A: the full-box panel offers the take-out. */
async function toCycle(user: User) {
  await pickSpool(user, 'S-6')
  await pickPlace(user, 'Dry box B')
  await user.click(await screen.findByRole('button', { name: 'Move' }))
  await user.click(await screen.findByRole('button', { name: 'Move S-9 first' }))
  await pickPlace(user, 'Dry box A')
  await user.click(await screen.findByRole('button', { name: 'Move' }))
  return screen.findByRole('button', { name: 'Take S-6 out first' })
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
    conflictOnce()
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

  it('focuses the step heading when the step changes, and not on the first view', async () => {
    const { user } = renderPage(<SwapPage />)
    const first = screen.getByRole('heading', { name: 'Which spool?' })
    expect(document.activeElement).not.toBe(first)
    await pickSpool(user, 'S-3')
    const next = await screen.findByRole('heading', { name: 'Where does it go?' })
    await waitFor(() => expect(document.activeElement).toBe(next))
  })

  it('describes the stale step on its focused heading', async () => {
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-3')
    await moveSpool(spoolId(3), L.DRY_BOX)
    await pickPlace(user, 'Workshop')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    const heading = await screen.findByRole('heading', { name: 'The spool moved meanwhile' })
    await waitFor(() => expect(document.activeElement).toBe(heading))
    expect(heading).toHaveAccessibleDescription(/S-3 is now in Dry box 1\./)
  })

  it('offers "Try again" after a network error, which sends the same move', async () => {
    server.use(http.post('/api/v1/filament/spools/:id/move', () => HttpResponse.error(), { once: true }))
    const sent = moves()
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-3')
    await pickPlace(user, 'Dry box 1')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('ScadBuddy could not reach its server')
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(within(logOf()).getByText('S-3 → Dry box 1')).toBeInTheDocument())
    expect(sent).toHaveLength(2)
    expect(sent[1]).toEqual(sent[0])
  })

  it('says once that the current spool was archived, and asks before the waiting spool moves', async () => {
    const { b } = await seedBoxes()
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-6')
    await pickPlace(user, 'Dry box B')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    await user.click(await screen.findByRole('button', { name: 'Move S-9 first' }))
    await archiveSpool(spoolId(9))
    const sent = moves()
    await pickPlace(user, 'Shelf A')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    const confirm = await screen.findByRole('button', { name: 'Move S-6 to Dry box B' })
    expect(screen.getAllByText('S-9 was archived.')).toHaveLength(1)
    expect(sent).toHaveLength(1)
    await user.click(confirm)
    await waitFor(() => expect(within(logOf()).getByText('S-6 → Dry box B')).toBeInTheDocument())
    expect(sent.at(-1)?.body).toMatchObject({ location_id: b, if_occupied: 'refuse' })
  })

  it('archives a box’s spool for a waiting move, and "Try again" keeps the archive in the log', async () => {
    const { a, b } = await seedBoxes()
    const c = await box('Dry box C')
    await moveSpool(spoolId(3), c.id)
    const sent = moves()
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-6')
    await pickPlace(user, 'Dry box B')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    await user.click(await screen.findByRole('button', { name: 'Move S-9 first' }))
    await pickPlace(user, 'Dry box C')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    const archive = await screen.findByRole('button', { name: 'Archive S-3' })
    conflictOnce()
    await user.click(archive)
    await user.click(await screen.findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(within(logOf()).getByText('S-6 → Dry box B')).toBeInTheDocument())
    expect(within(logOf()).getByText('S-3 archived')).toBeInTheDocument()
    expect(within(logOf()).getByText('S-9 → Dry box C')).toBeInTheDocument()
    expect(sent.slice(-2)).toEqual([
      { spool: spoolId(9), body: { location_id: c.id, if_occupied: 'replace', expected_occupant_id: spoolId(3), expected_location_id: b } },
      { spool: spoolId(6), body: { location_id: b, if_occupied: 'refuse', expected_location_id: a } },
    ])
  })

  it('shows a waiting spool that moved before its take-out, and asks before the next move', async () => {
    const { a, b } = await seedBoxes()
    const { user } = renderPage(<SwapPage />)
    const out = await toCycle(user)
    await moveSpool(spoolId(6), L.SHELF_A)
    const sent = moves()
    await user.click(out)
    expect(await screen.findByText('S-6 is now in Shelf A.')).toBeInTheDocument()
    const confirm = screen.getByRole('button', { name: 'Move S-9 to Dry box A' })
    expect(sent).toHaveLength(1)
    await user.click(confirm)
    await waitFor(() => expect(within(logOf()).getByText('S-6 → Dry box B')).toBeInTheDocument())
    expect(sent.slice(1)).toEqual([
      { spool: spoolId(9), body: { location_id: a, if_occupied: 'refuse', expected_location_id: b } },
      { spool: spoolId(6), body: { location_id: b, if_occupied: 'refuse', expected_location_id: L.SHELF_A } },
    ])
  })

  it('drops a waiting spool archived before its take-out, and asks before the next move', async () => {
    const { a, b } = await seedBoxes()
    const { user } = renderPage(<SwapPage />)
    const out = await toCycle(user)
    await archiveSpool(spoolId(6))
    const sent = moves()
    await user.click(out)
    const confirm = await screen.findByRole('button', { name: 'Move S-9 to Dry box A' })
    expect(screen.getAllByText('S-6 was archived.')).toHaveLength(1)
    expect(screen.queryByRole('list', { name: 'Waiting moves' })).toBeNull()
    await user.click(confirm)
    await waitFor(() => expect(within(logOf()).getByText('S-9 → Dry box A')).toBeInTheDocument())
    expect(sent.slice(1)).toEqual([{ spool: spoolId(9), body: { location_id: a, if_occupied: 'refuse', expected_location_id: b } }])
    expect(await screen.findByRole('heading', { name: 'Which spool?' })).toBeInTheDocument()
  })

  it('in a frame, offers its own tab and starts no NFC and no camera', async () => {
    const top = window.top
    Object.defineProperty(window, 'top', { value: {}, configurable: true })
    const reader = vi.fn()
    const getUserMedia = vi.fn()
    ;(window as unknown as { NDEFReader: unknown }).NDEFReader = reader
    // A camera and a BarcodeDetector, so the page offers "Use camera" outside a frame (IdentifySpool.test.tsx `installCamera`).
    ;(window as unknown as { BarcodeDetector: unknown }).BarcodeDetector = class {
      detect() {
        return Promise.resolve([])
      }
    }
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
      const placeWays = screen.getByRole('radiogroup', { name: 'How do you name the place?' })
      expect(within(placeWays).queryByRole('radio', { name: 'Scan tag' })).toBeNull()
      await user.click(within(placeWays).getByRole('radio', { name: 'QR code' }))
      expect(screen.queryByRole('button', { name: 'Use camera' })).toBeNull()
      expect(screen.queryByRole('button', { name: /^Write tag/ })).toBeNull()
      expect(reader).not.toHaveBeenCalled()
      expect(getUserMedia).not.toHaveBeenCalled()
    } finally {
      Object.defineProperty(window, 'top', { value: top, configurable: true })
      Object.defineProperty(navigator, 'mediaDevices', { value: media, configurable: true })
      delete (window as unknown as { NDEFReader?: unknown }).NDEFReader
      delete (window as unknown as { BarcodeDetector?: unknown }).BarcodeDetector
    }
  })

  it('after a failed take-out, "Choose another place" names a place for the taken-out spool', async () => {
    const { a, b } = await seedBoxes()
    const { user } = renderPage(<SwapPage />)
    const out = await toCycle(user)
    server.use(http.post('/api/v1/filament/spools/:id/move', () => HttpResponse.error(), { once: true }))
    await user.click(out)
    await screen.findByRole('alert')
    const sent = moves()
    await user.click(screen.getByRole('button', { name: 'Choose another place' }))
    expect(await screen.findByRole('heading', { name: 'Where does it go?' })).toBeInTheDocument()
    expect(screen.getByText(/in Dry box A/)).toBeInTheDocument()
    await pickPlace(user, 'Shelf A')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    await waitFor(() => expect(within(logOf()).getByText('S-6 → Dry box B')).toBeInTheDocument())
    expect(within(logOf()).getByText('S-6 → Shelf A')).toBeInTheDocument()
    expect(within(logOf()).getByText('S-9 → Dry box A')).toBeInTheDocument()
    expect(sent).toEqual([
      { spool: spoolId(6), body: { location_id: L.SHELF_A, if_occupied: 'refuse', expected_location_id: a } },
      { spool: spoolId(9), body: { location_id: a, if_occupied: 'refuse', expected_location_id: b } },
      { spool: spoolId(6), body: { location_id: b, if_occupied: 'refuse', expected_location_id: L.SHELF_A } },
    ])
  })

  it('a retry that finds the spool already in its target counts the move as done', async () => {
    server.use(http.post('/api/v1/filament/spools/:id/move', () => HttpResponse.error(), { once: true }))
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-3')
    await pickPlace(user, 'Dry box 1')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    await screen.findByRole('alert')
    // The first move went through; only its answer was lost.
    await moveSpool(spoolId(3), L.DRY_BOX)
    const sent = moves()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('S-3 is already in Dry box 1.')).toBeInTheDocument()
    expect(within(logOf()).getByText('S-3 → Dry box 1')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Which spool?' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Move S-3 to Dry box 1' })).toBeNull()
    expect(sent).toHaveLength(1)
  })

  it('a retry for a taken-out spool that is already out goes on with the chain', async () => {
    const { a, b } = await seedBoxes()
    const { user } = renderPage(<SwapPage />)
    const out = await toCycle(user)
    server.use(http.post('/api/v1/filament/spools/:id/move', () => HttpResponse.error(), { once: true }))
    await user.click(out)
    await screen.findByRole('alert')
    await moveSpool(spoolId(6), null)
    const sent = moves()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(within(logOf()).getByText('S-6 → Dry box B')).toBeInTheDocument())
    expect(within(logOf()).getByText('S-6 → no place')).toBeInTheDocument()
    expect(sent.slice(1)).toEqual([
      { spool: spoolId(9), body: { location_id: a, if_occupied: 'refuse', expected_location_id: b } },
      { spool: spoolId(6), body: { location_id: b, if_occupied: 'refuse', expected_location_id: null } },
    ])
  })

  it('does not move the focus to the heading while the move is sending', async () => {
    let answer: () => void = () => undefined
    const held = new Promise<void>((resolve) => (answer = resolve))
    server.use(
      http.post(
        '/api/v1/filament/spools/:id/move',
        async () => {
          await held
          return undefined
        },
        { once: true },
      ),
    )
    const { user } = renderPage(<SwapPage />)
    await pickSpool(user, 'S-3')
    await pickPlace(user, 'Dry box 1')
    await user.click(await screen.findByRole('button', { name: 'Move' }))
    const sending = await screen.findByRole('heading', { name: 'Moving…' })
    expect(document.activeElement).not.toBe(sending)
    answer()
    const next = await screen.findByRole('heading', { name: 'Which spool?' })
    await waitFor(() => expect(document.activeElement).toBe(next))
  })

  it('names each "Write tag" button by what it writes', async () => {
    const stub = installNdefStub()
    try {
      const { user } = renderPage(<SwapPage />)
      await pickSpool(user, 'S-3')
      const spoolWrite = await screen.findByRole('button', { name: 'Write tag for S-3' })
      expect(spoolWrite).toHaveTextContent('Write tag')
      await pickPlace(user, 'Dry box 1')
      const placeWrite = await screen.findByRole('button', { name: 'Write tag for Dry box 1' })
      expect(placeWrite).toHaveTextContent('Write tag')
    } finally {
      stub.restore()
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
