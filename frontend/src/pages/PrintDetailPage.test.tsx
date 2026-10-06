import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import type { ReactNode } from 'react'
import { Route, Routes, useParams } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Job } from '../api/types'
import type * as embed from '../lib/embed'
import { DownloadBlockedError, downloadBlob, openExternal } from '../lib/embed'
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
    expect(within(runs).getByText('1004.2 g, not this print\'s')).toHaveAttribute(
      'title',
      expect.stringContaining('not used for the cost'),
    )
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
    expect(screen.getByText('Bambuddy library file')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Provenance' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Customize from this' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'ScadBuddy render' })).not.toBeInTheDocument()
    const files = await section('Files')
    expect(within(files).getByText('Sliced file')).toBeInTheDocument()
    expect(within(files).queryByText('Parameters')).not.toBeInTheDocument()
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
    expect(video.getAttribute('poster')).toMatch(/^data:image\//)
    video.currentTime = 3
    await user.click(within(timelapse).getByRole('button', { name: 'Go to 0:00' }))
    expect(video.currentTime).toBe(0)
  })

  it('lists every file with its size and downloads it as a blob', async () => {
    const requested: string[] = []
    server.events.on('request:start', ({ request }) => requested.push(new URL(request.url).pathname))
    const { user } = render(35)
    const files = await section('Files')
    const rows = within(files).getAllByRole('listitem')
    // The ScadBuddy 3MF, its preview mesh, the sliced file and the parameters.
    expect(rows).toHaveLength(4)
    expect(rows[0]).toHaveTextContent('ScadBuddy 3MF')
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
    expect(await screen.findByRole('status')).toHaveTextContent(/deleted in Bambuddy/)
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
