import { http, HttpResponse } from 'msw'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { RawInputs } from '../components/RawInputs'
import { migrateIfOld } from './useMigratedInputs'

describe('migrateIfOld', () => {
  it('leaves current inputs alone without a request', async () => {
    const outcome = await migrateIfOld('demo', { params: {}, v: 2 }, 2)
    expect(outcome).toEqual({ kind: 'ready', inputs: { params: {}, v: 2 } })
  })

  it('migrates old inputs through the API', async () => {
    server.use(
      http.post('/api/v1/models/demo/inputs/migrate', () =>
        HttpResponse.json({ inputs: { params: {}, v: 2, house: {} }, from_version: 0, to_version: 2 }),
      ),
    )
    const outcome = await migrateIfOld('demo', { params: {}, v: 0 }, 2)
    expect(outcome).toEqual({ kind: 'ready', inputs: { params: {}, v: 2, house: {} } })
  })

  it('shows raw inputs read-only when migration fails', async () => {
    server.use(
      http.post('/api/v1/models/demo/inputs/migrate', () =>
        HttpResponse.json(
          { detail: "these inputs are v5; the template's INPUTS_VERSION is 2" },
          { status: 422 },
        ),
      ),
    )
    const outcome = await migrateIfOld('demo', { params: {}, v: 5 }, 2)
    expect(outcome.kind).toBe('failed')
    if (outcome.kind !== 'failed') return
    render(<RawInputs inputs={outcome.inputs} error={outcome.error} />)
    expect(screen.getByText(/these inputs are v5/)).toBeInTheDocument()
    expect(screen.getByRole('textbox')).toHaveAttribute('readonly')
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toContain('"v": 5')
  })
})
