import { describe, expect, it } from 'vitest'
import { durableHealthPort } from './support/durableHealth.js'

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
