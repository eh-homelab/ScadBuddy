import { trace } from '@opentelemetry/api'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Job, Output } from '../api/types'
import { bridge } from '../agent/bridge'
import { NO_EXTRA, type InputsExtra } from '../lib/inputs'
import { outputs } from '../mocks/fixtures'
import { installTestTracing } from '../test/tracing'
import { renderPage } from '../test/utils'
import { ActionBar } from './ActionBar'
import { chooseProject, pickerValue } from '../test/projectPicker'
import { CURSOR_HEADER, trackRequest } from '../lib/readCursor'
import { getRealtime } from '../lib/realtime'
import { fakeRealtime } from '../lib/realtime.fake'

const job: Job = {
  id: 'a'.repeat(32),
  slug: 'name-keychain',
  status: 'done',
  created_at: '2026-09-29T00:00:00Z',
  preview_url: '/api/v1/jobs/aaaa/preview.glb',
  colors: ['#FF0000'],
}

function setup(
  rendering = false,
  job_: Job | undefined = job,
  output: Output | undefined = undefined,
  overrides: Partial<ComponentProps<typeof ActionBar>> = {},
) {
  const view = renderPage(
    <ActionBar
      slug="name-keychain"
      job={job_}
      rendering={rendering}
      output={output}
      capture={async () => null}
      captureImage={async () => null}
      viewSize={() => ({ width: 800, height: 500 })}
      extra={NO_EXTRA}
      fit={undefined}
      onPrinterModel={() => undefined}
      onGenerated={() => undefined}
      onSent={() => undefined}
      onRan={() => undefined}
      {...overrides}
    />,
  )
  return view
}

