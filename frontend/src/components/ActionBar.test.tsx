import { trace } from '@opentelemetry/api'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Job, Output } from '../api/types'
import { NO_EXTRA } from '../lib/inputs'
import { outputs } from '../mocks/fixtures'
import { installTestTracing } from '../test/tracing'
import { renderPage } from '../test/utils'
import { ActionBar } from './ActionBar'

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
      const { user } = setup(false, job, undefined, {
        onGenerated,
        capture: async () => new Blob(['png'], { type: 'image/png' }),
      })
      await user.click(screen.getByRole('button', { name: 'Generate' }))
      await waitFor(() => expect(onGenerated).toHaveBeenCalled())
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
