import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createTagBackup, postTagRead } from '../../../api/filament'
import { spoolId } from '../../../mocks/features/filament/seed'
import { filamentLinks } from '../../../lib/filamentLinks'
import { NfcSession } from '../../../lib/webNfc'
import { installNdefStub, type NdefStub } from '../../../test/ndefStub'
import { renderPage } from '../../../test/utils'
import { KnownTag, TagChipLine } from './KnownTag'

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

describe('Clear tag (#8362)', () => {
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

  it('clears the tag, says so, and hands the new read on when closed', async () => {
    const uid = '04:a1:b2:c3:d4:e5:f6'
    const result = await postTagRead({ uid, records: [] })
    const onWritten = vi.fn()
    const { user } = renderPage(<KnownTag spool={result.spool!} tag={result.tag} session={session} onWritten={onWritten} />)
    await user.click(screen.getByRole('button', { name: 'Clear tag' }))
    const flow = screen.getByRole('region', { name: 'Clear tag' })
    await within(flow).findByText('Hold the tag still. If asked, lift it and tap again.')
    stub.tap({ serialNumber: uid, records: [] })
    await waitFor(() => expect(stub.writes).toHaveLength(1))
    stub.tap({ serialNumber: uid, records: [{ recordType: 'empty', data: new Uint8Array() }] })
    expect(await within(flow).findByText('Cleared and checked.')).toBeInTheDocument()
    expect(within(flow).getByText(/one empty record and is on no spool/)).toBeInTheDocument()
    expect(onWritten).not.toHaveBeenCalled()
    await user.click(within(flow).getByRole('button', { name: 'Done' }))
    expect(onWritten).toHaveBeenCalledWith(uid, [{ record_type: 'empty', media_type: null, id: null, encoding: null, lang: null, data_hex: '' }], expect.objectContaining({ next: 'bind_or_create' }))
  })

  it('offers no Clear for a Bambu tag', async () => {
    const result = await postTagRead({ uid: '04:a1:b2:c3:d4:e5:f6', records: [] })
    renderPage(<KnownTag spool={result.spool!} tag={{ ...result.tag!, kind: 'bambu_mifare' }} session={session} />)
    expect(await screen.findByRole('button', { name: 'Write tag' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Clear tag' })).toBeNull()
  })
})

describe('TagChipLine (#8362)', () => {
  it('names the chip and its bytes, and that OpenSpool needs more than an NTAG213', () => {
    const { rerender } = renderPage(<TagChipLine info={{ chip: 'NTAG215', user_bytes: 504, max_ndef_bytes: 496, writable: true }} />)
    expect(screen.getByLabelText('Tag chip')).toHaveTextContent('NTAG215 · 504 bytes')
    expect(screen.queryByText(/OpenSpool does not support/)).toBeNull()
    rerender(<TagChipLine info={{ chip: 'NTAG213', user_bytes: 144, max_ndef_bytes: 137, writable: true }} />)
    expect(screen.getByText(/OpenSpool does not support an NTAG213/)).toBeInTheDocument()
    rerender(<TagChipLine info={{ max_ndef_bytes: 137, writable: false }} />)
    expect(screen.getByLabelText('Tag chip')).toHaveTextContent('137 bytes, read-only')
  })

  it('shows nothing in a browser, which reads no chip', () => {
    const { container } = renderPage(<TagChipLine />)
    expect(container).toBeEmptyDOMElement()
  })
})
