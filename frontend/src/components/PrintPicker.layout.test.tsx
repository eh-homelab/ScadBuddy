import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Output } from '../api/types'
import { queuedResult } from '../mocks/choices'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { choose } from '../test/select'
import { renderPage } from '../test/utils'
import { SECTIONS_KEY } from './print/printSections'
import { PrintPicker } from './PrintPicker'

// #6885 — the dialog's layout: one summary row in both modes (printer, nozzle, bed plate,
// quality), Advanced's collapsible sections, the footer's estimate and blocking check, and
// a remembered held option as a neutral chip.

vi.mock('./print/PlateScene', () => ({ PlateScene: () => <div data-testid="plate-scene" /> }))

const output = fixtures.outputs[0] as Output

beforeEach(() => {
  resetMockState()
  window.localStorage.removeItem(SECTIONS_KEY)
})
afterEach(() => vi.restoreAllMocks())

function renderPicker() {
  return renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
}

async function loaded() {
  await screen.findByRole('switch', { name: 'Advanced' })
  await screen.findByTestId('filament-slot-1')
}

describe('PrintPicker · the summary row (#6885)', () => {
  it('shows in Simple what will print: the printer, the nozzle, the bed plate and the quality', async () => {
    renderPicker()
    await loaded()
    const row = screen.getByTestId('print-summary')
    expect(within(row).getByRole('combobox', { name: 'Printer' })).toHaveTextContent('3DP-31B-598')
    expect(within(row).getByTestId('print-summary-nozzle')).toHaveTextContent(/^0\.4 mm (Standard|High Flow)$/)
    expect(within(row).getByTestId('print-summary-plate')).toHaveTextContent('Textured PEI Plate')
    expect(within(row).getByTestId('print-summary-quality')).toHaveTextContent('0.20mm Standard @BBL H2C')
    // Each step stays shut until its cell is opened.
    expect(screen.queryByLabelText('Plate')).toBeNull()
    expect(screen.queryByRole('group', { name: /nozzles/i })).toBeNull()
  })

  it('opens the bed plate in place in Simple, sends the plate chosen there, and says to swap it (#6884)', async () => {
    const run = vi.spyOn(api, 'runPrint').mockResolvedValue(queuedResult)
    const { user } = renderPicker()
    await loaded()
    await user.click(screen.getByTestId('print-summary-plate-open'))
    await choose(user, screen.getByLabelText('Plate'), 'Engineering Plate')
    expect(screen.getByTestId('print-summary-plate')).toHaveTextContent('Engineering Plate')
    // Shut again, the row still says to swap the plate.
    await user.click(screen.getByTestId('print-summary-plate-open'))
    expect(screen.queryByLabelText('Plate')).toBeNull()
    expect(screen.getByTestId('print-summary-plate-note')).toHaveTextContent(
      "The 3DP-31B-598's last print used Textured PEI Plate. Swap to Engineering Plate before this starts.",
    )

    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())
    await user.click(screen.getByTestId('run-print'))
    await waitFor(() => expect(run).toHaveBeenCalled())
    expect(run.mock.calls[0]![1].choices).toMatchObject({ bed_type: 'Engineering Plate' })
  })

  it('has one nozzle control: the nozzle cell opens the nozzle step in Simple, and Advanced has it once', async () => {
    const run = vi.spyOn(api, 'runPrint').mockResolvedValue(queuedResult)
    const { user } = renderPicker()
    await loaded()
    expect(screen.queryByTestId('flow-high_flow')).toBeNull()

    await user.click(screen.getByTestId('print-summary-nozzle-open'))
    const step = screen.getByRole('group', { name: /nozzles/i })
    await user.click(within(step).getByRole('radio', { name: 'Left High Flow' }))
    await user.click(within(step).getByRole('radio', { name: 'Right High Flow' }))
    expect(screen.getByTestId('print-summary-nozzle')).toHaveTextContent('0.4 mm High Flow')

    await user.click(screen.getByRole('switch', { name: 'Advanced' }))
    expect(screen.getAllByRole('group', { name: /nozzles/i })).toHaveLength(1)
    await user.click(screen.getByRole('switch', { name: 'Advanced' }))

    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())
    await user.click(screen.getByTestId('run-print'))
    await waitFor(() => expect(run).toHaveBeenCalled())
    expect(run.mock.calls[0]![1].choices?.nozzles).toEqual([
      { size: '0.4', flow: 'high_flow' },
      { size: '0.4', flow: 'high_flow' },
    ])
  })
})

