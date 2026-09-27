import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from '../api/client'
import { models } from '../mocks/fixtures'
import { renderPage } from '../test/utils'
import { UploadDialog } from './UploadDialog'

// As in CataloguePage.test: the multipart POST itself is exercised by the Playwright
// run, because jsdom's File and Node's fetch cannot agree on a multipart body. These
// pin what the dialog hands `uploadModel`.
const uploaded = models[0] as (typeof models)[number]

function file(name: string, body = 'x') {
  return new File([body], name)
}

function inFolder(folder: string, name: string, body = 'x'): File {
  const made = file(name, body)
  Object.defineProperty(made, 'webkitRelativePath', { value: `${folder}/${name}` })
  return made
}

function render() {
  const onUploaded = vi.fn()
  const view = renderPage(<UploadDialog open onClose={() => {}} onUploaded={onUploaded} />, {
    userEventOptions: { applyAccept: false },
  })
  return { ...view, onUploaded, dialog: screen.getByRole('dialog', { name: 'Add a model' }) }
}

afterEach(() => vi.restoreAllMocks())

describe('UploadDialog', () => {
  it('uploads a whole model folder, named after the folder as the seed names it', async () => {
    const upload = vi.spyOn(api, 'uploadModel').mockResolvedValue(uploaded)
    const { user, dialog, onUploaded } = render()
    const scad = inFolder('name-keychain', 'model.scad', 'cube(10);')
    const meta = inFolder('name-keychain', 'model.json', '{"name": "Name Keychain"}')
    const thumbnail = inFolder('name-keychain', 'thumbnail.png')
    const readme = inFolder('name-keychain', 'README.md', '# Name Keychain')

    await user.upload(within(dialog).getByLabelText('Model folder'), [
      scad,
      meta,
      thumbnail,
      readme,
      inFolder('name-keychain', 'verify.sh'),
    ])

    expect(await within(dialog).findByText('name-keychain/model.scad')).toBeInTheDocument()
    expect(within(dialog).getByTestId('upload-meta')).toHaveTextContent('Name Keychain')
    expect(within(dialog).getByTestId('upload-thumbnail')).toHaveTextContent('thumbnail.png')
    expect(within(dialog).getByTestId('upload-readme')).toHaveTextContent('README.md')
    expect(within(dialog).getByTestId('upload-ignored')).toHaveTextContent('verify.sh')

    await user.click(within(dialog).getByRole('button', { name: 'Add model' }))

    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith(uploaded))
    expect(upload).toHaveBeenCalledWith(scad, {
      filename: 'name-keychain.scad',
      meta,
      thumbnail,
      readme,
    })
  })

  it('names a bare model.scad dropped beside its model.json after the model', async () => {
    const upload = vi.spyOn(api, 'uploadModel').mockResolvedValue(uploaded)
    const { dialog, user } = render()
    const scad = file('model.scad')
    const meta = file('model.json', '{"name": "Widget Deluxe"}')

    fireEvent.drop(within(dialog).getByTestId('upload-dropzone'), {
      dataTransfer: { files: [scad, meta], items: [] },
    })
    expect(await within(dialog).findByText(/Widget Deluxe/)).toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Add model' }))

    await waitFor(() => expect(upload).toHaveBeenCalledOnce())
    expect(upload.mock.calls[0]?.[1]).toMatchObject({ filename: 'Widget Deluxe.scad', meta })
  })

  it('attaches a thumbnail and README to a single .scad', async () => {
    const upload = vi.spyOn(api, 'uploadModel').mockResolvedValue(uploaded)
    const { dialog, user } = render()
    const scad = file('Vase Mode.scad')
    const thumbnail = file('cover.png')
    const readme = file('notes.md')

    await user.upload(within(dialog).getByLabelText('OpenSCAD source file'), scad)
    await user.upload(within(dialog).getByLabelText('Thumbnail (PNG)'), thumbnail)
    await user.upload(within(dialog).getByLabelText('README (Markdown)'), readme)
    expect(within(dialog).getByTestId('upload-thumbnail')).toHaveTextContent('cover.png')

    await user.click(within(dialog).getByRole('button', { name: 'Add model' }))

    await waitFor(() => expect(upload).toHaveBeenCalledOnce())
    expect(upload).toHaveBeenCalledWith(scad, {
      filename: 'Vase Mode.scad',
      meta: undefined,
      thumbnail,
      readme,
    })
  })

  it('refuses a thumbnail that is not a PNG, and one can be removed again', async () => {
    const upload = vi.spyOn(api, 'uploadModel').mockResolvedValue(uploaded)
    const { dialog, user } = render()
    const scad = file('widget.scad')

    await user.upload(within(dialog).getByLabelText('OpenSCAD source file'), [
      scad,
      file('thumbnail.png'),
    ])
    await user.upload(within(dialog).getByLabelText('Thumbnail (PNG)'), file('cover.jpg'))
    expect(within(dialog).getByRole('alert')).toHaveTextContent('must be a PNG')
    expect(within(dialog).getByTestId('upload-thumbnail')).toHaveTextContent('thumbnail.png')

    await user.click(within(dialog).getByRole('button', { name: 'Remove thumbnail' }))
    expect(within(dialog).getByTestId('upload-thumbnail')).toHaveTextContent('None')

    await user.click(within(dialog).getByRole('button', { name: 'Add model' }))
    await waitFor(() => expect(upload).toHaveBeenCalledOnce())
    expect(upload.mock.calls[0]?.[1]?.thumbnail).toBeUndefined()
  })

  it('says which image and README it took when a folder has several', async () => {
    const { dialog, user } = render()

    await user.upload(within(dialog).getByLabelText('Model folder'), [
      inFolder('widget', 'model.scad'),
      inFolder('widget', 'side.png'),
      inFolder('widget', 'front.png'),
      inFolder('widget', 'NOTES.md'),
      inFolder('widget', 'build.md'),
    ])

    expect(await within(dialog).findByTestId('upload-thumbnail')).toHaveTextContent('front.png')
    expect(within(dialog).getByTestId('upload-readme')).toHaveTextContent('NOTES.md')
    expect(within(dialog).getByTestId('upload-ignored')).toHaveTextContent(
      'Not uploaded: build.md (README is NOTES.md), side.png (thumbnail is front.png)',
    )
  })

  it('refuses a thumbnail over 2 MiB, attached or in a folder', async () => {
    const upload = vi.spyOn(api, 'uploadModel').mockResolvedValue(uploaded)
    const { dialog, user } = render()
    const big = () => new File([new Uint8Array(2 * 1024 * 1024 + 1)], 'thumbnail.png')

    await user.upload(within(dialog).getByLabelText('OpenSCAD source file'), file('widget.scad'))
    await user.upload(within(dialog).getByLabelText('Thumbnail (PNG)'), big())
    expect(within(dialog).getByRole('alert')).toHaveTextContent('2 MiB or smaller')
    expect(within(dialog).getByTestId('upload-thumbnail')).toHaveTextContent('None')

    await user.upload(within(dialog).getByLabelText('Model folder'), [
      inFolder('widget', 'model.scad'),
      Object.defineProperty(big(), 'webkitRelativePath', { value: 'widget/thumbnail.png' }),
    ])
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'The thumbnail must be 2 MiB or smaller. thumbnail.png was left out.',
    )
    expect(within(dialog).getByTestId('upload-thumbnail')).toHaveTextContent('None')

    await user.click(within(dialog).getByRole('button', { name: 'Add model' }))
    await waitFor(() => expect(upload).toHaveBeenCalledOnce())
    expect(upload.mock.calls[0]?.[1]?.thumbnail).toBeUndefined()
  })

  it('leaves out a model.json over 64 KiB, chosen or in a folder', async () => {
    const upload = vi.spyOn(api, 'uploadModel').mockResolvedValue(uploaded)
    const { dialog, user } = render()
    /** A readable model.json of exactly `size` bytes; JSON allows trailing spaces. */
    const metaOf = (size: number, name: string) => {
      const body = JSON.stringify({ name })
      return new File([body + ' '.repeat(size - body.length)], 'model.json')
    }

    await user.upload(within(dialog).getByLabelText('OpenSCAD source file'), [
      file('widget.scad'),
      metaOf(64 * 1024 + 1, 'Too Big'),
    ])
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'The model.json must be 64 KiB or smaller. model.json was left out.',
    )
    expect(within(dialog).getByTestId('upload-meta')).toHaveTextContent('Named after the file')

    await user.upload(within(dialog).getByLabelText('Model folder'), [
      inFolder('widget', 'model.scad'),
      Object.defineProperty(metaOf(64 * 1024 + 1, 'Too Big'), 'webkitRelativePath', {
        value: 'widget/model.json',
      }),
    ])
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'The model.json must be 64 KiB or smaller. model.json was left out.',
    )
    expect(within(dialog).getByTestId('upload-meta')).toHaveTextContent('Named after the file')

    await user.click(within(dialog).getByRole('button', { name: 'Add model' }))
    await waitFor(() => expect(upload).toHaveBeenCalledOnce())
    expect(upload.mock.calls[0]?.[1]?.meta).toBeUndefined()
  })

  it('keeps a model.json of exactly 64 KiB', async () => {
    const { dialog, user } = render()
    const body = JSON.stringify({ name: 'At The Cap' })
    const atCap = new File([body + ' '.repeat(64 * 1024 - body.length)], 'model.json')

    await user.upload(within(dialog).getByLabelText('OpenSCAD source file'), [
      file('widget.scad'),
      atCap,
    ])

    expect(await within(dialog).findByTestId('upload-meta')).toHaveTextContent('At The Cap')
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('refuses a README over the server limit, attached or in a folder', async () => {
    const upload = vi.spyOn(api, 'uploadModel').mockResolvedValue(uploaded)
    const { dialog, user } = render()
    const long = 'x'.repeat(1_000_001)

    await user.upload(within(dialog).getByLabelText('OpenSCAD source file'), file('widget.scad'))
    await user.upload(within(dialog).getByLabelText('README (Markdown)'), file('notes.md', long))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('at most 1,000,000 characters')
    expect(within(dialog).getByTestId('upload-readme')).toHaveTextContent('None')

    await user.upload(within(dialog).getByLabelText('Model folder'), [
      inFolder('widget', 'model.scad'),
      inFolder('widget', 'README.md', long),
    ])
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'The README must be at most 1,000,000 characters. README.md was left out.',
    )
    expect(within(dialog).getByTestId('upload-readme')).toHaveTextContent('None')

    await user.click(within(dialog).getByRole('button', { name: 'Add model' }))
    await waitFor(() => expect(upload).toHaveBeenCalledOnce())
    expect(upload.mock.calls[0]?.[1]?.readme).toBeUndefined()
  })

  it('shows the server refusing a folder whose model.json is not JSON', async () => {
    // Spied, as the other uploads here are: the multipart body cannot cross from
    // jsdom into Node's fetch. The mock's own 422 is pinned in handlers.test, and
    // the whole path in the browser by e2e/model-details.spec.
    vi.spyOn(api, 'uploadModel').mockRejectedValue(
      new ApiError({
        type: 'about:blank',
        title: 'Unprocessable Content',
        status: 422,
        detail: 'the model.json is not valid JSON',
      }),
    )
    const { dialog, user } = render()

    await user.upload(within(dialog).getByLabelText('Model folder'), [
      inFolder('widget', 'model.scad'),
      inFolder('widget', 'model.json', '{not json'),
    ])
    await user.click(await within(dialog).findByRole('button', { name: 'Add model' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'the model.json is not valid JSON',
    )
    expect(screen.getByRole('dialog', { name: 'Add a model' })).toBeInTheDocument()
  })

  it('says so when a folder holds no source', async () => {
    const { dialog, user } = render()

    await user.upload(within(dialog).getByLabelText('Model folder'), [
      inFolder('empty', 'README.md'),
      inFolder('empty', 'thumbnail.png'),
    ])

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('no .scad file')
    expect(within(dialog).getByRole('button', { name: 'Add model' })).toBeDisabled()
  })
})
