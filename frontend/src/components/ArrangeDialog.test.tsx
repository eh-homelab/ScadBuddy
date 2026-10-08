import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { LibraryEntry, Output } from '../api/types'
import { fromFiles, fromOutputs } from '../lib/arrange'
import * as fixtures from '../mocks/fixtures'
import { libraryFiles } from '../mocks/features/library'
import { addMockOutput, lastArrangeRequest, problem } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { ArrangeDialog } from './ArrangeDialog'

const first = fixtures.outputs[0] as Output
const second: Output = { ...first, id: 'o-2', name: 'second', manifest: [] }

describe('ArrangeDialog: sources of any kind (#1864)', () => {
  /** An output of a second template, with one object of its own. */
  const bin: Output = {
    ...first,
    id: '9'.repeat(32),
    slug: 'gridfinity-bin',
    name: 'Bin',
    manifest: [{ ...first.manifest![0]!, part: 'piece-bin', slug: 'gridfinity-bin', bom_piece: 'bin', count: 1 }],
  }
  /** The bag clip, uploaded by ScadBuddy as a copy of `first` (the mock's library). */
  const clip = { ...(libraryFiles.find((f) => f.id === 89) as LibraryEntry), output_id: first.id }
  const wand = { ...(libraryFiles.find((f) => f.id === 67) as LibraryEntry), output_id: null }

  it('arranges two templates and a library file, filed under the template chosen', async () => {
    addMockOutput(bin)
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog
        open
        sources={[...fromOutputs([bin]), ...fromFiles([clip, wand])]}
        onClose={vi.fn()}
        onArranged={onArranged}
      />,
    )
    // The clip arranges through the output it is a copy of; the wand is not ScadBuddy's.
    expect(await screen.findByLabelText('Copies of wall — Reagan')).toHaveValue(2)
    expect(screen.getByLabelText('Copies of bin — Bin')).toHaveValue(1)
    expect(screen.getByText(`${wand.filename} was not made by ScadBuddy, so it cannot be arranged yet.`)).toBeVisible()
    const filing = screen.getByLabelText('File under')
    expect(filing).toHaveValue('gridfinity-bin')
    await user.selectOptions(filing, 'name-keychain')
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
    expect(lastArrangeRequest()).toMatchObject({
      slug: 'name-keychain',
      objects: [
        { output_id: bin.id, part: 'piece-bin', count: 1 },
        { output_id: first.id, part: 'piece-wall', count: 2 },
      ],
    })
    expect(onArranged).toHaveBeenCalledWith(
      expect.objectContaining({ output: expect.objectContaining({ slug: 'name-keychain' }) }),
    )
  })

  it('lists an output once when its library file is added too', async () => {
    renderPage(
      <ArrangeDialog open sources={[...fromOutputs([first]), ...fromFiles([clip])]} onClose={vi.fn()} onArranged={vi.fn()} />,
    )
    expect(screen.getAllByLabelText('Copies of wall — Reagan')).toHaveLength(1)
    expect(screen.queryByLabelText('File under')).not.toBeInTheDocument()
  })

  it('adds library files and outputs of another model before the run', async () => {
    addMockOutput(bin)
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={vi.fn()} />,
    )
    await user.click(screen.getByRole('button', { name: 'Add from a model' }))
    await user.selectOptions(await screen.findByLabelText('Model'), 'gridfinity-bin')
    await user.click(await screen.findByRole('checkbox', { name: 'Bin' }))
    await user.click(screen.getByRole('button', { name: 'Add (1)' }))
    expect(await screen.findByLabelText('Copies of bin — Bin')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Add files' }))
    await user.selectOptions(await screen.findByLabelText('Library folder'), '1')
    await user.click(await screen.findByRole('checkbox', { name: /Clara's Wand/ }))
    await user.click(screen.getByRole('button', { name: 'Add (1)' }))
    expect(
      await screen.findByText("Clara's Wand.3mf was not made by ScadBuddy, so it cannot be arranged yet."),
    ).toBeVisible()
  })
})

describe('ArrangeDialog', () => {
  it('lists every object with its count and sends the build list', async () => {
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={onArranged} />,
    )
    const count = screen.getByLabelText('Copies of wall — Reagan')
    expect(count).toHaveValue(2)
    await user.clear(count)
    await user.type(count, '5')
    await user.selectOptions(screen.getByLabelText('Goal'), 'by_colour')
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
    expect(onArranged).toHaveBeenCalledWith({
      output: expect.objectContaining({ arranged_from: [first.id] }),
      plates: 2,
    })
    expect(lastArrangeRequest()).toMatchObject({
      goal: 'by_colour',
      objects: [{ output_id: first.id, part: 'piece-wall', count: 5 }],
    })
  })

  it('says which outputs need a re-render', () => {
    renderPage(
      <ArrangeDialog open sources={fromOutputs([second])} onClose={vi.fn()} onArranged={vi.fn()} />,
    )
    expect(screen.getByRole('status')).toHaveTextContent(
      'second was saved before Arrange existed; re-render to get its layout.',
    )
    expect(screen.getByRole('button', { name: 'Arrange' })).toBeEnabled()
  })

  it('shows why an arrange failed', async () => {
    const created_at = '2026-09-28T12:00:00Z'
    server.use(
      http.post('/api/v1/outputs/arrange', () =>
        HttpResponse.json(
          { id: 'arrange-fail', slug: 'name-keychain', status: 'pending', created_at },
          { status: 202 },
        ),
      ),
      http.get('/api/v1/jobs/arrange-fail', () =>
        HttpResponse.json({
          id: 'arrange-fail',
          slug: 'name-keychain',
          status: 'failed',
          created_at,
          error: "group 'big' does not fit on one plate",
        }),
      ),
    )
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={onArranged} />,
    )
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("group 'big' does not fit on one plate")
    expect(onArranged).not.toHaveBeenCalled()
  })

  it('names several outputs that cannot be arranged in one sentence', () => {
    const third: Output = { ...first, id: 'o-3', name: 'third', manifest: [] }
    renderPage(
      <ArrangeDialog open sources={fromOutputs([second, third])} onClose={vi.fn()} onArranged={vi.fn()} />,
    )
    expect(screen.getByRole('status')).toHaveTextContent(
      'second and third were saved before Arrange existed; re-render to get their layout.',
    )
  })

  describe('an output saved before Arrange (#902)', () => {
    const nova = fixtures.outputs[1] as Output
    const workshop = fixtures.outputs[2] as Output

    it('asks first, and Cancel queues nothing', async () => {
      const backfill = vi.spyOn(api, 'backfillOutput')
      const arrange = vi.spyOn(api, 'arrangeOutputs')
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first, nova])} onClose={vi.fn()} onArranged={vi.fn()} />,
      )
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      const prompt = screen.getByRole('group', { name: 'Re-render first' })
      expect(prompt).toHaveTextContent('Re-render Nova, then arrange?')
      await user.click(within(prompt).getByRole('button', { name: 'Cancel' }))
      expect(screen.queryByRole('group', { name: 'Re-render first' })).not.toBeInTheDocument()
      expect(backfill).not.toHaveBeenCalled()
      expect(arrange).not.toHaveBeenCalled()
      expect(screen.getByRole('button', { name: 'Arrange' })).toBeEnabled()
    })

    it('re-renders each one, then arranges them all', async () => {
      const backfill = vi.spyOn(api, 'backfillOutput')
      const onArranged = vi.fn()
      const { user } = renderPage(
        <ArrangeDialog
          open
          sources={fromOutputs([first, nova, workshop])}
          onClose={vi.fn()}
          onArranged={onArranged}
        />,
      )
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      expect(screen.getByRole('group', { name: 'Re-render first' })).toHaveTextContent(
        'Re-render Nova and Workshop, then arrange?',
      )
      await user.click(screen.getByRole('button', { name: 'Re-render' }))
      expect(await screen.findByRole('list', { name: 'Re-render progress' })).toHaveTextContent('Nova')
      await waitFor(() => expect(onArranged).toHaveBeenCalledOnce(), { timeout: 5000 })
      expect(backfill.mock.calls.map(([id]) => id).sort()).toEqual([nova.id, workshop.id].sort())
      expect(lastArrangeRequest()?.objects).toEqual([
        { output_id: first.id, part: 'piece-wall', count: 2 },
        { output_id: nova.id, part: 'piece-body', count: 1 },
        { output_id: workshop.id, part: 'piece-body', count: 1 },
      ])
    })

    it('reports a failed re-render and arranges the rest', async () => {
      server.use(
        http.post(`/api/v1/outputs/${workshop.id}/backfill`, () =>
          problem(422, 'Unprocessable Content', 'revision abc is no longer in the template history'),
        ),
      )
      const onArranged = vi.fn()
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([nova, workshop])} onClose={vi.fn()} onArranged={onArranged} />,
      )
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await user.click(screen.getByRole('button', { name: 'Re-render' }))
      await waitFor(() => expect(onArranged).toHaveBeenCalledOnce(), { timeout: 5000 })
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Workshop could not be re-rendered: revision abc is no longer in the template history',
      )
      expect(lastArrangeRequest()?.objects).toEqual([{ output_id: nova.id, part: 'piece-body', count: 1 }])
      expect(onArranged).toHaveBeenCalledWith(
        expect.objectContaining({
          skipped:
            'Workshop could not be re-rendered: revision abc is no longer in the template history. It was left out of the arrange.',
        }),
      )
    })

    it('arranges nothing when no re-render succeeds', async () => {
      server.use(
        http.get(`/api/v1/outputs/${nova.id}`, () =>
          HttpResponse.json({ ...nova, backfill: { job_id: 'gone', error: 'openscad exited with 1' } }),
        ),
      )
      const arrange = vi.spyOn(api, 'arrangeOutputs')
      const onArranged = vi.fn()
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([nova])} onClose={vi.fn()} onArranged={onArranged} />,
      )
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await user.click(screen.getByRole('button', { name: 'Re-render' }))
      expect(await screen.findByRole('alert', {}, { timeout: 5000 })).toHaveTextContent(
        'Nova could not be re-rendered: openscad exited with 1. Nothing was arranged.',
      )
      expect(arrange).not.toHaveBeenCalled()
      expect(onArranged).not.toHaveBeenCalled()
    })

    it('arranges the outputs that never needed a re-render when every re-render fails', async () => {
      server.use(
        http.post(`/api/v1/outputs/${nova.id}/backfill`, () =>
          problem(422, 'Unprocessable Content', 'revision abc is gone'),
        ),
      )
      const onArranged = vi.fn()
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first, nova])} onClose={vi.fn()} onArranged={onArranged} />,
      )
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await user.click(screen.getByRole('button', { name: 'Re-render' }))
      await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
      expect(onArranged).toHaveBeenCalledWith(
        expect.objectContaining({ skipped: 'Nova could not be re-rendered: revision abc is gone. It was left out of the arrange.' }),
      )
      expect(lastArrangeRequest()?.objects).toEqual([{ output_id: first.id, part: 'piece-wall', count: 2 }])
    })

    it('opens again without the last alert or prompt', async () => {
      server.use(
        http.post(`/api/v1/outputs/${nova.id}/backfill`, () =>
          problem(422, 'Unprocessable Content', 'revision abc is gone'),
        ),
      )
      // History keeps the dialog mounted and only flips `open`.
      function Reopening() {
        const [open, setOpen] = useState(true)
        return (
          <>
            <button onClick={() => setOpen(true)}>Open it</button>
            <ArrangeDialog
              open={open}
              sources={fromOutputs([nova])}
              onClose={() => setOpen(false)}
              onArranged={vi.fn()}
            />
          </>
        )
      }
      const { user } = renderPage(<Reopening />)
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await user.click(screen.getByRole('button', { name: 'Re-render' }))
      expect(await screen.findByRole('alert')).toHaveTextContent('Nothing was arranged.')
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      expect(screen.getByRole('group', { name: 'Re-render first' })).toBeInTheDocument()
      await user.keyboard('{Escape}')
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      await user.click(screen.getByRole('button', { name: 'Open it' }))
      await screen.findByRole('dialog', { name: 'Arrange' })
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.queryByRole('group', { name: 'Re-render first' })).not.toBeInTheDocument()
    })

    it('asks when Arrange itself says an output needs a re-render', async () => {
      // The list was read before the output lost its objects, or another tab saw it.
      const stale: Output = { ...nova, manifest: first.manifest }
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([stale])} onClose={vi.fn()} onArranged={vi.fn()} />,
      )
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      expect(await screen.findByRole('group', { name: 'Re-render first' })).toHaveTextContent(
        'Re-render Nova, then arrange?',
      )
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it('stops polling when it is closed mid-re-render', async () => {
      server.use(
        http.get('/api/v1/jobs/:id', ({ params }) =>
          HttpResponse.json({
            id: String(params['id']),
            slug: 'name-keychain',
            status: 'running',
            created_at: '2026-09-28T12:00:00Z',
          }),
        ),
      )
      const read = vi.spyOn(api, 'getJob')
      const reread = vi.spyOn(api, 'getOutput')
      const arrange = vi.spyOn(api, 'arrangeOutputs')
      const dialog = (open: boolean) => (
        <ArrangeDialog open={open} sources={fromOutputs([nova])} onClose={vi.fn()} onArranged={vi.fn()} />
      )
      const { user, rerender } = renderPage(dialog(true))
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await user.click(screen.getByRole('button', { name: 'Re-render' }))
      await waitFor(() => expect(read).toHaveBeenCalled())
      rerender(dialog(false))
      const polls = read.mock.calls.length + reread.mock.calls.length
      await new Promise((resolve) => setTimeout(resolve, 1500))
      expect(read.mock.calls.length + reread.mock.calls.length).toBe(polls)
      expect(arrange).not.toHaveBeenCalled()
    })
  })

  it('announces its progress and marks Arrange busy while it waits', async () => {
    const created_at = '2026-09-28T12:00:00Z'
    server.use(
      http.post('/api/v1/outputs/arrange', () =>
        HttpResponse.json({ id: 'arrange-slow', slug: 'name-keychain', status: 'pending', created_at }, { status: 202 }),
      ),
      http.get('/api/v1/jobs/arrange-slow', () =>
        HttpResponse.json({ id: 'arrange-slow', slug: 'name-keychain', status: 'running', created_at }),
      ),
    )
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={vi.fn()} />,
    )
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    const progress = await screen.findByText('Arranging…')
    expect(progress).toHaveAttribute('aria-live', 'polite')
    expect(screen.getByRole('button', { name: 'Arrange' })).toHaveAttribute('aria-busy', 'true')
  })

  it('stops waiting when it is closed, so the job is never saved behind its back', async () => {
    // Final review M3: a closed dialog kept polling; reopened and clicked again, both
    // loops saved the same coalesced job as two outputs.
    const created_at = '2026-09-28T12:00:00Z'
    let status = 'pending'
    server.use(
      http.post('/api/v1/outputs/arrange', () =>
        HttpResponse.json({ id: 'arrange-wait', slug: 'name-keychain', status: 'pending', created_at }, { status: 202 }),
      ),
      http.get('/api/v1/jobs/arrange-wait', () =>
        HttpResponse.json({ id: 'arrange-wait', slug: 'name-keychain', status, created_at }),
      ),
    )
    const save = vi.spyOn(api, 'createOutput')
    const onArranged = vi.fn()
    // `rerender` swaps the whole tree, router included; the dialog needs none.
    const dialog = (open: boolean) => (
      <ArrangeDialog open={open} sources={fromOutputs([first])} onClose={vi.fn()} onArranged={onArranged} />
    )
    const { user, rerender } = renderPage(dialog(true))
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await screen.findByText('Waiting for a worker…')
    rerender(dialog(false))
    status = 'done'
    await new Promise((resolve) => setTimeout(resolve, 1500))
    expect(save).not.toHaveBeenCalled()
    expect(onArranged).not.toHaveBeenCalled()
  })

  it('acts on nothing when it is closed while the output is being saved', async () => {
    let saved!: (output: Output) => void
    vi.spyOn(api, 'createOutput').mockImplementation(
      () => new Promise<Output>((resolve) => (saved = resolve)),
    )
    const onArranged = vi.fn()
    const dialog = (open: boolean) => (
      <ArrangeDialog open={open} sources={fromOutputs([first])} onClose={vi.fn()} onArranged={onArranged} />
    )
    const { user, rerender } = renderPage(dialog(true))
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(api.createOutput).toHaveBeenCalled())
    rerender(dialog(false))
    saved({ ...first, id: 'o-new' })
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(onArranged).not.toHaveBeenCalled()
  })
})
