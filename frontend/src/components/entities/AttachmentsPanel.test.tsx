import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { entityMock, seedEntityAttachment } from '../../mocks/features/entities'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { AttachmentsPanel } from './AttachmentsPanel'

const SPOOL = 'spool:5d1c0000-0000-4000-8000-000000000001'
const PNG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
const photo = (name = 'receipt.png') => new File([new Uint8Array([...PNG, 0, 0, 0, 0])], name, { type: 'image/png' })

describe('AttachmentsPanel (#6950)', () => {
  beforeEach(() => resetMockState())

  it("lists the entity's files, a photo by its thumbnail", async () => {
    const shot = seedEntityAttachment(SPOOL, { filename: 'shelf.png', kind: 'image', media_type: 'image/png', role: 'photo', caption: 'On the shelf' })
    seedEntityAttachment(SPOOL, { filename: 'tds.pdf', kind: 'document', media_type: 'application/pdf', role: 'manual' })
    renderPage(<AttachmentsPanel entity={SPOOL} />)

    const list = await screen.findByRole('list', { name: 'Files' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    const card = within(list).getByRole('listitem', { name: 'On the shelf' })
    expect(within(card).getByRole('img', { name: 'On the shelf' })).toHaveAttribute(
      'src',
      `/api/v1/entities/${encodeURIComponent(SPOOL)}/attachments/${shot.id}/thumbnail`,
    )
    expect(within(card).getByText('Photo')).toBeInTheDocument()
    const pdf = within(list).getByRole('listitem', { name: 'tds.pdf' })
    expect(within(pdf).getByText('Manual')).toBeInTheDocument()
  })

  it('uploads a file with its role and caption', async () => {
    const { user } = renderPage(<AttachmentsPanel entity={SPOOL} />)
    expect(await screen.findByText('No files yet.')).toBeInTheDocument()

    await choose(user, screen.getByRole('combobox', { name: 'Add files as' }), 'Receipt')
    await user.type(screen.getByLabelText('Caption for new files'), 'Bought in March')
    await user.upload(screen.getByLabelText('Attach files'), photo())

    const card = await screen.findByRole('listitem', { name: 'Bought in March' })
    expect(within(card).getByText('Receipt')).toBeInTheDocument()
    expect(entityMock.uploads).toEqual([{ entity: SPOOL, filename: 'receipt.png', role: 'receipt', caption: 'Bought in March' }])
  })

  it('changes a caption and a role', async () => {
    seedEntityAttachment(SPOOL, { filename: 'label.png', role: 'photo' })
    const { user } = renderPage(<AttachmentsPanel entity={SPOOL} />)
    const card = await screen.findByRole('listitem', { name: 'label.png' })

    await user.click(within(card).getByRole('button', { name: 'Edit label.png' }))
    await choose(user, within(card).getByRole('combobox', { name: 'Role of label.png' }), 'Label')
    await user.type(within(card).getByLabelText('Caption for label.png'), 'Its label')
    await user.click(within(card).getByRole('button', { name: 'Save' }))

    const saved = await screen.findByRole('listitem', { name: 'Its label' })
    expect(within(saved).getByText('Label')).toBeInTheDocument()
    expect(entityMock.attachments.get(SPOOL)?.[0]).toMatchObject({ role: 'label', caption: 'Its label' })
  })

  it('removes a file only after the confirm', async () => {
    seedEntityAttachment(SPOOL, { filename: 'old.png' })
    const { user } = renderPage(<AttachmentsPanel entity={SPOOL} />)
    const card = await screen.findByRole('listitem', { name: 'old.png' })

    await user.click(within(card).getByRole('button', { name: 'Remove old.png' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Remove old.png?' })
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(entityMock.attachments.get(SPOOL)).toHaveLength(1)

    await user.click(within(card).getByRole('button', { name: 'Remove old.png' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Remove' }))
    expect(await screen.findByText('No files yet.')).toBeInTheDocument()
    expect(entityMock.attachments.get(SPOOL)).toHaveLength(0)
  })

  it("shows a print's Bambuddy files as names with a link to the print, read only", async () => {
    seedEntityAttachment('print_archive:42', { filename: 'plate_1.jpg', caption: 'First layer', source: 'bambuddy', sha256: null })
    renderPage(<AttachmentsPanel entity="print_archive:42" />)

    const card = await screen.findByRole('listitem', { name: 'First layer' })
    expect(within(card).queryByRole('img')).not.toBeInTheDocument()
    expect(within(card).getByRole('link', { name: 'Open the print' })).toHaveAttribute('href', '/prints/42')
    expect(screen.queryByLabelText('Attach files')).not.toBeInTheDocument()
    expect(within(card).queryByRole('button', { name: /Remove/ })).not.toBeInTheDocument()
  })

  it("sends an order's files to the order page", async () => {
    renderPage(<AttachmentsPanel entity="order:o-1" />)
    const link = await screen.findByRole('link', { name: 'Add files on the order page' })
    expect(link).toHaveAttribute('href', '/orders/o-1')
    expect(screen.queryByLabelText('Attach files')).not.toBeInTheDocument()
  })

  it("says the server's refusal of an upload", async () => {
    entityMock.refuseNextUpload = 'the file is larger than the upload limit'
    const { user } = renderPage(<AttachmentsPanel entity={SPOOL} />)
    await screen.findByText('No files yet.')
    await user.upload(screen.getByLabelText('Attach files'), photo())
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('receipt.png: the file is larger than the upload limit'))
  })

  it('keeps the caption when every upload failed, to send again', async () => {
    entityMock.refuseNextUpload = 'the store is moving'
    const { user } = renderPage(<AttachmentsPanel entity={SPOOL} />)
    await screen.findByText('No files yet.')
    await user.type(screen.getByLabelText('Caption for new files'), 'Bought in March')
    await user.upload(screen.getByLabelText('Attach files'), photo())
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('receipt.png: the store is moving'))
    expect(screen.getByLabelText('Caption for new files')).toHaveValue('Bought in March')
  })

  it("shows a file Bambuddy keeps as a print's only on a print archive", async () => {
    seedEntityAttachment(SPOOL, { filename: 'odd.png', source: 'bambuddy', sha256: null })
    renderPage(<AttachmentsPanel entity={SPOOL} />)
    const card = await screen.findByRole('listitem', { name: 'odd.png' })
    expect(within(card).queryByRole('link', { name: 'Open the print' })).not.toBeInTheDocument()
  })
})
