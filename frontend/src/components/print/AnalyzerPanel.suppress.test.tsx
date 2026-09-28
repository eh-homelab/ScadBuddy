import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import type { AnalysisRequest } from '../../api/types'
import { analysisReport, overhangDiagnostic } from '../../mocks/analyzers'
import * as fixtures from '../../mocks/fixtures'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AnalyzerPanel } from './AnalyzerPanel'

const output = fixtures.outputs[0]!
const request: AnalysisRequest = {
  printer_id: 1,
  plate_id: 1,
  choices: {
    nozzles: [{ size: '0.4', flow: 'standard' }],
    tier: 'standard',
    bed_type: 'Textured PEI Plate',
  },
}

/** Every body posted to `/analyzers/decisions`, and every decision deleted. */
function watchDecisions() {
  const posted: Record<string, unknown>[] = []
  const deleted: string[] = []
  server.events.on('request:start', async ({ request: sent }) => {
    const path = new URL(sent.url).pathname
    if (sent.method === 'POST' && path === '/api/v1/analyzers/decisions') {
      posted.push((await sent.clone().json()) as Record<string, unknown>)
    }
    if (sent.method === 'DELETE' && path.startsWith('/api/v1/analyzers/decisions/')) {
      deleted.push(path.split('/').at(-1)!)
    }
  })
  return { posted, deleted }
}

function renderPanel() {
  return renderPage(<AnalyzerPanel outputId={output.id} request={request} />)
}

afterEach(() => server.events.removeAllListeners())

describe('AnalyzerPanel · suppress at a scope', () => {
  it('needs a reason, then suppresses this finding at the chosen scope', async () => {
    const { posted } = watchDecisions()
    const { user } = renderPanel()
    const row = await screen.findByTestId('diagnostic-SB1002:part-2')
    await user.click(within(row).getByRole('button', { name: 'Suppress…' }))

    const form = within(row).getByRole('form', { name: 'Suppress SB1002' })
    const submit = within(form).getByRole('button', { name: 'Suppress' })
    // The narrowest scope is the default, as simple mode's is in #284.
    expect(within(form).getByLabelText('Scope')).toHaveDisplayValue('This print')
    expect(submit).toBeDisabled()
    await user.type(within(form).getByLabelText('Reason'), '   ')
    expect(submit).toBeDisabled()

    await user.selectOptions(within(form).getByLabelText('Scope'), 'This template')
    await user.type(within(form).getByLabelText('Reason'), 'the seam is inside the ring')
    await user.click(submit)

    await waitFor(() => expect(screen.queryByTestId('diagnostic-SB1002:part-2')).toBeNull())
    expect(posted).toEqual([
      {
        diagnostic_id: 'SB1002',
        instance: 'SB1002:part-2',
        kind: 'suppress',
        scope: { kind: 'template', key: 'name-keychain' },
        reason: 'the seam is inside the ring',
        enforced: false,
        confirm: false,
      },
    ])
    await user.click(screen.getByText('1 not shown'))
    expect(screen.getByTestId('checks-set-aside')).toHaveTextContent(
      'suppressed for This template: the seam is inside the ring',
    )
    expect(screen.getByTestId('checks-headline')).toHaveTextContent('1 suggestion')
  })

  it('suppresses every finding of the rule when asked', async () => {
    const { posted } = watchDecisions()
    const { user } = renderPanel()
    const row = await screen.findByTestId('diagnostic-SB1002:part-2')
    await user.click(within(row).getByRole('button', { name: 'Suppress…' }))
    const form = within(row).getByRole('form', { name: 'Suppress SB1002' })
    await user.click(within(form).getByRole('checkbox', { name: 'Every SB1002 finding' }))
    await user.type(within(form).getByLabelText('Reason'), 'hollow by design')
    await user.click(within(form).getByRole('button', { name: 'Suppress' }))
    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0]).toMatchObject({ instance: null, scope: { kind: 'print', key: output.id } })
  })

  it('removes a suppression, so the finding is listed again', async () => {
    const { deleted } = watchDecisions()
    const { user } = renderPanel()
    const row = await screen.findByTestId('diagnostic-SB1002:part-2')
    await user.click(within(row).getByRole('button', { name: 'Suppress…' }))
    const form = within(row).getByRole('form', { name: 'Suppress SB1002' })
    await user.type(within(form).getByLabelText('Reason'), 'for now')
    await user.click(within(form).getByRole('button', { name: 'Suppress' }))
    await waitFor(() => expect(screen.queryByTestId('diagnostic-SB1002:part-2')).toBeNull())

    await user.click(screen.getByText('1 not shown'))
    await user.click(
      within(screen.getByTestId('checks-set-aside')).getByRole('button', {
        name: 'Remove the suppression of SB1002',
      }),
    )
    expect(await screen.findByTestId('diagnostic-SB1002:part-2')).toBeVisible()
    expect(deleted).toHaveLength(1)
  })

  it('shows why a suppression was refused, and keeps the form', async () => {
    server.use(
      http.post('/api/v1/analyzers/decisions', () =>
        HttpResponse.json(
          {
            title: 'Service Unavailable',
            status: 503,
            detail: 'the analyzer decision store cannot be reached (OperationalError)',
          },
          { status: 503 },
        ),
      ),
    )
    const { user } = renderPanel()
    const row = await screen.findByTestId('diagnostic-SB1002:part-2')
    await user.click(within(row).getByRole('button', { name: 'Suppress…' }))
    const form = within(row).getByRole('form', { name: 'Suppress SB1002' })
    await user.type(within(form).getByLabelText('Reason'), 'r')
    await user.click(within(form).getByRole('button', { name: 'Suppress' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent(
      'the analyzer decision store cannot be reached (OperationalError)',
    )
    expect(screen.getByTestId('diagnostic-SB1002:part-2')).toBeVisible()
  })

  it('offers no suppression when decisions cannot be stored', async () => {
    server.use(
      http.post('/api/v1/analyzers/run', () =>
        HttpResponse.json({
          ...analysisReport(output, request, [overhangDiagnostic]),
          decisions_available: false,
          decisions_reason: 'the decision store cannot be reached (OperationalError)',
        }),
      ),
    )
    renderPanel()
    await screen.findByTestId('diagnostic-SB1003')
    expect(screen.queryByRole('button', { name: 'Suppress…' })).toBeNull()
  })
})