describe("Generate's menu", () => {
  it('opens the rendered image dialog from its item', async () => {
    const { user } = setup()
    const toggle = screen.getByRole('button', { name: 'More to generate' })
    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')

    await user.click(screen.getByRole('menuitem', { name: /Rendered image/ }))
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Rendered image' })).toBeInTheDocument()
  })

  it('offers the colour breakdown only for a render with more than one colour (#1289)', async () => {
    const { user } = setup()
    await user.click(screen.getByRole('button', { name: 'More to generate' }))
    expect(screen.queryByRole('menuitem', { name: /Colours/ })).not.toBeInTheDocument()
  })

  it('opens the colour breakdown of the render on screen (#1289)', async () => {
    const getJobColours = vi
      .spyOn(api, 'getJobColours')
      .mockResolvedValue({ image: new Blob(['png']), colours: ['#FF0000', '#00FF00'], columns: 2 })
    URL.createObjectURL = vi.fn(() => 'blob:colours')
    URL.revokeObjectURL = vi.fn()
    const { user } = setup(false, { ...job, colors: ['#FF0000', '#00FF00'] })
    await user.click(screen.getByRole('button', { name: 'More to generate' }))
    await user.click(screen.getByRole('menuitem', { name: /Colours/ }))

    expect(screen.getByRole('dialog', { name: 'Colours' })).toBeInTheDocument()
    expect(await screen.findByText('Tile 2: Extruder 2, #00FF00')).toBeInTheDocument()
    expect(getJobColours).toHaveBeenCalledWith(job.id, 'iso', expect.any(AbortSignal))
    getJobColours.mockRestore()
  })

  it('offers "Add to order…" once an output exists, opening its dialog (#4249)', async () => {
    const { user, unmount } = setup()
    await user.click(screen.getByRole('button', { name: 'More to generate' }))
    expect(screen.getByRole('menuitem', { name: /Add to order/ })).toHaveAttribute('aria-disabled', 'true')
    await user.keyboard('{Escape}')
    unmount()
    const output = { id: 'a'.repeat(32), slug: 'name-keychain', name: 'Reagan' } as unknown as Output
    const { user: again } = setup(false, job, output)
    await again.click(screen.getByRole('button', { name: 'More to generate' }))
    await again.click(screen.getByRole('menuitem', { name: /Add to order/ }))
    expect(await screen.findByRole('dialog', { name: 'Add to order' })).toBeInTheDocument()
  })

  it('closes on a click outside it, and not on one inside it', async () => {
    const { user } = setup()
    await user.click(screen.getByRole('button', { name: 'More to generate' }))
    const menu = screen.getByRole('menu')

    fireEvent.mouseDown(menu)
    expect(screen.getByRole('menu')).toBeInTheDocument()

    await user.click(screen.getByText('1 colour'))
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'More to generate' })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
  })

  it('closes on Escape', async () => {
    const { user } = setup()
    await user.click(screen.getByRole('button', { name: 'More to generate' }))
    expect(screen.getByRole('menu')).toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('toggles closed from its own button', async () => {
    const { user } = setup()
    const toggle = screen.getByRole('button', { name: 'More to generate' })
    await user.click(toggle)
    await user.click(toggle)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  describe('menu button keyboard pattern (#968, WAI-ARIA APG Menu Button)', () => {
    it('opens on Enter with focus on the first item, and Escape returns focus to the button', async () => {
      const { user } = setup()
      const toggle = screen.getByRole('button', { name: 'More to generate' })
      toggle.focus()
      await user.keyboard('{Enter}')
      const item = screen.getByRole('menuitem', { name: /Rendered image/ })
      expect(item).toHaveFocus()

      await user.keyboard('{Escape}')
      expect(screen.queryByRole('menu')).not.toBeInTheDocument()
      expect(toggle).toHaveFocus()
    })

    it('opens on ArrowDown and ArrowUp too', async () => {
      const { user } = setup()
      const toggle = screen.getByRole('button', { name: 'More to generate' })
      toggle.focus()
      await user.keyboard('{ArrowDown}')
      expect(screen.getByRole('menuitem', { name: /Rendered image/ })).toHaveFocus()
      await user.keyboard('{Escape}')

      await user.keyboard('{ArrowUp}')
      expect(screen.getByRole('menuitem', { name: /Rendered image/ })).toHaveFocus()
    })

    it('keeps arrow keys on its items', async () => {
      const { user } = setup()
      screen.getByRole('button', { name: 'More to generate' }).focus()
      await user.keyboard('{Enter}')
      const item = screen.getByRole('menuitem', { name: /Rendered image/ })
      for (const key of ['{ArrowDown}', '{ArrowUp}', '{Home}', '{End}']) {
        await user.keyboard(key)
        expect(item).toHaveFocus()
      }
    })

    it('closes when Tab leaves it, rather than staying open behind', async () => {
      const { user } = setup()
      screen.getByRole('button', { name: 'More to generate' }).focus()
      await user.keyboard('{Enter}')
      await user.tab()
      expect(screen.queryByRole('menu')).not.toBeInTheDocument()
      expect(document.activeElement).not.toBe(document.body)
    })

    it('chooses an item with Enter', async () => {
      const { user } = setup()
      screen.getByRole('button', { name: 'More to generate' }).focus()
      await user.keyboard('{Enter}')
      await user.keyboard('{Enter}')
      expect(screen.queryByRole('menu')).not.toBeInTheDocument()
      expect(screen.getByRole('dialog', { name: 'Rendered image' })).toBeInTheDocument()
    })
  })

  it('cannot open while the preview is rendering', () => {
    setup(true)
    expect(screen.getByRole('button', { name: 'More to generate' })).toBeDisabled()
  })
})

describe('Generate', () => {
  // #754 — between a param edit settling and the new render's `rendering` flag
  // flipping true, `job`/`rendering` still describe the PREVIOUS, already-`done`
  // render: `upToDate` is the only prop that already knows the preview is stale.
  it('is disabled while a newer render is pending, even though the previous job is done', () => {
    setup(false, job, undefined, { upToDate: false })
    expect(screen.getByRole('button', { name: 'Generate' })).toBeDisabled()
  })

  it('does nothing when clicked in that state', async () => {
    const onGenerated = vi.fn()
    const { user } = setup(false, job, undefined, { upToDate: false, onGenerated })
    await user.click(screen.getByRole('button', { name: 'Generate' }))
    expect(onGenerated).not.toHaveBeenCalled()
  })
})

describe('Generate, traced', () => {
  it('is one generate span, with the output request and the later thumbnail inside it', async () => {
    const tracing = installTestTracing()
    try {
      const active: Record<string, string | undefined> = {}
      vi.spyOn(api, 'createOutput').mockImplementation(async () => {
        active.create = trace.getActiveSpan()?.spanContext().spanId
        return outputs[0]!
      })
      vi.spyOn(api, 'putThumbnail').mockImplementation(async () => {
        active.thumbnail = trace.getActiveSpan()?.spanContext().spanId
      })
      const onGenerated = vi.fn()
      const extra = { demo: { touched: true } }
      const { user } = setup(false, job, undefined, {
        onGenerated,
        extra,
        capture: async () => new Blob(['png'], { type: 'image/png' }),
      })
      await user.click(screen.getByRole('button', { name: 'Generate' }))
      // With the UI state it saved with: a stale or default one would show "Saved …"
      // against the wrong state (#1471).
      await waitFor(() =>
        expect(onGenerated).toHaveBeenCalledWith(expect.objectContaining({ id: outputs[0]!.id }), extra),
      )
      await waitFor(() => expect(tracing.exporter.getFinishedSpans()).toHaveLength(1))

      const [span] = tracing.exporter.getFinishedSpans()
      expect(span?.name).toBe('generate')
      expect(span?.attributes).toEqual({
        'scadbuddy.slug': 'name-keychain',
        'scadbuddy.job_id': job.id,
        'scadbuddy.output_id': outputs[0]!.id,
      })
      // The thumbnail goes after an await (the capture): only `within` keeps it in the trace.
      expect(active).toEqual({ create: span?.spanContext().spanId, thumbnail: span?.spanContext().spanId })
    } finally {
      vi.restoreAllMocks()
      tracing.uninstall()
    }
  })
})

describe("the agent's generate (#1471)", () => {
  afterEach(() => vi.restoreAllMocks())

  function bar(extra: InputsExtra, output: Output | undefined, onGenerated: (o: Output, e: InputsExtra) => void) {
    return (
      <ActionBar
        slug="name-keychain"
        job={job}
        rendering={false}
        output={output}
        capture={async () => null}
        captureImage={async () => null}
        viewSize={() => ({ width: 800, height: 500 })}
        extra={extra}
        fit={undefined}
        onPrinterModel={() => undefined}
        onGenerated={onGenerated}
        onSent={() => undefined}
        onRan={() => undefined}
      />
    )
  }

  it('resolves, and says the output is superseded, when the UI state moves while it saves', async () => {
    let release!: (output: Output) => void
    const create = vi.spyOn(api, 'createOutput').mockImplementation(
      () => new Promise<Output>((resolve) => (release = resolve)),
    )
    const before = { demo: { step: 1 } }
    const onGenerated = vi.fn()
    const { rerender } = renderPage(bar(before, undefined, onGenerated))
    const outcome = bridge.call('generate', { timeout_ms: 5000 })
    await waitFor(() => expect(create).toHaveBeenCalledOnce())
    rerender(bar({ demo: { step: 2 } }, undefined, onGenerated))
    release(outputs[0]!)
    expect(await outcome).toMatchObject({
      ok: true,
      result: { output: { id: outputs[0]!.id }, superseded: true, note: expect.stringMatching(/generate again/i) },
    })
    expect(onGenerated).toHaveBeenCalledWith(expect.objectContaining({ id: outputs[0]!.id }), before)
  })

  it('does not call the output it shows superseded', async () => {
    vi.spyOn(api, 'createOutput').mockResolvedValue(outputs[0]!)
    const extra = { demo: { step: 1 } }
    let shown: Output | undefined
    const view = renderPage(bar(extra, undefined, (created) => (shown = created)))
    const outcome = bridge.call('generate', { timeout_ms: 5000 })
    await waitFor(() => expect(shown).toBeDefined())
    view.rerender(bar(extra, shown, () => undefined))
    const result = await outcome
    expect(result).toMatchObject({ ok: true, result: { output: { id: outputs[0]!.id } } })
    expect(result.ok && result.result).not.toHaveProperty('superseded')
  })
})

describe('keyboard focus and announcements (#967)', () => {
  /** Holds a promise open until `release`, so the busy state can be looked at. */
  function deferred<T>() {
    let release!: (value: T) => void
    const promise = new Promise<T>((resolve) => {
      release = resolve
    })
    return { promise, release }
  }

  afterEach(() => vi.restoreAllMocks())

  it('keeps focus on Generate while it runs and after, and announces the result', async () => {
    const held = deferred<Output>()
    const create = vi.spyOn(api, 'createOutput').mockImplementation(() => held.promise)
    const { user } = setup()
    const generate = screen.getByRole('button', { name: 'Generate' })
    generate.focus()

    await user.keyboard('{Enter}')
    const busy = screen.getByTestId('generate')
    expect(busy).toHaveTextContent('Generating')
    expect(busy).toHaveFocus()
    expect(busy).toHaveAttribute('aria-disabled', 'true')
    // Busy, so a second press is not a second output.
    await user.keyboard('{Enter}')
    expect(create).toHaveBeenCalledTimes(1)

    held.release(outputs[0]!)
    await waitFor(() => expect(screen.getByTestId('generate')).toHaveTextContent(/^Generate$/))
    expect(screen.getByTestId('generate')).toHaveFocus()
    expect(screen.getByTestId('generate')).not.toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByTestId('action-status')).toHaveTextContent(
      `Generated ${outputs[0]!.name ?? outputs[0]!.id.slice(0, 8)}. Download 3MF, Send to Bambuddy or Print it.`,
    )
    expect(screen.getByTestId('action-status')).toHaveAttribute('role', 'status')
  })

  it('announces a failed Generate in the live region', async () => {
    vi.spyOn(api, 'createOutput').mockRejectedValue(new Error('boom'))
    const { user } = setup()
    screen.getByRole('button', { name: 'Generate' }).focus()
    await user.keyboard('{Enter}')
    await waitFor(() =>
      expect(screen.getByTestId('action-status')).toHaveTextContent('Could not save this output.'),
    )
    expect(screen.getByRole('button', { name: 'Generate' })).toHaveFocus()
  })

  it('keeps focus on Download 3MF while it downloads, and announces it', async () => {
    const held = deferred<Response>()
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => held.promise)
    const { user } = setup(false, job, outputs[0])
    const download = screen.getByRole('button', { name: 'Download 3MF' })
    download.focus()

    await user.keyboard('{Enter}')
    expect(download).toHaveFocus()
    expect(download).toHaveAttribute('aria-disabled', 'true')

    held.release(new Response(new Blob(['3mf'])))
    await waitFor(() => expect(download).not.toHaveAttribute('aria-disabled', 'true'))
    expect(download).toHaveFocus()
    await waitFor(() => expect(screen.getByTestId('action-status')).toHaveTextContent('Downloaded'))
  })
})

