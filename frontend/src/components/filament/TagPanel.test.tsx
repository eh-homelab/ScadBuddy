import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { getSpool, unbindTag } from '../../api/filament'
import { spoolId, TAG } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { filamentLinks } from '../../lib/filamentLinks'
import { renderPage } from '../../test/utils'
import { TagPanel } from './TagPanel'

async function render(number: number, locked?: boolean) {
  const onChanged = vi.fn()
  const loaded = await getSpool(spoolId(number))
  const spool = locked === undefined ? loaded : { ...loaded, weight_locked: locked }
  const view = renderPage(<TagPanel spool={spool} onChanged={onChanged} />)
  return { ...view, onChanged }
}

describe('TagPanel', () => {
  it('links to the phone page to read or write the tag', async () => {
    await render(4)
    expect(screen.getByRole('link', { name: 'Read or write with a phone' })).toHaveAttribute('href', filamentLinks.scan)
  })

  it('shows the open tag and takes it off', async () => {
    const { user, onChanged } = await render(4)
    expect(screen.getByText(/A1B2C3D4/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Take off' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect((await getSpool(spoolId(4))).tag).toBeNull()
  })

  it('says whose spool holds the last tag and moves it here', async () => {
    const { user, onChanged } = await render(19)
    await user.click(screen.getByRole('button', { name: 'Tag is on spool #3 — Move it here' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect((await getSpool(spoolId(19))).tag?.id).toBe(TAG.MOVED)
    expect((await getSpool(spoolId(3))).tag).toBeNull()
  })

  it('binds the last tag again when no spool holds it', async () => {
    await unbindTag(TAG.BLACK)
    const { user, onChanged } = await render(4)
    expect(screen.queryByText(/Move it here/)).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Bind tag again' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect((await getSpool(spoolId(4))).tag?.id).toBe(TAG.BLACK)
  })

  it('binds a tag by its UID', async () => {
    const { user, onChanged } = await render(2)
    await user.type(screen.getByLabelText('Tag UID'), '04 AA BB CC')
    await user.click(screen.getByRole('button', { name: 'Bind tag' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect((await getSpool(spoolId(2))).tag?.tag_uid).toBe('04AABBCC')
  })

  it('test_move_it_here_is_not_an_override', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('*/api/v1/filament/spools/:id/tags', async ({ request }) => {
        bodies.push(await request.clone().json())
        return HttpResponse.json({}, { status: 201 })
      }),
    )
    const { user, onChanged } = await render(19)
    await user.click(screen.getByRole('button', { name: 'Tag is on spool #3 — Move it here' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(bodies[0]).toMatchObject({ override: false })
  })

  it('test_the_override_checkbox_needs_a_tray_uuid', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('*/api/v1/filament/spools/:id/tags', async ({ request }) => {
        bodies.push(await request.clone().json())
        return HttpResponse.json({}, { status: 201 })
      }),
    )
    const { user, onChanged } = await render(2)
    const box = screen.getByRole('checkbox', { name: 'This Bambu tag belongs to a different filament (override)' })
    await user.type(screen.getByLabelText('Tag UID'), '04AABBCC')
    expect(box).toBeDisabled()
    await user.type(screen.getByLabelText('Tray UUID'), '0123456789ABCDEF0123456789ABCDEF')
    expect(box).toBeEnabled()
    await user.click(box)
    await user.click(screen.getByRole('button', { name: 'Bind tag' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(bodies[0]).toMatchObject({ override: true, kind: 'bambu_mifare' })
  })

  it('test_scanning_fills_the_form', async () => {
    server.use(
      http.post('*/api/v1/filament/rfid/scan', () =>
        HttpResponse.json({ status: 'tag', tag_uid: '04A1B2C3', tray_uuid: null, kind: 'ntag_opentag', sb_spool: { id: spoolId(5), number: 5 } }),
      ),
    )
    const { user } = await render(2)
    await user.click(screen.getByRole('button', { name: 'Scan a tag' }))
    await waitFor(() => expect(screen.getByLabelText('Tag UID')).toHaveValue('04A1B2C3'))
    expect(screen.getByLabelText('Tray UUID')).toHaveValue('')
    expect(screen.getByRole('combobox', { name: 'Tag kind' })).toHaveTextContent('OpenTag (NTAG)')
    expect(screen.getByRole('link', { name: 'Spool #5' }).parentElement).toHaveTextContent('Spool #5 already holds this tag')
  })

  it('waits for the reader and sends the 30 second window', async () => {
    let sent: unknown
    server.use(
      http.post('*/api/v1/filament/rfid/scan', async ({ request }) => {
        sent = await request.json()
        await new Promise((resolve) => setTimeout(resolve, 150))
        return HttpResponse.json({ status: 'timeout' })
      }),
    )
    const { user } = await render(2)
    await user.click(screen.getByRole('button', { name: 'Scan a tag' }))
    expect(await screen.findByText('Hold the tag to the reader')).toBeInTheDocument()
    await screen.findByText('No tag seen')
    expect(sent).toMatchObject({ timeout_s: 30 })
  })

  it('test_a_scan_timeout_says_so', async () => {
    server.use(http.post('*/api/v1/filament/rfid/scan', () => HttpResponse.json({ status: 'timeout' })))
    const { user } = await render(2)
    await user.click(screen.getByRole('button', { name: 'Scan a tag' }))
    expect(await screen.findByText('No tag seen')).toBeInTheDocument()
    expect(screen.getByLabelText('Tag UID')).toHaveValue('')
  })

  it('says a reader that cannot be reached apart from no tag seen', async () => {
    server.use(
      http.post('*/api/v1/filament/rfid/scan', () =>
        HttpResponse.json({ title: 'Bad Gateway', status: 502, detail: 'Bambuddy refused' }, { status: 502, headers: { 'Content-Type': 'application/problem+json' } }),
      ),
    )
    const { user } = await render(2)
    await user.click(screen.getByRole('button', { name: 'Scan a tag' }))
    expect(await screen.findByText('Could not reach the reader through Bambuddy')).toBeInTheDocument()
    expect(screen.queryByText('No tag seen')).toBeNull()
  })

  const LOCK = 'Weight locked: the AMS percentage never changes grams left'

  it('marks an open override binding, and says the weight is locked only when the spool is', async () => {
    await render(3, false)
    expect(await screen.findByText('Override')).toBeInTheDocument()
    expect(screen.queryByText(LOCK)).toBeNull()
  })

  it('says the weight is locked from the spool, and keeps saying it after Take off', async () => {
    const { user, onChanged } = await render(4, true)
    expect(screen.getByText(LOCK)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Take off' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(screen.getByText(LOCK)).toBeInTheDocument()
  })

  it('drops the override mark when the same tag is bound again without override', async () => {
    const { user } = await render(3, false)
    expect(await screen.findByText('Override')).toBeInTheDocument()
    await user.type(screen.getByLabelText('Tag UID'), '04A1B2C3D4E5F6')
    await user.click(screen.getByRole('button', { name: 'Bind tag' }))
    await waitFor(() => expect(screen.queryByText('Override')).toBeNull())
  })

  it('does not say the spool you are on already holds the tag', async () => {
    server.use(
      http.post('*/api/v1/filament/rfid/scan', () =>
        HttpResponse.json({ status: 'tag', tag_uid: '04A1B2C3', tray_uuid: null, kind: 'ntag_opentag', sb_spool: { id: spoolId(2), number: 2 } }),
      ),
    )
    const { user } = await render(2)
    await user.click(screen.getByRole('button', { name: 'Scan a tag' }))
    await waitFor(() => expect(screen.getByLabelText('Tag UID')).toHaveValue('04A1B2C3'))
    expect(screen.queryByText(/already holds this tag/)).toBeNull()
  })

  it('shows no override mark on a plain binding', async () => {
    await render(4, false)
    await screen.findByText(/A1B2C3D4/)
    expect(screen.queryByText('Override')).toBeNull()
    expect(screen.queryByText(LOCK)).toBeNull()
  })

  it('never sends override with a kind other than bambu_mifare', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('*/api/v1/filament/spools/:id/tags', async ({ request }) => {
        bodies.push(await request.clone().json())
        return HttpResponse.json({}, { status: 201 })
      }),
    )
    const { user, onChanged } = await render(2)
    await user.type(screen.getByLabelText('Tray UUID'), '0123456789ABCDEF0123456789ABCDEF')
    const box = screen.getByRole('checkbox', { name: 'This Bambu tag belongs to a different filament (override)' })
    await user.click(box)
    expect(box).toBeChecked()
    await user.click(screen.getByRole('combobox', { name: 'Tag kind' }))
    await user.click(await screen.findByRole('option', { name: 'OpenTag (NTAG)' }))
    expect(box).not.toBeChecked()
    expect(box).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Bind tag' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(bodies[0]).toMatchObject({ override: false, kind: 'ntag_opentag' })
  })

  it('a bind with override locks the weight of the spool', async () => {
    const { user, onChanged } = await render(2)
    await user.type(screen.getByLabelText('Tray UUID'), '0123456789ABCDEF0123456789ABCDEF')
    await user.click(screen.getByRole('checkbox', { name: 'This Bambu tag belongs to a different filament (override)' }))
    await user.click(screen.getByRole('button', { name: 'Bind tag' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect((await getSpool(spoolId(2))).weight_locked).toBe(true)
  })

  it('refuses a bind with no key', async () => {
    const { user, onChanged } = await render(2)
    await user.click(screen.getByRole('button', { name: 'Bind tag' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/tag UID or a tray UUID/)
    expect(onChanged).not.toHaveBeenCalled()
  })
})
