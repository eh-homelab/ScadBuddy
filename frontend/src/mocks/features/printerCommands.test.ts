import { afterEach, describe, expect, it } from 'vitest'
import { commandAnswer, reset, setPrinterCommandSlow } from './printerCommands'

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
})
