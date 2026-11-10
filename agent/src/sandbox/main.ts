import { mkdir } from 'node:fs/promises'
import { egressProxy } from './egressProxy.js'
import { EGRESS_PORT, MAX_PROCESSES, SANDBOX_PORT } from './protocol.js'
import { sandboxServer } from './server.js'

// The `sandbox` image's entry (#5864): SCADBUDDY_SANDBOX_ROLE `exec` is a session's sandbox pod
// (server.ts), `egress` the sandbox pods' proxy (egressProxy.ts). Only Node's built-ins.

/** How long a sandbox pod waits with no request before it exits and its pod ends. */
export const IDLE_MS = 15 * 60_000

async function main(): Promise<void> {
  const role = process.env.SCADBUDDY_SANDBOX_ROLE ?? 'exec'
  if (role === 'egress') {
    const server = egressProxy({ log: (entry) => process.stdout.write(`${JSON.stringify(entry)}\n`) })
    server.listen(EGRESS_PORT, () => process.stdout.write(`sandbox egress proxy on ${EGRESS_PORT}\n`))
    return
  }
  if (role !== 'exec') throw new Error(`SCADBUDDY_SANDBOX_ROLE must be exec or egress, not ${JSON.stringify(role)}`)
  const token = process.env.SCADBUDDY_SANDBOX_TOKEN
  if (!token || token.length < 32) throw new Error('SCADBUDDY_SANDBOX_TOKEN must be set (32 characters or more)')
  const work = process.env.SCADBUDDY_SANDBOX_WORK ?? '/work'
  const home = process.env.SCADBUDDY_SANDBOX_HOME ?? '/tmp/home'
  await mkdir(work, { recursive: true })
  await mkdir(home, { recursive: true })
  const server = sandboxServer({
    work,
    home,
    token,
    proxy: process.env.SCADBUDDY_SANDBOX_PROXY || undefined,
    maxProcesses: MAX_PROCESSES,
    idleMs: Number(process.env.SCADBUDDY_SANDBOX_IDLE_MS ?? IDLE_MS),
    onIdle: () => {
      process.stdout.write('sandbox idle: exiting\n')
      process.exit(0)
    },
  })
  server.listen(SANDBOX_PORT, () => process.stdout.write(`sandbox exec server on ${SANDBOX_PORT}\n`))
}

main().catch((err: unknown) => {
  process.stderr.write(`${err instanceof Error ? err.message : String(err)}\n`)
  process.exit(1)
})
