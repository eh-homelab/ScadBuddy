import { act, fireEvent, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api, ApiError } from '../../api/client'
import type * as embed from '../../lib/embed'
import { DownloadBlockedError, downloadBlob } from '../../lib/embed'
import { seedSessionFile } from '../../mocks/features/sessionFiles'
import { renderPage } from '../../test/utils'
import { segmentOf, sourceLabel, uploadPath } from '../../lib/workspaceFiles'
import { SessionFiles } from './SessionFiles'

// #2329 part 3: the panel's Files, over the agent's /api/v1/ai/sessions/:id/files
// routes (msw: mocks/features/sessionFiles.ts). An upload is stubbed at the client:
// jsdom's File cannot pass through undici's multipart parser (mocks/handlers.test.ts
// says why); the mocked e2e run (e2e/session-files.spec.ts) posts real multipart.

vi.mock('../../lib/embed', async (original) => ({
  ...(await original<typeof embed>()),
  downloadBlob: vi.fn(async (load: () => Promise<Blob>) => {
    await load()
  }),
}))

const PNG = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='), (c) =>
  c.charCodeAt(0),
)
const text = (s: string) => new TextEncoder().encode(s)

beforeEach(() => {
  vi.mocked(downloadBlob).mockReset()
  vi.mocked(downloadBlob).mockImplementation(async (load) => {
    await load()
  })
  URL.createObjectURL = vi.fn(() => 'blob:preview')
  URL.revokeObjectURL = vi.fn()
  vi.spyOn(api, 'uploadAiSessionFile').mockImplementation(async (id, file, path) => {
    seedSessionFile(id, path, new Uint8Array(file.size), 'application/octet-stream', { kind: 'upload', by: 'browser' })
    return { file: (await api.listAiSessionFiles(id)).files.find((f) => f.path === path)! }
  })
})

afterEach(() => {
  vi.restoreAllMocks()
})

function render(props: Partial<Parameters<typeof SessionFiles>[0]> = {}) {
  const onInsert = vi.fn()
  renderPage(<SessionFiles sessionId="sess-f" canWrite onInsert={onInsert} {...props} />)
  return { onInsert }
}

