import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { BROKEN_SOURCE, keychainSchema, keychainSource } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { NewModelPage } from './NewModelPage'

// Monaco needs layout, workers and a canvas, none of which jsdom has; the real editor
// is exercised by the Playwright smoke test. Here it stands in as a textarea so the
// page's own behaviour — check, refuse, force — is what is under test.
vi.mock('../components/SourceEditor', () => ({
  SourceEditor: ({
    value,
    onChange,
    label,
    errors = [],
  }: {
    value: string
    onChange: (next: string) => void
    label: string
    errors?: { line?: number | null }[]
  }) => (
    <textarea
      aria-label={label}
      data-marked-lines={errors.map((error) => error.line ?? '').join(',')}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}))

function renderNew() {
  const user = userEvent.setup()
  const view = render(
    <MemoryRouter initialEntries={['/new']}>
      <Routes>
        <Route path="/new" element={<NewModelPage />} />
        <Route path="/m/:slug" element={<h1>Customizer</h1>} />
        <Route path="/" element={<h1>Catalogue</h1>} />
      </Routes>
    </MemoryRouter>,
  )
  return { user, ...view }
}

async function paste(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.click(screen.getByLabelText('OpenSCAD source'))
  await user.paste(text)
}

describe('NewModelPage, unsaved source (#997)', () => {
  it('asks before leaving a pasted source behind', async () => {
    const { user } = renderNew()
    await paste(user, 'cube(1);\n')

    await user.click(screen.getByRole('link', { name: 'Models' }))
    const dialog = await screen.findByRole('dialog', { name: 'Leave without saving?' })
    await user.click(within(dialog).getByRole('button', { name: 'Stay' }))
    expect(screen.getByLabelText('OpenSCAD source')).toHaveValue('cube(1);\n')

    await user.click(screen.getByRole('link', { name: 'Models' }))
    await user.click(await screen.findByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByRole('heading', { name: 'Catalogue' })).toBeInTheDocument()
  })

  it('leaves an empty page without asking', async () => {
    const { user } = renderNew()
    await user.click(screen.getByRole('link', { name: 'Models' }))
    expect(await screen.findByRole('heading', { name: 'Catalogue' })).toBeInTheDocument()
  })
})

describe('NewModelPage', () => {
  it('abandons a superseded check on the wire, not just on arrival', async () => {
    // The server checks under a small concurrency budget, so a check nobody is
    // waiting for any more must release its permit.
    const seen: AbortSignal[] = []
    server.use(
      http.post('*/api/v1/models/check', async ({ request }) => {
        seen.push(request.signal)
        await new Promise((resolve) => setTimeout(resolve, 10_000))
        return HttpResponse.json({ ok: true, checked: true, diagnostics: [], parameters: 0 })
      }),
    )

    const { user, unmount } = renderNew()
    await paste(user, keychainSource)
    await waitFor(() => expect(seen).toHaveLength(1), { timeout: 3_000 })
    const inflight = seen[0]

    unmount()
    await waitFor(() => expect(inflight?.aborted).toBe(true), { timeout: 3_000 })
  })

  it('will not save without a name and some source', async () => {
    const { user } = renderNew()
    const save = screen.getByRole('button', { name: 'Save and customize' })
    expect(save).toBeDisabled()

    await paste(user, keychainSource)
    expect(save).toBeDisabled()

    await user.type(screen.getByLabelText('Name'), 'Pasted Keychain')
    expect(save).toBeEnabled()
  })

  it('checks the source as it settles, without saving it', async () => {
    const { user } = renderNew()
    await paste(user, keychainSource)

    expect(await screen.findByText(/^Parses cleanly/)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Customizer' })).not.toBeInTheDocument()
  })

  it('reports the derived parameter count alongside a clean parse', async () => {
    const { user } = renderNew()
    await paste(user, keychainSource)

    const derived = keychainSchema.parameters?.length ?? 0
    expect(
      await screen.findByText(`Parses cleanly — ${derived} parameters.`),
    ).toBeInTheDocument()
  })

  it('reports the failing line and hands it to the editor as a marker', async () => {
    const { user } = renderNew()
    await paste(user, BROKEN_SOURCE)

    const report = await screen.findByTestId('check-report')
    expect(report).toHaveTextContent('Line 2')
    expect(report).toHaveTextContent('Parser error: syntax error')
    expect(screen.getByLabelText('OpenSCAD source')).toHaveAttribute('data-marked-lines', '2')
  })

  it('drops a verdict the moment the source it judged changes', async () => {
    const { user } = renderNew()
    await paste(user, BROKEN_SOURCE)
    await screen.findByTestId('check-report')

    await user.type(screen.getByLabelText('OpenSCAD source'), ']')
    expect(screen.queryByTestId('check-report')).not.toBeInTheDocument()
  })

  it('saves a pasted model and opens its customizer', async () => {
    const { user } = renderNew()
    await user.type(screen.getByLabelText('Name'), 'Pasted Keychain')
    await paste(user, keychainSource)

    await user.click(screen.getByRole('button', { name: 'Save and customize' }))
    expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
  })

  it('refuses a save that does not parse until it is forced', async () => {
    const { user } = renderNew()
    await user.type(screen.getByLabelText('Name'), 'Half Cube')
    await paste(user, BROKEN_SOURCE)

    await user.click(screen.getByRole('button', { name: 'Save and customize' }))
    expect(await screen.findByTestId('check-report')).toHaveTextContent('Line 2')
    expect(screen.queryByRole('heading', { name: 'Customizer' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Save anyway' }))
    expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
  })

  it('does not offer a forced save without a name', async () => {
    const { user } = renderNew()
    await user.type(screen.getByLabelText('Name'), 'Half Cube')
    await paste(user, BROKEN_SOURCE)
    await user.click(screen.getByRole('button', { name: 'Save and customize' }))
    expect(await screen.findByRole('button', { name: 'Save anyway' })).toBeEnabled()

    await user.clear(screen.getByLabelText('Name'))
    expect(screen.getByRole('button', { name: 'Save anyway' })).toBeDisabled()
  })

  it('reports a name that yields no slug', async () => {
    const { user } = renderNew()
    await user.type(screen.getByLabelText('Name'), '***')
    await paste(user, keychainSource)

    await user.click(screen.getByRole('button', { name: 'Save and customize' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('slug'))
  })

  describe('libraries the source uses (#169)', () => {
    const BOLTED = 'use <BOSL2/std.scad>\ninclude <parts.scad>\ncuboid([10, 10, 2]);\n'

    function watchCreates(): { libraries?: string[] }[] {
      const bodies: { libraries?: string[] }[] = []
      server.use(
        http.post('*/api/v1/models', async ({ request }) => {
          bodies.push((await request.clone().json()) as { libraries?: string[] })
          // Nothing returned: the default handler answers.
        }),
      )
      return bodies
    }

    it('suggests the curated libraries its use/include lines name, ticked', async () => {
      const { user } = renderNew()
      await paste(user, BOLTED)

      const suggestion = await screen.findByRole('checkbox', { name: /BOSL2/ })
      expect(suggestion).toBeChecked()
      expect(screen.getByTestId('detected-libraries')).toHaveTextContent('v2.0.761')
      expect(screen.queryByRole('checkbox', { name: /parts/ })).not.toBeInTheDocument()
    })

    it('suggests nothing for a source that uses no library', async () => {
      const { user } = renderNew()
      await paste(user, keychainSource)
      await screen.findByText(/^Parses cleanly/)

      expect(screen.queryByTestId('detected-libraries')).not.toBeInTheDocument()
    })

    it('pins the ticked suggestions when it saves', async () => {
      const bodies = watchCreates()
      const { user } = renderNew()
      await user.type(screen.getByLabelText('Name'), 'Bolted')
      await paste(user, BOLTED)
      await screen.findByRole('checkbox', { name: /BOSL2/ })

      await user.click(screen.getByRole('button', { name: 'Save and customize' }))
      expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
      expect(bodies.map((body) => body.libraries)).toEqual([['BOSL2']])
    })

    it('suggests nothing, and still saves, when the catalogue cannot be fetched', async () => {
      server.use(
        http.get('*/api/v1/libraries', () =>
          HttpResponse.json(
            { title: 'Internal Server Error', status: 500, detail: 'boom' },
            { status: 500, headers: { 'Content-Type': 'application/problem+json' } },
          ),
        ),
      )
      const bodies = watchCreates()
      const { user } = renderNew()
      await user.type(screen.getByLabelText('Name'), 'Bolted')
      await paste(user, BOLTED)
      await screen.findByText(/^Parses cleanly/)

      expect(screen.queryByTestId('detected-libraries')).not.toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Save and customize' }))
      expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
      expect(bodies.map((body) => body.libraries)).toEqual([[]])
    })

    it('leaves out a suggestion that was unticked', async () => {
      const bodies = watchCreates()
      const { user } = renderNew()
      await user.type(screen.getByLabelText('Name'), 'Bolted')
      await paste(user, BOLTED)
      await user.click(await screen.findByRole('checkbox', { name: /BOSL2/ }))

      await user.click(screen.getByRole('button', { name: 'Save and customize' }))
      expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
      expect(bodies.map((body) => body.libraries)).toEqual([[]])
    })
  })

  it('says a check that timed out timed out, rather than blaming the syntax', async () => {
    server.use(
      http.post('/api/v1/models/check', () =>
        HttpResponse.json({
          ok: false,
          checked: true,
          timed_out: true,
          diagnostics: [{ severity: 'error', message: 'the check timed out after 120s' }],
          log_tail: [],
        }),
      ),
    )

    const { user } = renderNew()
    await paste(user, keychainSource)

    expect(await screen.findByText(/check timed out/)).toBeInTheDocument()
    expect(screen.queryByText('OpenSCAD could not parse this.')).not.toBeInTheDocument()
  })

  it('shows a save refused on a timeout as a timeout', async () => {
    server.use(
      http.post('/api/v1/models', () =>
        HttpResponse.json(
          {
            title: 'Unprocessable Content',
            status: 422,
            detail: 'the parse check timed out',
            timed_out: true,
            diagnostics: [{ severity: 'error', message: 'the check timed out after 120s' }],
            log_tail: [],
          },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )

    const { user } = renderNew()
    await user.type(screen.getByLabelText('Name'), 'Slow Model')
    await paste(user, keychainSource)
    await screen.findByText(/^Parses cleanly/)

    await user.click(screen.getByRole('button', { name: 'Save and customize' }))
    expect(await screen.findByText(/check timed out/)).toBeInTheDocument()
  })
})
