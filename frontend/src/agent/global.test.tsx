import { screen, waitFor } from '@testing-library/react'
import { useEffect, useState } from 'react'
import { Route, Routes, useLocation, useNavigate } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { Button } from '../components/ui/button'
import { Dialog, DialogContent, DialogTitle } from '../components/ui/dialog'
import { registerSections } from '../pages/settings/areas'
import { resolveLocation, SECTION_REGISTRY } from '../pages/settings/registry'
import { renderPage } from '../test/utils'
import { bridge } from './bridge'
import { USER_ONLY } from './dom'
import { useGlobalAgentTools } from './global'
import { TOUCH_CLASS } from './highlight'

function Shell({ children }: { children?: React.ReactNode }) {
  useGlobalAgentTools()
  return <>{children}</>
}

function Form({ onSend }: { onSend: () => void }) {
  const [name, setName] = useState('Reagan')
  const [unit, setUnit] = useState('mm')
  const [open, setOpen] = useState(false)
  return (
    <>
      <label htmlFor="name">Name on the tag</label>
      <input id="name" value={name} onChange={(event) => setName(event.target.value)} />
      <p data-testid="echo">{name}</p>
      <label htmlFor="unit">Unit</label>
      <select id="unit" value={unit} onChange={(event) => setUnit(event.target.value)}>
        <option value="mm">Millimetres</option>
        <option value="in">Inches</option>
      </select>
      <p data-testid="unit">{unit}</p>
      <label htmlFor="preset">Preset</label>
      <select id="preset" defaultValue="">
        <option value="">None</option>
        <optgroup label="Shipped">
          <option value="a1b2">Bubbly keyring</option>
        </optgroup>
        <optgroup label="Saved">
          <option value="c3d4">Bubbly keyring</option>
        </optgroup>
      </select>
      <label htmlFor="key">API key</label>
      <input id="key" type="password" defaultValue="" />
      <Button onClick={() => setOpen(true)}>Send to Bambuddy</Button>
      <p role="alert">Name is too long</p>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogTitle>Send to Bambuddy</DialogTitle>
          <label htmlFor="copies">Copies</label>
          <input id="copies" type="number" defaultValue={1} />
          <Button onClick={onSend} {...USER_ONLY}>
            Send
          </Button>
        </DialogContent>
      </Dialog>
    </>
  )
}

/** Settings' redirect, as SettingsShell makes it (resolveLocation). */
function FakeSettings() {
  const { pathname, hash } = useLocation()
  const navigate = useNavigate()
  const target = resolveLocation(pathname, hash)
  const address = `/settings/${target.area}${target.hash}`
  useEffect(() => {
    if (`${pathname}${hash}` !== address) void navigate(address, { replace: true })
  }, [pathname, hash, address, navigate])
  return <h1>Settings</h1>
}

function renderShell(onSend = vi.fn()) {
  renderPage(
    <Shell>
      <Routes>
        <Route path="/" element={<Form onSend={onSend} />} />
        <Route path="/settings/*" element={<FakeSettings />} />
      </Routes>
    </Shell>,
  )
  return onSend
}

