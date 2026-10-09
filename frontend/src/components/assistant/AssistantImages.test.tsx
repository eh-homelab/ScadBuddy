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

vi.mock('../../agent/chat/availability', () => ({ useAiAvailability: () => ({ available: true }) }))

// The edge each preparation was asked to scale to.
const edges = vi.hoisted(() => [] as (number | undefined)[])

// jsdom has no canvas: the preparation itself is images.test.ts's; here it encodes the name.
vi.mock('../../agent/chat/images', async (importOriginal) => {
  const actual = await importOriginal<typeof Images>()
  // Kept for the lightbox as the real one keeps it (#1891).
  const remembered = (image: Parameters<typeof actual.rememberFullSize>[0]) => {
    actual.rememberFullSize(image)
    return image
  }
  return {
    ...actual,
    prepareImage: (file: File, _codec?: unknown, edge?: number) => {
      edges.push(edge)
      return file.type === 'image/svg+xml'
        ? Promise.reject(new Error(`${file.name} is not a PNG, JPEG, GIF or WebP image.`))
        : Promise.resolve(remembered({
            mediaType: 'image/png' as const,
            data: btoa(file.name),
            preview: { mediaType: 'image/jpeg' as const, data: btoa(`preview of ${file.name}`) },
          }))
    },
  }
})

let agent: MockAgentTransport
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

  it('takes a pasted image into the composer, uploads it, and sends its id with the message (#1941)', async () => {
    const uploaded: unknown[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname === '/api/v1/ai/attachments') {
        void request.clone().json().then((body) => uploaded.push(body))
      }
    })
    const { view, box } = await openComposer()
    paste(box, [png()])
    const list = await screen.findByRole('list', { name: 'Images to send' })
    expect(within(list).getByRole('img', { name: 'shot.png' })).toBeInTheDocument()
    server.events.removeAllListeners('request:start')
    expect(uploaded).toEqual([
      { mediaType: 'image/png', data: btoa('shot.png'), preview: { mediaType: 'image/jpeg', data: btoa('preview of shot.png') } },
    ])

    await view.user.type(box, 'What is this?{Enter}')
    await waitFor(() => expect(sentMessages()).toHaveLength(1))
    expect(sentMessages()[0]).toMatchObject({
      text: 'What is this?',
      images: [{ kind: 'attachment', id: expect.any(String) }],
    })
    // No image bytes in the socket's frame.
    expect(JSON.stringify(sentMessages()[0])).not.toContain(btoa('shot.png'))
    // Sent: the composer is empty again, and the transcript shows the preview.
    expect(queued()).not.toBeInTheDocument()
    const sent = await screen.findByRole('list', { name: 'Images sent' })
    expect(within(sent).getByRole('img', { name: 'Image 1 of 1' })).toHaveAttribute(
      'src',
      `data:image/jpeg;base64,${btoa('preview of shot.png')}`,
    )
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
    expect(within(queued()!).getAllByRole('img')).toHaveLength(4)
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

  it('opens a sent image full size over the panel, and Esc closes only the image (#1891)', async () => {
    const { view, box } = await openComposer()
    paste(box, [png()])
    await screen.findByRole('list', { name: 'Images to send' })
    await view.user.type(box, 'What is this?{Enter}')
    const sent = await screen.findByRole('list', { name: 'Images sent' })
    const thumb = within(sent).getByRole('button', { name: 'View image 1 of 1 larger' })
    await view.user.click(thumb)

    const dialog = screen.getByRole('dialog', { name: 'Image 1 of 1' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(within(dialog).getByRole('img', { name: 'Image 1 of 1, as sent' })).toHaveAttribute(
      'src',
      `data:image/png;base64,${btoa('shot.png')}`,
    )
    expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveFocus()

    await view.user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toBeVisible()
    expect(thumb).toHaveFocus()
  })
})
