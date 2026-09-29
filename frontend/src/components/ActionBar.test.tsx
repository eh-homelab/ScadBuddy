import { fireEvent, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
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

function setup(rendering = false, current: Job | undefined = job, output?: Output) {
  const view = renderPage(
    <ActionBar
      slug="name-keychain"
      job={current}
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

// #709 — the status area reserves a 16rem basis only while it shows something, so the
// buttons wrap below a status instead of truncating it, and never wrap for an empty one.
describe('the status area', () => {
  afterEach(() => vi.restoreAllMocks())

  it('reserves no basis while it is empty', () => {
    setup(false, { ...job, colors: [] })
    expect(screen.getByTestId('action-status')).toHaveClass('flex-1')
    expect(screen.getByTestId('action-status')).not.toHaveClass('flex-[1_1_16rem]')
  })

  it('reserves the basis while it shows the colors', () => {
    setup()
    expect(screen.getByTestId('action-status')).toHaveClass('flex-[1_1_16rem]')
  })

  it('reserves the basis while it shows the saved output', () => {
    setup(false, { ...job, colors: [] }, { id: 'b'.repeat(32), name: 'Keychain' } as Output)
    expect(screen.getByText('Saved Keychain')).toBeInTheDocument()
    expect(screen.getByTestId('action-status')).toHaveClass('flex-[1_1_16rem]')
  })

  it('reserves the basis while it shows only an error', async () => {
    vi.spyOn(api, 'createOutput').mockRejectedValue(new Error('down'))
    const { user } = setup(false, { ...job, colors: [] })
    expect(screen.getByTestId('action-status')).toHaveClass('flex-1')

    await user.click(screen.getByTestId('generate'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save this output.')
    expect(screen.getByTestId('action-status')).toHaveClass('flex-[1_1_16rem]')
  })
})