describe("the remembered project across a model change (#1660, #2142's review)", () => {
  afterEach(() => vi.restoreAllMocks())

  function bar(slug: string) {
    return (
      <ActionBar
        slug={slug}
        job={{ ...job, slug }}
        rendering={false}
        output={undefined}
        capture={async () => null}
        captureImage={async () => null}
        viewSize={() => ({ width: 800, height: 500 })}
        extra={NO_EXTRA}
        fit={undefined}
        onPrinterModel={() => undefined}
        onGenerated={() => undefined}
        onSent={() => undefined}
        onRan={() => undefined}
      />
    )
  }

  it("never moves the picker to the last model's project once the page is on another", async () => {
    vi.spyOn(api, 'getModelProjects').mockImplementation(async (slug) => ({
      projects: slug === 'apple' ? [{ preset_id: null, project_id: 1 }] : [],
    }))
    vi.spyOn(api, 'createOutput').mockResolvedValue(outputs[0]!)
    vi.spyOn(api, 'fileIntoProject').mockResolvedValue({ file_id: 1, folder_id: 2, name: 'x.3mf' } as never)
    let release!: () => void
    vi.spyOn(api, 'putModelProject').mockImplementation(
      () =>
        new Promise((resolve) => {
          release = () => resolve({ projects: [{ preset_id: null, project_id: 2 }] })
        }),
    )
    const { user, rerender } = renderPage(bar('apple'))
    const picker = () => screen.getByTestId('customize-project-select')
    await waitFor(() => expect(pickerValue(picker())).toBe('1'))
    await chooseProject(user, picker(), 2)
    await user.click(screen.getByRole('button', { name: 'Generate' }))
    await user.click(await screen.findByTestId('generate-file-chosen'))
    await waitFor(() => expect(api.putModelProject).toHaveBeenCalledOnce())

    // The page moves to a model filed nowhere, then apple's filing answers.
    rerender(bar('keychain'))
    await waitFor(() => expect(pickerValue(picker())).toBe(''))
    release()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(pickerValue(picker())).toBe('')
    // And back on apple, its own projects are read again, not the late answer's.
    rerender(bar('apple'))
    await waitFor(() => expect(pickerValue(picker())).toBe('1'))
  })

  describe("and the settings subscription's confirmation (#1039)", () => {
    /** `getModelProjects`, answering with cursor `e5` the way the server stamps a read. */
    function counted() {
      let reads = 0
      vi.spyOn(api, 'getModelProjects').mockImplementation(() => {
        const answered = trackRequest()
        reads += 1
        return Promise.resolve().then(() => {
          answered(new Headers({ [CURSOR_HEADER]: 'e5' }))
          return { projects: [] }
        })
      })
      return () => reads
    }

    it("reads the remembered projects once when the read saw the server's last event", async () => {
      const reads = counted()
      fakeRealtime({ confirmation: { last: 'e5' } })
      const check = vi.spyOn(getRealtime(), 'check')
      renderPage(bar('apple'))
      await waitFor(() => expect(reads()).toBe(1))
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(reads()).toBe(1)
      expect(check).not.toHaveBeenCalled()
    })

    it('reads them again when the server says settings changed since', async () => {
      const reads = counted()
      fakeRealtime({ confirmation: { last: 'e9' } })
      const check = vi.spyOn(getRealtime(), 'check').mockResolvedValue(['settings'])
      renderPage(bar('apple'))
      await waitFor(() => expect(reads()).toBe(2))
      expect(check).toHaveBeenCalledWith(['e5'], ['settings'])
    })
  })
})
