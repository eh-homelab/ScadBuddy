import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import type { LibraryEntry, Output } from '../api/types'
import { fromFiles, fromOutputs, objectNames } from '../lib/arrange'
import { startTracing } from '../lib/tracing'
import * as fixtures from '../mocks/fixtures'
import { libraryFiles } from '../mocks/features/library'
import { addMockOutput, lastArrangeRequest, problem } from '../mocks/handlers'
import { server } from '../mocks/server'
import { installTestTracing } from '../test/tracing'
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
    // The clip arranges through the output it is a copy of; the wand, which ScadBuddy did
    // not make, through the objects read from its 3MF (#1863).
    expect(await screen.findByLabelText('Copies of wall — Reagan')).toHaveValue(2)
    expect(screen.getByLabelText('Copies of bin — Bin')).toHaveValue(1)
    expect(await screen.findByLabelText(`Copies of Wand — ${wand.filename}`)).toHaveValue(1)
    const star = screen.getByLabelText(`Copies of Star — ${wand.filename}`)
    expect(star).toHaveValue(2)
    await user.clear(star)
    await user.type(star, '5')
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
        { library_file_id: wand.id, part: 'lib1-67-0', count: 1 },
        { library_file_id: wand.id, part: 'lib1-67-1', count: 5 },
      ],
    })
    expect(onArranged).toHaveBeenCalledWith(
      expect.objectContaining({ output: expect.objectContaining({ slug: 'name-keychain' }) }),
    )
  })

  it('says why a file ScadBuddy made is left out when its output cannot be read', async () => {
    const gone = { ...clip, output_id: '8'.repeat(32) }
    server.use(
      http.get(`/api/v1/outputs/${gone.output_id}`, () => problem(404, 'Not Found', 'no such output')),
    )
    renderPage(<ArrangeDialog open sources={fromFiles([gone])} onClose={vi.fn()} onArranged={vi.fn()} />)
    expect(await screen.findByText(/could not read what it made them from: .*: no such output\./)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Arrange' })).toBeDisabled()
  })

  it('reads a failed output again when it is opened again', async () => {
    const flaky = { ...clip, output_id: '7'.repeat(32) }
    let reads = 0
    server.use(
      http.get(`/api/v1/outputs/${flaky.output_id}`, () => {
        reads += 1
        return reads === 1 ? problem(503, 'Service Unavailable', 'try again') : HttpResponse.json({ ...first, id: flaky.output_id })
      }),
    )
    const dialog = (open: boolean) => (
      <ArrangeDialog open={open} sources={fromFiles([flaky])} onClose={vi.fn()} onArranged={vi.fn()} />
    )
    const { rerender } = renderPage(dialog(true))
    expect(await screen.findByText(/try again\./)).toBeVisible()
    rerender(dialog(false))
    rerender(dialog(true))
    expect(await screen.findByLabelText('Copies of wall — Reagan')).toBeVisible()
  })

  it('shows where one template and a library file are filed, and files them under another (#2284)', async () => {
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open sources={[...fromOutputs([first]), ...fromFiles([wand])]} onClose={vi.fn()} onArranged={onArranged} />,
    )
    expect(await screen.findByLabelText(`Copies of Wand — ${wand.filename}`)).toHaveValue(1)
    const filing = await screen.findByLabelText('File under')
    expect(filing).toHaveValue('name-keychain')
    await waitFor(() => expect(within(filing).getAllByRole('option')).toHaveLength(fixtures.models.length))
    expect(within(filing).getAllByRole('option')[0]).toHaveTextContent('Name Keychain')
    await user.selectOptions(filing, 'gridfinity-bin')
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
    expect(lastArrangeRequest()).toMatchObject({ slug: 'gridfinity-bin' })
    const binName = fixtures.models.find((m) => m.slug === 'gridfinity-bin')!.name
    expect(onArranged).toHaveBeenCalledWith(
      expect.objectContaining({
        filedUnder: binName,
        output: expect.objectContaining({
          slug: 'gridfinity-bin',
          arranged_from: [first.id, `library:${wand.id}`],
          // The file's objects are no template's, wherever the result is filed.
          manifest: expect.arrayContaining([expect.objectContaining({ library_file_id: wand.id, slug: '' })]),
        }),
      }),
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
    await user.click(await screen.findByRole('checkbox', { name: /^Bin · / }))
    await user.click(screen.getByRole('button', { name: 'Add (1)' }))
    expect(await screen.findByLabelText('Copies of bin — Bin')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Add files' }))
    await user.selectOptions(await screen.findByLabelText('Library folder'), '1')
    await user.click(await screen.findByRole('checkbox', { name: /Clara's Wand/ }))
    await user.click(screen.getByRole('button', { name: 'Add (1)' }))
    expect(await screen.findByLabelText("Copies of Wand — Clara's Wand.3mf")).toBeVisible()
  })

  it('leaves out a library file whose objects cannot be read, saying why (#1863)', async () => {
    const sliced = libraryFiles.find((f) => f.id === 104) as LibraryEntry
    renderPage(
      <ArrangeDialog open sources={[...fromOutputs([first]), ...fromFiles([sliced])]} onClose={vi.fn()} onArranged={vi.fn()} />,
    )
    expect(await screen.findByText(/^Left out: .*: it is sliced already/)).toHaveTextContent(sliced.filename)
    expect(screen.getByRole('button', { name: 'Arrange' })).toBeEnabled()
  })

  it('files library files alone under any template picked (#1863)', async () => {
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open sources={fromFiles([wand])} onClose={vi.fn()} onArranged={onArranged} />,
    )
    expect(await screen.findByLabelText(`Copies of Wand — ${wand.filename}`)).toHaveValue(1)
    const filing = await screen.findByLabelText('File under')
    // #2284 — by name, not slug; #3638 — the file's own new model first.
    await waitFor(() =>
      expect(within(filing).getAllByRole('option').map((o) => o.textContent)).toEqual([
        "A new model: Clara's Wand",
        ...fixtures.models.map((m) => m.name),
      ]),
    )
    await user.selectOptions(filing, 'gridfinity-bin')
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
    expect(lastArrangeRequest()).toMatchObject({
      slug: 'gridfinity-bin',
      objects: [
        { library_file_id: wand.id, part: 'lib1-67-0', count: 1 },
        { library_file_id: wand.id, part: 'lib1-67-1', count: 2 },
      ],
    })
    expect(onArranged).toHaveBeenCalledWith(
      expect.objectContaining({ output: expect.objectContaining({ slug: 'gridfinity-bin' }) }),
    )
  })
})

describe('ArrangeDialog: a library file filed under its own model (#3638)', () => {
  const wand = { ...(libraryFiles.find((f) => f.id === 67) as LibraryEntry), output_id: null }

  it('makes the file a model of its own, files the result there, and uses it next time', async () => {
    const onArranged = vi.fn()
    const create = vi.spyOn(api, 'createMeshModel')
    const dialog = (open: boolean) => (
      <ArrangeDialog open={open} sources={fromFiles([wand])} onClose={vi.fn()} onArranged={onArranged} />
    )
    const { user, rerender } = renderPage(dialog(true))
    const filing = await screen.findByLabelText('File under')
    await waitFor(() => expect(filing).toHaveDisplayValue("A new model: Clara's Wand"))
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
    expect(create).toHaveBeenCalledOnce()
    expect(create).toHaveBeenCalledWith(expect.objectContaining({ name: "Clara's Wand", library_file_id: wand.id }))
    expect(lastArrangeRequest()).toMatchObject({ slug: 'clara-s-wand' })
    expect(onArranged).toHaveBeenCalledWith(
      expect.objectContaining({ filedUnder: "Clara's Wand", output: expect.objectContaining({ slug: 'clara-s-wand' }) }),
    )
    // Again: the model made from the file is its home, and no second one is made.
    rerender(dialog(false))
    rerender(dialog(true))
    await waitFor(() => expect(screen.getByLabelText('File under')).toHaveDisplayValue("Clara's Wand"))
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalledTimes(2))
    expect(create).toHaveBeenCalledOnce()
    create.mockRestore()
  })

  it('still files under an existing model when one is picked', async () => {
    const { user } = renderPage(<ArrangeDialog open sources={fromFiles([wand])} onClose={vi.fn()} onArranged={vi.fn()} />)
    const filing = await screen.findByLabelText('File under')
    await waitFor(() => expect(within(filing).getAllByRole('option').length).toBeGreaterThan(1))
    await user.selectOptions(filing, 'gridfinity-bin')
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(lastArrangeRequest()).toMatchObject({ slug: 'gridfinity-bin' }))
  })
})

