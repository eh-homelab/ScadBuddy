import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it } from 'vitest'
import type { AnalysisRequest, AnalyzerDiagnostic } from '../../api/types'
import { analysisReport, overhangDiagnostic, verifiedFixDiagnostic } from '../../mocks/analyzers'
import * as fixtures from '../../mocks/fixtures'
import { setMockAnalyzerDiagnostics } from '../../mocks/features/analyzers'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AnalyzerPanel } from './AnalyzerPanel'

const output = fixtures.outputs[0]!

type Size = NonNullable<AnalysisRequest['choices']>['nozzles'][number]['size']

function request(size: Size = '0.4'): AnalysisRequest {
  return {
    printer_id: 1,
    plate_id: 1,
    all_plates: false,
    choices: {
      nozzles: [{ size, flow: 'standard' }],
      tier: 'standard',
      bed_type: 'Textured PEI Plate',
    },
  }
}

function watchApplies() {
  const bodies: Record<string, unknown>[] = []
  server.events.on('request:start', async ({ request: sent }) => {
    if (sent.method === 'POST' && new URL(sent.url).pathname === '/api/v1/analyzers/fixes/apply') {
      bodies.push((await sent.clone().json()) as Record<string, unknown>)
    }
  })
  return bodies
}

function renderPanel(size: Size = '0.4') {
  return renderPage(<AnalyzerPanel outputId={output.id} request={request(size)} />)
}

afterEach(() => server.events.removeAllListeners())

