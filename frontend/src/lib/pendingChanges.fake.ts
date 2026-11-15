import { act } from '@testing-library/react'
import { vi } from 'vitest'

/**
 * The agent's pending-input change stream (`lib/pendingChanges.ts`), driven by the test:
 * `changed()` is the agent's tick, `close()` the stream closed for good (a 503). Undo it
 * with `vi.unstubAllGlobals()`.
 */
export function fakePendingChanges() {
  const sources: FakeEventSource[] = []
  class FakeEventSource extends EventTarget {
    static readonly CLOSED = 2
    readyState = 1
    readonly url: string
    constructor(url: string) {
      super()
      this.url = url
      sources.push(this)
    }
    close() {
      this.readyState = 2
    }
  }
  vi.stubGlobal('EventSource', FakeEventSource)
  const open = () => sources.filter((s) => s.readyState !== 2)
  return {
    sources,
    open,
    async changed() {
      await act(async () => {
        for (const source of open()) source.dispatchEvent(new Event('changed'))
        await Promise.resolve()
      })
    },
    /** The open streams answering: their `open` event. */
    async opened() {
      await act(async () => {
        for (const source of open()) source.dispatchEvent(new Event('open'))
        await Promise.resolve()
      })
    },
    async close() {
      await act(async () => {
        for (const source of open()) {
          source.readyState = 2
          source.dispatchEvent(new Event('error'))
        }
        await Promise.resolve()
      })
    },
  }
}
