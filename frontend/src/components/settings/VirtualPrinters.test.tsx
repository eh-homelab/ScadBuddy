import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import { setVirtualAuth, virtualState, virtualWrites } from '../../mocks/features/printers'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { PrintersPanel } from './Printers'

// #4747 item 4: Bambuddy's virtual printers in Settings > Printers.

const PANEL = 'Bambuddy virtual printers'
const H2C_VP = 'Virtual printer H2C Virtual'

describe('VirtualPrintersPanel (#4747)', () => {
  it('shows only with a Bambuddy', async () => {
    renderPage(<PrintersPanel />)
    await screen.findByRole('listitem', { name: 'Printer Garage' })
    expect(screen.queryByRole('region', { name: PANEL })).toBeNull()
  })

  it("lists Bambuddy's virtual printers with their target and Bambuddy's auth", async () => {
    renderPage(<PrintersPanel bambuddyConfigured />)
    const vp = await screen.findByRole('listitem', { name: H2C_VP })
    expect(vp).toHaveTextContent('→ 3DP-31B-598 (queue)')
    expect(vp).toHaveTextContent('Enabled')
    expect(screen.getByLabelText('Bambuddy auth')).toHaveTextContent('Bambuddy auth: off')
  })

  it("runs Bambuddy's checks", async () => {
    const { user } = renderPage(<PrintersPanel bambuddyConfigured />)
    const vp = await screen.findByRole('listitem', { name: H2C_VP })
    await user.click(within(vp).getByRole('button', { name: 'Check' }))
    const checks = await within(vp).findByRole('status', { name: 'Checks for H2C Virtual' })
    expect(checks).toHaveTextContent("Bambuddy's checks: warnings")
    expect(checks).toHaveTextContent('port mqtt: pass')
  })

  it('creates an untargeted one with a write-only access code', async () => {
    const { user } = renderPage(<PrintersPanel bambuddyConfigured />)
    await screen.findByRole('listitem', { name: H2C_VP })
    const add = screen.getByRole('button', { name: 'Add virtual printer' })
    expect(isUserOnly(add)).toBe(true)
    await user.click(add)

    const form = screen.getByRole('form', { name: 'New virtual printer' })
    expect(isUserOnly(form)).toBe(true)
    await user.type(within(form).getByLabelText('Name'), 'Any H2C')
    const code = within(form).getByLabelText('Access code (8 characters)')
    expect(code).toHaveAttribute('type', 'password')
    expect(code).toHaveAttribute('maxLength', '8')
    await user.type(code, 'abcd1234')
    await user.click(within(form).getByRole('button', { name: 'Create in Bambuddy' }))

    await screen.findByRole('listitem', { name: 'Virtual printer Any H2C' })
    expect(virtualWrites()).toEqual([
      expect.objectContaining({ name: 'Any H2C', mode: 'queue', target: null, access_code: 'abcd1234' }),
    ])
    expect(screen.queryByRole('form', { name: 'New virtual printer' })).toBeNull()
  })

  it('takes no code for a targeted one: it uses its target’s', async () => {
    const { user } = renderPage(<PrintersPanel bambuddyConfigured />)
    const vp = await screen.findByRole('listitem', { name: H2C_VP })
    await user.click(within(vp).getByRole('button', { name: 'Edit' }))
    const form = within(vp).getByRole('form', { name: 'Edit H2C Virtual' })
    expect(within(form).queryByLabelText(/access code/i)).toBeNull()
    await choose(user, within(form).getByRole('combobox', { name: 'Mode' }), 'Proxy')
    await user.click(within(form).getByRole('button', { name: 'Save in Bambuddy' }))
    await waitFor(() => expect(vp).toHaveTextContent('(proxy)'))
    expect(virtualWrites()[0]).toEqual({ mode: 'proxy' })
  })

  it('keeps a target the registry has not imported when an edit leaves it alone (review #4784)', async () => {
    virtualState()[0] = { ...virtualState()[0]!, target: null, target_printer_id: 7 }
    const { user } = renderPage(<PrintersPanel bambuddyConfigured />)
    const vp = await screen.findByRole('listitem', { name: H2C_VP })
    expect(vp).toHaveTextContent('→ Bambuddy printer 7 (not in the registry) (queue)')
    await user.click(within(vp).getByRole('button', { name: 'Edit' }))
    const form = within(vp).getByRole('form', { name: 'Edit H2C Virtual' })
    expect(within(form).getByRole('combobox', { name: 'Sends to' })).toHaveTextContent('Bambuddy printer 7')
    expect(within(form).queryByLabelText(/access code/i)).toBeNull()
    const name = within(form).getByLabelText('Name')
    await user.clear(name)
    await user.type(name, 'Renamed')
    await user.click(within(form).getByRole('button', { name: 'Save in Bambuddy' }))
    await waitFor(() => expect(virtualWrites()).toHaveLength(1))
    expect(virtualWrites()[0]).toEqual({ name: 'Renamed' })
  })

  it('clears the shown checks once an edit is saved (review #4784)', async () => {
    const { user } = renderPage(<PrintersPanel bambuddyConfigured />)
    const vp = await screen.findByRole('listitem', { name: H2C_VP })
    await user.click(within(vp).getByRole('button', { name: 'Check' }))
    await within(vp).findByRole('status', { name: 'Checks for H2C Virtual' })
    await user.click(within(vp).getByRole('button', { name: 'Edit' }))
    const form = within(vp).getByRole('form', { name: 'Edit H2C Virtual' })
    await user.click(within(form).getByRole('switch', { name: 'Enabled' }))
    await user.click(within(form).getByRole('button', { name: 'Save in Bambuddy' }))
    await waitFor(() => expect(within(vp).queryByRole('status', { name: 'Checks for H2C Virtual' })).toBeNull())
    expect(virtualWrites()[0]).toEqual({ enabled: false })
  })

  it("says why a write is refused while Bambuddy's auth is on", async () => {
    setVirtualAuth(true)
    const { user } = renderPage(<PrintersPanel bambuddyConfigured />)
    const vp = await screen.findByRole('listitem', { name: H2C_VP })
    expect(screen.getByLabelText('Bambuddy auth')).toHaveTextContent('Bambuddy auth: on')
    expect(screen.getByText(/no API key can change a virtual printer/)).toBeInTheDocument()
    await user.click(within(vp).getByRole('button', { name: 'Delete H2C Virtual' }))
    await user.click(within(vp).getByRole('button', { name: 'Confirm deleting H2C Virtual' }))
    expect(await within(vp).findByRole('alert')).toHaveTextContent("while Bambuddy's auth is on")
  })

  it('deletes after a confirmation', async () => {
    const { user } = renderPage(<PrintersPanel bambuddyConfigured />)
    const vp = await screen.findByRole('listitem', { name: H2C_VP })
    await user.click(within(vp).getByRole('button', { name: 'Delete H2C Virtual' }))
    await user.click(within(vp).getByRole('button', { name: 'Confirm deleting H2C Virtual' }))
    await waitFor(() => expect(screen.queryByRole('listitem', { name: H2C_VP })).toBeNull())
    expect(screen.getByText('No virtual printers.')).toBeInTheDocument()
  })
})
