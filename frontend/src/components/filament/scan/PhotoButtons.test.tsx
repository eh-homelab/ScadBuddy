import { screen, waitFor, within } from '@testing-library/react'
import { http } from 'msw'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { entityMock } from '../../../mocks/features/entities'
import { server } from '../../../mocks/server'
import { renderPage } from '../../../test/utils'
import { PhotoButtons, type HeldPhotos } from './PhotoButtons'

const SPOOL = { id: 'sp-1', type_id: 'ty-1' }
const PNG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
const photo = (name = 'label.png') => new File([new Uint8Array([...PNG, 1, 2, 3, 4])], name, { type: 'image/png' })

/** The `Content-Type` of each attachment upload: a multipart form, never JSON with base64 in it. */
let uploadTypes: string[] = []
const listener = ({ request }: { request: Request }) => {
  if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/attachments')) uploadTypes.push(request.headers.get('content-type') ?? '')
}

/** Each stored file is a blob of the photo's own bytes. */
function storedFilesArePhotos() {
  expect(entityMock.files.size).toBeGreaterThan(0)
  for (const file of entityMock.files.values()) {
    expect(file).toBeInstanceOf(Blob)
    expect(file.size).toBe(PNG.length + 4)
  }
  expect(uploadTypes.length).toBeGreaterThan(0)
  for (const type of uploadTypes) expect(type).toMatch(/^multipart\/form-data/)
}

/** The page's part: it keeps the held photos across a remount (another read opened, then this one again). */
function Kept({ spool, shown }: { spool: { id: string; type_id: string | null } | null; shown: boolean }) {
  const [held, setHeld] = useState<HeldPhotos>({})
  return shown ? <PhotoButtons spool={spool} held={held} onHeld={setHeld} /> : <p>Another read</p>
}

