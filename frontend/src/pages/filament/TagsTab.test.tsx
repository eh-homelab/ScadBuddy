import { screen, waitFor, within } from '@testing-library/react'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { spoolId, TAG } from '../../mocks/features/filament/seed'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { TagsTab } from './TagsTab'

const table = () => screen.findByRole('table', { name: 'Tags' })
const rowOf = async (tag: string) => {
  const found = within(await table()).getAllByRole('row').find((row) => row.dataset.tag === tag)
  if (!found) throw new Error(`no row for tag ${tag}`)
  return found
}

describe('the Tags tab', () => {
  it('lists a rebound tag on its new spool, with its kind, since and override flag', async () => {
    renderPage(<TagsTab />)
    await table()
    const moved = await rowOf(TAG.MOVED)
    expect(within(moved).getByRole('link', { name: '#3' })).toHaveAttribute('href', `/filament/spools/${spoolId(3)}`)
    expect(within(moved).getByText('OpenSpool (NTAG)')).toBeInTheDocument()
    expect(within(moved).getByText('Yes')).toBeInTheDocument()
    expect(within(await rowOf(TAG.BLACK)).getByText('No')).toBeInTheDocument()
  })

  it('opens a tag history: where the tag has been, newest first', async () => {
    const { user } = renderPage(<TagsTab />)
    await table()
    await user.click(within(await rowOf(TAG.MOVED)).getByRole('button', { name: /History/ }))
    const history = await screen.findByRole('region', { name: 'Tag history' })
    const items = await within(history).findAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(within(items[0]!).getByRole('link', { name: '#3' })).toBeInTheDocument()
    expect(within(items[1]!).getByRole('link', { name: '#19' })).toBeInTheDocument()
    expect(items[1]).toHaveTextContent('spool empty')
  })

  it('shows a tag on no spool only as unbound, and filters by kind', async () => {
    const { user } = renderPage(<TagsTab />)
    await table()
    expect(within(await rowOf(TAG.LOOSE)).getByText('Not on a spool')).toBeInTheDocument()
    await choose(user, screen.getByRole('combobox', { name: 'Show' }), 'On a spool')
    expect(within(await table()).queryByText('Not on a spool')).toBeNull()
    await choose(user, screen.getByRole('combobox', { name: 'Kind' }), 'Bambu Lab (RFID)')
    const rows = within(await table()).getAllByRole('row').filter((row) => row.dataset.tag)
    expect(rows.map((row) => row.dataset.tag)).toEqual([TAG.BLACK])
  })

  it('still lists the tags when the spool numbers cannot be read, linking the spool by its number from the tag', async () => {
    server.use(http.get('/api/v1/filament/spools', () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
    renderPage(<TagsTab />)
    await table()
    expect(within(await rowOf(TAG.MOVED)).getByRole('link', { name: '#3' })).toHaveAttribute('href', `/filament/spools/${spoolId(3)}`)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('moves focus to the history when it opens', async () => {
    const { user } = renderPage(<TagsTab />)
    await table()
    await user.click(within(await rowOf(TAG.MOVED)).getByRole('button', { name: /History/ }))
    const history = await screen.findByRole('region', { name: 'Tag history' })
    await waitFor(() => expect(history).toHaveFocus())
  })

  it('shows the history of the tag clicked last, even when an earlier answer comes later', async () => {
    server.use(
      http.get(`/api/v1/filament/tags/${TAG.MOVED}`, async () => {
        await delay(300)
        return HttpResponse.json({ id: TAG.MOVED, kind: 'ntag_openspool', tag_uid: 'SLOW', tray_uuid: null, raw_blob: null, note: null, created_at: '2026-11-01T10:00:00Z', binding: null, spool_number: null, history: [] })
      }),
    )
    const { user } = renderPage(<TagsTab />)
    await table()
    await user.click(within(await rowOf(TAG.MOVED)).getByRole('button', { name: /History/ }))
    await user.click(within(await rowOf(TAG.BLACK)).getByRole('button', { name: /History/ }))
    expect(await screen.findByRole('heading', { name: /History of A1B2C3D4/ })).toBeInTheDocument()
    await delay(500)
    expect(screen.queryByRole('heading', { name: /SLOW/ })).toBeNull()
    expect(screen.getByRole('heading', { name: /History of A1B2C3D4/ })).toBeInTheDocument()
  })

  it('does not reopen a history that answers after Close', async () => {
    server.use(
      http.get(`/api/v1/filament/tags/${TAG.MOVED}`, async () => {
        await delay(200)
        return HttpResponse.json({ id: TAG.MOVED, kind: 'ntag_openspool', tag_uid: 'SLOW', tray_uuid: null, raw_blob: null, note: null, created_at: '2026-11-01T10:00:00Z', binding: null, spool_number: null, history: [] })
      }),
    )
    const { user } = renderPage(<TagsTab />)
    await table()
    await user.click(within(await rowOf(TAG.BLACK)).getByRole('button', { name: /History/ }))
    await screen.findByRole('region', { name: 'Tag history' })
    await user.click(within(await rowOf(TAG.MOVED)).getByRole('button', { name: /History/ }))
    await user.click(screen.getByRole('button', { name: 'Close' }))
    await delay(400)
    expect(screen.queryByRole('region', { name: 'Tag history' })).toBeNull()
  })

  it('says why the tags could not be read', async () => {
    server.use(http.get('/api/v1/filament/tags', () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
    renderPage(<TagsTab />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
  })
})
