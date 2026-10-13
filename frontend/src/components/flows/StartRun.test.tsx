import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { flowRequests } from '../../mocks/features/flows'
import { renderPage } from '../../test/utils'
import { StartRun } from './StartRun'

// #2483 — Start run: a flow with an inputs schema asks for them in a form drawn from it
// (src/mocks/features/flows.ts's `coasters`); one without starts at once.

function render(flowId: string, schema?: Record<string, unknown> | null) {
  return renderPage(
    <Routes>
      <Route path="/" element={<StartRun flowId={flowId} schema={schema} />} />
      <Route path="/workflows/runs/:id" element={<p>Run page</p>} />
    </Routes>,
  )
}

const starts = () => flowRequests().filter((r) => r.path.endsWith('/runs'))

describe('StartRun (#2483)', () => {
  it('starts a flow with no inputs at once', async () => {
    const { user } = render('def-swap', null)
    await user.click(screen.getByRole('button', { name: 'Start run' }))
    expect(await screen.findByText('Run page')).toBeInTheDocument()
    expect(starts().map((r) => r.body)).toEqual([{}])
  })

  it('asks for the inputs, at their defaults, and starts with them', async () => {
    const { user } = render('def-coasters')
    await user.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('dialog', { name: 'Start coasters' })
    expect(within(dialog).getByRole('spinbutton', { name: /Copies/ })).toHaveValue(2)
    expect(within(dialog).getByRole('combobox', { name: /Colour/ })).toHaveValue('red')
    expect(within(dialog).getByText('The template to render')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Start run' }))
    expect(within(dialog).getByText('Required')).toBeInTheDocument()
    expect(starts()).toEqual([])

    await user.type(within(dialog).getByRole('textbox', { name: /Template/ }), 'coaster')
    await user.selectOptions(within(dialog).getByRole('combobox', { name: /Colour/ }), 'blue')
    await user.click(within(dialog).getByRole('checkbox', { name: /Dry run/ }))
    await user.click(within(dialog).getByRole('button', { name: 'Start run' }))
    expect(await screen.findByText('Run page')).toBeInTheDocument()
    expect(starts().map((r) => r.body)).toEqual([
      { inputs: { slug: 'coaster', copies: 2, colour: 'blue', dry_run: true, plates: [1] } },
    ])
  })

  it('makes a required choice with no default be chosen, never shown chosen when it is not', async () => {
    const schema = {
      type: 'object',
      properties: { colour: { type: 'string', title: 'Colour', enum: ['red', 'blue'] } },
      required: ['colour'],
    }
    const { user } = renderPage(
      <Routes>
        <Route path="/" element={<StartRun flowId="def-swap" name="swap" schema={schema} />} />
        <Route path="/workflows/runs/:id" element={<p>Run page</p>} />
      </Routes>,
    )
    await user.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('dialog', { name: 'Start swap' })
    const colour = within(dialog).getByRole('combobox', { name: /Colour/ })
    expect(colour).toHaveValue('')
    expect(within(dialog).getByRole('option', { name: 'Choose…' })).toBeDisabled()
    await user.click(within(dialog).getByRole('button', { name: 'Start run' }))
    expect(within(dialog).getByText('Required')).toBeInTheDocument()
    await user.selectOptions(colour, 'red')
    await user.click(within(dialog).getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(starts().map((r) => r.body)).toEqual([{ inputs: { colour: 'red' } }]))
  })

  it("shows the server's refusal beside the form", async () => {
    const { user } = render('def-coasters')
    await user.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('dialog', { name: 'Start coasters' })
    await user.type(within(dialog).getByRole('textbox', { name: /Template/ }), 'coaster')
    const copies = within(dialog).getByRole('spinbutton', { name: /Copies/ })
    await user.clear(copies)
    await user.type(copies, '99')
    await user.click(within(dialog).getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(within(dialog).getByRole('alert')).toHaveTextContent('copies: 99 is greater than'))
    expect(screen.queryByText('Run page')).toBeNull()
  })
})