describe('AnalyzerPanel · fixes', () => {
  it('previews a fix as a cited diff, and holds Apply while its target is unverified', async () => {
    const { user } = renderPanel()
    const row = await screen.findByTestId('diagnostic-SB1003')
    await user.click(within(row).getByRole('button', { name: 'Preview fix: Turn on supports' }))

    const preview = await within(row).findByTestId('fix-preview-enable-support')
    const line = within(preview).getByTestId('change-enable_support')
    expect(line).toHaveTextContent('enable_support')
    expect(line).toHaveTextContent('unknown')
    expect(line).toHaveTextContent('1')
    expect(line).toHaveTextContent('Derived process preset')
    expect(preview).toHaveTextContent('every BBL system process preset inherits')
    expect(
      within(preview).getByRole('link', { name: /fdm_process_common\.json/ }),
    ).toHaveAttribute('target', '_blank')
    expect(within(preview).getByTestId('fix-blockers')).toHaveTextContent(
      "Whether Bambuddy's /local-presets/ can create a process preset",
    )
    expect(preview).toHaveTextContent('nothing sends it yet')
    expect(within(preview).getByRole('button', { name: 'Apply' })).toBeDisabled()
  })

  it('applies a previewed fix with its fingerprint, and shows it accepted', async () => {
    setMockAnalyzerDiagnostics([verifiedFixDiagnostic])
    const applies = watchApplies()
    const { user } = renderPanel()
    const row = await screen.findByTestId('diagnostic-SB9901')
    await user.click(within(row).getByRole('button', { name: 'Preview fix: Record a timelapse' }))
    const preview = await within(row).findByTestId('fix-preview-timelapse-on')
    expect(within(preview).getByTestId('change-timelapse')).toHaveTextContent('false')
    await user.selectOptions(within(preview).getByLabelText('Accept for'), 'This template')
    await waitFor(() => expect(within(preview).getByRole('button', { name: 'Apply' })).toBeEnabled())
    await user.click(within(preview).getByRole('button', { name: 'Apply' }))

    expect(await screen.findByTestId('fix-accepted-SB9901')).toHaveTextContent(
      'Fix accepted for This template: Record a timelapse',
    )
    expect(applies).toHaveLength(1)
    expect(applies[0]).toMatchObject({
      target: { output_id: output.id },
      diagnostic_key: 'SB9901',
      fix_id: 'timelapse-on',
      scope: { kind: 'template', key: 'name-keychain' },
      confirm: true,
    })
    expect(applies[0]!.fingerprint).toMatch(/^[0-9a-f]{64}$/)
    expect(screen.getByTestId('accepted-changes')).toHaveTextContent(
      '1 accepted change is recorded; nothing sends it to Bambuddy yet.',
    )
  })

  it('offers a finding about some slots no material scope to accept at', async () => {
    const options = async (slots: number[]) => {
      setMockAnalyzerDiagnostics([{ ...verifiedFixDiagnostic, slots }])
      const { user, unmount } = renderPanel()
      const row = await screen.findByTestId('diagnostic-SB9901')
      await user.click(within(row).getByRole('button', { name: 'Preview fix: Record a timelapse' }))
      const preview = await within(row).findByTestId('fix-preview-timelapse-on')
      const labels = within(within(preview).getByLabelText('Accept for'))
        .getAllByRole('option')
        .map((option) => option.textContent)
      unmount()
      return labels
    }

    expect(await options([])).toEqual(expect.arrayContaining(['Every pla print', 'Every petg print']))
    const slotted = await options([2])
    expect(slotted.some((label) => /^Every (pla|petg) print$/.test(label ?? ''))).toBe(false)
    expect(slotted).toContain('This template')
  })

  it('marks a preview stale when the print changes under it, and previews again', async () => {
    setMockAnalyzerDiagnostics([verifiedFixDiagnostic])
    const { user, rerender } = renderPanel()
    const row = await screen.findByTestId('diagnostic-SB9901')
    await user.click(within(row).getByRole('button', { name: 'Preview fix: Record a timelapse' }))
    const preview = await within(row).findByTestId('fix-preview-timelapse-on')
    await waitFor(() => expect(within(preview).getByRole('button', { name: 'Apply' })).toBeEnabled())

    rerender(
      <MemoryRouter>
        <AnalyzerPanel outputId={output.id} request={request('0.6')} />
      </MemoryRouter>,
    )
    expect(await within(preview).findByTestId('fix-stale')).toHaveTextContent(/Stale/)
    expect(within(preview).getByRole('button', { name: 'Apply' })).toBeDisabled()

    await user.click(within(preview).getByRole('button', { name: 'Preview again' }))
    await waitFor(() => expect(within(preview).queryByTestId('fix-stale')).toBeNull())
    expect(within(preview).getByRole('button', { name: 'Apply' })).toBeEnabled()
  })

  it('shows the backend refusing a stale fingerprint as stale', async () => {
    setMockAnalyzerDiagnostics([verifiedFixDiagnostic])
    server.use(
      http.post('/api/v1/analyzers/fixes/apply', () =>
        HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/analyzer-fix-stale',
            title: 'Conflict',
            status: 409,
            detail: 'the diff, its scope, the print or its base differ from the preview; preview it again',
          },
          { status: 409 },
        ),
      ),
    )
    const { user } = renderPanel()
    const row = await screen.findByTestId('diagnostic-SB9901')
    await user.click(within(row).getByRole('button', { name: 'Preview fix: Record a timelapse' }))
    const preview = await within(row).findByTestId('fix-preview-timelapse-on')
    await waitFor(() => expect(within(preview).getByRole('button', { name: 'Apply' })).toBeEnabled())
    await user.click(within(preview).getByRole('button', { name: 'Apply' }))
    expect(await within(preview).findByTestId('fix-stale')).toHaveTextContent(
      'the print or its base differ from the preview',
    )
    expect(within(preview).getByRole('button', { name: 'Apply' })).toBeDisabled()
  })

  it('drops an apply refusal when the scope changes, since that previews anew', async () => {
    setMockAnalyzerDiagnostics([verifiedFixDiagnostic])
    server.use(
      http.post('/api/v1/analyzers/fixes/apply', () =>
        HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/analyzer-fix-unverified',
            title: 'Conflict',
            status: 409,
            detail: 'this fix cannot be applied until where its settings land is verified',
            to_verify: ['Whether the queue item takes timelapse'],
          },
          { status: 409 },
        ),
      ),
    )
    const { user } = renderPanel()
    const row = await screen.findByTestId('diagnostic-SB9901')
    await user.click(within(row).getByRole('button', { name: 'Preview fix: Record a timelapse' }))
    const preview = await within(row).findByTestId('fix-preview-timelapse-on')
    await waitFor(() => expect(within(preview).getByRole('button', { name: 'Apply' })).toBeEnabled())
    await user.click(within(preview).getByRole('button', { name: 'Apply' }))
    expect(await within(preview).findByRole('alert')).toHaveTextContent(
      'until where its settings land is verified — Whether the queue item takes timelapse',
    )

    await user.selectOptions(within(preview).getByLabelText('Accept for'), 'This template')
    expect(within(preview).queryByRole('alert')).toBeNull()
    await waitFor(() => expect(within(preview).getByRole('button', { name: 'Apply' })).toBeEnabled())
    expect(within(preview).queryByRole('alert')).toBeNull()
  })

  it('says an accepted fix went stale, and lets it be removed', async () => {
    const stale: AnalyzerDiagnostic = {
      ...overhangDiagnostic,
      decision: {
        stale: true,
        decision: {
          id: 'e'.repeat(32),
          diagnostic_id: 'SB1003',
          instance: 'SB1003',
          kind: 'accept',
          scope: { kind: 'template', key: 'name-keychain' },
          enforced: false,
          fix_id: 'enable-support',
          diff_digest: '0'.repeat(64),
        },
      },
    }
    const removed: string[] = []
    server.use(
      http.post('/api/v1/analyzers/run', async ({ request: sent }) => {
        const body = (await sent.json()) as { request: AnalysisRequest }
        return HttpResponse.json(analysisReport(output, body.request, [stale]))
      }),
      http.delete('/api/v1/analyzers/decisions/:id', ({ params }) => {
        removed.push(String(params['id']))
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { user } = renderPanel()
    const notice = await screen.findByTestId('fix-stale-SB1003')
    expect(notice).toHaveTextContent(
      'The fix accepted for This template is stale: its diff has changed since, so this finding is open again.',
    )
    await user.click(within(notice).getByRole('button', { name: 'Remove the accepted fix of SB1003' }))
    await waitFor(() => expect(removed).toEqual(['e'.repeat(32)]))
  })
})
