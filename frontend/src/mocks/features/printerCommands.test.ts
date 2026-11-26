import { afterEach, describe, expect, it } from 'vitest'
import { commandAnswer, joined, reset, setPrinterCommandSlow } from './printerCommands'

const follow = (id: string) => fetch(new URL(`/api/v1/printers/p1/commands/${id}`, window.location.href))

describe('printer commands mock (#7276)', () => {
  afterEach(reset)

  it('answers a finished command again on every follow, as the backend reads a closed run', async () => {
    setPrinterCommandSlow(1)
    const started = commandAnswer('p1', new Request('http://x/', { method: 'POST' }), { done: true })
    expect(started.status).toBe(202)
    const { workflow_id } = (await started.json()) as { workflow_id: string }

    expect((await follow(workflow_id)).status).toBe(202)
    const first = await follow(workflow_id)
    expect(first.status).toBe(200)
    expect(await first.json()).toEqual({ done: true })
    const again = await follow(workflow_id)
    expect(again.status).toBe(200)
    expect(await again.json()).toEqual({ done: true })
  })

  it('joins a key sent again as the backend keys a run: printer, action and key, not the tray', () => {
    const post = (action: string, tray = 1) =>
      new Request(`http://x/api/v1/printers/p1/trays/0/${tray}/${action}`, {
        method: 'POST',
        headers: { 'Idempotency-Key': 'k1' },
      })
    commandAnswer('p1', post('load'), { action: 'load' })
    expect(joined('p1', post('load'))).toBeDefined()
    expect(joined('p1', post('load', 3))).toBeDefined()
    expect(joined('p1', post('unload'))).toBeUndefined()
    expect(joined('p2', post('load'))).toBeUndefined()
  })

  it('joins K-profile set and batch, which send the same command, but not delete', () => {
    const post = (path: string) =>
      new Request(`http://x/api/v1/printers/p1/kprofiles${path}`, {
        method: 'POST',
        headers: { 'Idempotency-Key': 'k2' },
      })
    commandAnswer('p1', post(''), { done: true })
    expect(joined('p1', post('/batch'))).toBeDefined()
    expect(joined('p1', post('/delete'))).toBeUndefined()
  })
})
