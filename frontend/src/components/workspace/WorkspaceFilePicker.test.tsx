import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AssistantOpenerContext } from '../../agent/chat/opener'
import { ApiError, api } from '../../api/client'
import { seedSessionFile } from '../../mocks/features/sessionFiles'
import { markPickerFileUnavailable, seedPickerSession } from '../../mocks/features/workspacePicker'
import { intersect } from '../../test/intersection'
import { renderPage } from '../../test/utils'
import { WorkspaceFilePicker, WorkspacePickButton } from './WorkspaceFilePicker'

// #4703: the shared workspace file picker, over the agent's GET /api/v1/ai/workspace-files
// and a pick's download with `expected_sha256` (msw: mocks/features/workspacePicker.ts and
// sessionFiles.ts).

const bytes = (s: string) => new TextEncoder().encode(s)
const MODEL = { type: 'model' as const, id: 'box' }
const PNG_ACCEPT = '.png,image/png'

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => 'blob:thumb')
  URL.revokeObjectURL = vi.fn()
})
afterEach(() => vi.restoreAllMocks())

function render(props: Partial<Parameters<typeof WorkspaceFilePicker>[0]> = {}) {
  const onFile = vi.fn<(file: File) => Promise<void>>(() => Promise.resolve())
  const onClose = vi.fn()
  renderPage(
    <WorkspaceFilePicker
      open
      onClose={onClose}
      resource={MODEL}
      resourceLabel="this model"
      accept={PNG_ACCEPT}
      onFile={onFile}
      {...props}
    />,
  )
  return { onFile, onClose }
}

