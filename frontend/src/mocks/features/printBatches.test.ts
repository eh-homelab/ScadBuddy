import { describe, expect, it } from 'vitest'
import { api, ApiError } from '../../api/client'
import type { BatchChoice, PrintRunRequest } from '../../api/types'
import { setMockPrintBatchOutcome } from './library'

// The batch print mock keeps in step with the backend's store and routes (#3798, #3932
// review), so the dialog's tests see what the real API answers.

const NO_SUCH_FILE = 999_999
const RUN = { plate: 1, copies: 1 } as unknown as PrintRunRequest
const choice = (request: PrintRunRequest): BatchChoice => ({ request, skipped: false, removed: false })

describe('batch print mock', () => {
  it('marks a refusal stale against the choices refused, so an edit back clears it', async () => {
    const batch = await api.createPrintBatch([{ library_file_id: NO_SUCH_FILE, filename: 'gone.3mf', sliced: false }])
    await api.putPrintBatchItem(batch.id, 0, choice(RUN))
    const queued = await api.queuePrintBatch(batch.id)
    expect(queued.items[0]?.outcome?.status).toBe('refused')

    const away = await api.putPrintBatchItem(batch.id, 0, choice({ ...RUN, copies: 2 } as PrintRunRequest))
    expect(away.items[0]?.outcome?.stale).toBe(true)
    const back = await api.putPrintBatchItem(batch.id, 0, choice(RUN))
    expect(back.items[0]?.outcome?.stale).toBe(false)
  })

  it('refuses to delete a batch that follows a file still being checked, 409', async () => {
    const batch = await api.createPrintBatch([{ library_file_id: NO_SUCH_FILE, filename: 'gone.3mf', sliced: false }])
    setMockPrintBatchOutcome(batch.id, 0, { status: 'pending', run_id: null, detail: null, attempt: 0, stale: false })

    const error = await api.deletePrintBatch(batch.id).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(409)
    expect((await api.getPrintBatch(batch.id)).id).toBe(batch.id)
  })
})
