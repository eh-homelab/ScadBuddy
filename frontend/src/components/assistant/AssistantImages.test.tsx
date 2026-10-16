import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import type { ClientMessage } from '../../agent/chat/protocol'
import { createMockAgentTransport, type MockAgentTransport } from '../../mocks/agent'
import { renderPage } from '../../test/utils'
import { server } from '../../mocks/server'
import { HttpResponse, http } from 'msw'
import { AppShell } from '../AppShell'
import type * as Images from '../../agent/chat/images'

// #1866 — images pasted, dropped or attached in the assistant's composer.
// #2479 — uploaded as bytes, shown from blob URLs, never base64.

vi.mock('../../agent/chat/availability', () => ({ useAiAvailability: () => ({ available: true }) }))

// The edge each preparation was asked to scale to.
const edges = vi.hoisted(() => [] as (number | undefined)[])

// jsdom has no canvas: the preparation itself is images.test.ts's; here it encodes the name.
vi.mock('../../agent/chat/images', async (importOriginal) => {
  const actual = await importOriginal<typeof Images>()
  return {
    ...actual,
    prepareImage: (file: File, _codec?: unknown, edge?: number) => {
      edges.push(edge)
      return file.type === 'image/svg+xml'
        ? Promise.reject(new Error(`${file.name} is not a PNG, JPEG, GIF or WebP image.`))
        : Promise.resolve({
            mediaType: 'image/png' as const,
            // A `big` image is 3.5 MiB: two of them are past the 6 MiB total.
            blob: new Blob([file.name.startsWith('big') ? new Uint8Array(3.5 * 1024 * 1024) : file.name], { type: 'image/png' }),
            preview: { mediaType: 'image/jpeg' as const, blob: new Blob([`preview of ${file.name}`], { type: 'image/jpeg' }) },
          })
    },
  }
})

let agent: MockAgentTransport

// jsdom has no blob URLs: each blob gets one of its own, and a revoke is recorded.
const revoked: string[] = []
// The upload is multipart (#2479): msw parses it with Node 24's undici, which asserts each
// file part is Node's own `File`, so that is the global here (mocks/handlers.test.ts
// `withNodeFile` says more), as it is for the browser worker and the real server.
let NodeFile: typeof File
beforeAll(async () => {
  const builtin = 'node:buffer'
  ;({ File: NodeFile } = (await import(/* @vite-ignore */ builtin)) as { File: typeof File })
})
afterEach(() => {
  vi.unstubAllGlobals()
})
beforeEach(() => {
  vi.stubGlobal('File', NodeFile)
  let n = 0
  revoked.length = 0
  URL.createObjectURL = vi.fn(() => `blob:thumb-${++n}`)
  URL.revokeObjectURL = vi.fn((url: string) => void revoked.push(url))
})
const factory = () => {
  agent = createMockAgentTransport({ stepMs: 0 })
  return agent
}

const png = (name = 'shot.png') => new File(['png'], name, { type: 'image/png' })

const sentMessages = () =>
  agent.sent.filter((m): m is Extract<ClientMessage, { type: 'user.message' }> => m.type === 'user.message')

async function openComposer() {
  const view = renderPage(
    <Routes>
      <Route element={<AppShell embedded={false} assistantTransport={factory} />}>
        <Route path="*" element={<p>page</p>} />
      </Route>
    </Routes>,
    { route: '/m/name-keychain' },
  )
  await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
  const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
  return { view, box }
}

function paste(target: Element, files: File[], text = '') {
  fireEvent.paste(target, {
    clipboardData: { files, items: [], types: text ? ['text/plain', 'Files'] : ['Files'], getData: () => text },
  })
}

const queued = () => screen.queryByRole('list', { name: 'Images to send' })

