import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import type { ReactNode } from 'react'
import { Route, Routes, useNavigate, useParams } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Job } from '../api/types'
import type * as embed from '../lib/embed'
import { DownloadBlockedError, downloadBlob, isEmbedded, openExternal } from '../lib/embed'
import { outputs, prints, versionIds } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { PrintDetailPage } from './PrintDetailPage'

// WebGL does not exist in jsdom: the viewer is a stand-in that says what it was given.
vi.mock('../components/Preview', () => ({
  Preview: ({ job, leading }: { job?: Job; leading?: ReactNode }) => (
    <div data-testid="preview" data-url={job?.preview_url ?? ''}>
      {leading}
    </div>
  ),
}))

vi.mock('../lib/embed', async (original) => ({
  ...(await original<typeof embed>()),
  downloadBlob: vi.fn(async (load: () => Promise<Blob>) => {
    await load()
  }),
  openExternal: vi.fn(),
  isEmbedded: vi.fn(() => false),
}))

const REAGAN = outputs[0]!.id
const WORKSHOP = outputs[2]!.id

function render(archiveId: number | string) {
  return renderPage(
    <Routes>
      <Route path="/prints/:archiveId" element={<PrintDetailPage />} />
      <Route path="/edit/:outputId" element={<Landed name="edit" />} />
      <Route path="/m/:slug" element={<Landed name="model" />} />
      <Route path="/m/:slug/versions" element={<Landed name="versions" />} />
    </Routes>,
    { route: `/prints/${archiveId}` },
  )
}

/** Goes to another print while the detail page stays mounted. */
function NextPrint() {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => void navigate('/prints/91')}>
      Next print
    </button>
  )
}

function Landed({ name }: { name: string }) {
  const params = useParams()
  return <div data-testid="landed">{`${name}:${Object.values(params).join('/')}`}</div>
}

async function section(name: string): Promise<HTMLElement> {
  return await screen.findByRole('region', { name })
}

beforeEach(() => {
  vi.mocked(downloadBlob).mockReset()
  vi.mocked(downloadBlob).mockImplementation(async (load) => {
    await load()
  })
  vi.mocked(openExternal).mockClear()
  vi.mocked(isEmbedded).mockReturnValue(false)
})

