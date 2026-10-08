import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import type { ClientMessage } from '../../agent/chat/protocol'
import { createMockAgentTransport, type MockAgentTransport } from '../../mocks/agent'
import { renderPage } from '../../test/utils'
import { AppShell } from '../AppShell'
import type * as Images from '../../agent/chat/images'

// #1866 — images pasted, dropped or attached in the assistant's composer.

vi.mock('../../agent/chat/availability', () => ({ useAiAvailability: () => ({ available: true }) }))

// jsdom has no canvas: the preparation itself is images.test.ts's; here it encodes the name.
vi.mock('../../agent/chat/images', async (importOriginal) => {
  const actual = await importOriginal<typeof Images>()
  return {
    ...actual,
    prepareImage: (file: File) =>
      file.type === 'image/svg+xml'
        ? Promise.reject(new Error(`${file.name} is not a PNG, JPEG, GIF or WebP image.`))
        : Promise.resolve({
            mediaType: 'image/png' as const,
            data: btoa(file.name),
            preview: { mediaType: 'image/jpeg' as const, data: btoa(`preview of ${file.name}`) },
          }),
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
  it('takes a pasted image into the composer and sends it with the message', async () => {
    const { view, box } = await openComposer()
    paste(box, [png()])
    const list = await screen.findByRole('list', { name: 'Images to send' })
    expect(within(list).getByRole('img', { name: 'shot.png' })).toBeInTheDocument()

    await view.user.type(box, 'What is this?{Enter}')
    await waitFor(() => expect(sentMessages()).toHaveLength(1))
    expect(sentMessages()[0]).toMatchObject({
      text: 'What is this?',
      images: [{ mediaType: 'image/png', data: btoa('shot.png'), preview: { mediaType: 'image/jpeg' } }],
    })
    // Sent: the composer is empty again, and the transcript shows the preview.
    expect(queued()).not.toBeInTheDocument()
    const sent = await screen.findByRole('list', { name: 'Images sent' })
    expect(within(sent).getByRole('img', { name: 'Image 1 of 1' })).toHaveAttribute(
      'src',
      `data:image/jpeg;base64,${btoa('preview of shot.png')}`,
    )
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
})
