import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { postTagRead, type TagReadResult } from '../../../api/filament'
import { filamentLinks } from '../../../lib/filamentLinks'
import { server } from '../../../mocks/server'
import { renderPage } from '../../../test/utils'
import { NewTag } from './NewTag'

const hex = (text: string) => Array.from(new TextEncoder().encode(text), (byte) => byte.toString(16).padStart(2, '0')).join('')
const OPENSPOOL = [{ record_type: 'mime', media_type: 'application/json', data_hex: hex('{"protocol":"openspool","version":"1.0","type":"PLA","color_hex":"FFAABB","min_temp":"220","max_temp":"240"}') }]

describe('NewTag (#7273)', () => {
  it('shows only the decoded fields that are set, and creates the spool from the draft', async () => {
    const uid = '04:01:02:03:04:05:06'
    const result = await postTagRead({ uid, records: OPENSPOOL })
    expect(result.next).toBe('bind_or_create')
    const bound = vi.fn<(next: TagReadResult) => void>()
    const { user } = renderPage(<NewTag uid={uid} records={OPENSPOOL} result={result} onBound={bound} />)
    const decoded = screen.getByRole('region', { name: 'On the tag' })
    expect(within(decoded).getByText('OpenSpool 1.0')).toBeInTheDocument()
    expect(within(decoded).queryByText('Brand')).toBeNull()
    expect(within(decoded).getByText('220–240 °C')).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Filament label' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Create a spool from this tag' }))
    await waitFor(() => expect(bound).toHaveBeenCalledOnce())
    expect(bound.mock.calls[0]![0].next).toBe('open_spool')
  })

  it('binds the spool it made when the bind failed, and makes no second spool', async () => {
    const uid = '04:01:02:03:04:05:08'
    const result = await postTagRead({ uid, records: OPENSPOOL })
    let creates = 0
    const count = ({ request }: { request: Request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname === '/api/v1/filament/spools') creates += 1
    }
    server.events.on('request:start', count)
    let refuse = true
    server.use(
      http.post('*/api/v1/filament/tags/reads', () => {
        if (!refuse) return undefined
        refuse = false
        return HttpResponse.json({ type: 'about:blank', title: 'Internal Server Error', status: 500, detail: 'the database did not answer' }, { status: 500 })
      }),
    )
    try {
      const bound = vi.fn<(next: TagReadResult) => void>()
      const { user } = renderPage(<NewTag uid={uid} records={OPENSPOOL} result={result} onBound={bound} />)
      await user.click(screen.getByRole('button', { name: 'Create a spool from this tag' }))
      expect(await screen.findByRole('alert')).toHaveTextContent(/^Spool #\d+ was made, but the tag was not bound to it/)
      expect(screen.queryByRole('button', { name: 'Create a spool from this tag' })).toBeNull()
      await user.click(screen.getByRole('button', { name: /^Bind to spool #\d+$/ }))
      await waitFor(() => expect(bound).toHaveBeenCalledOnce())
      expect(bound.mock.calls[0]![0].next).toBe('open_spool')
      expect(creates).toBe(1)
    } finally {
      server.events.removeListener('request:start', count)
    }
  })

  it('links to a hand-made spool when the tag names no known material', async () => {
    const uid = '04:01:02:03:04:05:07'
    const result = await postTagRead({ uid, records: [] })
    renderPage(<NewTag uid={uid} records={[]} result={result} onBound={() => {}} />)
    expect(screen.getByRole('link', { name: 'Make the spool by hand' })).toHaveAttribute('href', filamentLinks.newSpool)
    expect(screen.queryByRole('button', { name: 'Create a spool from this tag' })).toBeNull()
    expect(screen.getByRole('searchbox', { name: 'Bind to a spool' })).toBeInTheDocument()
  })
})
