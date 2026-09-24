import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import type { PrintOptions } from '../api/types'
import { printOptions as printOptionsFixture } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { PrintOptionsDisclosure } from './PrintOptionsDisclosure'

function Harness({ printerId }: { printerId?: number | null } = {}) {
  const [value, setValue] = useState<PrintOptions>({})
  return (
    <>
      <PrintOptionsDisclosure
        slug="name-keychain"
        printerId={printerId}
        value={value}
        onChange={setValue}
      />
      <pre data-testid="sent">{JSON.stringify(value)}</pre>
    </>
  )
}

/** The disclosure is collapsed by default, so every test opens it first. */
async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByText('Options'))
  await waitFor(() => expect(screen.getByLabelText('Timelapse')).toBeInTheDocument())
}

function row(label: string) {
  return screen.getByLabelText(label).closest('li') as HTMLElement
}

describe('PrintOptionsDisclosure', () => {
  it('starts collapsed and says nothing is changed', async () => {
    render(<Harness />)
    expect(screen.queryByLabelText('Timelapse')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByText("Bambuddy's defaults")).toBeInTheDocument())
  })

  it('shows every option with the value Bambuddy would use and where it came from', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await open(user)

    expect(within(row('Timelapse')).getByText(/Off · Bambuddy's default/)).toBeInTheDocument()
    expect(
      within(row('Bed levelling')).getByText(/Auto · Bambuddy's default/),
    ).toBeInTheDocument()
    // The select offers Bambuddy's default as a real, named choice.
    expect(screen.getByLabelText('Timelapse')).toHaveValue('')
    expect(
      within(row('Timelapse')).getByRole('option', { name: 'Bambuddy default (Off)' }),
    ).toBeInTheDocument()
  })

  it('marks a value that differs from Bambuddy’s default and not one that matches it', async () => {
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({
          ...printOptionsFixture,
          global_options: { timelapse: true, layer_inspect: false },
        }),
      ),
    )
    const user = userEvent.setup()
    render(<Harness />)
    await open(user)

    expect(screen.getByTestId('non-default-timelapse')).toBeInTheDocument()
    // layer_inspect is remembered as false, which is exactly Bambuddy's default.
    expect(screen.queryByTestId('non-default-layer_inspect')).not.toBeInTheDocument()
    expect(screen.getByText('1 change')).toBeInTheDocument()
  })

  it('resolves per-printer over global and per-model over both', async () => {
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({
          ...printOptionsFixture,
          global_options: { timelapse: true, use_ams: false },
          printers: { '1': { timelapse: false } },
          models: { 'name-keychain': { quantity: 4 } },
        }),
      ),
    )
    const user = userEvent.setup()
    render(<Harness />)
    await open(user)

    expect(within(row('Timelapse')).getByText(/Off · from this printer/)).toBeInTheDocument()
    expect(within(row('Use the AMS')).getByText(/Off · from everywhere/)).toBeInTheDocument()
    expect(within(row('Quantity')).getByText(/4 · from this model/)).toBeInTheDocument()
  })

  it('reports a per-send change to the parent without saving it', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await open(user)

    await user.selectOptions(screen.getByLabelText('Timelapse'), 'true')

    expect(screen.getByTestId('sent')).toHaveTextContent('{"timelapse":true}')
    expect(within(row('Timelapse')).getByText(/On · from this send/)).toBeInTheDocument()
  })

  it('clears a per-send change back to Bambuddy’s default', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await open(user)
    await user.selectOptions(screen.getByLabelText('Timelapse'), 'true')

    await user.selectOptions(screen.getByLabelText('Timelapse'), '')

    expect(screen.getByTestId('sent')).toHaveTextContent('{}')
  })

  it('remembers the edits for the printer the server named', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await open(user)
    await user.selectOptions(screen.getByLabelText('Timelapse'), 'false')

    await user.click(screen.getByRole('button', { name: 'Remember' }))

    await waitFor(() =>
      expect(screen.getByText('Remembered for this printer.')).toBeInTheDocument(),
    )
    // The per-send overlay is emptied, because the value now comes from the printer.
    expect(screen.getByTestId('sent')).toHaveTextContent('{}')
    expect(within(row('Timelapse')).getByText(/Off · from this printer/)).toBeInTheDocument()
  })

  it('remembers for the model when that scope is chosen', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await open(user)
    await user.selectOptions(screen.getByLabelText('Timelapse'), 'true')
    await user.selectOptions(screen.getByLabelText('Remember for'), 'model')

    await user.click(screen.getByRole('button', { name: 'Remember' }))

    await waitFor(() => expect(screen.getByText('Remembered for this model.')).toBeInTheDocument())
    expect(within(row('Timelapse')).getByText(/On · from this model/)).toBeInTheDocument()
  })

  it('keeps what a scope already remembered when a second option is saved to it', async () => {
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({ ...printOptionsFixture, printers: { '1': { timelapse: true } } }),
      ),
    )
    const puts: unknown[] = []
    server.use(
      http.put('/api/v1/settings/print-options', async ({ request }) => {
        const body = (await request.json()) as { options: PrintOptions }
        puts.push(body)
        return HttpResponse.json({
          defaults: printOptionsFixture.defaults,
          global_options: {},
          printers: { '1': body.options },
          models: {},
        })
      }),
    )
    const user = userEvent.setup()
    render(<Harness />)
    await open(user)
    await user.selectOptions(screen.getByLabelText('Bed levelling'), 'off')

    await user.click(screen.getByRole('button', { name: 'Remember' }))

    await waitFor(() => expect(puts).toHaveLength(1))
    // Not just the one field the dialog touched: the PUT replaces the scope wholesale.
    expect(puts[0]).toMatchObject({ options: { bed_levelling: 'off', timelapse: true } })
    expect(within(row('Timelapse')).getByText(/On · from this printer/)).toBeInTheDocument()
  })

  it('still knows the printer after a save, since the PUT does not report one', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await open(user)
    await user.selectOptions(screen.getByLabelText('Timelapse'), 'true')

    await user.click(screen.getByRole('button', { name: 'Remember' }))

    await waitFor(() =>
      expect(screen.getByText('Remembered for this printer.')).toBeInTheDocument(),
    )
    // Reading printer_id off the PUT response would have blanked both of these.
    expect(screen.getByRole('option', { name: 'This printer' })).toBeEnabled()
    expect(within(row('Timelapse')).getByText(/On · from this printer/)).toBeInTheDocument()
  })

  it('clamps a number row to its bound instead of letting the server 422 it', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await open(user)

    // A browser only enforces min/max on form submission, and this never submits one.
    fireEvent.change(screen.getByLabelText('Chamber preheat target'), {
      target: { value: '90' },
    })
    fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '5000' } })

    expect(screen.getByLabelText('Chamber preheat target')).toHaveValue(65)
    expect(screen.getByLabelText('Quantity')).toHaveValue(1000)
    expect(screen.getByTestId('sent')).toHaveTextContent(
      '{"preheat_chamber_target_override":65,"quantity":1000}',
    )
  })

  it('rounds a decimal, since every one of these fields is an integer server-side', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await open(user)

    fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '3.5' } })
    fireEvent.change(screen.getByLabelText('Chamber preheat target'), {
      target: { value: '20.4' },
    })

    expect(screen.getByTestId('sent')).toHaveTextContent(
      '{"quantity":4,"preheat_chamber_target_override":20}',
    )
  })

  it('forgets a scope', async () => {
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({ ...printOptionsFixture, printers: { '1': { timelapse: true } } }),
      ),
    )
    const user = userEvent.setup()
    render(<Harness />)
    await open(user)
    expect(screen.getByTestId('non-default-timelapse')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Forget' }))

    await waitFor(() =>
      expect(screen.queryByTestId('non-default-timelapse')).not.toBeInTheDocument(),
    )
  })

  it('disables the per-printer scope when no printer is known and says why', async () => {
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({ ...printOptionsFixture, printer_id: null }),
      ),
    )
    const user = userEvent.setup()
    render(<Harness printerId={null} />)
    await open(user)

    expect(screen.getByRole('option', { name: 'This printer' })).toBeDisabled()
    expect(screen.getByText(/No printer is picked yet/)).toBeInTheDocument()
  })

  it('prefers a printer the caller already knows over the one the server resolved', async () => {
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({
          ...printOptionsFixture,
          printer_id: 1,
          printers: { '1': { timelapse: true }, '9': { timelapse: false } },
        }),
      ),
    )
    const user = userEvent.setup()
    render(<Harness printerId={9} />)
    await open(user)

    expect(within(row('Timelapse')).getByText(/Off · from this printer/)).toBeInTheDocument()
  })

  it('will not Remember when the prior state could not be read, but will still Forget', async () => {
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({ title: 'Bad Gateway', status: 502, detail: 'Bambuddy said no' }, {
          status: 502,
        }),
      ),
    )
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByText('Options'))
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())

    // Remembering would replace the scope with only these edits, deleting whatever it held.
    expect(screen.getByRole('button', { name: 'Remember' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Forget' })).toBeEnabled()
    expect(screen.getByText(/Remember is unavailable/)).toBeInTheDocument()
  })

  it('saves to the model when This printer is selected but unavailable', async () => {
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({ ...printOptionsFixture, printer_id: null }),
      ),
    )
    const puts: unknown[] = []
    server.use(
      http.put('/api/v1/settings/print-options', async ({ request }) => {
        puts.push(await request.json())
        return HttpResponse.json({
          defaults: printOptionsFixture.defaults,
          global_options: {},
          printers: {},
          models: { 'name-keychain': { timelapse: true } },
        })
      }),
    )
    const user = userEvent.setup()
    render(<Harness printerId={null} />)
    await open(user)
    await user.selectOptions(screen.getByLabelText('Timelapse'), 'true')

    // "This printer" is disabled but still the select's value, so the click must not
    // silently do nothing.
    await user.click(screen.getByRole('button', { name: 'Remember' }))

    await waitFor(() => expect(screen.getByText('Remembered for this model.')).toBeInTheDocument())
    expect(puts).toEqual([
      { scope: 'model', key: 'name-keychain', options: { timelapse: true } },
    ])
  })

  it('reports a failure to read the remembered options instead of pretending', async () => {
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({ title: 'Conflict', status: 409, detail: 'no Bambuddy URL' }, { status: 409 }),
      ),
    )
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByText('Options'))

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('no Bambuddy URL'))
  })
})