describe('global tools', () => {
  it('fills a controlled field through React, so its onChange runs and the change highlights', async () => {
    renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('fill'))

    const outcome = await bridge.call('fill', { label: 'Name on the tag', value: 'Nova' })
    expect(outcome).toEqual({ ok: true, result: { filled: 'Name on the tag', value: 'Nova' } })
    expect(screen.getByTestId('echo')).toHaveTextContent('Nova')
    expect(screen.getByLabelText('Name on the tag')).toHaveClass(TOUCH_CLASS)

    expect(await bridge.call('fill', { label: 'Unit', value: 'in' })).toMatchObject({ ok: true })
    expect(screen.getByTestId('unit')).toHaveTextContent('in')
  })

  it("fills a select by an option's visible label, since that is what the agent sees", async () => {
    renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('fill'))

    expect(await bridge.call('fill', { label: 'Unit', value: 'Inches' })).toEqual({
      ok: true,
      result: { filled: 'Unit', value: 'in' },
    })
    expect(screen.getByTestId('unit')).toHaveTextContent('in')
  })

  it('refuses a value a select does not offer or labels twice, and never fills a password', async () => {
    renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('fill'))

    const bad = await bridge.call('fill', { label: 'Unit', value: 'cubits' })
    expect(!bad.ok && bad.error.code).toBe('invalid_args')
    expect(!bad.ok && bad.error.message).toContain('"Millimetres" ("mm"), "Inches" ("in")')

    // Two options with one label: refuse rather than pick either, and list their values.
    const ambiguous = await bridge.call('fill', { label: 'Preset', value: 'Bubbly keyring' })
    expect(!ambiguous.ok && ambiguous.error.code).toBe('invalid_args')
    expect(!ambiguous.ok && ambiguous.error.message).toContain('2 options of "Preset" are labelled "Bubbly keyring"')
    expect(!ambiguous.ok && ambiguous.error.message).toContain('"Bubbly keyring" ("a1b2"), "Bubbly keyring" ("c3d4")')
    expect(screen.getByLabelText('Preset')).toHaveValue('')

    const key = await bridge.call('fill', { label: 'API key', value: 'secret' })
    expect(!key.ok && key.error.code).toBe('refused')
    expect(screen.getByLabelText('API key')).toHaveValue('')
  })

  it('adds no confirm note when a field in an already open dialog rejects the value', async () => {
    renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('fill'))
    await bridge.call('click', { role: 'button', name: 'send to bambuddy' })
    await screen.findByRole('dialog', { name: 'Send to Bambuddy' })

    // A number input sanitises "two" to "", so the value differs from what was asked.
    const outcome = await bridge.call('fill', { label: 'Copies', value: 'two' })
    expect(outcome).toEqual({ ok: true, result: { filled: 'Copies', value: '' } })
  })

  it('clicks by role and name, but never a confirmation only the user may press', async () => {
    const onSend = renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('click'))

    expect(await bridge.call('click', { role: 'button', name: 'send to bambuddy' })).toMatchObject({ ok: true })
    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    expect(dialog).toBeInTheDocument()

    const confirm = await bridge.call('click', { role: 'button', name: 'Send' })
    expect(!confirm.ok && confirm.error.code).toBe('refused')
    expect(onSend).not.toHaveBeenCalled()

    const missing = await bridge.call('click', { role: 'link', name: 'Nowhere' })
    expect(!missing.ok && missing.error.code).toBe('invalid_args')
  })

  it('highlights by role and name and changes nothing, a user-only confirmation included (#1919)', async () => {
    const onSend = renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('highlight'))

    const pointed = await bridge.call('highlight', { role: 'button', name: 'send to bambuddy' })
    expect(pointed).toEqual({ ok: true, result: { highlighted: { role: 'button', name: 'Send to Bambuddy' } } })
    expect(screen.getByRole('button', { name: 'Send to Bambuddy' })).toHaveClass(TOUCH_CLASS)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    await bridge.call('click', { role: 'button', name: 'send to bambuddy' })
    await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    expect(await bridge.call('highlight', { role: 'button', name: 'Send' })).toMatchObject({ ok: true })
    expect(screen.getByRole('button', { name: 'Send' })).toHaveClass(TOUCH_CLASS)
    expect(onSend).not.toHaveBeenCalled()

    const missing = await bridge.call('highlight', { role: 'link', name: 'Nowhere' })
    expect(!missing.ok && missing.error.code).toBe('invalid_args')
    expect(!missing.ok && missing.error.message).toBe('No visible link named "Nowhere". Take a snapshot to see what is on screen.')
  })

  it('navigates in-app only, and reports where it landed', async () => {
    renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('navigate'))

    expect(await bridge.call('navigate', { route: '/settings' })).toEqual({
      ok: true,
      result: { route: '/settings/general' },
    })
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument()

    const away = await bridge.call('navigate', { route: '//evil.example/' })
    expect(!away.ok && away.error.code).toBe('invalid_args')
    const scheme = await bridge.call('navigate', { route: 'https://evil.example/' })
    expect(!scheme.ok && scheme.error.code).toBe('invalid_args')
  })

  it('answers a hash-only move at once, with the hash (#781)', async () => {
    renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('navigate'))
    await bridge.call('navigate', { route: '/settings/general' })

    vi.useFakeTimers()
    try {
      // No timer is advanced: an answer that waited would still be pending.
      const answer = await bridge.call('navigate', { route: '/settings/general#connection' })
      expect(answer).toEqual({ ok: true, result: { route: '/settings/general#connection' } })
    } finally {
      vi.useRealTimers()
    }
  })

  it('answers a same-area hash move as soon as the sections load, not after the full wait (#5023)', async () => {
    renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('navigate'))
    await bridge.call('navigate', { route: '/settings/general' })

    registerSections([])
    vi.useFakeTimers()
    try {
      let answer: unknown
      const pending = bridge.call('navigate', { route: '/settings/general#connection' }).then((value) => (answer = value))
      await vi.advanceTimersByTimeAsync(100)
      expect(answer).toBeUndefined()
      registerSections(SECTION_REGISTRY)
      await vi.advanceTimersByTimeAsync(50)
      expect(answer).toEqual({ ok: true, result: { route: '/settings/general#connection' } })
      await pending
    } finally {
      vi.useRealTimers()
      registerSections(SECTION_REGISTRY)
    }
  })

  it('answers a hash move outside Settings at once while the sections are unloaded', async () => {
    renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('navigate'))
    await bridge.call('navigate', { route: '/' })

    registerSections([])
    vi.useFakeTimers()
    try {
      // No timer is advanced: an answer that waited for the Settings sections would still be
      // pending after the flush, and the test says so at once instead of hanging.
      let answer: unknown
      const pending = bridge.call('navigate', { route: '/#top' }).then((value) => (answer = value))
      await vi.advanceTimersByTimeAsync(0)
      expect(answer, 'navigate waited for the Settings sections').toEqual({ ok: true, result: { route: '/#top' } })
      await pending
    } finally {
      vi.useRealTimers()
      registerSections(SECTION_REGISTRY)
    }
  })

  it.each(['/settings/library', '/settings'])(
    'waits for Settings to redirect a section of another area, from %s (#781)',
    async (start) => {
      renderShell()
      await waitFor(() => expect(bridge.liveNames()).toContain('navigate'))
      await bridge.call('navigate', { route: start })
      const answer = await bridge.call('navigate', { route: `${start}#connection` })
      expect(answer).toEqual({ ok: true, result: { route: '/settings/general#connection' } })
    },
  )

  it('snapshots the route, dialogs, fields, errors and interactive elements', async () => {
    renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('snapshot'))

    await bridge.call('fill', { label: 'Name on the tag', value: 'Nova' })
    const page = await bridge.call('snapshot', {})
    expect(page.ok).toBe(true)
    if (!page.ok) return
    const snapshot = page.result as ReturnType<typeof bridge.snapshot>
    expect(snapshot.route).toBe('/')
    expect(snapshot.dialogs).toEqual([])
    expect(snapshot.fields).toEqual(
      expect.arrayContaining([
        { label: 'Name on the tag', role: 'textbox', value: 'Nova' },
        {
          label: 'Unit',
          role: 'combobox',
          value: 'mm',
          options: [
            { label: 'Millimetres', value: 'mm' },
            { label: 'Inches', value: 'in' },
          ],
        },
        // A password is never read back.
        { label: 'API key', role: 'textbox', value: '' },
      ]),
    )
    expect(snapshot.errors).toEqual(['Name is too long'])
    expect(snapshot.elements).toContainEqual({ role: 'button', name: 'Send to Bambuddy' })
    expect(snapshot.tools).toEqual(expect.arrayContaining(['navigate', 'snapshot', 'click', 'fill']))

    // With a dialog up, the snapshot is the dialog: that is all a user can reach.
    await bridge.call('click', { role: 'button', name: 'Send to Bambuddy' })
    await screen.findByRole('dialog')
    const inDialog = bridge.snapshot()
    expect(inDialog.dialogs).toEqual(['Send to Bambuddy'])
    expect(inDialog.fields).toEqual([{ label: 'Copies', role: 'spinbutton', value: '1' }])
    expect(inDialog.elements).toContainEqual({ role: 'button', name: 'Send', userOnly: true })
  })
})
