import { screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { Dialog } from '../components/ui/Dialog'
import { Button } from '../components/ui/Button'
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
      <label htmlFor="key">API key</label>
      <input id="key" type="password" defaultValue="" />
      <Button onClick={() => setOpen(true)}>Send to Bambuddy</Button>
      <p role="alert">Name is too long</p>
      <Dialog open={open} title="Send to Bambuddy" onClose={() => setOpen(false)}>
        <label htmlFor="copies">Copies</label>
        <input id="copies" type="number" defaultValue={1} />
        <Button onClick={onSend} {...USER_ONLY}>
          Send
        </Button>
      </Dialog>
    </>
  )
}

function renderShell(onSend = vi.fn()) {
  renderPage(
    <Shell>
      <Routes>
        <Route path="/" element={<Form onSend={onSend} />} />
        <Route path="/settings" element={<h1>Settings</h1>} />
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

  it('refuses a value a select does not offer, and never fills a password', async () => {
    renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('fill'))

    const bad = await bridge.call('fill', { label: 'Unit', value: 'cubits' })
    expect(!bad.ok && bad.error.code).toBe('invalid_args')

    const key = await bridge.call('fill', { label: 'API key', value: 'secret' })
    expect(!key.ok && key.error.code).toBe('refused')
    expect(screen.getByLabelText('API key')).toHaveValue('')
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

  it('navigates in-app only, and reports where it landed', async () => {
    renderShell()
    await waitFor(() => expect(bridge.liveNames()).toContain('navigate'))

    expect(await bridge.call('navigate', { route: '/settings' })).toEqual({
      ok: true,
      result: { route: '/settings' },
    })
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument()

    const away = await bridge.call('navigate', { route: '//evil.example/' })
    expect(!away.ok && away.error.code).toBe('invalid_args')
    const scheme = await bridge.call('navigate', { route: 'https://evil.example/' })
    expect(!scheme.ok && scheme.error.code).toBe('invalid_args')
  })

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
        { label: 'Unit', role: 'combobox', value: 'mm' },
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
