import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { keychainReadme } from '../mocks/fixtures'
import { emitRealtime } from '../mocks/realtime'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { EditDetailsButton } from './EditDetailsButton'

interface Sent {
  method: string
  path: string
  body: unknown
}

/** Every write the form makes, in order. */
function recordWrites(): Sent[] {
  const sent: Sent[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === 'GET') return
    const entry: Sent = { method: request.method, path: new URL(request.url).pathname, body: null }
    sent.push(entry)
    // The client labels every non-multipart request JSON, bodiless DELETEs included.
    if (request.headers.get('content-type')?.includes('application/json')) {
      void request
        .clone()
        .text()
        .then((text) => {
          entry.body = text ? (JSON.parse(text) as unknown) : null
        })
    }
  })
  return sent
}

async function open(slug = 'name-keychain') {
  const onSaved = vi.fn()
  const view = renderPage(<EditDetailsButton slug={slug} onSaved={onSaved} />, {
    userEventOptions: { applyAccept: false },
  })
  await view.user.click(screen.getByRole('button', { name: 'Edit details' }))
  const dialog = screen.getByRole('dialog', { name: 'Edit details' })
  await within(dialog).findByLabelText('Name')
  return { ...view, dialog, onSaved }
}

afterEach(() => {
  vi.restoreAllMocks()
  server.events.removeAllListeners()
})