describe('assistant images (#1866)', () => {
  it('scales to the long edge stored in Settings', async () => {
    let served = () => {}
    const read = new Promise<void>((resolve) => (served = resolve))
    server.use(
      http.get('/api/v1/ai/settings/images', () => {
        served()
        return HttpResponse.json({ long_edge: 2000, min: 200, max: 2576 })
      }),
    )
    edges.length = 0
    const { box } = await openComposer()
    // Read when the panel opens; an image pasted once it has answered is scaled to it.
    await read
    await new Promise((r) => setTimeout(r, 50))
    paste(box, [png()])
    await screen.findByRole('list', { name: 'Images to send' })
    expect(edges).toEqual([2000])
  })

  it('takes a pasted image into the composer, uploads its bytes, and sends its id with the message (#1941, #2479)', async () => {
    const uploaded: Promise<Record<string, unknown>>[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname === '/api/v1/ai/attachments') {
        uploaded.push(
          (async () => {
            const form = await request.clone().formData()
            const part = async (name: string) => {
              const file = form.get(name) as File
              return { type: file.type, text: await file.text() }
            }
            return { contentType: request.headers.get('content-type'), image: await part('image'), preview: await part('preview') }
          })(),
        )
      }
    })
    const { view, box } = await openComposer()
    paste(box, [png()])
    const list = await screen.findByRole('list', { name: 'Images to send' })
    // The thumbnail is the preview's blob URL, never a data: URL.
    expect((await within(list).findByRole('img', { name: 'shot.png' })).getAttribute('src')).toMatch(/^blob:/)
    server.events.removeAllListeners('request:start')
    expect(await Promise.all(uploaded)).toEqual([
      {
        contentType: expect.stringMatching(/^multipart\/form-data/),
        image: { type: 'image/png', text: 'shot.png' },
        preview: { type: 'image/jpeg', text: 'preview of shot.png' },
      },
    ])

    await view.user.type(box, 'What is this?{Enter}')
    await waitFor(() => expect(sentMessages()).toHaveLength(1))
    expect(sentMessages()[0]).toMatchObject({
      text: 'What is this?',
      images: [{ kind: 'attachment', id: expect.any(String) }],
    })
    // No image bytes in the socket's frame.
    expect(JSON.stringify(sentMessages()[0])).not.toContain('shot.png')
    // Sent: the composer is empty again, its thumbnail's URL let go, and the transcript
    // shows the preview the turn names, from the agent's store.
    expect(queued()).not.toBeInTheDocument()
    expect(revoked).toEqual(['blob:thumb-1'])
    const sent = await screen.findByRole('list', { name: 'Images sent' })
    const preview = within(sent).getByRole('img', { name: 'Image 1 of 1' }).getAttribute('src') ?? ''
    expect(preview).toMatch(/^\/api\/v1\/ai\/sessions\/[^/]+\/blobs\/[0-9a-f]{64}\.jpg$/)
    expect(await (await fetch(new URL(preview, window.location.href))).text()).toBe('preview of shot.png')
  })

  it('says why an image could not be uploaded, and leaves it out (#1941)', async () => {
    server.use(
      http.post('/api/v1/ai/attachments', () =>
        HttpResponse.json({ detail: 'too many images waiting to be sent' }, { status: 429 }),
      ),
    )
    const { box } = await openComposer()
    paste(box, [png()])
    expect(await screen.findByText('shot.png could not be uploaded: too many images waiting to be sent')).toBeInTheDocument()
    expect(queued()).not.toBeInTheDocument()
  })

  it('counts images still uploading against the total, so two attaches at once cannot pass it (#2119)', async () => {
    let release = () => {}
    const gate = new Promise<void>((resolve) => (release = resolve))
    let uploads = 0
    server.use(
      http.post('/api/v1/ai/attachments', async () => {
        uploads += 1
        await gate
        return HttpResponse.json({ id: crypto.randomUUID() })
      }),
    )
    const { box } = await openComposer()
    paste(box, [png('big-1.png')])
    await waitFor(() => expect(uploads).toBe(1))
    // The first is still uploading: the second, which would pass the total
    // on attached images alone, is refused.
    paste(box, [png('big-2.png')])
    expect(
      await screen.findByText("big-2.png was not added: this message's images would be too large together."),
    ).toBeInTheDocument()
    release()
    const list = await screen.findByRole('list', { name: 'Images to send' })
    expect((await within(list).findAllByRole('img')).map((i) => i.getAttribute('alt'))).toEqual(['big-1.png'])
    expect(uploads).toBe(1)
  })

  it('deletes the upload of an image removed before sending (#1941)', async () => {
    const deleted: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'DELETE') deleted.push(new URL(request.url).pathname)
    })
    const { view, box } = await openComposer()
    paste(box, [png('a.png')])
    await screen.findByRole('list', { name: 'Images to send' })
    await view.user.click(screen.getByRole('button', { name: 'Remove a.png' }))
    await waitFor(() => expect(deleted).toHaveLength(1))
    server.events.removeAllListeners('request:start')
    expect(deleted[0]).toMatch(/^\/api\/v1\/ai\/attachments\/[0-9a-f-]{36}$/)
  })

  it('leaves a paste that carries text to the text box', async () => {
    const { box } = await openComposer()
    paste(box, [png()], 'some cells')
    await new Promise((r) => setTimeout(r, 20))
    expect(queued()).not.toBeInTheDocument()
  })

  it('removes an image before sending, so the message goes without it', async () => {
    const { view, box } = await openComposer()
    paste(box, [png('a.png'), png('b.png')])
    const list = await screen.findByRole('list', { name: 'Images to send' })
    await waitFor(() => expect(within(list).getAllByRole('img')).toHaveLength(2))
    await view.user.click(screen.getByRole('button', { name: 'Remove a.png' }))
    expect(within(list).getAllByRole('img').map((i) => i.getAttribute('alt'))).toEqual(['b.png'])
    await view.user.click(screen.getByRole('button', { name: 'Remove b.png' }))
    expect(queued()).not.toBeInTheDocument()

    await view.user.type(box, 'no pictures{Enter}')
    await waitFor(() => expect(sentMessages()).toHaveLength(1))
    expect(sentMessages()[0]).not.toHaveProperty('images')
  })

  it('takes images dropped on the composer', async () => {
    const { box } = await openComposer()
    fireEvent.drop(box, { dataTransfer: { files: [png('dropped.png')], items: [], types: ['Files'] } })
    expect(await screen.findByRole('img', { name: 'dropped.png' })).toBeInTheDocument()
  })

  it('takes images picked with Attach images', async () => {
    const { view } = await openComposer()
    await view.user.upload(screen.getByLabelText('Attach images'), [png('picked.png')])
    expect(await screen.findByRole('img', { name: 'picked.png' })).toBeInTheDocument()
  })

  it('says why an image was not taken: its type, or past the count', async () => {
    const { box } = await openComposer()
    fireEvent.drop(box, {
      dataTransfer: { files: [new File(['<svg/>'], 'logo.svg', { type: 'image/svg+xml' })], items: [], types: ['Files'] },
    })
    expect(await screen.findByText('logo.svg is not a PNG, JPEG, GIF or WebP image.')).toBeInTheDocument()

    paste(box, ['1', '2', '3', '4', '5'].map((n) => png(`${n}.png`)))
    expect(await screen.findByText('At most 4 images per message: 5.png was not added.')).toBeInTheDocument()
    expect(await within(queued()!).findAllByRole('img')).toHaveLength(4)
  })

  it('asks for words to go with images, and sends nothing without them', async () => {
    const { box } = await openComposer()
    paste(box, [png()])
    await screen.findByRole('list', { name: 'Images to send' })
    expect(screen.getByText('Add a message to send with the images.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(sentMessages()).toHaveLength(0)
  })

  it('opens a sent image full size from the agent’s store over the panel, and Esc closes only the image (#1891)', async () => {
    const { view, box } = await openComposer()
    paste(box, [png()])
    await screen.findByRole('list', { name: 'Images to send' })
    await view.user.type(box, 'What is this?{Enter}')
    const sent = await screen.findByRole('list', { name: 'Images sent' })
    const thumb = within(sent).getByRole('button', { name: 'View image 1 of 1 larger' })
    await view.user.click(thumb)

    const dialog = screen.getByRole('dialog', { name: 'Image 1 of 1' })
    // Modal: Radix's (SentImages.test.tsx checks the panel is hidden behind it).
    expect(dialog).toHaveAttribute('data-modal')
    // The turn names the image the agent stored (the msw store serves it); jsdom loads no
    // images, so the load is the test's to fire.
    const full = within(dialog).getByAltText('Image 1 of 1, as sent')
    const src = full.getAttribute('src') ?? ''
    expect(src).toMatch(/^\/api\/v1\/ai\/sessions\/[^/]+\/blobs\/[0-9a-f]{64}\.png$/)
    const served = await fetch(new URL(src, window.location.href))
    expect(served.headers.get('content-type')).toBe('image/png')
    expect(await served.text()).toBe('shot.png')
    fireEvent.load(full)
    expect(within(dialog).getByRole('img', { name: 'Image 1 of 1, as sent' })).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveFocus()

    await view.user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toBeVisible()
    await waitFor(() => expect(thumb).toHaveFocus())
  })
})