describe('ArrangeDialog: a copy in other colours (#3637)', () => {
  const wand = { ...(libraryFiles.find((f) => f.id === 67) as LibraryEntry), output_id: null }

  it('adds a copy of a library object with one colour swapped', async () => {
    const onArranged = vi.fn()
    const { user } = renderPage(<ArrangeDialog open sources={fromFiles([wand])} onClose={vi.fn()} onArranged={onArranged} />)
    const label = `Wand — ${wand.filename}`
    expect(await screen.findByLabelText(`Copies of ${label}`)).toHaveValue(1)
    expect(screen.getByRole('img', { name: `${label} prints in #7B1FA2, #FFD54F` })).toBeVisible()
    await user.click(screen.getByRole('button', { name: `Add a copy of ${label} in other colours` }))
    const copy = `${label}, copy 1 in other colours`
    expect(screen.getByLabelText(`Copies of ${copy}`)).toHaveValue(1)
    // Its editor opens at once: purple becomes magenta, yellow stays.
    fireEvent.input(screen.getByLabelText(`#7B1FA2 of ${copy} prints in`), { target: { value: '#ff00ff' } })
    expect(screen.getByRole('img', { name: `${copy} prints in #FF00FF, #FFD54F` })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
    expect(lastArrangeRequest()?.objects).toEqual([
      { library_file_id: wand.id, part: 'lib1-67-0', count: 1 },
      { library_file_id: wand.id, part: 'lib1-67-0', count: 1, recolour: { '#7B1FA2': '#FF00FF' } },
      { library_file_id: wand.id, part: 'lib1-67-1', count: 2 },
    ])
    // Each colouring stays its own object, and magenta is a filament of its own.
    const saved = (onArranged.mock.calls[0]![0] as { output: Output }).output
    expect(saved.colors).toEqual(expect.arrayContaining(['#7B1FA2', '#FFD54F', '#FF00FF']))
    expect(saved.manifest?.filter((m) => m.part === 'lib1-67-0').map((m) => m.recolour)).toEqual([
      {},
      { '#7B1FA2': '#FF00FF' },
    ])
  })

  it('takes a colour from a spool, keeps a colour back, and drops a copy', async () => {
    const { user } = renderPage(<ArrangeDialog open sources={fromFiles([wand])} onClose={vi.fn()} onArranged={vi.fn()} />)
    const label = `Star — ${wand.filename}`
    await user.click(await screen.findByRole('button', { name: `Add a copy of ${label} in other colours` }))
    const copy = `${label}, copy 1 in other colours`
    const editor = screen.getByRole('list', { name: `Colours of ${copy}` })
    await user.click(within(editor).getByRole('button', { name: 'Choose from a filament' }))
    const spools = await within(editor).findByRole('list', { name: `Filaments for #FFD54F of ${copy}` })
    const [spool] = within(spools)
      .getAllByRole('button')
      .filter((b) => !(b as HTMLButtonElement).disabled && b.getAttribute('aria-pressed') !== 'true')
    await user.click(spool!)
    const keep = within(editor).getByRole('button', { name: 'Keep #FFD54F' })
    await user.click(keep)
    expect(screen.getByRole('img', { name: `${copy} prints in #FFD54F` })).toBeVisible()
    await user.click(screen.getByRole('button', { name: `Remove ${copy}` }))
    expect(screen.queryByLabelText(`Copies of ${copy}`)).not.toBeInTheDocument()
  })

  it("lists an arranged output's recoloured copies as their own row and keeps their colours", async () => {
    const entry = first.manifest![0]!
    const swap = { [entry.colours[0]!.toUpperCase()]: '#FF00FF' }
    const twice: Output = {
      ...first,
      id: '6'.repeat(32),
      manifest: [
        { ...entry, count: 1, recolour: {} },
        { ...entry, count: 1, recolour: swap },
      ],
    }
    addMockOutput(twice)
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([twice])} onClose={vi.fn()} onArranged={onArranged} />,
    )
    expect(await screen.findByLabelText('Copies of wall — Reagan')).toHaveValue(1)
    expect(screen.getByLabelText('Copies of wall (other colours) — Reagan')).toHaveValue(1)
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
    expect(lastArrangeRequest()?.objects).toEqual([
      { output_id: twice.id, part: entry.part, count: 1, recolour: {} },
      { output_id: twice.id, part: entry.part, count: 1, recolour: swap },
    ])
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
      filedUnder: 'Name Keychain',
    })
    expect(lastArrangeRequest()).toMatchObject({
      goal: 'by_colour',
      objects: [{ output_id: first.id, part: 'piece-wall', count: 5 }],
    })
  })

  it('is one traced action from the arrange to its save (#2292)', async () => {
    const seen: { method: string; path: string; traceparent: string | null }[] = []
    const listen = ({ request }: { request: Request }) => {
      seen.push({
        method: request.method,
        path: new URL(request.url).pathname,
        traceparent: request.headers.get('traceparent'),
      })
    }
    server.events.on('request:start', listen)
    const stop = startTracing()
    try {
      const onArranged = vi.fn()
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={onArranged} />,
      )
      await user.click(await screen.findByRole('button', { name: 'Arrange' }))
      await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
    } finally {
      server.events.removeListener('request:start', listen)
      await stop()
    }
    const arrange = seen.find((call) => call.method === 'POST' && call.path === '/api/v1/outputs/arrange')
    const save = seen.find((call) => call.method === 'POST' && /^\/api\/v1\/models\/[^/]+\/outputs$/.test(call.path))
    expect(arrange?.traceparent).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/)
    // The save follows an await: it joins the arrange's trace only through the action.
    expect(save?.traceparent?.split('-')[1]).toBe(arrange?.traceparent?.split('-')[1])
  })

  it('passes no name, never a slug, when the model list cannot be read (#2541)', async () => {
    let failed = false
    server.use(
      http.get('/api/v1/models', () => {
        failed = true
        return problem(503, 'Service Unavailable', 'down')
      }),
    )
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={onArranged} />,
    )
    await waitFor(() => expect(failed).toBe(true))
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
    expect(onArranged.mock.calls[0]![0]).not.toHaveProperty('filedUnder')
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
      // #2283 — the stage, above each output's own line.
      expect(screen.getByText('Re-rendering 2 outputs first…')).toBeInTheDocument()
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
      const tracing = installTestTracing()
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([nova])} onClose={vi.fn()} onArranged={onArranged} />,
      )
      try {
        await user.click(screen.getByRole('button', { name: 'Arrange' }))
        await user.click(screen.getByRole('button', { name: 'Re-render' }))
        expect(await screen.findByRole('alert', {}, { timeout: 5000 })).toHaveTextContent(
          'Nova could not be re-rendered: openscad exited with 1. Nothing was arranged.',
        )
        // #2892: the span says nothing was arranged, not an ordinary arrange.
        const span = tracing.exporter.getFinishedSpans().find((s) => s.name === 'arrange')
        expect(span?.attributes['scadbuddy.outcome']).toBe('nothing_arranged')
        expect(span?.attributes['scadbuddy.output_id']).toBeUndefined()
      } finally {
        tracing.uninstall()
      }
      expect(arrange).not.toHaveBeenCalled()
      expect(onArranged).not.toHaveBeenCalled()
      // #2286 — tried again from here, at once: no second question.
      const backfill = vi.spyOn(api, 'backfillOutput')
      await user.click(screen.getByRole('button', { name: 'Re-render again' }))
      await waitFor(() => expect(backfill).toHaveBeenCalledWith(nova.id, expect.any(AbortSignal)))
      expect(screen.queryByRole('group', { name: 'Re-render first' })).not.toBeInTheDocument()
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

    it('names the saved arrange by what was arranged, not an output whose re-render failed (#2749 review)', async () => {
      const bin: Output = { ...first, id: 'b'.repeat(32), slug: 'gridfinity-bin', name: 'Bin', manifest: [] }
      addMockOutput(bin)
      server.use(
        http.post(`/api/v1/outputs/${bin.id}/backfill`, () => problem(422, 'Unprocessable Content', 'revision abc is gone')),
      )
      const onArranged = vi.fn()
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first, bin])} onClose={vi.fn()} onArranged={onArranged} />,
      )
      const binName = fixtures.models.find((m) => m.slug === 'gridfinity-bin')!.name
      // Before Arrange, the stale one is about to be re-rendered: it is in the placeholder.
      await waitFor(() =>
        expect(screen.getByLabelText('Name')).toHaveAttribute('placeholder', `Arrange of Name Keychain, ${binName}`),
      )
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await user.click(screen.getByRole('button', { name: 'Re-render' }))
      await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
      expect(lastArrangeRequest()?.name).toBe('Arrange of Name Keychain')
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

    it('keeps the rows as they are while the re-render question is open (#3528)', async () => {
      const stale: Output = { ...nova, manifest: first.manifest }
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first, stale])} onClose={vi.fn()} onArranged={vi.fn()} />,
      )
      const removes = await screen.findAllByRole('button', { name: /^Remove / })
      await user.click(removes[0]!)
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await screen.findByRole('group', { name: 'Re-render first' })
      // Unanswered, the question is about this arrange: nothing changes it underneath.
      expect(screen.getByRole('button', { name: /^Restore 1 removed row/ })).toBeDisabled()
      expect(screen.getByRole('button', { name: 'Add from a model' })).toBeDisabled()
    })

    it('leaves a flagged output with objects out once it is removed (#2754 review)', async () => {
      // The list showed Nova's objects; Arrange's refusal flagged it. Removed, it is in
      // neither the re-render nor the request, and no prompt opens over an empty list.
      const stale: Output = { ...nova, manifest: first.manifest }
      const backfill = vi.spyOn(api, 'backfillOutput')
      const onArranged = vi.fn()
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first, stale])} onClose={vi.fn()} onArranged={onArranged} />,
      )
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await user.click(within(await screen.findByRole('group', { name: 'Re-render first' })).getByRole('button', { name: 'Cancel' }))
      await user.click(screen.getByRole('button', { name: /^Remove .*Nova$/ }))
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
      expect(lastArrangeRequest()?.objects.map((o) => o.output_id)).toEqual(first.manifest!.map(() => first.id))
      expect(screen.queryByRole('group', { name: 'Re-render first' })).not.toBeInTheDocument()
      expect(backfill).not.toHaveBeenCalled()
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
    const progress = await screen.findByText('Laying out the objects…')
    expect(progress).toHaveAttribute('aria-live', 'polite')
    expect(screen.getByRole('button', { name: 'Arrange' })).toHaveAttribute('aria-busy', 'true')
  })

  describe('names, not ids (#2285)', () => {
    it('names an unnamed output by its model and short id, and an object by its model', async () => {
      const unnamed: Output = {
        ...first,
        id: 'a1ac23f1'.padEnd(32, '0'),
        name: null,
        manifest: [{ ...first.manifest![0]!, bom_piece: null, file: 'model.scad' }],
      }
      addMockOutput(unnamed)
      renderPage(<ArrangeDialog open sources={fromOutputs([unnamed])} onClose={vi.fn()} onArranged={vi.fn()} />)
      expect(await screen.findByLabelText('Copies of Name Keychain — a1ac23f1')).toBeInTheDocument()
      expect(screen.queryByText(/model\.scad/)).not.toBeInTheDocument()
    })

    it('numbers two objects of one output that would read the same', async () => {
      const twice: Output = {
        ...first,
        id: 'b'.repeat(32),
        name: 'Pair',
        manifest: [
          { ...first.manifest![0]!, part: 'p-1', bom_piece: null },
          { ...first.manifest![0]!, part: 'p-2', bom_piece: null, count: 1 },
        ],
      }
      addMockOutput(twice)
      renderPage(<ArrangeDialog open sources={fromOutputs([twice])} onClose={vi.fn()} onArranged={vi.fn()} />)
      expect(await screen.findByLabelText('Copies of Name Keychain 1 — Pair')).toBeInTheDocument()
      expect(screen.getByLabelText('Copies of Name Keychain 2 — Pair')).toBeInTheDocument()
    })

    it('lists an output that needs a re-render as a row; its Re-render makes it an ordinary row', async () => {
      const stale: Output = { ...first, id: 'c0ffee00'.padEnd(32, '0'), name: null, manifest: [] }
      addMockOutput(stale)
      const backfill = vi.spyOn(api, 'backfillOutput')
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first, stale])} onClose={vi.fn()} onArranged={vi.fn()} />,
      )
      const label = 'Name Keychain — c0ffee00'
      expect(await screen.findByText(label, { exact: false })).toHaveTextContent('needs a re-render')
      // The note names it as the row does, never by its 32-hex id.
      expect(screen.getByRole('status')).toHaveTextContent('Name Keychain c0ffee00 was saved before Arrange existed')
      await user.click(screen.getByRole('button', { name: `Re-render ${label}` }))
      await waitFor(() => expect(backfill).toHaveBeenCalledWith(stale.id, expect.any(AbortSignal)))
      expect(await screen.findByLabelText(/^Copies of .* — c0ffee00$/, {}, { timeout: 5000 })).toBeInTheDocument()
      expect(screen.queryByText('needs a re-render', { exact: false })).not.toBeInTheDocument()
    })

    it("traces a row's Re-render as one action (#2754 review)", async () => {
      const stale: Output = { ...first, id: 'c0ffee00'.padEnd(32, '0'), name: null, manifest: [] }
      addMockOutput(stale)
      const tracing = installTestTracing()
      try {
        const { user } = renderPage(
          <ArrangeDialog open sources={fromOutputs([first, stale])} onClose={vi.fn()} onArranged={vi.fn()} />,
        )
        await user.click(await screen.findByRole('button', { name: 'Re-render Name Keychain — c0ffee00' }))
        await waitFor(() => expect(tracing.exporter.getFinishedSpans().map((s) => s.name)).toContain('rerender'), {
          timeout: 5000,
        })
        const span = tracing.exporter.getFinishedSpans().find((s) => s.name === 'rerender')
        expect(span?.attributes['scadbuddy.output_id']).toBe(stale.id)
      } finally {
        tracing.uninstall()
      }
    })

    it('takes an output that needs a re-render out with Remove, asking nothing about it', async () => {
      const stale: Output = { ...first, id: 'c0ffee00'.padEnd(32, '0'), name: null, manifest: [] }
      addMockOutput(stale)
      const backfill = vi.spyOn(api, 'backfillOutput')
      const onArranged = vi.fn()
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first, stale])} onClose={vi.fn()} onArranged={onArranged} />,
      )
      await user.click(await screen.findByRole('button', { name: 'Remove Name Keychain — c0ffee00' }))
      expect(screen.queryByText('needs a re-render', { exact: false })).not.toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      expect(screen.queryByRole('group', { name: 'Re-render first' })).not.toBeInTheDocument()
      await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
      expect(lastArrangeRequest()?.objects.map((o) => o.output_id)).toEqual([first.id])
      expect(backfill).not.toHaveBeenCalled()
    })

    it('names an output added as a library file by the file', async () => {
      const stale: Output = { ...first, id: 'd'.repeat(32), name: null, manifest: [] }
      addMockOutput(stale)
      const file = { ...(libraryFiles.find((f) => f.id === 89) as LibraryEntry), output_id: stale.id }
      renderPage(<ArrangeDialog open sources={fromFiles([file])} onClose={vi.fn()} onArranged={vi.fn()} />)
      expect(await screen.findByRole('status')).toHaveTextContent(`${file.filename} was saved before Arrange existed`)
    })

    it('lists models by name with a search, and outputs with their date and size', async () => {
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={vi.fn()} />,
      )
      await user.click(await screen.findByRole('button', { name: 'Add from a model' }))
      const select = await screen.findByLabelText('Model')
      await waitFor(() => expect(within(select).getAllByRole('option').length).toBeGreaterThan(2))
      const names = within(select)
        .getAllByRole('option')
        .slice(1)
        .map((o) => o.textContent ?? '')
      expect(names).toEqual([...names].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' })))
      await user.type(screen.getByLabelText('Find a model'), 'keychain')
      const found = within(select).getAllByRole('option').slice(1)
      expect(found.length).toBeGreaterThan(0)
      expect(found.every((o) => /keychain/i.test(o.textContent ?? ''))).toBe(true)
      await user.selectOptions(select, 'name-keychain')
      expect(await screen.findByRole('checkbox', { name: /^Nova · .* ago · .*mm · needs a re-render$/ })).toBeInTheDocument()
    })

    it('finds a model by its slug as well as its name (#2754 review)', async () => {
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={vi.fn()} />,
      )
      await user.click(await screen.findByRole('button', { name: 'Add from a model' }))
      const select = await screen.findByLabelText('Model')
      await waitFor(() => expect(within(select).getAllByRole('option').length).toBeGreaterThan(2))
      // "Name Keychain" holds no hyphen: only its slug matches.
      await user.type(screen.getByLabelText('Find a model'), 'name-keychain')
      const found = within(select).getAllByRole('option').slice(1)
      expect(found.map((o) => o.textContent)).toEqual(['Name Keychain'])
    })
  })

  describe('controls (#2286)', () => {
    it('has a Close button, a primary Arrange, and starts on the name', async () => {
      const onClose = vi.fn()
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first])} onClose={onClose} onArranged={vi.fn()} />,
      )
      const dialog = await screen.findByRole('dialog', { name: 'Arrange' })
      expect(within(dialog).getByLabelText('Name')).toHaveFocus()
      expect(within(dialog).getByRole('button', { name: 'Arrange' }).className).toContain('bg-accent')
      await user.click(within(dialog).getByRole('button', { name: 'Close' }))
      expect(onClose).toHaveBeenCalledOnce()
    })

    it('takes a row out with Remove, and leaves it out of the request', async () => {
      const onArranged = vi.fn()
      const bin: Output = {
        ...first,
        id: '8'.repeat(32),
        name: 'Bin',
        manifest: [{ ...first.manifest![0]!, part: 'piece-bin', bom_piece: 'bin', count: 1 }],
      }
      addMockOutput(bin)
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first, bin])} onClose={vi.fn()} onArranged={onArranged} />,
      )
      const label = await screen.findByLabelText(/^Copies of .*Bin/)
      const rowLabel = label.getAttribute('aria-label')!.replace(/^Copies of /, '')
      await user.click(screen.getByRole('button', { name: `Remove ${rowLabel}` }))
      expect(screen.queryByLabelText(`Copies of ${rowLabel}`)).not.toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
      expect(lastArrangeRequest()?.objects.map((o) => o.output_id)).toEqual([first.id])
    })

    it('files under the other template once every row of the first is removed', async () => {
      const onArranged = vi.fn()
      const bin: Output = {
        ...first,
        id: '7'.repeat(32),
        slug: 'gridfinity-bin',
        name: 'Bin',
        manifest: [{ ...first.manifest![0]!, part: 'piece-bin', slug: 'gridfinity-bin', bom_piece: 'bin', count: 1 }],
      }
      addMockOutput(bin)
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first, bin])} onClose={vi.fn()} onArranged={onArranged} />,
      )
      await screen.findByLabelText('Copies of bin — Bin')
      const firstName = first.name ?? first.id
      for (const button of screen.getAllByRole('button', { name: new RegExp(` — ${firstName}$`) })) {
        await user.click(button)
      }
      expect(screen.queryByLabelText(new RegExp(`^Copies of .* — ${firstName}$`))).not.toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
      expect(lastArrangeRequest()).toMatchObject({
        slug: 'gridfinity-bin',
        objects: [{ output_id: bin.id, part: 'piece-bin', count: 1 }],
        // Named for what it holds, not the template it no longer does.
        name: `Arrange of ${fixtures.models.find((m) => m.slug === 'gridfinity-bin')!.name}`,
      })
    })

    it('saves an unnamed arrange under the models it holds, shown as the placeholder', async () => {
      const onArranged = vi.fn()
      const { user } = renderPage(
        <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={onArranged} />,
      )
      await waitFor(() => expect(screen.getByLabelText('Name')).toHaveAttribute('placeholder', 'Arrange of Name Keychain'))
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
      expect(lastArrangeRequest()?.name).toBe('Arrange of Name Keychain')
    })
  })

  describe('the default name (#2286)', () => {
    const wand = { ...(libraryFiles.find((f) => f.id === 67) as LibraryEntry), output_id: null }

    it('stays within the 200 characters the server takes, however long the names', async () => {
      const onArranged = vi.fn()
      const long = { ...wand, filename: `${'w'.repeat(240)}.3mf` }
      const { user } = renderPage(
        <ArrangeDialog open sources={[...fromOutputs([first]), ...fromFiles([long])]} onClose={vi.fn()} onArranged={onArranged} />,
      )
      await screen.findByLabelText(`Copies of Wand — ${long.filename}`)
      await waitFor(() =>
        expect(screen.getByLabelText('Name')).toHaveAttribute('placeholder', expect.stringContaining('1 more')),
      )
      const placeholder = screen.getByLabelText('Name').getAttribute('placeholder')!
      expect(placeholder.length).toBeLessThanOrEqual(200)
      // The model's name fits; the file that does not is counted rather than cut off.
      expect(placeholder).toBe('Arrange of Name Keychain, 1 more')
      expect(screen.getByLabelText('Name')).toHaveAttribute('maxLength', '200')
      await user.click(screen.getByRole('button', { name: 'Arrange' }))
      await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
      expect(lastArrangeRequest()?.name).toBe(placeholder)
    })

    it('cuts a lone name too long to fit, with an ellipsis', async () => {
      const long = { ...wand, filename: `${'w'.repeat(240)}.3mf` }
      renderPage(<ArrangeDialog open sources={fromFiles([long])} onClose={vi.fn()} onArranged={vi.fn()} />)
      await screen.findByLabelText(`Copies of Wand — ${long.filename}`)
      const placeholder = screen.getByLabelText('Name').getAttribute('placeholder')!
      expect(placeholder).toHaveLength(200)
      expect(placeholder).toMatch(/^Arrange of w+…$/)
    })

    it('leaves out a library file whose every row was removed, and Restore brings them back', async () => {
      const { user } = renderPage(
        <ArrangeDialog open sources={[...fromOutputs([first]), ...fromFiles([wand])]} onClose={vi.fn()} onArranged={vi.fn()} />,
      )
      const stemmed = wand.filename.replace(/\.[^.]+$/, '')
      await waitFor(() =>
        expect(screen.getByLabelText('Name')).toHaveAttribute('placeholder', `Arrange of Name Keychain, ${stemmed}`),
      )
      for (const button of screen.getAllByRole('button', { name: new RegExp(` — ${wand.filename}$`) })) {
        await user.click(button)
      }
      expect(screen.getByLabelText('Name')).toHaveAttribute('placeholder', 'Arrange of Name Keychain')
      await user.click(screen.getByRole('button', { name: 'Restore 2 removed rows' }))
      expect(screen.getByLabelText(`Copies of Wand — ${wand.filename}`)).toBeInTheDocument()
      expect(screen.getByLabelText('Name')).toHaveAttribute('placeholder', `Arrange of Name Keychain, ${stemmed}`)
      expect(screen.queryByRole('button', { name: /^Restore/ })).not.toBeInTheDocument()
    })
  })

  it('says it is saving once the layout is done, and how long a stage has run (#2283)', async () => {
    server.use(http.post('/api/v1/models/:slug/outputs', () => new Promise<never>(() => {})))
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={vi.fn()} />,
    )
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    expect(await screen.findByText('Saving the arranged output…', {}, { timeout: 5000 })).toHaveAttribute(
      'aria-live',
      'polite',
    )
    expect(screen.queryByText('Laying out the objects…')).not.toBeInTheDocument()
    expect(await screen.findByTestId('arrange-elapsed', {}, { timeout: 5000 })).toHaveTextContent(/^\d+ s$/)
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

  it('offers Try again after a failed save, which keeps the save key so one output is saved', async () => {
    const save = vi
      .spyOn(api, 'createOutput')
      .mockRejectedValueOnce(
        new ApiError({
          type: 'https://scadbuddy.dev/problems/temporal-refused',
          title: 'Refused',
          status: 500,
          detail: 'ScadBuddy could not start this; its server log says why.',
          may_have_started: true,
        }),
      )
      .mockResolvedValueOnce({ ...first, id: 'o-new' })
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={onArranged} />,
    )
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('its server log says why')
    expect(screen.queryByText(/Temporal/)).toBeNull()
    const arrange = vi.spyOn(api, 'arrangeOutputs')
    arrange.mockClear()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalled())
    // Only the save is tried again: the same job, under the same key.
    expect(arrange).not.toHaveBeenCalled()
    const [failed, again] = save.mock.calls
    expect(again![1]).toBe(failed![1])
    expect(failed![5]).toBeTruthy()
    expect(again![5]).toBe(failed![5])
  })

  it('keeps the save key on Try again when the model list answered in between (#2749 review)', async () => {
    let answer: (models: typeof fixtures.models) => void = () => {}
    vi.spyOn(api, 'listModels').mockReturnValue(new Promise((resolve) => (answer = resolve)))
    const save = vi
      .spyOn(api, 'createOutput')
      .mockRejectedValueOnce(
        new ApiError({ title: 'No answer', status: 0, detail: 'The server did not answer.', may_have_started: true }),
      )
      .mockResolvedValueOnce({ ...first, id: 'o-new' })
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={onArranged} />,
    )
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await screen.findByRole('alert')
    // The default name exists only now, after the first attempt.
    answer(fixtures.models)
    await waitFor(() => expect(screen.getByLabelText('Name')).toHaveAttribute('placeholder', 'Arrange of Name Keychain'))
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalled())
    const [failed, again] = save.mock.calls
    expect(again![1]).toBe(failed![1])
    expect(again![5]).toBe(failed![5])
  })

  it('joins the arrange it started on Try again, under the same key (#3526)', async () => {
    const arrange = vi
      .spyOn(api, 'arrangeOutputs')
      .mockRejectedValueOnce(
        new ApiError({ title: 'No answer', status: 0, detail: 'The server did not answer.', may_have_started: true }),
      )
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={onArranged} />,
    )
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await screen.findByRole('alert')
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalled())
    const [failed, again] = arrange.mock.calls
    expect(failed![2]).toBeTruthy()
    expect(again![2]).toBe(failed![2])
  })

  it('offers no Try again for a request the server refused as it is', async () => {
    vi.spyOn(api, 'createOutput').mockRejectedValueOnce(
      new ApiError({ type: 'about:blank', title: 'Unprocessable', status: 422, detail: 'no such template' }),
    )
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={vi.fn()} />,
    )
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('no such template')
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('tries a save that definitely failed again under a new key, not the recorded failure', async () => {
    const save = vi
      .spyOn(api, 'createOutput')
      .mockRejectedValueOnce(
        new ApiError({ type: 'about:blank', title: 'Failed', status: 500, detail: 'ScadBuddy failed unexpectedly' }),
      )
      .mockResolvedValueOnce({ ...first, id: 'o-new' })
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={onArranged} />,
    )
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await screen.findByRole('alert')
    const arrange = vi.spyOn(api, 'arrangeOutputs')
    arrange.mockClear()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalled())
    // The same finished job, but a new key: the old one would replay the recorded 500.
    expect(arrange).not.toHaveBeenCalled()
    const [failed, again] = save.mock.calls
    expect(again![1]).toBe(failed![1])
    expect(again![5]).toBeTruthy()
    expect(again![5]).not.toBe(failed![5])
  })

  it('arranges anew after the save of a finished arrange was refused', async () => {
    vi.spyOn(api, 'createOutput')
      .mockRejectedValueOnce(
        new ApiError({ type: 'about:blank', title: 'Not Found', status: 404, detail: 'the result of job is gone' }),
      )
      .mockResolvedValueOnce({ ...first, id: 'o-new' })
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={vi.fn()} />,
    )
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await screen.findByRole('alert')
    const arrange = vi.spyOn(api, 'arrangeOutputs')
    arrange.mockClear()
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(arrange).toHaveBeenCalledTimes(1))
  })

  it('saves a changed arrange under a new key', async () => {
    const save = vi
      .spyOn(api, 'createOutput')
      .mockRejectedValueOnce(new Error('could not save'))
      .mockResolvedValueOnce({ ...first, id: 'o-new' })
    const { user } = renderPage(
      <ArrangeDialog open sources={fromOutputs([first])} onClose={vi.fn()} onArranged={vi.fn()} />,
    )
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await screen.findByRole('alert')
    await user.type(screen.getByLabelText(/name/i), 'renamed')
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(save).toHaveBeenCalledTimes(2))
    expect(save.mock.calls[1]![5]).not.toBe(save.mock.calls[0]![5])
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

describe('objectNames (#2754 review)', () => {
  it('names a manifest in one pass, not one per object', () => {
    const manifest = (parts: number) =>
      ({
        ...fixtures.outputs[0],
        manifest: Array.from({ length: parts }, (_, i) => ({ part: `p${i}`, slug: 'same', bom_piece: null, count: 1 })),
      }) as unknown as Output
    const time = (parts: number) => {
      const output = manifest(parts)
      let best = Infinity
      for (let i = 0; i < 3; i++) {
        const began = performance.now()
        objectNames(output, (slug) => slug)
        best = Math.min(best, performance.now() - began)
      }
      return best
    }
    const names = objectNames(manifest(3), (slug) => slug)
    expect([...names.values()]).toEqual(['same 1', 'same 2', 'same 3'])
    // Four times the parts is about four times the work; a count per object is sixteen.
    expect(time(16_000)).toBeLessThan(8 * time(4_000) + 20)
  })
})
