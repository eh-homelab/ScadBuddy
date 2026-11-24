import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getType, upsertType, type ProfileFetch } from '../../api/filament'
import { T } from '../../mocks/features/filament/seed'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { when } from './formClasses'
import { ProfileFetchPanel } from './ProfileFetchPanel'

const RUN: ProfileFetch = {
  id: '00000000-0000-4000-8000-00000000fe01',
  type_id: T.JADE,
  tdfp_id: 1234,
  status: 'running',
  targets: [{ printer_model: 'H2C', nozzles: ['0.2', '0.4', '0.6', '0.8'] }],
  missing: [],
  k_values: 0,
  notes: [],
  error: null,
  started_at: '2026-11-18T12:00:00Z',
  finished_at: null,
}

const URL = '*/api/v1/filament/types/:id/profile-fetch'

function show(props: Partial<Parameters<typeof ProfileFetchPanel>[0]> = {}) {
  return renderPage(<ProfileFetchPanel typeId={T.JADE} tdfpId={1234} idEdited={false} onEnded={() => {}} pollMs={20} {...props} />)
}

const fetchButton = () => screen.getByRole('button', { name: 'Fetch from 3DFP' })

describe('ProfileFetchPanel (#6625)', () => {
  beforeEach(() => resetMockState())

  it('is off without a saved 3DFP id and says why', async () => {
    show({ tdfpId: null })
    expect(fetchButton()).toBeDisabled()
    expect(fetchButton()).toHaveAccessibleDescription('Enter the 3DFP id above and save the type to fetch its presets.')
  })

  it('is off while the 3DFP id has an unsaved edit and says to save first', async () => {
    show({ idEdited: true })
    expect(fetchButton()).toBeDisabled()
    expect(fetchButton()).toHaveAccessibleDescription('Save the type first: the fetch uses the saved 3DFP id.')
  })

  it('shows the running run a joined start answers', async () => {
    server.use(http.post(URL, () => HttpResponse.json({ run: RUN, joined: true }, { status: 202 })), http.get(URL, () => HttpResponse.json({ run: null, gaps: [] })))
    const { user } = show({ pollMs: 60_000 })
    await user.click(fetchButton())
    const last = await screen.findByRole('status', { name: 'Last fetch' })
    expect(last).toHaveTextContent('Running')
    expect(last).toHaveTextContent('Exporting 4 presets for H2C from 3DFP.')
    expect(last).toHaveTextContent('A fetch of this type was already running, so this follows it.')
    expect(fetchButton()).toBeDisabled()
  })

  it('lists the keys a partial run could not set, in plain words, and its notes', async () => {
    const partial: ProfileFetch = {
      ...RUN,
      status: 'partial',
      finished_at: '2026-11-18T12:05:00Z',
      k_values: 2,
      missing: [
        { printer_model: 'H2C', nozzle_diameter: '0.2', reason: '3dfilamentprofiles has no 0.2 mm export for this filament' },
        { printer_model: 'H2C', nozzle_diameter: '0.8', reason: 'the profile-fetch worker sent no file for it' },
      ],
      notes: ['printer 3DP-31B-598 did not answer for its K values'],
    }
    server.use(http.get(URL, () => HttpResponse.json({ run: partial, gaps: [] })))
    show()
    const last = await screen.findByRole('status', { name: 'Last fetch' })
    expect(last).toHaveTextContent('Partly imported')
    expect(last).toHaveTextContent('2 presets were set for H2C. 2 K values were read from the printers.')
    expect(within(last).getByText('These presets were not set:')).toBeInTheDocument()
    const missing = within(last).getByRole('list', { name: 'Presets not set' })
    expect(within(missing).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'H2C with a 0.2 mm nozzle: 3dfilamentprofiles has no 0.2 mm export for this filament',
      'H2C with a 0.8 mm nozzle: the profile-fetch worker sent no file for it',
    ])
    expect(within(within(last).getByRole('list', { name: 'Fetch notes' })).getByText('printer 3DP-31B-598 did not answer for its K values')).toBeInTheDocument()
  })

  it("shows a failed run's error", async () => {
    server.use(http.get(URL, () => HttpResponse.json({ run: { ...RUN, status: 'failed', finished_at: '2026-11-18T12:05:00Z', error: 'no profile-fetch worker picked the fetch up' }, gaps: [] })))
    show()
    const last = await screen.findByRole('status', { name: 'Last fetch' })
    expect(last).toHaveTextContent('Failed')
    expect(last).toHaveTextContent('no profile-fetch worker picked the fetch up')
    expect(fetchButton()).toBeEnabled()
  })

  it('reads a running fetch again until it ends, then says so once', async () => {
    await upsertType({ tdfp_id: 1234 }, T.JADE)
    const onEnded = vi.fn()
    const { user } = show({ onEnded })
    await waitFor(() => expect(fetchButton()).toBeEnabled())
    await user.click(fetchButton())
    // The mock's fetch ends on the read after the start.
    const last = await screen.findByRole('status', { name: 'Last fetch' })
    await waitFor(() => expect(last).toHaveTextContent('Imported'))
    expect(last).toHaveTextContent('4 presets were set for H2C.')
    await waitFor(() => expect(onEnded).toHaveBeenCalledTimes(1))
    expect(onEnded.mock.calls[0]![0]).toMatchObject({ status: 'imported' })
    expect((await getType(T.JADE)).presets.filter((row) => row.printer_model === 'H2C')).toHaveLength(4)
  })

  it('keeps reading after a failed read, and the next good read clears its error', async () => {
    let reads = 0
    server.use(
      http.post(URL, () => HttpResponse.json({ run: RUN, joined: false }, { status: 202 })),
      http.get(URL, () => {
        reads += 1
        if (reads === 1) return HttpResponse.json({ run: null, gaps: [] })
        if (reads === 2) return HttpResponse.json({ type: 'about:blank', title: 'Bad Gateway', status: 502, detail: 'the API did not answer' }, { status: 502 })
        return HttpResponse.json({ run: { ...RUN, status: 'imported', finished_at: '2026-11-18T12:05:00Z' }, gaps: [] })
      }),
    )
    const { user } = show()
    await user.click(fetchButton())
    await waitFor(() => expect(screen.getByRole('status', { name: 'Last fetch' })).toHaveTextContent('Imported'))
    expect(reads).toBeGreaterThanOrEqual(3)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('says, in plain words, that no printer has a model, and links to Printers', async () => {
    server.use(
      http.post(URL, () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/profile-fetch/no_printer_models', title: 'Unprocessable Content', status: 422, detail: 'no printer has a model to fetch presets for', code: 'no_printer_models' },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = show()
    await user.click(fetchButton())
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('No printer has a model, so there is nothing to fetch presets for. Set a model in Printers.')
    expect(within(alert).getByRole('link', { name: 'Set a model in Printers' })).toHaveAttribute('href', expect.stringContaining('/settings/printing#printers'))
  })

  it("refuses a start for a type the mock has no 3DFP id for with the API's code", async () => {
    // The seed's types have no 3DFP id: the button is on here only because the prop says so.
    const { user } = show()
    await user.click(fetchButton())
    expect(await screen.findByRole('alert')).toHaveTextContent('This type has no saved 3DFP id. Enter it above and save the type.')
  })

  it('says that a fetch replaces the presets for the chosen nozzles, also after a run (R-T9-6)', async () => {
    server.use(http.get(URL, () => HttpResponse.json({ run: { ...RUN, status: 'imported', finished_at: '2026-11-18T12:05:00Z' }, gaps: [] })))
    show()
    await screen.findByRole('status', { name: 'Last fetch' })
    expect(fetchButton()).toHaveAccessibleDescription(
      "Keeps a preset for each printer model and nozzle in ScadBuddy, with a copy in Bambuddy when Settings → Printing asks for one, and sets it here. It replaces this type's presets for the nozzles chosen in Settings → Printing.",
    )
  })

  it('writes when the run ended as the other filament pages do (R-T9-3)', async () => {
    server.use(http.get(URL, () => HttpResponse.json({ run: { ...RUN, status: 'imported', finished_at: '2026-11-18T12:05:00Z' }, gaps: [] })))
    show()
    const last = await screen.findByRole('status', { name: 'Last fetch' })
    expect(last).toHaveTextContent(`Ended ${when('2026-11-18T12:05:00Z')}. 4 presets were set for H2C.`)
  })

  it('lists the gaps no run is filling, leaving out the keys the last run listed as missing (R-T9-1)', async () => {
    const gaps = [
      { printer_model: 'H2C', nozzle_diameter: '0.2', reason: '3dfilamentprofiles has no 0.2 mm export for this filament' },
      { printer_model: 'P1S', nozzle_diameter: '0.4', reason: 'no preset for this printer and nozzle yet' },
      { printer_model: 'P1S', nozzle_diameter: '0.6', reason: 'no preset for this printer and nozzle yet' },
    ]
    const partial: ProfileFetch = { ...RUN, status: 'partial', finished_at: '2026-11-18T12:05:00Z', missing: [gaps[0]!] }
    server.use(http.get(URL, () => HttpResponse.json({ run: partial, gaps })))
    show()
    const last = await screen.findByRole('status', { name: 'Last fetch' })
    const missing = within(last).getByRole('list', { name: 'Presets not set' })
    expect(within(missing).getAllByRole('listitem')).toHaveLength(1)
    const open = screen.getByRole('list', { name: 'Presets missing' })
    expect(within(open).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'P1S with a 0.4 mm nozzle: no preset for this printer and nozzle yet',
      'P1S with a 0.6 mm nozzle: no preset for this printer and nozzle yet',
    ])
    expect(screen.getByText('These printers and nozzles have no preset yet. Fetch from 3DFP adds them:')).toBeInTheDocument()
  })

  it('lists the gaps when the type was never fetched, and not while a run is going (R-T9-1)', async () => {
    const gaps = [{ printer_model: 'H2C', nozzle_diameter: '0.4', reason: 'no preset for this printer and nozzle yet' }]
    let run: ProfileFetch | null = null
    server.use(
      http.get(URL, () => HttpResponse.json({ run, gaps })),
      http.post(URL, () => {
        run = RUN
        return HttpResponse.json({ run: RUN, joined: false }, { status: 202 })
      }),
    )
    const { user } = show({ pollMs: 60_000 })
    const open = await screen.findByRole('list', { name: 'Presets missing' })
    expect(within(open).getByRole('listitem')).toHaveTextContent('H2C with a 0.4 mm nozzle: no preset for this printer and nozzle yet')
    await user.click(fetchButton())
    await waitFor(() => expect(screen.getByRole('status', { name: 'Last fetch' })).toHaveTextContent('Running'))
    expect(screen.queryByRole('list', { name: 'Presets missing' })).not.toBeInTheDocument()
  })

  it('lets no read that started before a start overwrite its run, and keeps reading (R-T9-4)', async () => {
    let gets = 0
    let release!: () => void
    const held = new Promise<void>((resolve) => (release = resolve))
    let done = false
    const old: ProfileFetch = { ...RUN, id: '00000000-0000-4000-8000-00000000fe00', status: 'imported', finished_at: '2026-11-17T12:05:00Z' }
    const fresh: ProfileFetch = { ...RUN, id: '00000000-0000-4000-8000-00000000fe02', status: 'imported', finished_at: '2026-11-18T12:05:00Z' }
    server.use(
      http.post(URL, () => HttpResponse.json({ run: RUN, joined: false }, { status: 202 })),
      http.get(URL, async () => {
        gets += 1
        if (gets === 1) {
          await held
          return HttpResponse.json({ run: old, gaps: [] })
        }
        return HttpResponse.json({ run: done ? fresh : RUN, gaps: [] })
      }),
    )
    const onEnded = vi.fn()
    const { user } = show({ onEnded })
    await waitFor(() => expect(gets).toBe(1))
    await user.click(fetchButton())
    const last = await screen.findByRole('status', { name: 'Last fetch' })
    await waitFor(() => expect(last).toHaveTextContent('Running'))
    release()
    await waitFor(() => expect(gets).toBeGreaterThanOrEqual(3))
    expect(last).toHaveTextContent('Running')
    expect(onEnded).not.toHaveBeenCalled()
    done = true
    await waitFor(() => expect(onEnded).toHaveBeenCalledTimes(1))
    expect(onEnded.mock.calls[0]![0]).toMatchObject({ id: fresh.id })
    expect(last).toHaveTextContent('Imported')
  })

  it('says a 503 once, in a plain sentence (R-T9-5)', async () => {
    const failed: ProfileFetch = { ...RUN, status: 'failed', finished_at: '2026-11-18T12:00:10Z', error: 'the fetch could not be started' }
    let run: ProfileFetch | null = null
    server.use(
      http.get(URL, () => HttpResponse.json({ run, gaps: [] })),
      http.post(URL, () => {
        run = failed
        return HttpResponse.json(
          { type: 'about:blank', title: 'Service Unavailable', status: 503, detail: 'the fetch could not be started' },
          { status: 503, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
    )
    const { user } = show()
    await user.click(fetchButton())
    const last = await screen.findByRole('status', { name: 'Last fetch' })
    await waitFor(() => expect(last).toHaveTextContent('Failed'))
    expect(screen.getByRole('alert')).toHaveTextContent('ScadBuddy could not start the fetch. Try again in a minute.')
    expect(screen.queryByText(/the fetch could not be started/)).not.toBeInTheDocument()
  })
})
