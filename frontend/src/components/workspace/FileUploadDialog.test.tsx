import { screen, waitFor } from '@testing-library/react'
import { renderPage } from '../../test/utils'
import { FileUploadDialog } from './FileUploadDialog'

// #4703: one file from disk, or from a session's workspace (the picker's own tests cover that).

function render(onFile: (file: File) => Promise<void>) {
  const onClose = vi.fn()
  const view = renderPage(
    <FileUploadDialog
      open
      title="Upload into MakerWorld"
      description="The file keeps its name in the library."
      onClose={onClose}
      accept=".3mf"
      hint="3MF"
      onFile={onFile}
      workspace={{ resource: null, label: 'this folder' }}
    />,
    { userEventOptions: { applyAccept: false } },
  )
  return { ...view, onClose }
}

describe('FileUploadDialog', () => {
  it('takes a file from disk, then closes', async () => {
    const onFile = vi.fn(() => Promise.resolve())
    const { user, onClose } = render(onFile)
    const file = new File(['3mf'], 'part.3mf')
    await user.upload(screen.getByLabelText('Upload a file'), file)
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(onFile).toHaveBeenCalledWith(file)
    // Without the assistant there is no session to pick from.
    expect(screen.queryByRole('button', { name: 'From a session…' })).toBeNull()
  })

  it('shows what the target refused, and stays open', async () => {
    const { user, onClose } = render(() => Promise.reject(new Error('a mesh model opens a .3mf or an .stl')))
    await user.upload(screen.getByLabelText('Upload a file'), new File(['x'], 'part.obj'))
    expect(await screen.findByRole('alert')).toHaveTextContent('a mesh model opens a .3mf or an .stl')
    expect(onClose).not.toHaveBeenCalled()
  })
})