describe('PrintPicker · Advanced sections (#6885)', () => {
  it('groups the steps into sections, and remembers a closed one for this viewer', async () => {
    const { user, unmount } = renderPicker()
    await loaded()
    await user.click(screen.getByRole('switch', { name: 'Advanced' }))

    const titles = screen
      .getAllByRole('region')
      .filter((region) => region.id.startsWith('print-section-'))
      .map((region) => document.getElementById(region.getAttribute('aria-labelledby') ?? '')?.textContent)
    expect(titles).toEqual([
      'Nozzles',
      'Quality and plate',
      'Supports, prime tower and print sequence',
      'Filing and copies',
      'Slicer settings',
    ])
    const tower = screen.getByRole('region', { name: 'Supports, prime tower and print sequence' })
    const sequence = within(tower).getAllByText('Print sequence')[0]!
    expect(sequence).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Supports, prime tower and print sequence' }))
    expect(sequence).not.toBeVisible()
    expect(JSON.parse(window.localStorage.getItem(SECTIONS_KEY) ?? '{}')).toMatchObject({ tower: false, nozzles: true })

    unmount()
    const again = renderPicker()
    await loaded()
    await again.user.click(screen.getByRole('switch', { name: 'Advanced' }))
    expect(screen.getByRole('button', { name: 'Supports, prime tower and print sequence' })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
    expect(screen.getByRole('button', { name: 'Nozzles' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('opens a closed section from its summary cell', async () => {
    window.localStorage.setItem(SECTIONS_KEY, JSON.stringify({ nozzles: false }))
    const { user } = renderPicker()
    await loaded()
    await user.click(screen.getByRole('switch', { name: 'Advanced' }))
    expect(screen.getByRole('button', { name: 'Nozzles' })).toHaveAttribute('aria-expanded', 'false')
    await user.click(screen.getByTestId('print-summary-nozzle-open'))
    expect(screen.getByRole('button', { name: 'Nozzles' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('names in a closed section what it still sends: a remembered hold, the sequence', async () => {
    window.localStorage.setItem(SECTIONS_KEY, JSON.stringify({ filing: false, tower: false }))
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({ ...fixtures.printOptions, global_options: { manual_start: true } }),
      ),
    )
    const { user } = renderPicker()
    await loaded()
    await user.click(screen.getByRole('switch', { name: 'Advanced' }))
    const filing = screen.getByRole('button', { name: 'Filing and copies' })
    expect(filing).toHaveAttribute('aria-expanded', 'false')
    await waitFor(() => expect(filing).toHaveTextContent('Waits for a manual start'))

    const tower = screen.getByRole('button', { name: 'Supports, prime tower and print sequence' })
    expect(tower).not.toHaveTextContent('Print sequence by object')
    await user.click(tower)
    await choose(user, screen.getByLabelText('Print sequence'), 'By object')
    await user.click(tower)
    expect(tower).toHaveTextContent('Print sequence by object')
  })

  it('keeps every section open when the storage cannot be read', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const { user } = renderPicker()
    await loaded()
    await user.click(screen.getByRole('switch', { name: 'Advanced' }))
    expect(screen.getByRole('button', { name: 'Slicer settings' })).toHaveAttribute('aria-expanded', 'true')
  })
})

describe('PrintPicker · the footer (#6885)', () => {
  it('shows the slice estimate beside Print', async () => {
    renderPicker()
    await loaded()
    const footer = await screen.findByTestId('footer-estimate', {}, { timeout: 8000 })
    expect(within(footer).getByTestId('footer-time')).toHaveTextContent('30m')
    expect(within(footer).getByTestId('footer-grams')).toHaveTextContent('6.7 g')
  })

  it('names the first check that holds Print', async () => {
    const refusal = 'Slot 1 has no spool this printer can feed.'
    server.use(http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [refusal], warnings: [] })))
    renderPicker()
    await loaded()
    expect(await screen.findByTestId('footer-blocking')).toHaveTextContent(refusal)
    expect(screen.getByTestId('run-print')).toBeDisabled()
  })
})

describe('PrintPicker · a remembered held option (#6885)', () => {
  it('shows as a neutral chip with "Not this time" and "Stop remembering", not an error', async () => {
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({ ...fixtures.printOptions, global_options: { manual_start: true } }),
      ),
    )
    renderPicker()
    await loaded()
    const chip = await screen.findByTestId('holding-manual_start')
    expect(chip).toHaveTextContent('Waits for a manual start.')
    expect(chip).toHaveTextContent('Remembered for every print.')
    expect(chip.className).not.toMatch(/warn|destructive/)
    expect(chip).not.toHaveAttribute('role', 'alert')
    expect(within(chip).getByRole('button', { name: 'Not this time' })).toBeInTheDocument()
    expect(within(chip).getByRole('button', { name: 'Stop remembering' })).toBeInTheDocument()
  })
})