describe('WorkspaceFilePicker', () => {
  it('lists the sessions that touched the resource, newest first, with the files the target takes', async () => {
    seedPickerSession({ id: 's-old', title: 'Older work', updated_at: '2026-11-01T10:00:00Z', touched: [MODEL] })
    seedPickerSession({ id: 's-new', title: 'Newer work', updated_at: '2026-11-05T10:00:00Z', touched: [MODEL] })
    seedPickerSession({ id: 's-other', title: 'Some lid', updated_at: '2026-11-06T10:00:00Z', touched: [{ type: 'model', id: 'lid' }] })
    seedSessionFile('s-old', 'renders/front.png', bytes('png'), 'image/png', { tool: 'workspace_save' })
    seedSessionFile('s-old', 'notes.txt', bytes('text'), 'text/plain')
    seedSessionFile('s-other', 'lid.png', bytes('lid'), 'image/png')
    render()
    const list = await screen.findByRole('list', { name: 'Assistant sessions' })
    const sessions = within(list).getAllByRole('listitem', { name: /work$/ })
    expect(sessions.map((s) => s.getAttribute('aria-label'))).toEqual(['Newer work', 'Older work'])
    expect(sessions[0]).toHaveTextContent('No file of this kind')
    expect(within(sessions[1]!).getByRole('button', { name: 'Use renders/front.png' })).toHaveTextContent('image/png')
    expect(within(list).queryByText('notes.txt')).toBeNull()
    expect(within(list).queryByText('lid.png')).toBeNull()
  })

  it('links an empty default list to Advanced', async () => {
    seedPickerSession({ id: 's-a', title: 'Other model work', touched: [{ type: 'model', id: 'lid' }] })
    seedSessionFile('s-a', 'lid.png', bytes('lid'), 'image/png')
    render()
    expect(await screen.findByTestId('workspace-picker-empty')).toHaveTextContent('No assistant session worked on this model yet.')
    await userEvent.click(screen.getByRole('button', { name: 'Look in every session (Advanced)' }))
    expect(await screen.findByRole('button', { name: 'Use lid.png' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Advanced' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('lists every session in Advanced, archived ones too, and searches titles and file names', async () => {
    seedPickerSession({ id: 's-a', title: 'Keychain ideas' })
    seedPickerSession({ id: 's-b', title: 'Old stuff', archived: true })
    seedSessionFile('s-a', 'front.png', bytes('a'), 'image/png')
    seedSessionFile('s-a', 'back.png', bytes('b'), 'image/png')
    seedSessionFile('s-b', 'logo.png', bytes('c'), 'image/png')
    render({ resource: null })
    expect(await screen.findByRole('button', { name: 'Use logo.png' })).toBeInTheDocument()
    expect(screen.getByRole('listitem', { name: 'Old stuff' })).toHaveTextContent('Archived')
    // Without a resource there is no default list to go back to.
    expect(screen.queryByRole('button', { name: /^Worked on/ })).toBeNull()

    await userEvent.type(screen.getByRole('searchbox', { name: 'Search sessions and files' }), 'FRONT')
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Use logo.png' })).toBeNull())
    expect(await screen.findByRole('button', { name: 'Use front.png' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Use back.png' })).toBeNull()

    await userEvent.clear(screen.getByRole('searchbox', { name: 'Search sessions and files' }))
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search sessions and files' }), 'nothing at all')
    expect(await screen.findByText('No session or file matches.')).toBeInTheDocument()
  })

  it('asks the agent for the target types only', async () => {
    const list = vi.spyOn(api, 'listWorkspacePicks')
    seedPickerSession({ id: 's-a', title: 'Meshes', touched: [MODEL] })
    seedSessionFile('s-a', 'part.stl', bytes('solid'), 'model/stl')
    seedSessionFile('s-a', 'part.png', bytes('png'), 'image/png')
    render({ accept: '.3mf,.stl' })
    expect(await screen.findByRole('button', { name: 'Use part.stl' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Use part.png' })).toBeNull()
    expect(screen.getByRole('listitem', { name: 'Meshes' })).not.toHaveTextContent('No file of this kind')
    expect(list).toHaveBeenCalledWith({ resource: MODEL, accept: '.3mf,.stl', q: '' })
  })

  it('greys out a file it cannot read now, with the reason', async () => {
    seedPickerSession({
      id: 's-a',
      title: 'Sealed',
      touched: [MODEL],
      unavailable: 'its workspace files cannot be read now: no key',
    })
    seedSessionFile('s-a', 'gone.png', bytes('x'), 'image/png')
    markPickerFileUnavailable('s-a', 'gone.png', 'no key')
    const { onFile } = render()
    const session = await screen.findByRole('listitem', { name: 'Sealed' })
    expect(session).toHaveAttribute('aria-disabled', 'true')
    expect(session).toHaveTextContent('its workspace files cannot be read now: no key')
    expect(within(session).getByRole('button', { name: 'Use gone.png' })).toBeDisabled()
    expect(onFile).not.toHaveBeenCalled()
  })

  it('hands the picked file to the target as a File, then closes', async () => {
    seedPickerSession({ id: 's-a', title: 'Work', touched: [MODEL] })
    seedSessionFile('s-a', 'renders/front.png', bytes('png bytes'), 'image/png')
    const { onFile, onClose } = render()
    await userEvent.click(await screen.findByRole('button', { name: 'Use renders/front.png' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    const [file, pick] = onFile.mock.calls[0] as unknown as [File, { sessionId: string; path: string; sha256: string }]
    expect(file.name).toBe('front.png')
    expect(file.type).toBe('image/png')
    expect(await file.text()).toBe('png bytes')
    expect(pick).toMatchObject({ sessionId: 's-a', path: 'renders/front.png' })
  })

  it('refuses a file that changed since the list was read, and reads the list again', async () => {
    seedPickerSession({ id: 's-a', title: 'Work', touched: [MODEL] })
    seedSessionFile('s-a', 'thumb.png', bytes('first'), 'image/png')
    const list = vi.spyOn(api, 'listWorkspacePicks')
    const { onFile, onClose } = render()
    const use = await screen.findByRole('button', { name: 'Use thumb.png' })
    // The file changes after the list was drawn.
    seedSessionFile('s-a', 'thumb.png', bytes('second, longer'), 'image/png')
    const reads = list.mock.calls.length
    await userEvent.click(use)
    expect(await screen.findByRole('alert')).toHaveTextContent('thumb.png changed after the list was shown, so it was not used.')
    await waitFor(() => expect(list.mock.calls.length).toBeGreaterThan(reads))
    expect(onFile).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
    // The new list carries the new sha256, and the pick then goes through.
    await userEvent.click(await screen.findByRole('button', { name: 'Use thumb.png' }))
    await waitFor(() => expect(onFile).toHaveBeenCalled())
    expect(await (onFile.mock.calls[0]![0] as File).text()).toBe('second, longer')
  })

  it("shows what the target refused, and stays open", async () => {
    seedPickerSession({ id: 's-a', title: 'Work', touched: [MODEL] })
    seedSessionFile('s-a', 'big.png', bytes('x'), 'image/png')
    const { onClose } = render({ onFile: () => Promise.reject(new Error('That image is larger than 10 MB.')) })
    await userEvent.click(await screen.findByRole('button', { name: 'Use big.png' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('That image is larger than 10 MB.')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('says a file is gone when it was deleted after the list was shown, and reads the list again', async () => {
    seedPickerSession({ id: 's-a', title: 'Work', touched: [MODEL] })
    seedSessionFile('s-a', 'gone.png', bytes('x'), 'image/png')
    const list = vi.spyOn(api, 'listWorkspacePicks')
    const { onFile } = render()
    const use = await screen.findByRole('button', { name: 'Use gone.png' })
    vi.spyOn(api, 'pickAiSessionFile').mockRejectedValueOnce(new ApiError(404, 'no file gone.png in this session'))
    const reads = list.mock.calls.length
    await userEvent.click(use)
    expect(await screen.findByRole('alert')).toHaveTextContent('gone.png is gone: the file or its session was deleted')
    await waitFor(() => expect(list.mock.calls.length).toBeGreaterThan(reads))
    expect(onFile).not.toHaveBeenCalled()
  })

  it('reads a thumbnail only once its row comes into view', async () => {
    seedPickerSession({ id: 's-a', title: 'Work', touched: [MODEL] })
    seedSessionFile('s-a', 'one.png', bytes('1'), 'image/png')
    seedSessionFile('s-a', 'two.png', bytes('2'), 'image/png')
    const read = vi.spyOn(api, 'getAiSessionFile')
    render()
    const one = await screen.findByRole('button', { name: 'Use one.png' })
    await screen.findByRole('button', { name: 'Use two.png' })
    // Out of view, nothing is read.
    await new Promise((settle) => setTimeout(settle, 50))
    expect(read).not.toHaveBeenCalled()
    intersect(one)
    await waitFor(() => expect(read).toHaveBeenCalledWith('s-a', 'one.png'))
    expect(read).toHaveBeenCalledTimes(1)
    expect(await within(one).findByRole('presentation')).toHaveAttribute('src', 'blob:thumb')
  })
})

describe('WorkspacePickButton', () => {
  it('shows nothing without the assistant, and opens the picker with it', async () => {
    const props = { resource: MODEL, accept: PNG_ACCEPT, onFile: vi.fn() }
    const { unmount } = renderPage(<WorkspacePickButton {...props} />)
    expect(screen.queryByRole('button', { name: 'From a session…' })).toBeNull()
    unmount()
    renderPage(
      <AssistantOpenerContext.Provider value={{ openSession: vi.fn() }}>
        <WorkspacePickButton {...props} />
      </AssistantOpenerContext.Provider>,
    )
    await userEvent.click(screen.getByRole('button', { name: 'From a session…' }))
    expect(await screen.findByTestId('workspace-file-picker')).toBeInTheDocument()
  })
})