describe('SessionFiles', () => {
  it('lists the files with size, type, source and time, and the usage against the cap', async () => {
    seedSessionFile('sess-f', 'downloads/part.3mf', new Uint8Array(2500), 'model/3mf', { tool: 'http_request', url: 'https://example.com/part.3mf' })
    seedSessionFile('sess-f', 'notes.txt', text('hello'), 'text/plain', { kind: 'upload', by: 'browser' })
    render()
    const list = await screen.findByRole('list', { name: 'Workspace files' })
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('downloads/part.3mf')
    expect(items[0]).toHaveTextContent('2.5 kB')
    expect(items[0]).toHaveTextContent('model/3mf')
    expect(items[0]).toHaveTextContent('from example.com')
    expect(items[1]).toHaveTextContent('uploaded by you')
    expect(screen.getByText(/2 files · 2.5 kB of 210 MB/)).toBeInTheDocument()
  })

  it('says so when there are none', async () => {
    render()
    expect(await screen.findByText(/No files yet/)).toBeInTheDocument()
  })

  it('uploads a picked file under uploads/ and puts its reference in the composer', async () => {
    const user = userEvent.setup()
    const { onInsert } = render()
    await screen.findByText(/No files yet/)
    await user.upload(screen.getByLabelText('Upload files to the workspace'), new File(['cube(1);'], 'my part.scad'))
    expect(await screen.findByText('uploads/my_part.scad')).toBeInTheDocument()
    expect(onInsert).toHaveBeenCalledWith('[workspace file uploads/my_part.scad]')
    // The same name again is numbered rather than refused.
    await user.upload(screen.getByLabelText('Upload files to the workspace'), new File(['x'], 'my part.scad'))
    expect(await screen.findByText('uploads/my_part-2.scad')).toBeInTheDocument()
  })

  it('uploads a dropped file', async () => {
    const { onInsert } = render()
    const zone = await screen.findByTestId('session-files')
    const file = new File(['data'], 'dropped.bin')
    fireEvent.dragOver(zone, { dataTransfer: { types: ['Files'], files: [file] } })
    fireEvent.drop(zone, { dataTransfer: { types: ['Files'], files: [file] } })
    expect(await screen.findByText('uploads/dropped.bin')).toBeInTheDocument()
    expect(onInsert).toHaveBeenCalledWith('[workspace file uploads/dropped.bin]')
  })

  it('takes the next free name when the list it numbered from was stale (#2469)', async () => {
    const user = userEvent.setup()
    const { onInsert } = render()
    await screen.findByText(/No files yet/)
    // The assistant writes uploads/a.txt after the list was read.
    seedSessionFile('sess-f', 'uploads/a.txt', text('theirs'), 'text/plain', null)
    vi.mocked(api.uploadAiSessionFile).mockRejectedValueOnce(new ApiError(409, 'uploads/a.txt already exists'))
    await user.upload(screen.getByLabelText('Upload files to the workspace'), new File(['x'], 'a.txt'))
    expect(await screen.findByText('uploads/a-2.txt')).toBeInTheDocument()
    expect(onInsert).toHaveBeenCalledWith('[workspace file uploads/a-2.txt]')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows why an upload was refused', async () => {
    vi.mocked(api.uploadAiSessionFile).mockRejectedValueOnce(
      new ApiError(413, 'the workspace would hold 300 bytes; it holds at most 200'),
    )
    const user = userEvent.setup()
    const { onInsert } = render()
    await screen.findByText(/No files yet/)
    await user.upload(screen.getByLabelText('Upload files to the workspace'), new File(['x'], 'a.txt'))
    expect(await screen.findByRole('alert')).toHaveTextContent('a.txt was not uploaded: the workspace would hold 300 bytes')
    expect(onInsert).not.toHaveBeenCalled()
  })

  it('downloads a file through downloadBlob, which works inside the Bambuddy frame', async () => {
    seedSessionFile('sess-f', 'notes.txt', text('hello'), 'text/plain')
    const user = userEvent.setup()
    render()
    await user.click(await screen.findByRole('button', { name: 'Download notes.txt' }))
    expect(downloadBlob).toHaveBeenCalledWith(expect.any(Function), 'notes.txt')
    const load = vi.mocked(downloadBlob).mock.calls[0]![0]
    expect(await (await load()).text()).toBe('hello')
  })

  it('says when the download window was blocked', async () => {
    seedSessionFile('sess-f', 'notes.txt', text('hello'), 'text/plain')
    vi.mocked(downloadBlob).mockRejectedValueOnce(new DownloadBlockedError())
    const user = userEvent.setup()
    render()
    await user.click(await screen.findByRole('button', { name: 'Download notes.txt' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('notes.txt was not downloaded')
  })

  it('previews an image through a blob URL, never the route itself', async () => {
    seedSessionFile('sess-f', 'shot.png', PNG, 'image/png')
    seedSessionFile('sess-f', 'pic.svg', text('<svg/>'), 'image/svg+xml')
    const user = userEvent.setup()
    render()
    await screen.findByText('shot.png')
    // SVG is never previewed.
    expect(screen.getAllByRole('button', { name: 'Preview' })).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Preview' }))
    const img = await screen.findByRole('img', { name: 'shot.png' })
    expect(img).toHaveAttribute('src', 'blob:preview')
    await user.click(screen.getByRole('button', { name: 'Hide' }))
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview')
  })

  it('deletes a file only after a confirm', async () => {
    seedSessionFile('sess-f', 'notes.txt', text('hello'), 'text/plain')
    const user = userEvent.setup()
    render()
    await user.click(await screen.findByRole('button', { name: 'Delete notes.txt' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Delete notes.txt?' })
    await user.click(within(confirm).getByRole('button', { name: 'Cancel' }))
    expect(screen.getByText('notes.txt')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Delete notes.txt' }))
    await user.click(within(await screen.findByRole('alertdialog', { name: 'Delete notes.txt?' })).getByRole('button', { name: 'Delete file' }))
    expect(await screen.findByText(/No files yet/)).toBeInTheDocument()
  })

  it('keeps the confirm open until the delete is answered, and shows a refusal after it (review of #3727)', async () => {
    seedSessionFile('sess-f', 'notes.txt', text('hello'), 'text/plain')
    let refuse: (reason: unknown) => void = () => {}
    const remove = vi
      .spyOn(api, 'deleteAiSessionFile')
      .mockImplementation(() => new Promise((_, reject) => (refuse = reject)))
    const user = userEvent.setup()
    render()
    await user.click(await screen.findByRole('button', { name: 'Delete notes.txt' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Delete notes.txt?' })
    await user.click(within(confirm).getByRole('button', { name: 'Delete file' }))
    expect(remove).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('alertdialog', { name: 'Delete notes.txt?' })).toBeInTheDocument()
    expect(within(confirm).getByRole('button', { name: 'Delete file' })).toBeDisabled()
    await act(async () => refuse(new ApiError(409, 'busy')))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(await screen.findByText(/notes\.txt was not deleted/)).toBeInTheDocument()
    expect(screen.getByText('notes.txt')).toBeInTheDocument()
  })

  it('offers no upload or delete on a session the user cannot write', async () => {
    seedSessionFile('sess-f', 'notes.txt', text('hello'), 'text/plain')
    render({ canWrite: false })
    expect(await screen.findByRole('button', { name: 'Download notes.txt' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Upload' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete notes.txt' })).not.toBeInTheDocument()
  })

  it('reads again when its refresh key moves', async () => {
    const view = renderPage(<SessionFiles sessionId="sess-f" canWrite onInsert={() => {}} refreshKey={0} />)
    await screen.findByText(/No files yet/)
    seedSessionFile('sess-f', 'later.txt', text('x'), 'text/plain')
    await act(async () => {
      view.rerender(<SessionFiles sessionId="sess-f" canWrite onInsert={() => {}} refreshKey={1} />)
    })
    expect(await screen.findByText('later.txt')).toBeInTheDocument()
  })
})

describe('upload paths', () => {
  it('makes a name one safe path segment', () => {
    expect(segmentOf('my part (1).stl')).toBe('my_part__1_.stl')
    expect(segmentOf('.env')).toBe('_.env')
    expect(segmentOf('-x')).toBe('_-x')
    expect(segmentOf('ü.txt')).toBe('_.txt')
  })

  it('numbers a name past the ones taken', () => {
    expect(uploadPath('a.stl', new Set())).toBe('uploads/a.stl')
    expect(uploadPath('a.stl', new Set(['uploads/a.stl', 'uploads/a-2.stl']))).toBe('uploads/a-3.stl')
    expect(uploadPath('README', new Set(['uploads/README']))).toBe('uploads/README-2')
  })

  it('names a source in a few words', () => {
    expect(sourceLabel(null)).toBe('')
    expect(sourceLabel({ kind: 'upload' })).toBe('uploaded by you')
    expect(sourceLabel({ url: 'https://a.example/x' })).toBe('from a.example')
    expect(sourceLabel({ tool: 'workspace_save' })).toBe('by workspace_save')
  })
})