describe('PrintDetailPage (#311)', () => {
  it('shows the outcome: status, times, filament, cost, printer and runs', async () => {
    render(35)
    const outcome = await section('Outcome')
    const facts = within(outcome.querySelector('dl') as HTMLElement)
    expect(facts.getByText('Succeeded')).toBeInTheDocument() // as the list's badge says it
    expect(facts.getByText('3DP-31B-598')).toBeInTheDocument()
    expect(facts.getByText('1h 47m')).toBeInTheDocument() // actual 6437 s
    expect(facts.getByText('1h 34m')).toBeInTheDocument() // estimate 5647 s
    expect(facts.getByText('16.36 g PLA')).toBeInTheDocument()
    expect(facts.getByText('0.43')).toBeInTheDocument()
    const runs = within(outcome).getByRole('list', { name: 'Runs' })
    expect(within(runs).getAllByRole('listitem')).toHaveLength(2)
    expect(runs).toHaveTextContent('Cancelled')
  })

  it('numbers a printer Bambuddy gave no name for as the list does (#2363)', async () => {
    const done = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/:id', () =>
        HttpResponse.json({ ...done, printer_id: 3, printer_name: null, outcome: { ...done.outcome, printer_id: 3, printer_name: null } }),
      ),
    )
    render(35)
    const outcome = await section('Outcome')
    const facts = within(outcome.querySelector('dl') as HTMLElement)
    expect(facts.getByText('Printer 3')).toBeInTheDocument()
    expect(facts.queryByText('#3')).not.toBeInTheDocument()
  })

  it('shows what the sliced file says, slot by slot (#1860)', async () => {
    render(35)
    const slicing = await section('Slicing')
    const facts = within(slicing.querySelector('dl') as HTMLElement)
    expect(facts.getByText('H2C')).toBeInTheDocument()
    expect(facts.getByText('Textured PEI Plate')).toBeInTheDocument()
    expect(facts.getByText('86')).toBeInTheDocument()
    expect(facts.getByText('230 °C')).toBeInTheDocument()
    // Not length()'s one decimal, which showed 0.08 mm as 0.1 mm.
    expect(facts.getByText('0.08 mm')).toBeInTheDocument()
    expect(facts.getByText('0.4 mm')).toBeInTheDocument()
    expect(facts.getByText('3.57 m')).toBeInTheDocument()
    const slots = within(slicing).getByRole('list', { name: 'Filament by slot' })
    expect(within(slots).getAllByRole('listitem').map((slot) => slot.textContent)).toEqual([
      'Slot 1PLA11.34 g',
      'Slot 2PLA5.02 g',
    ])
    expect(within(slots).getByRole('img', { name: '#FF9425' })).toBeInTheDocument()
  })

  it('shows Bambuddy’s bookkeeping and every other field it has (#1860)', async () => {
    const { user } = render(35)
    const record = await section('In Bambuddy')
    expect(record).toHaveTextContent('3 keychains')
    expect(record).toHaveTextContent('87.7%')
    expect(record).toHaveTextContent('1 succeeded, 0 failed')
    await user.click(within(record).getByText('All other fields (2)'))
    expect(within(record).getByLabelText('All other fields')).toHaveTextContent('original_archive_id—')
  })

  it('rounds a time accuracy Bambuddy answers as a long float', async () => {
    const done = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/35', () =>
        HttpResponse.json({ ...done, record: { ...done.record!, time_accuracy_percent: 87.666666 } }),
      ),
    )
    render(35)
    const record = await section('In Bambuddy')
    expect(record).toHaveTextContent('87.7%')
    expect(record).not.toHaveTextContent('87.666666')
  })

  it('paints only a hex colour as a swatch (#1860)', async () => {
    const done = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/35', () =>
        HttpResponse.json({
          ...done,
          slicing: {
            ...done.slicing!,
            filament_slots: [{ slot_id: 1, used_grams: 1, filament_type: 'PLA', color: 'url(https://x.example/t.png)' }],
          },
        }),
      ),
    )
    render(35)
    const slots = within(await section('Slicing')).getByRole('list', { name: 'Filament by slot' })
    expect(within(slots).getByRole('img', { name: 'url(https://x.example/t.png)' })).not.toHaveAttribute('style')
  })

  it('rounds Bambuddy’s amounts, names whose currency the cost is in, and opens its links (#1860 review)', async () => {
    const done = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/35', () =>
        HttpResponse.json({
          ...done,
          record: {
            ...done.record!,
            filament_actual_grams: 16.300000001,
            energy_kwh: 0.123456,
            energy_cost: 0.5,
            makerworld_url: 'https://makerworld.com/en/models/1',
            external_url: 'javascript:alert(1)',
          },
        }),
      ),
    )
    const { user } = render(35)
    const record = await section('In Bambuddy')
    expect(record).toHaveTextContent('16.3 g')
    expect(record).not.toHaveTextContent('16.300000001')
    expect(record).toHaveTextContent('0.12 kWh')
    expect(record).toHaveTextContent("0.50 (in Bambuddy's currency)")
    await user.click(within(record).getByRole('button', { name: 'https://makerworld.com/en/models/1' }))
    expect(openExternal).toHaveBeenCalledWith('https://makerworld.com/en/models/1')
    expect(within(record).queryByRole('button', { name: 'javascript:alert(1)' })).toBeNull()
    expect(record).toHaveTextContent('javascript:alert(1)')
  })

  it('says why a library print has no provenance (#1860)', async () => {
    const done = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/35', () =>
        HttpResponse.json({ ...done, output_id: null, slug: null, library_file_id: 89, provenance: null }),
      ),
    )
    render(35)
    expect(await section('Provenance')).toHaveTextContent('Printed from a Bambuddy library file, not a ScadBuddy template')
  })

  it('marks a run whose filament reading is not believed (#950)', async () => {
    const done = prints.find((print) => print.archive_id === 35)!
    const [, last] = done.outcome.runs
    server.use(
      http.get('/api/v1/prints/35', () =>
        HttpResponse.json({
          ...done,
          outcome: {
            ...done.outcome,
            runs: [{ ...last!, filament_used_grams: 1004.2, cost: 0.43, filament_reading_suspect: true }],
          },
        }),
      ),
    )
    render(35)
    const outcome = await section('Outcome')
    const runs = within(outcome).getByRole('list', { name: 'Runs' })
    const reading = within(runs).getByText('1004.2 g, not this print\'s')
    expect(reading).toHaveAccessibleDescription(expect.stringContaining('not used for the cost'))
    // Its tooltip opens from the keyboard too (#3674 review).
    act(() => reading.focus())
    expect(await screen.findByRole('tooltip')).toHaveTextContent('not used for the cost')
  })

  it('shows why a failed print failed', async () => {
    render(36)
    const outcome = await section('Outcome')
    expect(within(outcome).getByText('Failed')).toBeInTheDocument()
    expect(within(outcome).getByText('Spaghetti detected')).toBeInTheDocument()
  })

  it('shows the provenance: template, revision linked to Versions, and the parameters', async () => {
    const { user } = render(35)
    const provenance = await section('Provenance')
    const params = within(provenance).getByRole('table', { name: 'Parameters' })
    expect(within(params).getByText('name')).toBeInTheDocument()
    expect(within(params).getByText('Reagan')).toBeInTheDocument()
    await user.click(within(provenance).getByRole('link', { name: versionIds.edited.slice(0, 7) }))
    expect(await screen.findByTestId('landed')).toHaveTextContent('versions:name-keychain')
  })

  it('shows a library file print without a template, parameters or ScadBuddy files (#976)', async () => {
    const base = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/90', () =>
        HttpResponse.json({
          ...base,
          archive_id: 90,
          output_id: null,
          slug: null,
          library_file_id: 89,
          library_file_name: null,
          output_name: 'Bambu Spool Lock',
          params_diff: null,
          provenance: null,
          files: base.files.filter((file) => file.kind === 'sliced' || file.kind === 'source'),
          links: { ...base.links, customize_url: null },
        }),
      ),
    )
    render(90)
    expect(await screen.findByRole('heading', { name: 'Bambu Spool Lock' })).toBeInTheDocument()
    // #1755 — with no name for the file (gone from Bambuddy), the breadcrumb says what it
    // was, and still leads to every print of it.
    expect(screen.getByRole('link', { name: 'Bambuddy library file' })).toHaveAttribute('href', '/prints?file=89')
    // #1860 — the section says why it is empty.
    expect(screen.getByRole('region', { name: 'Provenance' })).toHaveTextContent('not a ScadBuddy template')
    expect(screen.queryByRole('link', { name: 'Customize from this' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'ScadBuddy render' })).not.toBeInTheDocument()
    const files = await section('Files')
    expect(within(files).getByText('Sliced file')).toBeInTheDocument()
    expect(within(files).queryByText('Parameters')).not.toBeInTheDocument()
  })

  it('shows a library print’s plate beside it and lists the library file (#1753)', async () => {
    const base = prints.find((print) => print.archive_id === 35)!
    const preview = '/api/v1/print/library/89/preview.glb?plate=2'
    server.use(
      http.get('/api/v1/prints/90', () =>
        HttpResponse.json({
          ...base,
          archive_id: 90,
          output_id: null,
          slug: null,
          library_file_id: 89,
          output_name: 'Bambu Spool Lock',
          params_diff: null,
          provenance: null,
          files: [
            { kind: 'library_file', name: 'spool-lock.3mf', size: 1234, url: '/api/v1/print/library/89/file' },
            { kind: 'preview_glb', name: 'spool-lock.glb', size: null, url: preview },
            ...base.files.filter((file) => file.kind === 'sliced'),
          ],
          links: { ...base.links, customize_url: null },
        }),
      ),
    )
    const { user } = render(90)
    // #2731 — the plate's image first: its mesh can be megabytes, loaded only when asked.
    const plate = await section('Library file')
    expect(within(plate).getByRole('img', { name: 'Plate 2 of the library file' })).toHaveAttribute(
      'src',
      '/api/v1/print/library/89/plates/2/thumbnail',
    )
    expect(screen.queryByTestId('preview')).not.toBeInTheDocument()
    await user.click(within(plate).getByRole('button', { name: 'Show in 3D' }))
    expect(await screen.findByTestId('preview')).toHaveAttribute('data-url', preview)
    const files = await section('Files')
    expect(within(files).getByText('Library file')).toBeInTheDocument()
    expect(within(files).getByText('spool-lock.3mf')).toBeInTheDocument()
  })

  it('starts each library print on its image, even in the same mounted page (#2915)', async () => {
    const base = prints.find((print) => print.archive_id === 35)!
    const libraryPrint = (archiveId: number, fileId: number) => ({
      ...base,
      archive_id: archiveId,
      output_id: null,
      slug: null,
      library_file_id: fileId,
      params_diff: null,
      provenance: null,
      files: [
        {
          kind: 'preview_glb',
          name: 'plate.glb',
          size: null,
          url: `/api/v1/print/library/${fileId}/preview.glb?plate=1`,
        },
      ],
      links: { ...base.links, customize_url: null },
    })
    server.use(
      http.get('/api/v1/prints/:id', ({ params }) =>
        HttpResponse.json(params['id'] === '90' ? libraryPrint(90, 89) : libraryPrint(91, 88)),
      ),
    )
    const { user } = renderPage(
      <Routes>
        <Route
          path="/prints/:archiveId"
          element={
            <>
              <PrintDetailPage />
              <NextPrint />
            </>
          }
        />
      </Routes>,
      { route: '/prints/90' },
    )
    await user.click(within(await section('Library file')).getByRole('button', { name: 'Show in 3D' }))
    expect(await screen.findByTestId('preview')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Next print' }))

    const plate = await screen.findByRole('img', { name: 'Plate 1 of the library file' })
    expect(plate).toHaveAttribute('src', '/api/v1/print/library/88/plates/1/thumbnail')
    expect(screen.queryByTestId('preview')).not.toBeInTheDocument()
  })

  it('shows a placeholder, not a broken image, for a plate image that will not load (#2915)', async () => {
    const base = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/90', () =>
        HttpResponse.json({
          ...base,
          archive_id: 90,
          output_id: null,
          slug: null,
          library_file_id: 89,
          params_diff: null,
          provenance: null,
          files: [
            { kind: 'preview_glb', name: 'p.glb', size: null, url: '/api/v1/print/library/89/preview.glb?plate=2' },
          ],
          links: { ...base.links, customize_url: null },
        }),
      ),
    )
    render(90)
    const plate = await section('Library file')
    fireEvent.error(within(plate).getByRole('img', { name: 'Plate 2 of the library file' }))

    expect(within(plate).queryByRole('img')).not.toBeInTheDocument()
    expect(within(plate).getByText('No image of plate 2')).toBeInTheDocument()
    expect(within(plate).getByRole('button', { name: 'Show in 3D' })).toBeInTheDocument()
  })

  it('links the template', async () => {
    const { user } = render(35)
    const provenance = await section('Provenance')
    await user.click(within(provenance).getByRole('link', { name: 'name-keychain' }))
    expect(await screen.findByTestId('landed')).toHaveTextContent('model:name-keychain')
  })

  it('shows the photos, timelapse and plate image in the gallery, and opens the lightbox', async () => {
    const { user } = render(35)
    const gallery = await section('Gallery')
    expect(within(gallery).getByText('1 of 3')).toBeInTheDocument()
    expect(within(gallery).getByTestId('play-badge-timelapse')).toBeInTheDocument()
    await user.click(within(gallery).getByRole('button', { name: 'Open Finish photo' }))
    await screen.findByRole('dialog', {}, { timeout: 3000 })
  })

  it('names the plate image by what the plate holds (#2055)', async () => {
    render(35)
    const gallery = await section('Gallery')
    const slides = within(gallery).getAllByRole('group')
    expect(within(slides.at(-1)!).getByRole('img')).toHaveAttribute('alt', 'Keychain as sliced')
  })

  it('leads the gallery with the finish photo, then the other photos', async () => {
    const done = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/35', () =>
        HttpResponse.json({
          ...done,
          media: { ...done.media, photos: [{ name: 'a1b2c3d4.jpg', url: '/api/v1/prints/35/photos/a1b2c3d4.jpg' }] },
        }),
      ),
    )
    render(35)
    const gallery = await section('Gallery')
    const slides = within(gallery).getAllByRole('group')
    expect(within(slides[0]!).getByRole('img')).toHaveAttribute('alt', 'Finish photo')
    expect(within(slides[1]!).getByRole('img')).toHaveAttribute('alt', 'Photo 1')
    expect(within(gallery).getByText('1 of 4')).toBeInTheDocument()
  })

  it('shows the ScadBuddy render beside the print, for comparison', async () => {
    render(35)
    const render3d = await screen.findByTestId('preview')
    expect(render3d).toHaveAttribute('data-url', `/api/v1/outputs/${REAGAN}/preview.glb`)
  })

  it('plays the timelapse in a native video from the proxy, and its frames seek it', async () => {
    const { user } = render(35)
    const timelapse = await section('Timelapse')
    const video = timelapse.querySelector('video') as HTMLVideoElement
    expect(video).toHaveAttribute('src', '/api/v1/prints/35/timelapse')
    expect(video).toHaveAttribute('controls')
    expect(video).toHaveAttribute('playsinline')
    // #2512 — the still by URL, never base64 in the detail's JSON.
    expect(video).toHaveAttribute('poster', '/api/v1/prints/35/timelapse/frames/0')
    video.currentTime = 3
    await user.click(await within(timelapse).findByRole('button', { name: 'Go to 0:00' }))
    expect(video.currentTime).toBe(0)
    expect(within(timelapse).getByText(/1680×1080/)).toBeInTheDocument()
  })

  it('shows the print before the timelapse stills arrive, then shows them (#2360)', async () => {
    let release: () => void = () => undefined
    const arrived = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.get('/api/v1/prints/:archiveId/timelapse/frames', async () => {
        await arrived
        return HttpResponse.json({
          info: null,
          frames: [
            { timestamp: 0, url: '/api/v1/prints/35/timelapse/frames/0' },
            { timestamp: 2, url: '/api/v1/prints/35/timelapse/frames/1' },
          ],
        })
      }),
    )
    render(35)
    const timelapse = await section('Timelapse')
    expect(timelapse.querySelector('video')).toHaveAttribute('poster', '/api/v1/prints/35/timelapse/frames/0')
    expect(within(timelapse).queryByRole('list', { name: 'Timelapse frames' })).not.toBeInTheDocument()
    release()
    const strip = await within(timelapse).findByRole('list', { name: 'Timelapse frames' })
    expect(within(strip).getByRole('button', { name: 'Go to 0:02' }).querySelector('img')).toHaveAttribute(
      'src',
      '/api/v1/prints/35/timelapse/frames/1',
    )
  })

  it('lists every file with its size and downloads it as a blob', async () => {
    const requested: string[] = []
    server.events.on('request:start', ({ request }) => requested.push(new URL(request.url).pathname))
    const { user } = render(35)
    const files = await section('Files')
    const rows = within(files).getAllByRole('listitem')
    // The editable 3MF, its preview mesh, the sliced file, the parameters, then (#894)
    // the finish photo and the timelapse.
    expect(rows).toHaveLength(6)
    expect(rows[0]).toHaveTextContent('Editable model (3MF)')
    expect(rows[0]).toHaveTextContent('48 kB')
    expect(rows[2]).toHaveTextContent('Sliced file')
    expect(rows[2]).toHaveTextContent('2.1 MB')

    await user.click(within(rows[2]!).getByRole('button', { name: /Download/ }))
    await waitFor(() =>
      expect(downloadBlob).toHaveBeenCalledWith(expect.any(Function), 'name-keychain-reagan.gcode.3mf'),
    )
    expect(requested).toContain('/api/v1/prints/35/files/sliced')
    server.events.removeAllListeners()
  })

  it('saves the timelapse from its URL under its own extension, not through memory (#3107)', async () => {
    const done = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/35', () =>
        HttpResponse.json({
          ...done,
          media: { ...done.media, timelapse: { ...done.media.timelapse!, filename: 'print-35-timelapse.avi' } },
        }),
      ),
    )
    const clicked: { href: string; download: string }[] = []
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ href: this.getAttribute('href') ?? '', download: this.download })
    })
    const { user } = render(35)
    const files = await section('Files')
    await user.click(within(files).getByRole('button', { name: 'Download print-35-timelapse.avi' }))

    expect(clicked).toEqual([{ href: '/api/v1/prints/35/timelapse', download: 'print-35-timelapse.avi' }])
    expect(downloadBlob).not.toHaveBeenCalled()

    // In Bambuddy's frame the sandbox drops that: the popup's path, as for any file.
    vi.mocked(isEmbedded).mockReturnValue(true)
    await user.click(within(files).getByRole('button', { name: 'Download print-35-timelapse.avi' }))
    await waitFor(() => expect(downloadBlob).toHaveBeenCalledWith(expect.any(Function), 'print-35-timelapse.avi'))
    expect(clicked).toHaveLength(1)
    click.mockRestore()
  })

  it('reads the output once to open the editable 3MF with its mesh (#3108)', async () => {
    const read = vi.spyOn(api, 'getOutput')
    const { user } = render(35)
    const files = await section('Files')
    // The page reads it too, for itself: only what the dialog reads is counted.
    const before = read.mock.calls.length
    await user.click(within(files).getByRole('button', { name: /^Editable model \(3MF\), .*, open$/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Editable model (3MF)' })
    expect(await within(dialog).findByTestId('preview')).toBeInTheDocument()
    expect(read.mock.calls.slice(before).filter(([id]) => id === REAGAN)).toHaveLength(1)
    read.mockRestore()
  })

  it("says a slicer project's view is the print's slicing, not the file's (#3108)", async () => {
    const done = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/35', () =>
        HttpResponse.json({
          ...done,
          files: [...done.files, { kind: 'source', name: 'keychain.3mf', size: 1000, url: '/api/v1/prints/35/files/source' }],
        }),
      ),
    )
    const { user } = render(35)
    const files = await section('Files')
    await user.click(within(files).getByRole('button', { name: /^Sliced file, .*, open$/ }))
    let dialog = await screen.findByRole('dialog', { name: 'Sliced file' })
    expect(within(dialog).queryByText('How this print was sliced')).not.toBeInTheDocument()
    await user.keyboard('{Escape}')

    await user.click(within(files).getByRole('button', { name: /^Slicer project \(3MF\), .*, open$/ }))
    dialog = await screen.findByRole('dialog', { name: 'Slicer project (3MF)' })
    expect(within(dialog).getByText('How this print was sliced')).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Slicing summary')).toBeInTheDocument()
  })

  it('gives each file an icon of its kind, a friendly label over its name, and a download of its own (#894)', async () => {
    render(35)
    const files = await section('Files')
    const rows = within(files).getAllByRole('listitem')
    expect(rows.map((row) => row.querySelector('[data-testid^="file-icon-"]')?.getAttribute('data-testid'))).toEqual([
      'file-icon-output_3mf',
      'file-icon-preview_glb',
      'file-icon-sliced',
      'file-icon-parameters',
      'file-icon-photo',
      'file-icon-timelapse',
    ])
    expect(
      within(rows[1]!).getByRole('button', { name: '3D preview, name-keychain-reagan.glb, open' }),
    ).toBeInTheDocument()
    expect(within(rows[1]!).getByRole('button', { name: 'Download name-keychain-reagan.glb' })).toBeInTheDocument()
  })

  it('opens a file in the app without downloading it (#894)', async () => {
    const { user } = render(35)
    const files = await section('Files')
    const open = (name: RegExp) => user.click(within(files).getByRole('button', { name }))

    await open(/^3D preview, .*, open$/)
    let dialog = await screen.findByRole('dialog', { name: '3D preview' })
    expect(await within(dialog).findByTestId('preview')).toHaveAttribute('data-url', `/api/v1/outputs/${REAGAN}/preview.glb`)
    await user.keyboard('{Escape}')

    await open(/^Sliced file, .*, open$/)
    dialog = await screen.findByRole('dialog', { name: 'Sliced file' })
    expect(within(dialog).getByRole('img', { name: 'Keychain' })).toHaveAttribute('src', '/api/v1/prints/35/plates/1/thumbnail')
    expect(within(dialog).getByLabelText('Slicing summary')).toHaveTextContent('Slot 2PLA · 5.02 g')
    await user.keyboard('{Escape}')

    await open(/^Parameters, .*, open$/)
    dialog = await screen.findByRole('dialog', { name: 'Parameters' })
    const table = within(dialog).getByRole('table', { name: 'Parameters' })
    expect(within(table).getByText('(changed from the default)', { exact: false })).toBeInTheDocument()
    await user.keyboard('{Escape}')

    await open(/^Photo, .*, open$/)
    dialog = await screen.findByRole('dialog', { name: 'Photo' })
    expect(within(dialog).getByRole('img')).toHaveAttribute('src', expect.stringContaining('/api/v1/prints/35/photos/'))
    await user.keyboard('{Escape}')

    await open(/^Timelapse, .*, open$/)
    dialog = await screen.findByRole('dialog', { name: 'Timelapse' })
    expect(within(dialog).getByLabelText('Timelapse')).toHaveAttribute('src', '/api/v1/prints/35/timelapse')
    expect(downloadBlob).not.toHaveBeenCalled()
  })

  it('opens the editable 3MF: its mesh and colours, and its plates only when there are several (#894)', async () => {
    const { user } = render(35)
    const files = await section('Files')
    const open = () => user.click(within(files).getByRole('button', { name: /^Editable model \(3MF\), .*, open$/ }))

    await open()
    let dialog = await screen.findByRole('dialog', { name: 'Editable model (3MF)' })
    expect(await within(dialog).findByTestId('preview')).toHaveAttribute('data-url', `/api/v1/outputs/${REAGAN}/preview.glb`)
    const colours = outputs[0]!.colors ?? []
    expect(colours.length).toBeGreaterThan(0)
    for (const colour of colours) expect(dialog.querySelector(`[data-colour="${colour.toUpperCase()}"]`)).not.toBeNull()
    expect(within(dialog).queryByRole('list', { name: 'Plates' })).not.toBeInTheDocument()
    await user.keyboard('{Escape}')

    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: true },
          { index: 2, has_thumbnail: true },
        ]),
      ),
    )
    await open()
    dialog = await screen.findByRole('dialog', { name: 'Editable model (3MF)' })
    const plates = await within(dialog).findByRole('list', { name: 'Plates' })
    expect(within(plates).getAllByRole('img').map((img) => img.getAttribute('src'))).toEqual([
      `/api/v1/outputs/${REAGAN}/plates/1/thumbnail`,
      `/api/v1/outputs/${REAGAN}/plates/2/thumbnail`,
    ])
  })

  it('opens the editable 3MF without a mesh when its 3D preview is gone', async () => {
    const done = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/35', () =>
        HttpResponse.json({ ...done, files: done.files.filter((file) => file.kind !== 'preview_glb') }),
      ),
    )
    const { user } = render(35)
    const files = await section('Files')
    await user.click(within(files).getByRole('button', { name: /^Editable model \(3MF\), .*, open$/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Editable model (3MF)' })
    const first = (outputs[0]!.colors ?? [])[0]!.toUpperCase()
    await waitFor(() => expect(dialog.querySelector(`[data-colour="${first}"]`)).not.toBeNull())
    expect(within(dialog).queryByTestId('preview')).not.toBeInTheDocument()
  })

  it('copies the parameters as JSON, and says so when there is no clipboard', async () => {
    // user-event stands in a clipboard of its own.
    const { user } = render(35)
    const files = await section('Files')
    await user.click(within(files).getByRole('button', { name: /^Parameters, .*, open$/ }))
    let dialog = await screen.findByRole('dialog', { name: 'Parameters' })
    await user.click(within(dialog).getByRole('button', { name: 'Copy JSON' }))
    expect(await within(dialog).findByRole('button', { name: 'Copied' })).toBeInTheDocument()
    expect(JSON.parse(await navigator.clipboard.readText())).toMatchObject({ name: 'Reagan' })
    await user.keyboard('{Escape}')

    const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    try {
      await user.click(within(files).getByRole('button', { name: /^Parameters, .*, open$/ }))
      dialog = await screen.findByRole('dialog', { name: 'Parameters' })
      await user.click(within(dialog).getByRole('button', { name: 'Copy JSON' }))
      expect(within(dialog).getByRole('alert')).toHaveTextContent('could not be copied here')
    } finally {
      if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard)
    }
  })

  it('downloads the parameters as JSON', async () => {
    const { user } = render(35)
    const files = await section('Files')
    await user.click(within(files).getByRole('button', { name: 'Download parameters as JSON' }))
    expect(downloadBlob).toHaveBeenCalledWith(expect.any(Function), 'name-keychain-reagan-params.json')
    const load = vi.mocked(downloadBlob).mock.calls[0]![0]
    expect(JSON.parse(await (await load()).text())).toMatchObject({ name: 'Reagan', text_size: 14 })
  })

  it('says to allow pop-ups when a download is blocked inside Bambuddy', async () => {
    vi.mocked(downloadBlob).mockRejectedValue(new DownloadBlockedError())
    const { user } = render(35)
    const files = await section('Files')
    await user.click(within(files).getByRole('button', { name: 'Download name-keychain-reagan.gcode.3mf' }))
    expect(await within(files).findByRole('alert')).toHaveTextContent('Allow pop-ups')
  })

  it('says so when the parameters cannot be saved either', async () => {
    vi.mocked(downloadBlob).mockRejectedValue(new DownloadBlockedError())
    const { user } = render(35)
    const files = await section('Files')
    await user.click(within(files).getByRole('button', { name: 'Download parameters as JSON' }))
    expect(await within(files).findByRole('alert')).toHaveTextContent('Allow pop-ups')
  })

  it('customizes from this print through the edit route', async () => {
    const { user } = render(35)
    await user.click(await screen.findByRole('link', { name: 'Customize from this' }))
    expect(await screen.findByTestId('landed')).toHaveTextContent(`edit:${REAGAN}`)
  })

  it('opens the print in Bambuddy', async () => {
    const { user } = render(35)
    await user.click(await screen.findByRole('button', { name: 'Open in Bambuddy' }))
    expect(openExternal).toHaveBeenCalledWith('https://bambuddy.example/archives')
  })

  it('prints again only once the send is confirmed', async () => {
    let posted = 0
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'POST' && request.url.endsWith('/prints/35/reprint')) posted += 1
    })
    const { user } = render(35)
    await user.click(await screen.findByRole('button', { name: 'Print again' }))
    const dialog = await screen.findByRole('dialog', { name: 'Print again' })
    expect(dialog).toHaveTextContent('3DP-31B-598')
    expect(posted).toBe(0)

    await user.click(within(dialog).getByRole('button', { name: 'Queue' }))
    expect(await within(dialog).findByText(/Queued as/)).toHaveTextContent('#200')
    expect(posted).toBe(1)
    await user.click(within(dialog).getByRole('button', { name: 'Open in queue' }))
    expect(openExternal).toHaveBeenCalledWith('https://bambuddy.example/queue')
    server.events.removeAllListeners()
  })

  it('offers no Print again while the print is still going (#2736)', async () => {
    const done = prints.find((print) => print.archive_id === 35)!
    server.use(
      http.get('/api/v1/prints/35', () =>
        HttpResponse.json({ ...done, status: 'printing', outcome: { ...done.outcome, status: 'printing' } }),
      ),
    )
    render(35)
    await section('Outcome')
    expect(screen.queryByRole('button', { name: 'Print again' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Open in Bambuddy' })).toBeInTheDocument()
  })

  it('says why Bambuddy would not queue it', async () => {
    server.use(
      http.post('/api/v1/prints/35/reprint', () =>
        HttpResponse.json(
          { title: 'Bambuddy API key scope', status: 409, detail: 'The key needs the Manage Queue scope' },
          { status: 409 },
        ),
      ),
    )
    const { user } = render(35)
    await user.click(await screen.findByRole('button', { name: 'Print again' }))
    const dialog = await screen.findByRole('dialog', { name: 'Print again' })
    await user.click(within(dialog).getByRole('button', { name: 'Queue' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Manage Queue')
  })

  it('shows what ScadBuddy still knows of a print deleted in Bambuddy', async () => {
    render(38)
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(/This print was deleted in Bambuddy/),
    )
    const provenance = await section('Provenance')
    expect(within(provenance).getByRole('table', { name: 'Parameters' })).toHaveTextContent('Workshop')
    const files = await section('Files')
    // Its own 3MF and the parameters; Bambuddy's sliced file went with the archive.
    expect(within(files).getAllByRole('listitem')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: 'Print again' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Open in Bambuddy' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Gallery' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Customize from this' })).toHaveAttribute('href', `/edit/${WORKSHOP}`)
  })

  it('pulls a timelapse from the printer only on request', async () => {
    const asked: string[] = []
    server.events.on('request:start', ({ request }) => {
      const url = new URL(request.url)
      if (url.pathname === '/api/v1/prints/36') asked.push(url.search)
    })
    const { user } = render(36)
    const timelapse = await section('Timelapse')
    expect(timelapse.querySelector('video')).toBeNull()
    expect(asked).toEqual([''])

    await user.click(within(timelapse).getByRole('button', { name: 'Look on the printer' }))
    const pull = await within(timelapse).findByRole('button', { name: 'Pull timelapse from printer' })
    expect(asked).toEqual(['', '?printer_media=1'])
    // An outward write: only a person may press it, never the agent's fallback click.
    expect(pull).toHaveAttribute('data-agent-user-only')
    // Only the timelapse is offered, not the camera recording.
    expect(timelapse).toHaveTextContent('video_2026-09-26_20-01-00.mp4')
    expect(timelapse).not.toHaveTextContent('ipcam-record')

    await user.click(pull)
    await waitFor(() => expect(timelapse.querySelector('video')).toHaveAttribute('src', '/api/v1/prints/36/timelapse'))
    server.events.removeAllListeners()
  })

  it('says when the printer has no timelapse for it', async () => {
    const { user } = render(37)
    const timelapse = await section('Timelapse')
    await user.click(within(timelapse).getByRole('button', { name: 'Look on the printer' }))
    expect(await within(timelapse).findByText(/No timelapse on the printer/)).toBeInTheDocument()
    expect(within(timelapse).queryByRole('button', { name: 'Pull timelapse from printer' })).not.toBeInTheDocument()
  })

  it('explains a key that may not list the printer', async () => {
    server.use(
      http.get('/api/v1/prints/37', ({ request }) =>
        HttpResponse.json({
          ...prints.find((print) => print.archive_id === 37)!,
          printer_media: new URL(request.url).searchParams.has('printer_media')
            ? { archive_id: 37, printer_id: 2, local_timelapse: null, remote_files: [], warnings: ['printer_files_forbidden'] }
            : null,
        }),
      ),
    )
    const { user } = render(37)
    const timelapse = await section('Timelapse')
    await user.click(within(timelapse).getByRole('button', { name: 'Look on the printer' }))
    expect(await within(timelapse).findByText(/Control Printer/)).toBeInTheDocument()
  })

  it('says so when the print is not one of ScadBuddy’s', async () => {
    render(99)
    expect(await screen.findByRole('alert')).toHaveTextContent('not a print of any ScadBuddy output')
  })
})
