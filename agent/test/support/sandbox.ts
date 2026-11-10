import { randomBytes } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import type { Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { tmpdir } from 'node:os'
import path from 'node:path'
import type { SandboxEndpoint, Sandboxes } from '../../src/sandbox/client.js'
import { sandboxServer } from '../../src/sandbox/server.js'

/** A local exec server (sandbox/server.ts) on a temporary work directory. */
export async function startLocalSandbox(options: { idleMs?: number; onIdle?: () => void } = {}): Promise<{
  endpoint: SandboxEndpoint
  work: string
  server: Server
  close: () => Promise<void>
}> {
  const root = await mkdtemp(path.join(tmpdir(), 'sandbox-'))
  const work = path.join(root, 'work')
  const home = path.join(root, 'home')
  const { mkdir } = await import('node:fs/promises')
  await mkdir(work)
  await mkdir(home)
  const token = randomBytes(32).toString('hex')
  const server = sandboxServer({ work, home, token, idleMs: options.idleMs ?? 60_000, onIdle: options.onIdle ?? (() => {}), pathEnv: process.env.PATH ?? '/usr/bin:/bin' })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const port = (server.address() as AddressInfo).port
  return {
    endpoint: { url: `http://127.0.0.1:${port}`, token },
    work,
    server,
    close: async () => {
      await new Promise<void>((resolve) => server.close(() => resolve()))
      await rm(root, { recursive: true, force: true })
    },
  }
}

/** One local sandbox per session, as the pod provider gives one pod per session. */
export class LocalSandboxes implements Sandboxes {
  readonly started = new Map<string, Awaited<ReturnType<typeof startLocalSandbox>>>()
  acquires = 0

  async acquire(sessionId: string): Promise<SandboxEndpoint> {
    this.acquires++
    let box = this.started.get(sessionId)
    if (!box) {
      box = await startLocalSandbox()
      this.started.set(sessionId, box)
    }
    return box.endpoint
  }

  async close(): Promise<void> {
    for (const box of this.started.values()) await box.close()
    this.started.clear()
  }
}
