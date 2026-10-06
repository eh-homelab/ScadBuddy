import { trace } from '@opentelemetry/api'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'
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

  describe('menu button keyboard pattern (#968, WAI-ARIA APG Menu Button)', () => {
    it('opens on Enter with focus on the first item, and Escape returns focus to the button', async () => {
      const { user } = setup()
      const toggle = screen.getByRole('button', { name: 'More to generate' })
      toggle.focus()
      await user.keyboard('{Enter}')
      const item = screen.getByRole('menuitem', { name: /Rendered image/ })
      expect(item).toHaveFocus()
      expect(item).toHaveAttribute('tabindex', '-1')

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