describe('PhotoButtons (#7273)', () => {
  beforeEach(() => {
    uploadTypes = []
    server.events.on('request:start', listener)
  })
  afterEach(() => server.events.removeListener('request:start', listener))

  it('saves a filament label on the spool and its type', async () => {
    const { user } = renderPage(<PhotoButtons spool={SPOOL} />)
    await user.upload(screen.getByLabelText('Filament label'), photo())
    await waitFor(() => expect(within(screen.getByRole('group', { name: 'Filament label' })).getByText('Saved')).toBeInTheDocument())
    expect(entityMock.uploads).toEqual([
      { entity: 'spool:sp-1', filename: 'label.png', role: 'filament_label', caption: '' },
      { entity: 'spool_type:ty-1', filename: 'label.png', role: 'filament_label', caption: '' },
    ])
    storedFilesArePhotos()
  })

  it('saves a spool label on the spool only', async () => {
    const { user } = renderPage(<PhotoButtons spool={SPOOL} />)
    await user.upload(screen.getByLabelText('Spool label'), photo('spool.png'))
    await waitFor(() => expect(within(screen.getByRole('group', { name: 'Spool label' })).getByText('Saved')).toBeInTheDocument())
    expect(entityMock.uploads).toEqual([{ entity: 'spool:sp-1', filename: 'spool.png', role: 'spool_label', caption: '' }])
    storedFilesArePhotos()
  })

  it('holds photos of a new tag until a spool exists', async () => {
    const { user, rerender } = renderPage(<PhotoButtons spool={null} />)
    await user.upload(screen.getByLabelText('Filament label'), photo())
    const group = screen.getByRole('group', { name: 'Filament label' })
    expect(within(group).getByRole('img', { name: 'Filament label photo' })).toHaveAttribute('src', expect.stringMatching(/^blob:/))
    expect(within(group).getByText('Kept until the spool is made')).toBeInTheDocument()
    expect(entityMock.uploads).toEqual([])
    rerender(<PhotoButtons spool={{ id: 'sp-2', type_id: null }} />)
    await waitFor(() => expect(within(screen.getByRole('group', { name: 'Filament label' })).getByText('Saved')).toBeInTheDocument())
    expect(entityMock.uploads).toEqual([{ entity: 'spool:sp-2', filename: 'label.png', role: 'filament_label', caption: '' }])
    storedFilesArePhotos()
  })

  it('sends a photo taken while the last one uploads, once that ends', async () => {
    let open: () => void = () => {}
    const gate = new Promise<void>((resolve) => (open = resolve))
    let first = true
    server.use(
      http.post('/api/v1/entities/:ref/attachments', async () => {
        if (!first) return undefined
        first = false
        await gate
        return undefined
      }),
    )
    const { user } = renderPage(<PhotoButtons spool={{ id: 'sp-3', type_id: null }} />)
    await user.upload(screen.getByLabelText('Spool label'), photo('first.png'))
    const group = screen.getByRole('group', { name: 'Spool label' })
    await within(group).findByText('Saving…')
    await user.upload(screen.getByLabelText('Spool label'), photo('second.png'))
    open()
    await waitFor(() => expect(entityMock.uploads.map((one) => one.filename)).toEqual(['first.png', 'second.png']))
    await waitFor(() => expect(within(group).getByText('Saved')).toBeInTheDocument())
    storedFilesArePhotos()
  })

  it("sends a held filament label only where it is not yet saved", async () => {
    let refuseType = true
    server.use(
      http.post('/api/v1/entities/:ref/attachments', ({ params }) => {
        if (!refuseType || !String(params.ref).startsWith('spool_type:')) return undefined
        refuseType = false
        return new Response(JSON.stringify({ detail: 'the store refused the file' }), { status: 422, headers: { 'Content-Type': 'application/problem+json' } })
      }),
    )
    const { user, rerender } = renderPage(<Kept spool={null} shown />)
    await user.upload(screen.getByLabelText('Filament label'), photo())
    rerender(<Kept spool={SPOOL} shown />)
    expect(await screen.findByText('Not saved. Try again.')).toBeInTheDocument()
    expect(entityMock.uploads.map((one) => one.entity)).toEqual(['spool:sp-1'])
    // Another read opens, then this one again: the photo is still there, and only the type is left.
    rerender(<Kept spool={SPOOL} shown={false} />)
    rerender(<Kept spool={SPOOL} shown />)
    const group = screen.getByRole('group', { name: 'Filament label' })
    expect(within(group).getByText('Not saved. Try again.')).toBeInTheDocument()
    expect(within(group).getByRole('img', { name: 'Filament label photo' })).toBeInTheDocument()
    await user.click(within(group).getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(within(group).getByText('Saved')).toBeInTheDocument())
    expect(entityMock.uploads.map((one) => one.entity)).toEqual(['spool:sp-1', 'spool_type:ty-1'])
  })

  it('keeps a failed photo of a known spool when another read opens', async () => {
    entityMock.refuseNextUpload = 'the file is larger than the upload limit'
    const { user, rerender } = renderPage(<Kept spool={SPOOL} shown />)
    await user.upload(screen.getByLabelText('Spool label'), photo('spool.png'))
    expect(await screen.findByText('Not saved. Try again.')).toBeInTheDocument()
    rerender(<Kept spool={SPOOL} shown={false} />)
    rerender(<Kept spool={SPOOL} shown />)
    const group = screen.getByRole('group', { name: 'Spool label' })
    expect(within(group).getByText('Not saved. Try again.')).toBeInTheDocument()
    expect(within(group).getByRole('img', { name: 'Spool label photo' })).toBeInTheDocument()
    expect(entityMock.uploads).toEqual([])
  })

  it('keeps a photo that failed to upload', async () => {
    entityMock.refuseNextUpload = 'the file is larger than the upload limit'
    const { user } = renderPage(<PhotoButtons spool={SPOOL} />)
    await user.upload(screen.getByLabelText('Spool label'), photo('spool.png'))
    const group = screen.getByRole('group', { name: 'Spool label' })
    expect(await within(group).findByText('Not saved. Try again.')).toBeInTheDocument()
    expect(within(group).getByRole('img', { name: 'Spool label photo' })).toBeInTheDocument()
    expect(entityMock.uploads).toEqual([])
    await user.click(within(group).getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(within(group).getByText('Saved')).toBeInTheDocument())
    expect(entityMock.uploads).toEqual([{ entity: 'spool:sp-1', filename: 'spool.png', role: 'spool_label', caption: '' }])
    storedFilesArePhotos()
  })
})
