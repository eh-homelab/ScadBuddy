import { screen, waitFor } from '@testing-library/react'
import { Route, Routes, useLocation, useNavigationType } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { addTask, setTask } from '../mocks/features/tasks'
import { L, spoolId } from '../mocks/features/filament/seed'
import { renderPage } from '../test/utils'
import { CodePage } from './CodePage'

function Where() {
  const where = useLocation()
  return (
    <>
      <p data-testid="where">{where.pathname}</p>
      <p data-testid="query">{where.search}</p>
      <p data-testid="how">{useNavigationType()}</p>
    </>
  )
}

function render(route: string) {
  return renderPage(
    <Routes>
      <Route path="/x/:code" element={<CodePage />} />
      <Route path="*" element={<Where />} />
    </Routes>,
    { route },
  )
}

describe('the code page', () => {
  it('opens a location code at its page', async () => {
    render('/x/L-3')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(`/filament/locations/${L.DRY_BOX}`))
  })

  it('opens a spool code at its page, with any case and no dash', async () => {
    render('/x/s1')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(`/filament/spools/${spoolId(1)}`))
  })

  it('names an unknown code and takes another', async () => {
    const { user } = render('/x/Q-9')
    expect(await screen.findByRole('heading', { name: 'No such code: Q-9' })).toBeInTheDocument()
    await user.type(screen.getByRole('textbox', { name: 'Code' }), 'L-2')
    await user.click(screen.getByRole('button', { name: 'Open' }))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(`/filament/locations/${L.SHELF_A}`))
  })

  // #8062 (spec §6.3) — a scanned code first asks whether an open task waits for it.
  const scanSchema = { type: 'object', properties: { spool: { type: 'string', title: 'Spool', 'x-sb-widget': 'scan', expects: { kinds: ['location'] } } }, required: ['spool'] }

  it('offers the one task that waits for the code, with the code in its scan field', async () => {
    addTask({ id: 'scan-1', title: 'Check the box', schema: scanSchema })
    render('/x/L-3')
    expect(await screen.findByRole('heading', { name: 'Check the box' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('textbox', { name: /Spool/ })).toHaveValue('L-3'))
    expect(screen.getByRole('link', { name: 'Open L-3 instead' })).toHaveAttribute('href', `/filament/locations/${L.DRY_BOX}`)
    expect(screen.queryByTestId('where')).toBeNull()
  })

  it('waits for the task list before it opens the entity', async () => {
    addTask({ id: 'scan-1', title: 'Check the box', schema: scanSchema })
    const listTasks = api.listTasks.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => {
      release = resolve
    })
    vi.spyOn(api, 'listTasks').mockImplementationOnce(async (...args) => {
      await held
      return listTasks(...args)
    })
    render('/x/L-3')
    expect(await screen.findByRole('status', { name: 'Looking up the code' })).toBeInTheDocument()
    // The code's own lookup answers first; the page stays.
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.queryByTestId('where')).toBeNull()
    release()
    expect(await screen.findByRole('heading', { name: 'Check the box' })).toBeInTheDocument()
    expect(screen.queryByTestId('where')).toBeNull()
  })

  it('lists several tasks with a link to each run', async () => {
    addTask({ id: 'scan-1', title: 'Check the box', run_id: 'run-a', schema: scanSchema })
    addTask({ id: 'scan-2', title: 'Dry the box', run_id: 'run-b', schema: scanSchema })
    render('/x/L-3')
    expect(await screen.findByRole('link', { name: 'Check the box' })).toHaveAttribute('href', '/workflows/runs/run-a')
    expect(screen.getByRole('link', { name: 'Dry the box' })).toHaveAttribute('href', '/workflows/runs/run-b')
    expect(screen.getByRole('link', { name: 'Open L-3 instead' })).toBeInTheDocument()
  })

  it('opens the entity when no task waits', async () => {
    addTask({ id: 'scan-1', title: 'Check a spool', schema: { ...scanSchema, properties: { spool: { ...scanSchema.properties.spool, expects: { kinds: ['spool'] } } } } })
    render('/x/L-3')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(`/filament/locations/${L.DRY_BOX}`))
  })

  it('opens the entity and logs when the task read fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    vi.spyOn(api, 'listTasks').mockRejectedValueOnce(new Error('down'))
    render('/x/L-3')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(`/filament/locations/${L.DRY_BOX}`))
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('tasks'), expect.anything())
    warn.mockRestore()
  })

  it('fills the scan field the code fits', async () => {
    const field = (title: string, kinds: string[]) => ({ type: 'string', title, 'x-sb-widget': 'scan', expects: { kinds } })
    addTask({
      id: 'two',
      title: 'Two scans',
      schema: { type: 'object', properties: { spool: field('Spool', ['spool']), box: field('Box', ['location']) } },
    })
    render('/x/L-3')
    await waitFor(() => expect(screen.getByRole('textbox', { name: /Box/ })).toHaveValue('L-3'))
    expect(screen.getByRole('textbox', { name: /Spool/ })).toHaveValue('')
  })

  it('shows the result after the answer, and links to the entity', async () => {
    addTask({ id: 'scan-1', title: 'Check the box', schema: scanSchema })
    const { user } = render('/x/L-3')
    await waitFor(() => expect(screen.getByRole('textbox', { name: /Spool/ })).toHaveValue('L-3'))
    await waitFor(() => expect(screen.getAllByRole('status')[0]).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Answer' }))
    expect(await screen.findByText('Answered.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open L-3' })).toHaveAttribute('href', `/filament/locations/${L.DRY_BOX}`)
    expect(screen.queryByTestId('where')).toBeNull()
  })

  it('says so when the task was answered elsewhere', async () => {
    addTask({ id: 'scan-1', title: 'Check the box', schema: scanSchema })
    const { user } = render('/x/L-3')
    await waitFor(() => expect(screen.getByRole('textbox', { name: /Spool/ })).toHaveValue('L-3'))
    await waitFor(() => expect(screen.getAllByRole('status')[0]).toBeInTheDocument())
    setTask('scan-1', { state: 'answered' })
    await user.click(screen.getByRole('button', { name: 'Answer' }))
    expect(await screen.findByText('This task was answered elsewhere.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open L-3' })).toBeInTheDocument()
  })

  it('searches the spools for a code that is not a ScadBuddy code, in its place in the history (#8372)', async () => {
    render('/x/6975373321234')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/filament/spools'))
    const query = new URLSearchParams(screen.getByTestId('query').textContent ?? '')
    expect(query.get('q')).toBe('gtin:6975373321234')
    expect(query.get('from')).toBe('scan')
    expect(screen.getByTestId('how')).toHaveTextContent('REPLACE')
  })

  it('keeps the sentence for a ScadBuddy code that is not found (#8372)', async () => {
    render('/x/S-999')
    expect(await screen.findByRole('heading', { name: 'No such code: S-999' })).toBeInTheDocument()
  })
})