describe('EditDetailsButton', () => {
  it('opens on the model as it is, README included', async () => {
    const { dialog } = await open()

    expect(within(dialog).getByLabelText('Name')).toHaveValue('Name Keychain')
    expect(within(dialog).getByLabelText('Tags')).toHaveValue('keychain, two-colour, text')
    expect(within(dialog).getByLabelText('README')).toHaveValue(keychainReadme)
    expect(within(dialog).getByTestId('thumbnail-state')).toHaveTextContent('Set on this model')
  })

  it('sends only what changed, one revision each', async () => {
    const sent = recordWrites()
    const { dialog, user, onSaved } = await open()

    await user.clear(within(dialog).getByLabelText('Name'))
    await user.type(within(dialog).getByLabelText('Name'), 'Keyring')
    await user.clear(within(dialog).getByLabelText('Tags'))
    await user.type(within(dialog).getByLabelText('Tags'), 'keychain, gift')
    await user.type(within(dialog).getByLabelText('README'), 'Prints flat.\n')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(sent.map(({ method, path }) => `${method} ${path}`)).toEqual([
      'PATCH /api/v1/models/name-keychain',
      'PUT /api/v1/models/name-keychain/readme',
    ])
    expect(sent[0]?.body).toEqual({ name: 'Keyring', tags: ['keychain', 'gift'] })
    expect(sent[1]?.body).toEqual({ content: `${keychainReadme}Prints flat.\n` })
    expect(await api.getReadme('name-keychain')).toBe(`${keychainReadme}Prints flat.\n`)
    expect(onSaved.mock.calls[0]?.[0]).toMatchObject({ name: 'Keyring', has_readme: true })
  })

  it('saves nothing when nothing changed', async () => {
    const sent = recordWrites()
    const { dialog, user, onSaved } = await open()

    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(sent).toEqual([])
  })

  it('removes an emptied README rather than saving an empty one', async () => {
    const sent = recordWrites()
    const { dialog, user, onSaved } = await open()

    await user.clear(within(dialog).getByLabelText('README'))
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(sent.map(({ method, path }) => `${method} ${path}`)).toEqual([
      'DELETE /api/v1/models/name-keychain/readme',
    ])
    expect(onSaved.mock.calls[0]?.[0]).toMatchObject({ has_readme: false })
  })

  it('says a whitespace-only README removes it, then removes it on save', async () => {
    const removeReadme = vi.spyOn(api, 'removeReadme')
    const setReadme = vi.spyOn(api, 'setReadme')
    const { dialog, user, onSaved } = await open()
    expect(within(dialog).queryByTestId('readme-state')).not.toBeInTheDocument()

    await user.clear(within(dialog).getByLabelText('README'))
    await user.type(within(dialog).getByLabelText('README'), '   ')

    // Visible before anything is sent, the way a removed thumbnail is.
    expect(within(dialog).getByTestId('readme-state')).toHaveTextContent(
      'Saving will remove the README',
    )
    expect(removeReadme).not.toHaveBeenCalled()

    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(removeReadme).toHaveBeenCalledExactlyOnceWith('name-keychain')
    expect(setReadme).not.toHaveBeenCalled()
    expect(onSaved.mock.calls[0]?.[0]).toMatchObject({ has_readme: false })
  })

  it('leaves a stored whitespace-only README alone when only the name changes', async () => {
    server.use(
      http.get('/api/v1/models/:slug/readme', () =>
        HttpResponse.text('  \n\t\n', {
          headers: { 'Content-Type': 'text/markdown; charset=utf-8' },
        }),
      ),
    )
    const removeReadme = vi.spyOn(api, 'removeReadme')
    const setReadme = vi.spyOn(api, 'setReadme')
    const updateModel = vi.spyOn(api, 'updateModel')
    const { dialog, user, onSaved } = await open()
    expect(within(dialog).getByLabelText('README')).toHaveValue('  \n\t\n')

    await user.clear(within(dialog).getByLabelText('Name'))
    await user.type(within(dialog).getByLabelText('Name'), 'Keyring')
    expect(within(dialog).queryByTestId('readme-state')).not.toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(updateModel).toHaveBeenCalledExactlyOnceWith('name-keychain', { name: 'Keyring' })
    expect(removeReadme).not.toHaveBeenCalled()
    expect(setReadme).not.toHaveBeenCalled()
  })

  it('removes a stored whitespace-only README only when asked to', async () => {
    server.use(
      http.get('/api/v1/models/:slug/readme', () =>
        HttpResponse.text('   ', { headers: { 'Content-Type': 'text/markdown; charset=utf-8' } }),
      ),
    )
    const removeReadme = vi.spyOn(api, 'removeReadme')
    const { dialog, user, onSaved } = await open()

    await user.click(within(dialog).getByRole('button', { name: 'Remove README' }))
    expect(within(dialog).getByTestId('readme-state')).toHaveTextContent(
      'Saving will remove the README',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(removeReadme).toHaveBeenCalledExactlyOnceWith('name-keychain')
  })

  it('offers Remove README, which empties it and says so', async () => {
    const { dialog, user } = await open()

    await user.click(within(dialog).getByRole('button', { name: 'Remove README' }))

    expect(within(dialog).getByLabelText('README')).toHaveValue('')
    expect(within(dialog).getByTestId('readme-state')).toHaveTextContent(
      'Saving will remove the README',
    )
  })

  it('makes no call for a whitespace-only README on a model without one', async () => {
    const sent = recordWrites()
    const { dialog, user, onSaved } = await open('gridfinity-bin')
    expect(within(dialog).queryByRole('button', { name: 'Remove README' })).not.toBeInTheDocument()

    await user.type(within(dialog).getByLabelText('README'), '  \n ')
    expect(within(dialog).getByTestId('readme-state')).toHaveTextContent('no README is saved')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(sent).toEqual([])
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('saves an edited README exactly as typed', async () => {
    const setReadme = vi.spyOn(api, 'setReadme')
    const removeReadme = vi.spyOn(api, 'removeReadme')
    const { dialog, user, onSaved } = await open('gridfinity-bin')

    await user.type(within(dialog).getByLabelText('README'), '  # Bin\n\nIndented.  ')
    expect(within(dialog).queryByTestId('readme-state')).not.toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(setReadme).toHaveBeenCalledExactlyOnceWith('gridfinity-bin', '  # Bin\n\nIndented.  ')
    expect(removeReadme).not.toHaveBeenCalled()
  })

  it('sets a new thumbnail from a chosen PNG', async () => {
    // Spied, as the multipart upload cannot cross jsdom into Node's fetch.
    const setThumbnail = vi
      .spyOn(api, 'setThumbnail')
      .mockImplementation(async (slug) => ({ ...(await api.getModel(slug)), version: 'next' }))
    const { dialog, user, onSaved } = await open('gridfinity-bin')
    expect(within(dialog).getByTestId('thumbnail-state')).toHaveTextContent('None set')

    const png = new File(['png'], 'cover.png', { type: 'image/png' })
    await user.upload(within(dialog).getByLabelText('Thumbnail (PNG)'), png)
    expect(within(dialog).getByTestId('thumbnail-state')).toHaveTextContent('cover.png')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(setThumbnail).toHaveBeenCalledWith('gridfinity-bin', png)
  })

  it('shows the server\'s 422 when a file named .png is not a PNG by its bytes', async () => {
    // The client checks a name or type; the server reads the signature. Node's own
    // `File` and `FormData` stand in for jsdom's, so the real multipart PUT crosses
    // into Node's fetch and reaches the mock's `_require_png`, not a spy.
    const builtin = 'node:buffer'
    const { File: NodeFile } = (await import(/* @vite-ignore */ builtin)) as { File: typeof File }
    const NodeFormData = (
      await new Response('a=b', {
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      }).formData()
    ).constructor as typeof FormData
    vi.stubGlobal('File', NodeFile)
    vi.stubGlobal('FormData', NodeFormData)
    try {
      const setThumbnail = vi.spyOn(api, 'setThumbnail')
      const { dialog, user, onSaved } = await open()

      const renamed = new NodeFile(['GIF89a'], 'cover.png', { type: 'image/png' })
      await user.upload(within(dialog).getByLabelText('Thumbnail (PNG)'), renamed)
      expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument()
      await user.click(within(dialog).getByRole('button', { name: 'Save' }))

      expect(await within(dialog).findByRole('alert')).toHaveTextContent(
        'the thumbnail is not a PNG',
      )
      expect(setThumbnail).toHaveBeenCalledWith('name-keychain', renamed)
      expect(onSaved).not.toHaveBeenCalled()
      expect((await api.getModel('name-keychain')).thumbnail_source).toBe('model')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('refuses a thumbnail that is not a PNG', async () => {
    const { dialog, user } = await open()

    await user.upload(
      within(dialog).getByLabelText('Thumbnail (PNG)'),
      new File(['gif'], 'cover.gif', { type: 'image/gif' }),
    )

    expect(within(dialog).getByRole('alert')).toHaveTextContent('must be a PNG')
    expect(within(dialog).getByTestId('thumbnail-state')).toHaveTextContent('Set on this model')
  })

  it('holds a README over the server limit back, and every other change with it', async () => {
    const sent = recordWrites()
    const { dialog, user } = await open()
    const readme = within(dialog).getByLabelText('README')
    await user.clear(within(dialog).getByLabelText('Name'))
    await user.type(within(dialog).getByLabelText('Name'), 'Keyring')

    // Pasted, not typed: a million keystrokes would take all day.
    fireEvent.change(readme, { target: { value: 'x'.repeat(1_000_000) } })
    expect(within(dialog).queryByTestId('readme-limit')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeEnabled()

    fireEvent.change(readme, { target: { value: 'x'.repeat(1_000_001) } })
    expect(within(dialog).getByTestId('readme-limit')).toHaveTextContent(
      'The README must be at most 1,000,000 characters.',
    )
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(sent).toEqual([])
  })

  it('refuses a thumbnail over 10 MiB before anything is sent', async () => {
    const setThumbnail = vi.spyOn(api, 'setThumbnail')
    const { dialog, user } = await open()
    expect(within(dialog).getByText('PNG, up to 10 MiB')).toBeInTheDocument()

    const big = new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'huge.png', { type: 'image/png' })
    await user.upload(within(dialog).getByLabelText('Thumbnail (PNG)'), big)

    expect(within(dialog).getByRole('alert')).toHaveTextContent('10 MiB or smaller')
    expect(within(dialog).getByTestId('thumbnail-state')).toHaveTextContent('Set on this model')
    expect(setThumbnail).not.toHaveBeenCalled()
  })

  it('removes the thumbnail set on the model', async () => {
    const sent = recordWrites()
    const { dialog, user, onSaved } = await open()

    await user.click(within(dialog).getByRole('button', { name: 'Remove thumbnail' }))
    expect(within(dialog).getByTestId('thumbnail-state')).toHaveTextContent('Removed on save')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(sent.map(({ method, path }) => `${method} ${path}`)).toEqual([
      'DELETE /api/v1/models/name-keychain/thumbnail',
    ])
    // The keychain has been generated, so its first plate image takes over.
    expect(onSaved.mock.calls[0]?.[0]).toMatchObject({ thumbnail_source: 'output' })
  })

  it('stays open and says why when a step is refused, without resending what landed', async () => {
    const sent = recordWrites()
    server.use(
      http.put('/api/v1/models/:slug/readme', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Unprocessable', status: 422, detail: 'the README is bad' },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { dialog, user, onSaved } = await open()

    await user.type(within(dialog).getByLabelText('Description'), ' Now with more.')
    await user.type(within(dialog).getByLabelText('README'), 'More.')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('the README is bad')
    expect(onSaved).not.toHaveBeenCalled()

    server.resetHandlers()
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(sent.map(({ method, path }) => `${method} ${path}`)).toEqual([
      'PATCH /api/v1/models/name-keychain',
      'PUT /api/v1/models/name-keychain/readme',
      'PUT /api/v1/models/name-keychain/readme',
    ])
  })

  it('will not save a model without a name', async () => {
    const { dialog, user } = await open()
    await user.clear(within(dialog).getByLabelText('Name'))
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()
  })
})

describe('EditDetailsButton, live (#269)', () => {
  it('keeps what is being typed and offers the details changed elsewhere', async () => {
    const { user, dialog } = await open()
    const name = within(dialog).getByLabelText('Name')
    await user.clear(name)
    await user.type(name, 'Typed here')

    await api.updateModel('name-keychain', { name: 'Changed Elsewhere' })
    emitRealtime('model.updated', ['model:name-keychain'], { slug: 'name-keychain' })
    expect(await within(dialog).findByText(/changed elsewhere since you opened them/)).toBeInTheDocument()
    expect(name).toHaveValue('Typed here')

    await user.click(within(dialog).getByRole('button', { name: 'Load the latest' }))
    await waitFor(() => expect(within(dialog).getByLabelText('Name')).toHaveValue('Changed Elsewhere'))
  })

  it('shows the latest load when an earlier, slower one answers after it', async () => {
    const model = await api.getModel('name-keychain')
    let answerFirst: (() => void) | undefined
    vi.spyOn(api, 'getModel')
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            answerFirst = () => resolve({ ...model, name: 'Stale' })
          }),
      )
      .mockResolvedValueOnce({ ...model, name: 'Fresh' })
    const view = renderPage(<EditDetailsButton slug="name-keychain" onSaved={vi.fn()} />)
    await view.user.click(screen.getByRole('button', { name: 'Edit details' }))
    await view.user.keyboard('{Escape}')
    await view.user.click(screen.getByRole('button', { name: 'Edit details' }))
    const dialog = screen.getByRole('dialog', { name: 'Edit details' })
    await waitFor(() => expect(within(dialog).getByLabelText('Name')).toHaveValue('Fresh'))
    answerFirst?.()
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(within(dialog).getByLabelText('Name')).toHaveValue('Fresh')
  })

  it('ignores a model update that leaves these details as they were (a pin, a source save)', async () => {
    const { dialog } = await open()
    emitRealtime('model.updated', ['model:name-keychain'], { slug: 'name-keychain' })
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(within(dialog).queryByText(/changed elsewhere since you opened them/)).not.toBeInTheDocument()
  })
})
