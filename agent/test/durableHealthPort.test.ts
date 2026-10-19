import { describe, expect, it } from 'vitest'
import { durableHealthPort, notStarted } from './support/durableHealth.js'

describe('durableHealthPort (#3655)', () => {
  it("reads the sidecar's own line, not a stray one that mentions a port", () => {
    const log = [
      'Resolved 120 packages in 3ms',
      'INFO:httpx:health on port 1',
      'INFO:scadbuddy_durable:health on port 41234',
      'INFO:scadbuddy_durable:worker running',
    ].join('\n')
    expect(durableHealthPort(log)).toBe(41234)
  })

  it('is undefined until the sidecar names its port, its whole line', () => {
    expect(durableHealthPort('INFO:httpx:health on port 1\n')).toBeUndefined()
    // A chunk that ends inside the number.
    expect(durableHealthPort('INFO:scadbuddy_durable:health on port 412')).toBeUndefined()
    expect(durableHealthPort('INFO:scadbuddy_durable:health on port 41234\n')).toBe(41234)
  })
})

describe('notStarted (#4086 review)', () => {
  it('names the port helper when the port line was never read', () => {
    const { message } = notStarted('the durable worker', undefined, 'log')
    expect(message).toMatch(/never got that far.*durableHealthPort/)
    // The hint goes before the captured log, never in place of it.
    expect(message.endsWith(': log')).toBe(true)
  })

  it('says only what it was told once the port was read', () => {
    expect(notStarted('the durable worker', 41234, 'log').message).toBe('the durable worker did not start: log')
  })
})
