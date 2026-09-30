import { fireEvent, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { Job, Output } from '../api/types'
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
