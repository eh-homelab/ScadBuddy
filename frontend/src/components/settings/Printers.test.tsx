import { readFileSync } from 'node:fs'
import { act, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import { takeSnapshot } from '../../agent/snapshot'
import { PROBE_FAILED, UNREACHABLE, printersState } from '../../mocks/features/printers'
import { emitRealtime } from '../../mocks/realtime'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { SIGNAL_READ_MIN_MS } from '../../lib/useAsync'
import { PrintersPanel } from './Printers'

/** `LanLink.access_code`'s `maxLength` in the spec `pnpm gen:api` (run before `pnpm test`)
 * exported: the backend's bound, read rather than restated (#4497 review). */
function specAccessCodeMax(): number {
  const path = process.env.SCADBUDDY_OPENAPI_JSON ?? `${import.meta.dirname}/../../../../backend/openapi.json`
  let text: string
  try {
    text = readFileSync(path, 'utf8')
  } catch (cause) {
    throw new Error(`no API spec at ${path}: run \`pnpm gen:api\` first (pnpm test does)`, { cause })
  }
  const spec = JSON.parse(text) as {
    components?: { schemas?: { LanLink?: { properties?: { access_code?: { maxLength?: unknown } } } } }
  }
  const max = spec.components?.schemas?.LanLink?.properties?.access_code?.maxLength
  if (typeof max !== 'number') throw new Error(`LanLink.access_code.maxLength not in the spec at ${path}`)
  return max
}

const H2C = 'Printer 3DP-31B-598'
const GARAGE = 'Printer Garage'

describe('PrintersPanel (#3823)', () => {
  it('lists printers with their links and path', async () => {
    renderPage(<PrintersPanel />)

    const h2c = await screen.findByRole('listitem', { name: H2C })
    expect(h2c).toHaveTextContent('01P00A000000001')
    expect(h2c).toHaveTextContent('H2C')
    expect(h2c).toHaveTextContent('Bambuddy printer 1')
    expect(h2c).toHaveTextContent('No LAN link')
    expect(within(h2c).getByRole('combobox', { name: 'How 3DP-31B-598 prints' })).toHaveTextContent('Through Bambuddy')

    const garage = screen.getByRole('listitem', { name: GARAGE })
    expect(garage).toHaveTextContent('Not linked to Bambuddy')
    expect(within(garage).getByRole('combobox', { name: 'How Garage prints' })).toHaveTextContent('Automatic')
  })

  it("reads again on a registry change, not on a printer's LAN report (#4340 review I3)", async () => {
    const reads: string[] = []
    let open = 0
    const ours = (request: Request) => request.method === 'GET' && new URL(request.url).pathname.startsWith('/api/v1/printers')
    server.events.on('request:start', ({ request }) => {
      if (!ours(request)) return
      open += 1
      reads.push(new URL(request.url).pathname)
    })
    server.events.on('request:end', ({ request }) => {
      if (ours(request)) open -= 1
    })
    /** No read in flight, and none started for a while: a trailing throttled read has run. */
    const settled = async () => {
      for (let quiet = 0; quiet < 3; ) {
        const before = reads.length
        await act(() => new Promise((resolve) => setTimeout(resolve, SIGNAL_READ_MIN_MS * 2)))
        quiet = open === 0 && reads.length === before ? quiet + 1 : 0
      }
    }
    renderPage(<PrintersPanel />)
    await screen.findByRole('listitem', { name: GARAGE })

    // A change elsewhere: the list follows it (and so is subscribed).
    printersState()[1]!.name = 'Shed'
    await waitFor(() => {
      emitRealtime('printer.changed', ['printers', `printer:${printersState()[1]!.id}`], {})
      expect(screen.getByRole('listitem', { name: 'Printer Shed' })).toBeInTheDocument()
    })
    await waitFor(() => expect(reads.filter((path) => path === '/api/v1/printers/discover').length).toBeGreaterThan(1))
    // The changes above may each have queued a read behind the throttle: let them all run.
    await settled()

    reads.length = 0
    for (let i = 0; i < 5; i += 1) {
      act(() => emitRealtime('printer.report', ['printers', `printer:${printersState()[0]!.id}`], { progress: i }))
    }
    await settled()
    expect(reads).toEqual([])
  })

  it("sets a printer's path", async () => {
    const { user } = renderPage(<PrintersPanel />)
    const garage = await screen.findByRole('listitem', { name: GARAGE })

    await choose(user, within(garage).getByRole('combobox', { name: 'How Garage prints' }), 'Over the LAN')

    await waitFor(() => expect(printersState()[1]!.preferred_path).toBe('native'))
  })

  it('links a discovered Bambuddy printer and disables one without a serial', async () => {
    const { user } = renderPage(<PrintersPanel />)
    const garage = await screen.findByRole('listitem', { name: GARAGE })

    const spare = await within(garage).findByRole('button', { name: 'Link Bambuddy printer Spare' })
    expect(spare).toBeDisabled()
    expect(garage).toHaveTextContent('Bambuddy did not report a serial')
    // The one already linked to another printer is not offered.
    expect(within(garage).queryByRole('button', { name: 'Link Bambuddy printer 3DP-31B-598' })).toBeNull()

    await user.click(within(garage).getByRole('button', { name: 'Link Bambuddy printer 3DP-77A-114' }))

    await waitFor(() => expect(garage).toHaveTextContent('Bambuddy printer 2'))
    expect(printersState()[1]!.bambuddy_printer_id).toBe(2)

    await user.click(within(garage).getByRole('button', { name: 'Unlink Bambuddy from Garage' }))
    await waitFor(() => expect(garage).toHaveTextContent('Not linked to Bambuddy'))
  })

  it('links a LAN printer and never shows the code again', async () => {
    const { user } = renderPage(<PrintersPanel />)
    const garage = await screen.findByRole('listitem', { name: GARAGE })
    const form = within(garage).getByRole('form', { name: 'LAN link for Garage' })
    expect(isUserOnly(form)).toBe(true)

    await user.type(within(form).getByLabelText('Host or IP address'), '10.0.0.9')
    await user.type(within(form).getByLabelText('Access code'), '12345678')
    expect(within(form).getByLabelText('Access code')).toHaveAttribute('type', 'password')
    // Never saved or autofilled into another printer's form (#4340 review M2).
    expect(within(form).getByLabelText('Access code')).toHaveAttribute('autocomplete', 'new-password')
    await user.click(within(form).getByRole('button', { name: 'Test and link' }))

    await waitFor(() => expect(garage).toHaveTextContent('LAN 10.0.0.9'))
    expect(document.body.textContent).not.toContain('12345678')
    for (const input of document.querySelectorAll('input')) expect(input.value).not.toBe('12345678')
    expect(printersState()[1]!.has_access_code).toBe(true)

    const unlink = within(garage).getByRole('button', { name: 'Unlink the LAN from Garage' })
    expect(isUserOnly(unlink)).toBe(true)
    await user.click(unlink)
    await waitFor(() => expect(garage).toHaveTextContent('No LAN link'))
  })

  it('takes an access code in any format, as typed (#4434)', async () => {
    const { user } = renderPage(<PrintersPanel />)
    const garage = await screen.findByRole('listitem', { name: GARAGE })
    const form = within(garage).getByRole('form', { name: 'LAN link for Garage' })
    const code = within(form).getByLabelText('Access code')
    expect(code).not.toHaveAttribute('pattern')
    // LanLink's bound only (#4483), never a format.
    expect(code).toHaveAttribute('maxlength', String(specAccessCodeMax()))
    expect(code).not.toHaveAttribute('inputmode')

    await user.type(within(form).getByLabelText('Host or IP address'), '10.0.0.9')
    await user.type(code, 'aB3-xY9z12')
    await user.click(within(form).getByRole('button', { name: 'Test and link' }))

    await waitFor(() => expect(garage).toHaveTextContent('LAN 10.0.0.9'))
    expect(printersState()[1]!.has_access_code).toBe(true)
  })

  it('shows the probe failure from a 502', async () => {
    const { user } = renderPage(<PrintersPanel />)
    const garage = await screen.findByRole('listitem', { name: GARAGE })
    const form = within(garage).getByRole('form', { name: 'LAN link for Garage' })

    await user.type(within(form).getByLabelText('Host or IP address'), UNREACHABLE)
    await user.type(within(form).getByLabelText('Access code'), '12345678')
    await user.click(within(form).getByRole('button', { name: 'Test and link' }))

    expect(await within(form).findByRole('alert')).toHaveTextContent(PROBE_FAILED)
    expect(printersState()[1]!.has_access_code).toBe(false)
  })

  it('adds a printer by its serial and forgets one', async () => {
    const { user } = renderPage(<PrintersPanel />)
    await screen.findByRole('listitem', { name: GARAGE })
    const form = screen.getByRole('form', { name: 'Add a printer' })

    await user.type(within(form).getByLabelText('Serial'), '01P00A0000000BB')
    await user.type(within(form).getByLabelText('Name'), 'Attic')
    await user.click(within(form).getByRole('button', { name: 'Add printer' }))

    const attic = await screen.findByRole('listitem', { name: 'Printer Attic' })
    await user.click(within(attic).getByRole('button', { name: 'Forget Attic' }))
    await waitFor(() => expect(screen.queryByRole('listitem', { name: 'Printer Attic' })).toBeNull())
  })

  it("says why a Bambuddy-linked printer can't be forgotten", async () => {
    const { user } = renderPage(<PrintersPanel />)
    const h2c = await screen.findByRole('listitem', { name: H2C })

    await user.click(within(h2c).getByRole('button', { name: 'Forget 3DP-31B-598' }))

    expect(await within(h2c).findByRole('alert')).toHaveTextContent('unlink it first')
  })

  it("shows Bambuddy's view of a linked printer, marking a difference (#4747)", async () => {
    printersState()[0]!.bambuddy_view!.name = 'Renamed in Bambuddy'
    renderPage(<PrintersPanel />)
    const h2c = await screen.findByRole('listitem', { name: H2C })
    const view = within(h2c).getByLabelText("Bambuddy's view of 3DP-31B-598")
    expect(view).toHaveTextContent('name Renamed in Bambuddy (ScadBuddy: 3DP-31B-598)')
    expect(view).toHaveTextContent('model H2C')
    expect(view).toHaveTextContent('IP 10.0.0.10')
    expect(view).toHaveTextContent('active')
    expect(view).toHaveTextContent('2 nozzles')
    expect(view.querySelectorAll('[data-differs]')).toHaveLength(1)
  })

  it('imports from Bambuddy, shows the summary, and lists a virtual printer as one (#4439, #4747)', async () => {
    const { user } = renderPage(<PrintersPanel bambuddyConfigured />)
    await screen.findByRole('listitem', { name: GARAGE })
    const button = screen.getByRole('button', { name: 'Import from Bambuddy' })
    expect(isUserOnly(button)).toBe(true)

    await user.click(button)

    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('1 added, 1 linked, 1 already linked, 1 skipped, 0 conflicts')
    expect(status).toHaveTextContent('Spare: skipped (Bambuddy did not report a serial')
    expect(status).toHaveTextContent('withholds the access code')
    const virtual = await screen.findByRole('listitem', { name: 'Printer H2C Virtual' })
    expect(virtual).toHaveTextContent('Bambuddy virtual printer → 3DP-31B-598 (queue)')
    expect(virtual).toHaveTextContent('Not a printer to print on')
    // Not a print target: no path, no LAN form, nothing to link.
    expect(within(virtual).queryByRole('combobox')).toBeNull()
    expect(within(virtual).queryByRole('form')).toBeNull()
    const garage = screen.getByRole('listitem', { name: GARAGE })
    await waitFor(() => expect(garage).toHaveTextContent('Bambuddy printer 2'))
    expect(within(garage).getByLabelText('Bambuddy sync note for Garage')).toHaveTextContent('withholds the access code')
  })

  it('says why Import from Bambuddy is off without a Bambuddy (#4439)', async () => {
    renderPage(<PrintersPanel />)
    await screen.findByRole('listitem', { name: GARAGE })
    expect(screen.getByRole('button', { name: 'Import from Bambuddy' })).toBeDisabled()
    expect(screen.getByText(/Set up the Bambuddy connection first/)).toBeInTheDocument()
  })

  it('names a virtual printer with no target as any printer of its model (#4747)', async () => {
    printersState().push({
      ...printersState()[1]!,
      id: '00000000-0000-4000-8000-0000000000f2',
      serial: '09400A391800002',
      name: 'Any H2C',
      kind: 'bambuddy_virtual',
      bambuddy_vp_id: 2,
      vp_mode: 'archive',
      forwards_to: null,
      bambuddy_view: { seen_at: '2026-10-26T00:00:00Z', name: 'Any H2C', model: 'H2C', target_printer_id: null },
    })
    renderPage(<PrintersPanel />)
    const row = await screen.findByRole('listitem', { name: 'Printer Any H2C' })
    expect(row).toHaveTextContent('Bambuddy virtual printer → any H2C (archive)')
  })

  it("leaves the serial out of the agent's page read, as the printer tools do (#4375)", async () => {
    renderPage(<PrintersPanel />)
    const h2c = await screen.findByRole('listitem', { name: H2C })
    expect(h2c).toHaveTextContent('01P00A000000001')

    const read = JSON.stringify(takeSnapshot({ route: '/settings#printers', page: {}, tools: [] }))
    expect(read).toContain('How 3DP-31B-598 prints')
    expect(read).not.toContain('01P00A000000001')
  })
})
