import { WAIT_FIRST_MS, WAIT_MAX_MS } from './backendUp.js'
import { CrudReadError } from './crudRead.js'
import { crudJson, type CrudTarget } from './crudStores.js'

// The two keys the agent derived from its own KEK until #5299 step 12, now the API's (backend
// api/crud_agent_keys.py, area `keys`), so this process mounts no KEK:
// - the approvals' input-hash key, released once and kept for the process's life. Every
//   process must hash with one key: a call prepared on one replica is confirmed on another;
// - a one-off fetch's grant (#959), minted by the API for one host and URL. The key stays there.

/** The input-hash key is not released yet: a hash cannot be made, so the call is refused. */
export class KeyNotReleasedError extends Error {
  override name = 'KeyNotReleasedError'
}

export type InputHashKeyOptions = {
  /** The back-off between asks, doubling (`WAIT_FIRST_MS` to `WAIT_MAX_MS`). */
  firstMs?: number
  maxMs?: number
  log?: (message: string) => void
}

/** The approvals' input-hash key, asked of the API from `start()` until it answers. */
export class InputHashKey {
  readonly #target: CrudTarget
  readonly #firstMs: number
  readonly #maxMs: number
  readonly #log: (message: string) => void
  #key: Buffer | undefined
  #started: Promise<void> | undefined
  #stopped = false
  #timer: NodeJS.Timeout | undefined
  #wake: (() => void) | undefined

  constructor(target: CrudTarget, options: InputHashKeyOptions = {}) {
    this.#target = target
    this.#firstMs = options.firstMs ?? WAIT_FIRST_MS
    this.#maxMs = options.maxMs ?? WAIT_MAX_MS
    this.#log = options.log ?? ((m) => console.log(m))
  }

  /** The key; KeyNotReleasedError until the API released it. */
  get(): Buffer {
    if (this.#key === undefined) throw new KeyNotReleasedError('the input-hash key is not released by the API yet; try again shortly')
    return this.#key
  }

  /** Whether the API released the key: readiness waits for it (api/dataHealth.ts, main.ts). */
  isReleased(): boolean {
    return this.#key !== undefined
  }

  /** Asks in the background until the API releases the key; never throws. */
  start(): void {
    this.#started ??= this.#run()
  }

  /** Resolves once the key is released (or `stop()` ended the asking). */
  released(): Promise<void> {
    this.start()
    return this.#started!
  }

  stop(): void {
    this.#stopped = true
    clearTimeout(this.#timer)
    this.#wake?.()
  }

  async #run(): Promise<void> {
    const logged = new Set<string>()
    let delay = this.#firstMs
    while (!this.#stopped) {
      let why: string
      try {
        const { key } = await crudJson<{ key?: unknown }>(this.#target.fetch ?? globalThis.fetch, this.#target.baseUrl, 'the input-hash key', '/keys/input-hash')
        const bytes = typeof key === 'string' ? Buffer.from(key, 'base64') : Buffer.alloc(0)
        if (bytes.length === 32) {
          this.#key = bytes
          return
        }
        why = 'the API answered a key that is not 32 bytes'
      } catch (err) {
        why = err instanceof CrudReadError ? err.message : (err as Error).message
      }
      // Each kind of reason once; the message names a status or a transport kind, never the key.
      if (!logged.has(why)) {
        logged.add(why)
        this.#log(`agent keys: waiting for the input-hash key (${why})`)
      }
      await new Promise<void>((resolve) => {
        this.#wake = resolve
        this.#timer = setTimeout(resolve, delay)
        this.#timer.unref()
      })
      delay = Math.min(delay * 2, this.#maxMs)
    }
  }
}

/** One-off fetch grants, minted by the API (#959). */
export class CrudFetchGrants {
  readonly #target: CrudTarget

  constructor(target: CrudTarget) {
    this.#target = target
  }

  /** A grant for one fetch of `url` from `host`; a bearer secret, never logged. */
  async mint(host: string, url: string): Promise<string> {
    const { grant } = await crudJson<{ grant: string }>(this.#target.fetch ?? globalThis.fetch, this.#target.baseUrl, 'a fetch grant', '/fetch-grants', {
      body: { host, url },
    })
    return grant
  }
}
