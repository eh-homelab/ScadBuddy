import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { prints } from '../../mocks/fixtures'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { PrintDetailPage } from '../../pages/PrintDetailPage'

function render() {
  return renderPage(
    <Routes>
      <Route path="/prints/:archiveId" element={<PrintDetailPage />} />
    </Routes>,
    { route: '/prints/35' },
  )
}

async function attachments(): Promise<HTMLElement> {
  return await screen.findByRole('region', { name: 'Photos and videos' })
}

const photo = () => new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'desk.png', { type: 'image/png' })
const video = () => new File([new Uint8Array(64)], 'spin.mp4', { type: 'video/mp4' })

describe('a print’s own photos and videos (#309, #311)', () => {
  it('offers the camera on a phone, and files, and says when there are none yet', async () => {
    render()
    const section = await attachments()
    const camera = within(section).getByLabelText('Take a photo')
    expect(camera).toHaveAttribute('capture', 'environment')
    expect(camera).toHaveAttribute('accept', 'image/*')
    expect(within(section).getByLabelText('Choose files')).toHaveAttribute('multiple')
    expect(section).toHaveTextContent('No photos or videos added yet.')
  })

  it('adds chosen files and shows them, in the gallery and the files too', async () => {
    const sent = vi.spyOn(api, 'addPrintAttachment')
    const { user } = render()
    const section = await attachments()
    await user.upload(within(section).getByLabelText('Choose files'), [photo(), video()])

    await waitFor(() => expect(sent).toHaveBeenCalledTimes(2))
    expect(sent.mock.calls.map(([archive, file]) => [archive, file.name])).toEqual([
      [35, 'desk.png'],
      [35, 'spin.mp4'],
    ])
    const added = await within(section).findByRole('list', { name: 'Added photos and videos' })
    await waitFor(() => expect(within(added).getAllByRole('listitem')).toHaveLength(2))
    expect(within(added).getByRole('img', { name: 'desk.png' })).toHaveAttribute(
      'src',
      '/api/v1/prints/35/attachments/1/file',
    )
    expect(within(added).getByLabelText('spin.mp4', { selector: 'video' })).toBeInTheDocument()
    const files = screen.getByRole('region', { name: 'Files' })
    expect(within(files).getByRole('button', { name: 'Video, spin.mp4, open' })).toBeInTheDocument()
    expect(within(files).getByRole('button', { name: 'Photo, desk.png, open' })).toBeInTheDocument()
    sent.mockRestore()
  })

  it('takes a dropped file', async () => {
    render()
    const drop = within(await attachments()).getByTestId('attach-drop')
    const data = { files: [photo()], types: ['Files'] }
    drop.dispatchEvent(Object.assign(new Event('drop', { bubbles: true }), { dataTransfer: data }))
    expect(await within(await attachments()).findByRole('img', { name: 'desk.png' })).toBeInTheDocument()
  })

  it('refuses a file over the upload limit before sending it, naming the limit', async () => {
    let posted = false
    server.use(
      http.get('/api/v1/settings', ({ request }) =>
        request.url.endsWith('/settings') ? HttpResponse.json({ media_upload_max_bytes: 10 }) : undefined,
      ),
      http.post('/api/v1/prints/35/attachments', () => {
        posted = true
        return HttpResponse.json({}, { status: 500 })
      }),
    )
    const { user } = render()
    const section = await attachments()
    await user.upload(within(section).getByLabelText('Choose files'), video())

    expect(await within(section).findByRole('alert')).toHaveTextContent('spin.mp4 is larger than the 0 MB upload limit.')
    expect(posted).toBe(false)
  })

  it('captions, promotes to the template’s gallery, and removes behind a confirmation', async () => {
    const { user } = render()
    const section = await attachments()
    await user.upload(within(section).getByLabelText('Choose files'), photo())
    const item = await within(section).findByRole('listitem', { name: 'desk.png' })

    await user.type(within(item).getByLabelText('Caption for desk.png'), 'On the desk')
    await user.click(within(item).getByRole('button', { name: 'Save' }))
    const captioned = await within(section).findByRole('listitem', { name: 'On the desk' })

    await user.click(within(captioned).getByRole('button', { name: 'Add to the template’s gallery' }))
    expect(await within(captioned).findByRole('button', { name: 'Added to the gallery' })).toBeDisabled()
    const done = prints.find((print) => print.archive_id === 35)!
    expect(within(captioned).getByRole('link', { name: 'See the gallery' })).toHaveAttribute('href', `/m/${done.slug}`)

    await user.click(within(captioned).getByRole('button', { name: 'Remove' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Remove On the desk?' })
    expect(confirm).toHaveTextContent('Delete it from ScadBuddy and Bambuddy?')
    await user.click(within(confirm).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(within(section).getByRole('listitem', { name: 'On the desk' })).toBeInTheDocument()

    await user.click(within(captioned).getByRole('button', { name: 'Remove' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }))
    expect(await within(section).findByText('No photos or videos added yet.')).toBeInTheDocument()
  })

  it('keeps one progress row per upload, though a phone names every photo alike', async () => {
    const pending: Array<() => void> = []
    const sent = vi.spyOn(api, 'addPrintAttachment').mockImplementation(
      () => new Promise((resolve) => pending.push(() => resolve(undefined as never))),
    )
    const { user } = render()
    const section = await attachments()
    const same = () => new File([new Uint8Array([0xff, 0xd8, 0xff])], 'image.jpg', { type: 'image/jpeg' })
    await user.upload(within(section).getByLabelText('Choose files'), same())
    await user.upload(within(section).getByLabelText('Take a photo'), same())
    const uploading = await within(section).findByRole('list', { name: 'Uploading' })
    await waitFor(() => expect(within(uploading).getAllByRole('listitem')).toHaveLength(2))
    pending[0]!()
    await waitFor(() => expect(within(uploading).getAllByRole('listitem')).toHaveLength(1))
    pending[1]!()
    await waitFor(() => expect(within(section).queryByRole('list', { name: 'Uploading' })).toBeNull())
    sent.mockRestore()
  })

  it('takes a caption as long as the server does', async () => {
    const { user } = render()
    const section = await attachments()
    await user.upload(within(section).getByLabelText('Choose files'), photo())
    const item = await within(section).findByRole('listitem', { name: 'desk.png' })
    expect(within(item).getByLabelText('Caption for desk.png')).toHaveAttribute('maxlength', '1000')
  })

  it('remembers a copy in the gallery after a reload, so it is not offered twice', async () => {
    const done = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/35', () =>
        HttpResponse.json({
          ...done,
          attachment_count: 1,
          media: {
            ...done.media,
            attachments: [
              {
                id: 7,
                kind: 'photo',
                caption: null,
                url: '/api/v1/prints/35/attachments/7/file',
                filename: 'kept.png',
                size_bytes: 10,
                promoted_to: [done.slug!],
                created_at: null,
              },
            ],
          },
        }),
      ),
    )
    render()
    const item = await within(await attachments()).findByRole('listitem', { name: 'kept.png' })
    expect(within(item).getByRole('button', { name: 'Added to the gallery' })).toBeDisabled()
  })
})
