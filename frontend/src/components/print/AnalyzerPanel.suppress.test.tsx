import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import type {
  AnalysisRequest,
  AnalyzerDecision,
  AnalyzerDiagnostic,
  DecisionCreate,
  ScopeRef,
} from '../../api/types'
import {
  analysisReport,
  analysisScopes,
  openEdgesDiagnostic,
  overhangDiagnostic,
} from '../../mocks/analyzers'
import { storedDecisions } from '../../mocks/features/analyzers'
import * as fixtures from '../../mocks/fixtures'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AnalyzerPanel } from './AnalyzerPanel'
import { SuppressForm } from './SuppressForm'

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

/** A suppression of SB1002:part-2 already stored, as another dialog or the agent leaves it. */
function stored(scope: ScopeRef, reason: string, over: Partial<DecisionCreate> = {}) {
  return api.createDecision({
    diagnostic_id: 'SB1002',
    instance: 'SB1002:part-2',
    kind: 'suppress',
    scope,
    reason,
    enforced: false,
    confirm: false,
    ...over,
  })
}

const setAside = () => within(screen.getByTestId('checks-set-aside'))

afterEach(() => server.events.removeAllListeners())

describe('AnalyzerPanel · suppress at a scope', () => {
  it('needs a reason, then suppresses this finding at the chosen scope', async () => {
    const { posted } = watchDecisions()
    const { user } = renderPanel()
    const row = await screen.findByTestId('diagnostic-SB1002:part-2')
    await user.click(within(row).getByRole('button', { name: 'Suppress…' }))

    const form = within(row).getByRole('form', { name: 'Suppress SB1002:part-2' })
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
    const form = within(row).getByRole('form', { name: 'Suppress SB1002:part-2' })
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
    const form = within(row).getByRole('form', { name: 'Suppress SB1002:part-2' })
    await user.type(within(form).getByLabelText('Reason'), 'for now')
    await user.click(within(form).getByRole('button', { name: 'Suppress' }))
    await waitFor(() => expect(screen.queryByTestId('diagnostic-SB1002:part-2')).toBeNull())

    await user.click(screen.getByText('1 not shown'))
    await user.click(
      within(screen.getByTestId('checks-set-aside')).getByRole('button', {
        name: 'Remove the suppression of SB1002:part-2',
      }),
    )
    expect(await screen.findByTestId('diagnostic-SB1002:part-2')).toBeVisible()
    expect(deleted).toHaveLength(1)
  })

  it('shows why a removal was refused, and keeps the row with Remove offered again', async () => {
    await stored({ kind: 'print', key: output.id }, 'for now')
    server.use(
      http.delete('/api/v1/analyzers/decisions/:id', () =>
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
    await user.click(await screen.findByText('1 not shown'))
    const remove = setAside().getByRole('button', { name: 'Remove the suppression of SB1002:part-2' })
    await user.click(remove)
    expect(await setAside().findByRole('alert')).toHaveTextContent(
      'the analyzer decision store cannot be reached (OperationalError)',
    )
    expect(setAside().getByText(/suppressed for This print: for now/)).toBeVisible()
    expect(remove).toBeEnabled()
    expect(screen.queryByTestId('diagnostic-SB1002:part-2')).toBeNull()
  })

  it('offers Remove again for the wider suppression beneath the one removed', async () => {
    await stored({ kind: 'template', key: output.slug }, 'second')
    await stored({ kind: 'print', key: output.id }, 'first')
    const { user } = renderPanel()
    await user.click(await screen.findByText('1 not shown'))
    expect(setAside().getByText(/suppressed for This print: first/)).toBeVisible()
    await user.click(setAside().getByRole('button', { name: 'Remove the suppression of SB1002:part-2' }))
    expect(await setAside().findByText(/suppressed for This template: second/)).toBeVisible()
    expect(setAside().getByRole('button', { name: 'Remove the suppression of SB1002:part-2' })).toBeEnabled()
  })

  it('asks before removing a suppression wider than the template', async () => {
    const { deleted } = watchDecisions()
    await stored({ kind: 'global', key: '' }, 'everywhere')
    const { user } = renderPanel()
    await user.click(await screen.findByText('1 not shown'))
    await user.click(setAside().getByRole('button', { name: 'Remove the suppression of SB1002:part-2' }))
    const ask = setAside().getByRole('group', { name: 'Remove the suppression of SB1002:part-2?' })
    expect(ask).toHaveTextContent('Remove it for every print?')
    await user.click(within(ask).getByRole('button', { name: 'Keep the suppression of SB1002:part-2' }))
    expect(deleted).toHaveLength(0)
    expect(setAside().getByRole('button', { name: 'Remove the suppression of SB1002:part-2' })).toBeEnabled()

    await user.click(setAside().getByRole('button', { name: 'Remove the suppression of SB1002:part-2' }))
    await user.click(
      setAside().getByRole('button', { name: 'Confirm removing the suppression of SB1002:part-2' }),
    )
    expect(await screen.findByTestId('diagnostic-SB1002:part-2')).toBeVisible()
    expect(deleted).toHaveLength(1)
  })

  it('does not remove an enforced suppression from the print dialog', async () => {
    await stored({ kind: 'template', key: output.slug }, 'policy', { enforced: true })
    const { user } = renderPanel()
    await user.click(await screen.findByText('1 not shown'))
    expect(setAside().queryByRole('button', { name: /Remove/ })).toBeNull()
    expect(setAside().getByText('(enforced, so not removable from here)')).toBeVisible()
  })

  it('offers a finding with an accepted fix only scopes at or under the acceptance', async () => {
    const accept: AnalyzerDecision = {
      id: 'c'.repeat(32),
      diagnostic_id: 'SB1002',
      instance: 'SB1002:part-2',
      kind: 'accept',
      scope: { kind: 'template', key: output.slug },
      reason: null,
      enforced: false,
      fix_id: 'none',
      created_at: '2026-09-28T00:00:00Z',
    }
    server.use(
      http.post('/api/v1/analyzers/run', () =>
        HttpResponse.json(analysisReport(output, request, [openEdgesDiagnostic], [accept])),
      ),
    )
    const { posted } = watchDecisions()
    const { user } = renderPanel()
    const row = await screen.findByTestId('diagnostic-SB1002:part-2')
    await user.click(within(row).getByRole('button', { name: 'Suppress…' }))
    const form = within(row).getByRole('form', { name: 'Suppress SB1002:part-2' })
    expect(within(form).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'This template',
      'These parameters',
      'This print',
    ])
    expect(form).toHaveTextContent('A fix is accepted for this template')

    const every = within(form).getByRole('checkbox', { name: 'Every SB1002 finding' })
    await user.click(every)
    expect(every).toBeChecked()
    await user.selectOptions(within(form).getByLabelText('Scope'), 'This template')
    expect(form).toHaveTextContent('This replaces the fix accepted for this template')
    expect(every).toBeDisabled()
    expect(every).not.toBeChecked()
    await user.type(within(form).getByLabelText('Reason'), 'rather not')
    await user.click(within(form).getByRole('button', { name: 'Suppress' }))
    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0]).toMatchObject({
      instance: 'SB1002:part-2',
      scope: { kind: 'template', key: output.slug },
    })
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
    const form = within(row).getByRole('form', { name: 'Suppress SB1002:part-2' })
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

  it('offers a finding about some slots no material scope, and a whole-print one each', async () => {
    const slotTwo: AnalyzerDiagnostic = { ...openEdgesDiagnostic, slots: [2] }
    server.use(
      http.post('/api/v1/analyzers/run', () =>
        HttpResponse.json(analysisReport(output, request, [overhangDiagnostic, slotTwo])),
      ),
    )
    const { user } = renderPanel()
    const options = async (key: string) => {
      const row = await screen.findByTestId(`diagnostic-${key}`)
      await user.click(within(row).getByRole('button', { name: 'Suppress…' }))
      const form = within(row).getByRole('form', { name: `Suppress ${key}` })
      return within(form)
        .getAllByRole('option')
        .map((option) => option.textContent)
    }
    const slotted = await options('SB1002:part-2')
    expect(slotted).not.toContain('Every pla print')
    expect(slotted).not.toContain('Every petg print')
    expect(slotted).toContain('Every print')
    const whole = await options('SB1003')
    expect(whole).toEqual(expect.arrayContaining(['Every pla print', 'Every petg print']))
  })
  it('names each instance of one rule on its own, open and suppressed', async () => {
    const partThree: AnalyzerDiagnostic = {
      ...openEdgesDiagnostic,
      key: 'SB1002:part-3',
      location: { kind: 'mesh', part: 3, colour: 'Red', edges_truncated: false },
    }
    server.use(
      http.post('/api/v1/analyzers/run', () =>
        HttpResponse.json(
          analysisReport(output, request, [openEdgesDiagnostic, partThree], storedDecisions()),
        ),
      ),
    )
    const { user } = renderPanel()
    for (const key of ['SB1002:part-2', 'SB1002:part-3']) {
      const row = await screen.findByTestId(`diagnostic-${key}`)
      await user.click(within(row).getByRole('button', { name: 'Suppress…' }))
    }
    // Both forms are open at once; each is found by its own instance, not the rule.
    for (const key of ['SB1002:part-2', 'SB1002:part-3']) {
      const form = screen.getByRole('form', { name: `Suppress ${key}` })
      expect(screen.getByTestId(`diagnostic-${key}`)).toContainElement(form)
      await user.type(within(form).getByLabelText('Reason'), 'hollow by design')
      await user.click(within(form).getByRole('button', { name: 'Suppress' }))
      await waitFor(() => expect(screen.queryByTestId(`diagnostic-${key}`)).toBeNull())
    }

    await user.click(screen.getByText('2 not shown'))
    const removes = setAside().getAllByRole('button', { name: /^Remove the suppression of / })
    expect(removes.map((button) => button.getAttribute('aria-label'))).toEqual([
      'Remove the suppression of SB1002:part-2',
      'Remove the suppression of SB1002:part-3',
    ])
    await user.click(setAside().getByRole('button', { name: 'Remove the suppression of SB1002:part-3' }))
    expect(await screen.findByTestId('diagnostic-SB1002:part-3')).toBeVisible()
    expect(screen.queryByTestId('diagnostic-SB1002:part-2')).toBeNull()
  })
})

describe('SuppressForm', () => {
  const scopesFor = (printerId: number): ScopeRef[] =>
    analysisScopes(output, { ...request, printer_id: printerId })

  it('posts this print, not a printer the report no longer lists', async () => {
    const { posted } = watchDecisions()
    const user = userEvent.setup()
    const props = { diagnostic: openEdgesDiagnostic, onDone: () => {}, onCancel: () => {} }
    const view = render(<SuppressForm {...props} scopes={scopesFor(1)} />)
    await user.selectOptions(screen.getByLabelText('Scope'), 'This printer (#1)')
    view.rerender(<SuppressForm {...props} scopes={scopesFor(2)} />)
    expect(screen.getByLabelText('Scope')).toHaveDisplayValue('This print')
    await user.type(screen.getByLabelText('Reason'), 'switched printers')
    await user.click(screen.getByRole('button', { name: 'Suppress' }))
    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0]).toMatchObject({ scope: { kind: 'print', key: output.id } })
  })

  it('asks before suppressing a problem wider than the template', async () => {
    const { posted } = watchDecisions()
    const user = userEvent.setup()
    const problem: AnalyzerDiagnostic = { ...openEdgesDiagnostic, severity: 'error' }
    render(
      <SuppressForm diagnostic={problem} scopes={scopesFor(1)} onDone={() => {}} onCancel={() => {}} />,
    )
    const submit = screen.getByRole('button', { name: 'Suppress' })
    await user.type(screen.getByLabelText('Reason'), 'known')
    expect(submit).toBeEnabled()
    expect(screen.queryByRole('checkbox', { name: /is a problem/ })).toBeNull()

    await user.selectOptions(screen.getByLabelText('Scope'), 'Every print')
    expect(submit).toBeDisabled()
    await user.click(
      screen.getByRole('checkbox', {
        name: 'SB1002 is a problem. Suppress it for every print, not only this template?',
      }),
    )
    expect(submit).toBeEnabled()
    await user.selectOptions(screen.getByLabelText('Scope'), 'Every pla print')
    expect(submit).toBeDisabled()
    await user.selectOptions(screen.getByLabelText('Scope'), 'This template')
    expect(submit).toBeEnabled()
    await user.click(submit)
    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0]).toMatchObject({ scope: { kind: 'template', key: output.slug } })
  })
})
