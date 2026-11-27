import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { getLocation, type LocationDetail } from '../../api/filament'
import { NfcSession } from '../../lib/webNfc'
import { L } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { installNdefStub, type NdefStub } from '../../test/ndefStub'
import { renderPage } from '../../test/utils'
import { WritePlaceTag } from './WritePlaceTag'

const FREE = '04:77:66:55:44:33:22'

describe('WritePlaceTag (#7168 R6)', () => {
  let stub: NdefStub
  let session: NfcSession
  let place: LocationDetail
  let readOnly: MockInstance
  beforeEach(async () => {
    stub = installNdefStub()
    readOnly = vi.spyOn((window as unknown as { NDEFReader: { prototype: { makeReadOnly: () => Promise<void> } } }).NDEFReader.prototype, 'makeReadOnly')
    session = new NfcSession()
    await session.start()
    place = await getLocation(L.SHELF_A)
  })
  afterEach(() => {
    expect(readOnly).toHaveBeenCalledTimes(0)
    session.stop()
    stub.restore()
  })

  const stepNamed = (name: string) => within(screen.getByRole('list', { name: 'Steps' })).getByText(name).closest('li')!

  it('writes the label URL of the place on the label base from Settings, and checks it', async () => {
    server.use(http.get('*/api/v1/settings', () => HttpResponse.json({ filament_label_base_url: 'https://labels.example', public_url: null })))
    renderPage(<WritePlaceTag session={session} place={place} />)
    const url = `https://labels.example/x/${place.code}`
    expect(await screen.findByText(url)).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Place tag' })).toBeInTheDocument()
    await screen.findByText('Hold the tag still. If asked, lift it and tap again.')
    stub.tap({ serialNumber: FREE, records: [] })
    await waitFor(() => expect(stepNamed('Read back')).toHaveAttribute('aria-current', 'step'))
    expect(stub.writes.at(-1)).toEqual([{ recordType: 'url', data: url }])
    stub.tap({ serialNumber: FREE, records: [{ recordType: 'url', data: new TextEncoder().encode(url) }] })
    expect(await screen.findByText('Written and checked.')).toBeInTheDocument()
  })

  it('writes nothing while the label base cannot be read', async () => {
    server.use(http.get('*/api/v1/settings', () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
    renderPage(<WritePlaceTag session={session} place={place} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('The label address in Settings could not be read, so nothing was written.')
    expect(screen.queryByRole('list', { name: 'Steps' })).not.toBeInTheDocument()
    expect(stub.writes).toHaveLength(0)
  })
})
