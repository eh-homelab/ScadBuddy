import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { server } from '../mocks/server'
import { ApiError, mayHaveRun, PRINTER_COMMAND_RUNNING, printerCommand, printRunPoll } from './client'

// #7276 P44, P51: a printer command over the LAN link answers 200 with its model, or 202
// `LanCommandPending`, which is followed at `GET /printers/{id}/commands/{command_id}`.

const PRINTER = '00000000-0000-4000-8000-0000000000c1'
const PATH = `/printers/${PRINTER}/kprofiles/delete`
const poll = { ...printRunPoll }

beforeEach(() => {
  printRunPoll.intervalMs = 1
})

afterEach(() => {
  Object.assign(printRunPoll, poll)
})

describe('printerCommand', () => {
  it('sends one Idempotency-Key, the same again after a lost answer', async () => {
    const keys: (string | null)[] = []
    server.use(
      http.post(`/api/v1${PATH}`, ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return keys.length === 1 ? HttpResponse.error() : HttpResponse.json({ success: true, confirmed: true, message: '' })
      }),
    )
    const answer = await printerCommand<{ success: boolean }>(PRINTER, PATH, { method: 'POST', body: '{}' })
    expect(answer).toEqual({ success: true, confirmed: true, message: '' })
    expect(keys).toHaveLength(2)
    expect(keys[0]).toMatch(/^[0-9a-f]{32}$/)
    expect(keys[1]).toBe(keys[0])
  })

  it('sends no key with a read', async () => {
    const keys: (string | null)[] = []
    server.use(
      http.get(`/api/v1/printers/${PRINTER}/kprofiles`, ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return HttpResponse.json({ profiles: [], nozzle_diameter: '0.4' })
      }),
    )
    await printerCommand(PRINTER, `/printers/${PRINTER}/kprofiles`)
    expect(keys).toEqual([null])
  })

  it('follows a 202 to the answer, never taking the 202 for it', async () => {
    let reads = 0
    const waiting: string[] = []
    server.use(
      http.post(`/api/v1${PATH}`, () => HttpResponse.json({ workflow_id: 'lan-command-7' }, { status: 202 })),
      http.get(`/api/v1/printers/${PRINTER}/commands/lan-command-7`, () =>
        ++reads < 3
          ? HttpResponse.json({ workflow_id: 'lan-command-7' }, { status: 202 })
          : HttpResponse.json({ success: true, confirmed: false, message: 'sent' }),
      ),
    )
    const answer = await printerCommand(PRINTER, PATH, { method: 'POST', body: '{}' }, { onWaiting: (id) => waiting.push(id) })
    expect(answer).toEqual({ success: true, confirmed: false, message: 'sent' })
    expect(reads).toBe(3)
    expect(waiting).toEqual(['lan-command-7'])
  })

  it("throws the follow's refusal as the route would", async () => {
    server.use(
      http.post(`/api/v1${PATH}`, () => HttpResponse.json({ workflow_id: 'lan-command-8' }, { status: 202 })),
      http.get(`/api/v1/printers/${PRINTER}/commands/lan-command-8`, () =>
        HttpResponse.json(
          { title: 'Conflict', status: 409, detail: 'The printer refused it', code: 'printer_refused' },
          { status: 409 },
        ),
      ),
    )
    await expect(printerCommand(PRINTER, PATH, { method: 'POST', body: '{}' })).rejects.toMatchObject({
      status: 409,
      detail: 'The printer refused it',
    })
  })

  it('gives up past its window with the command id, which may still run', async () => {
    printRunPoll.printerCommandFollowMs = 10
    let sends = 0
    server.use(
      http.post(`/api/v1${PATH}`, () => {
        sends++
        return HttpResponse.json({ workflow_id: 'lan-command-9' }, { status: 202 })
      }),
      http.get(`/api/v1/printers/${PRINTER}/commands/lan-command-9`, () =>
        HttpResponse.json({ workflow_id: 'lan-command-9' }, { status: 202 }),
      ),
    )
    const error = await printerCommand(PRINTER, PATH, { method: 'POST', body: '{}' }).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).problem.type).toBe(PRINTER_COMMAND_RUNNING)
    expect((error as ApiError).problem.command_id).toBe('lan-command-9')
    expect((error as ApiError).detail).toMatch(/Do not send it again/)
    expect(mayHaveRun(error)).toBe(true)
    expect(sends).toBe(1)
  })

  it('sends the key it is given, and stops following when its signal aborts', async () => {
    printRunPoll.intervalMs = 5
    const keys: (string | null)[] = []
    let follows = 0
    server.use(
      http.post(`/api/v1${PATH}`, ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return HttpResponse.json({ workflow_id: 'lan-command-10' }, { status: 202 })
      }),
      http.get(`/api/v1/printers/${PRINTER}/commands/lan-command-10`, () => {
        follows++
        return HttpResponse.json({ workflow_id: 'lan-command-10' }, { status: 202 })
      }),
    )
    const controller = new AbortController()
    const running = printerCommand(PRINTER, PATH, { method: 'POST', body: '{}' }, { key: 'k1', signal: controller.signal })
    await vi.waitFor(() => expect(follows).toBeGreaterThan(0))
    controller.abort()
    await expect(running).rejects.toMatchObject({ name: 'AbortError' })
    const seen = follows
    await new Promise((resolve) => setTimeout(resolve, 40))
    expect(follows).toBe(seen)
    expect(keys).toEqual(['k1'])
  })
})
