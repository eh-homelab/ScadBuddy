import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import type { AnalysisRequest, AnalyzerDiagnostic } from '../../api/types'
import {
  analysisReport,
  crashedDiagnostic,
  openEdgesDiagnostic,
  overhangDiagnostic,
} from '../../mocks/analyzers'
import * as fixtures from '../../mocks/fixtures'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AnalyzerPanel } from './AnalyzerPanel'

const output = fixtures.outputs[0]!
const request: AnalysisRequest = {
  printer_id: 1,
  plate_id: 1,
  all_plates: false,
  choices: {
    nozzles: [{ size: '0.4', flow: 'standard' }],
    tier: 'standard',
    bed_type: 'Textured PEI Plate',
  },
}

/** Answer the run with these findings, sorted and counted as the backend does. */
function reportWith(diagnostics: AnalyzerDiagnostic[], extra: object = {}) {
  server.use(
    http.post('/api/v1/analyzers/run', () =>
      HttpResponse.json({ ...analysisReport(output, request, diagnostics), ...extra }),
    ),
  )
}

function renderPanel(props: { allPlates?: boolean } = {}) {
  return renderPage(<AnalyzerPanel outputId={output.id} request={request} {...props} />)
}

describe('AnalyzerPanel', () => {
  it('lists each finding with its severity, code, message, location and cited sources', async () => {
    renderPanel()
    expect(await screen.findByTestId('checks-headline')).toHaveTextContent('2 suggestions')

    const overhang = screen.getByTestId('diagnostic-SB1003')
    expect(overhang).toHaveTextContent('Suggestion')
    expect(overhang).toHaveTextContent('SB1003')
    expect(overhang).toHaveTextContent('Overhangs past the support threshold')
    expect(overhang).toHaveTextContent(/38 mm² of overhang/)
    expect(overhang).toHaveTextContent('Where: a 18.0 × 8.0 × 1.0 mm region')

    const link = within(overhang).getByRole('link', {
      name: 'Bambu Studio PrintConfig.cpp: support_threshold_angle',
    })
    expect(link).toHaveAttribute('href', overhangDiagnostic.sources[0]!.url)
    // A new tab: inside Bambuddy's sandboxed iframe a same-frame link would replace the app.
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(overhang).toHaveTextContent(
      'Support will be generated for overhangs whose slope angle is below the threshold.',
    )

    expect(screen.getByTestId('diagnostic-SB1002:part-2')).toHaveTextContent(
      'Where: Part 2 (Black) · 3 located edges',
    )
  })

  it('lists a crashed analyzer as a finding of its own', async () => {
    reportWith([crashedDiagnostic('SB2001', 'Silk PLA'), openEdgesDiagnostic])
    renderPanel()
    const crashed = await screen.findByTestId('diagnostic-SB0001:SB2001')
    expect(crashed).toHaveTextContent('Warning')
    expect(crashed).toHaveTextContent('An analyzer failed')
    expect(crashed).toHaveTextContent('SB2001 (Silk PLA) failed and did not run: ValueError.')
    expect(crashed).toHaveTextContent('Where: The analyzer itself')
    expect(within(crashed).getByRole('link', { name: '#284 Print analyzers & fixers' })).toBeVisible()
  })

  it('says a problem is advisory, and never that it blocks Print', async () => {
    reportWith([{ ...openEdgesDiagnostic, id: 'SB1001', key: 'SB1001:part-2', severity: 'error' }])
    renderPanel()
    expect(await screen.findByTestId('checks-headline')).toHaveTextContent('1 problem')
    expect(screen.getByTestId('diagnostic-SB1001:part-2')).toHaveTextContent('Problem')
    expect(screen.getByTestId('print-checks')).toHaveTextContent(/none of these stops Print/)
  })

  it('shows a run that failed, and runs again on request', async () => {
    // Failing until the test says otherwise: the socket's resync on subscribing reads too.
    let failing = true
    server.use(
      http.post('/api/v1/analyzers/run', () => {
        return failing
          ? HttpResponse.json(
              { title: 'Internal Server Error', status: 500, detail: 'the geometry cache is corrupt' },
              { status: 500 },
            )
          : HttpResponse.json(analysisReport(output, request))
      }),
    )
    const { user } = renderPanel()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The checks could not run: the geometry cache is corrupt',
    )
    failing = false
    await user.click(screen.getByRole('button', { name: 'Check again' }))
    expect(await screen.findByTestId('diagnostic-SB1003')).toBeVisible()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says which checks were skipped and why', async () => {
    reportWith([], {
      skipped: [
        {
          id: 'SB3002',
          title: 'Not enough filament',
          missing: [{ name: 'inventory', available: false, reason: 'the output is not uploaded yet' }],
        },
      ],
    })
    const { user } = renderPanel()
    expect(await screen.findByTestId('checks-headline')).toHaveTextContent('Nothing to report')
    await user.click(screen.getByText('1 check did not run'))
    expect(screen.getByTestId('checks-skipped')).toHaveTextContent(
      'SB3002 Not enough filament: the output is not uploaded yet',
    )
  })

  it('sets suppressed and hidden findings aside, with the reason and the scope', async () => {
    reportWith([
      overhangDiagnostic,
      {
        ...openEdgesDiagnostic,
        status: 'suppressed',
        decision: {
          stale: false,
          decision: {
            id: 'f'.repeat(32),
            diagnostic_id: 'SB1002',
            instance: 'SB1002:part-2',
            kind: 'suppress',
            scope: { kind: 'template', key: 'name-keychain' },
            reason: 'the seam is inside the ring',
            enforced: false,
          },
        },
      },
    ])
    const { user } = renderPanel()
    await screen.findByTestId('diagnostic-SB1003')
    expect(screen.queryByTestId('diagnostic-SB1002:part-2')).toBeNull()
    await user.click(screen.getByText('1 not shown'))
    expect(screen.getByTestId('checks-set-aside')).toHaveTextContent(
      'SB1002 Open edges — suppressed for This template: the seam is inside the ring',
    )
  })

  it('names the plate the mesh checks read when every plate is printed', async () => {
    renderPanel({ allPlates: true })
    await screen.findByTestId('diagnostic-SB1003')
    expect(screen.getByTestId('print-checks')).toHaveTextContent(
      'The mesh checks read plate 1; the plate-fit and filament checks read every plate.',
    )
  })

  it('says why decisions were not applied when the store cannot be read', async () => {
    reportWith([overhangDiagnostic], {
      decisions_available: false,
      decisions_reason: 'the decision store cannot be reached (OperationalError)',
    })
    renderPanel()
    await waitFor(() =>
      expect(screen.getByTestId('print-checks')).toHaveTextContent(
        'the decision store cannot be reached (OperationalError)',
      ),
    )
  })
})
