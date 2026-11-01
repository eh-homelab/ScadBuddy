import { randomBytes, timingSafeEqual } from 'node:crypto'

// The credential gateway's tokens (plan 2026-10-13 Step 9.4, decision Q6): one
// per Claude Code process, random, and mapped to that process's credential
// only in this process's memory. A token is all Claude Code's environment
// holds, so a `Bash` command that reads it reaches the model only through the
// gateway, which meters it, and only while the process lives: the run releases
// it when its query ends (harness/run.ts).

/** 192 random bits, URL-safe: it is a path segment of the base URL. */
export function newToken(): string {
  return randomBytes(24).toString('base64url')
}

/** Whether `presented` is `token`, compared in constant time. */
export function sameToken(presented: string, token: string): boolean {
  const a = Buffer.from(presented)
  const b = Buffer.from(token)
  return a.length === b.length && timingSafeEqual(a, b)
}

export class GatewayTokens<T> {
  readonly #leases = new Map<string, T>()

  issue(value: T): string {
    const token = newToken()
    this.#leases.set(token, value)
    return token
  }

  get(token: string): T | undefined {
    return this.#leases.get(token)
  }

  release(token: string): void {
    this.#leases.delete(token)
  }

  get size(): number {
    return this.#leases.size
  }
}
