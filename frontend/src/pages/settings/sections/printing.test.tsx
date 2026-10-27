import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { server } from '../../../mocks/server'
import { region, renderSettingsSection } from '../testing'
import { Component as Printing } from './printing'

const save = () => within(region('Printing defaults')).getByRole('button', { name: 'Save Printing defaults' })

/** The form seeds itself from the server, so wait for the section to arrive. */
async function seeded() {
  await screen.findByLabelText('Keep finished print runs for (days)')
}

describe('Printing defaults', () => {
  afterEach(() => vi.restoreAllMocks())

  it('keeps print runs forever until a number of days is saved, and clearing keeps them again (#1052)', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<Printing />)
    await seeded()

    const days = screen.getByLabelText('Keep finished print runs for (days)')
    expect(days).toHaveValue(null)
    expect(days).toHaveAttribute('placeholder', 'Forever')

    await user.type(days, '7')
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toMatchObject({ print_run_retention_seconds: 604800 })
    expect((await api.getSettings()).print_run_retention_seconds).toBe(604800)

    await user.clear(days)
    // The button is disabled while the first save is in flight.
    await waitFor(() => expect(save()).toBeEnabled())
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(put.mock.calls[1]?.[0]).toMatchObject({ print_run_retention_seconds: null })
  })

  it('keeps finished operations for the days given, and clearing keeps them again (#1053)', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<Printing />)
    await seeded()

    const days = screen.getByLabelText('Keep finished Bambuddy operations for (days)')
    expect(days).toHaveAttribute('placeholder', 'Forever')
    expect(screen.getByText(/at least as long as Temporal's namespace retention/)).toBeInTheDocument()
    await user.type(days, '2')
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toMatchObject({ operation_retention_seconds: 172800 })

    await user.clear(days)
    // The button is disabled while the first save is in flight.
    await waitFor(() => expect(save()).toBeEnabled())
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(put.mock.calls[1]?.[0]).toMatchObject({ operation_retention_seconds: null })
  })

  it("shows the server refusing a retention below Temporal's beside the field (review #1063 r6 2)", async () => {
    const msg =
      'Keep them at least as long as Temporal keeps a finished operation (3 days): a retry after the record is gone cannot tell what it did.'
    server.use(
      http.put('/api/v1/settings', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Unprocessable Content',
            status: 422,
            detail: msg,
            errors: [{ loc: ['body', 'operation_retention_seconds'], msg }],
          },
          { status: 422, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderSettingsSection(<Printing />)
    await seeded()
    await user.type(screen.getByLabelText('Keep finished Bambuddy operations for (days)'), '2')
    await user.click(save())
    expect(await screen.findByText(msg)).toBeInTheDocument()
    // Beside the field, and its help with it, which explains the refusal (#2347).
    expect(screen.getByText(/at least as long as Temporal's namespace retention/)).toBeInTheDocument()
    expect(screen.getByLabelText('Keep finished Bambuddy operations for (days)')).toHaveAttribute('aria-invalid', 'true')
  })

  it('refuses a print run retention under a day, which would forget a retried print (#1061)', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<Printing />)
    await seeded()

    await user.type(screen.getByLabelText('Keep finished print runs for (days)'), '0.5')
    await user.click(save())
    expect(await screen.findByText(/Enter 1 to 3650 days/)).toBeInTheDocument()
    expect(put).not.toHaveBeenCalled()
  })

  it('refuses a retention over ten years, whose prune Postgres cannot compute', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<Printing />)
    await seeded()

    await user.type(screen.getByLabelText('Keep finished print runs for (days)'), '3651')
    await user.click(save())
    expect(await screen.findByText(/Enter 1 to 3650 days/)).toBeInTheDocument()
    expect(put).not.toHaveBeenCalled()
  })
})
