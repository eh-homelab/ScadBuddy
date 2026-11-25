import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createTagBackup, postTagRead } from '../../../api/filament'
import { spoolId } from '../../../mocks/features/filament/seed'
import { filamentLinks } from '../../../lib/filamentLinks'
import { NfcSession } from '../../../lib/webNfc'
import { installNdefStub, type NdefStub } from '../../../test/ndefStub'
import { renderPage } from '../../../test/utils'
import { KnownTag } from './KnownTag'

describe('KnownTag (#7273)', () => {
  it('shows the spool card, the photos, the weight, then the links in that order', async () => {
    const result = await postTagRead({ uid: '04:a1:b2:c3:d4:e5:f6', records: [] })
    renderPage(<KnownTag spool={result.spool!} />)
    const card = screen.getByRole('region', { name: 'Spool #3' })
    expect(within(card).getByText(/^PLA/)).toBeInTheDocument()
    expect(within(card).getByText('930 g left')).toBeInTheDocument()
    const filament = screen.getByRole('group', { name: 'Filament label' })
    const weight = await screen.findByLabelText('Gross weight (g)')
    const openSpool = screen.getByRole('link', { name: 'Open spool' })
    expect(openSpool).toHaveAttribute('href', filamentLinks.spool(spoolId(3)))
    // Without a scan session there is no NFC to write with.
    expect(screen.getByRole('button', { name: 'Write tag' })).toBeDisabled()
    const order = [card, filament, weight, openSpool]
    for (let at = 1; at < order.length; at += 1) {
      expect(order[at - 1]!.compareDocumentPosition(order[at]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
  })
})

describe('KnownTag writes (#7273)', () => {
  let stub: NdefStub
  let session: NfcSession
  beforeEach(async () => {
    stub = installNdefStub()
    session = new NfcSession()
    await session.start()
  })
  afterEach(() => {
    session.stop()
    stub.restore()
  })

  it('runs one write at a time: a write disables Restore, a restore disables Write tag', async () => {
    const uid = '04:a1:b2:c3:d4:e5:f6'
    await createTagBackup({ uid, records: [], reason: 'spool_write' })
    const result = await postTagRead({ uid, records: [] })
    const { user } = renderPage(<KnownTag spool={result.spool!} tag={result.tag} session={session} />)
    await user.click(await screen.findByRole('button', { name: 'Backups (1)' }))
    const restore = screen.getByRole('button', { name: 'Restore' })
    expect(restore).toBeEnabled()
    await user.click(screen.getByRole('button', { name: 'Write tag' }))
    await waitFor(() => expect(restore).toBeDisabled())
    await user.click(within(screen.getByRole('region', { name: 'Write tag' })).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Restore' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Write tag' })).toBeDisabled())
  })
})
