import { createHash } from 'node:crypto'
import { mkdir, readdir, readFile, symlink, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { SandboxClient, SandboxError } from '../src/sandbox/client.js'
import { STREAM_MAX_BYTES, syncPath } from '../src/sandbox/protocol.js'
import { commandEnv } from '../src/sandbox/server.js'
import { startLocalSandbox } from './support/sandbox.js'

const sha = (s: string | Buffer) => createHash('sha256').update(s).digest('hex')

describe('the sandbox exec server', () => {
  const boxes: Awaited<ReturnType<typeof startLocalSandbox>>[] = []
  afterEach(async () => {
    for (const box of boxes.splice(0)) await box.close()
  })
  const start = async (options: Parameters<typeof startLocalSandbox>[0] = {}) => {
    const box = await startLocalSandbox(options)
    boxes.push(box)
    return { box, client: new SandboxClient(box.endpoint, AbortSignal.timeout(30_000)) }
  }

  it('runs a bash command in the work directory and returns its output and exit code', async () => {
    const { box, client } = await start()
    const result = await client.exec('bash', 'pwd; echo out; echo err >&2; exit 3', 10_000)
    expect(result.exit_code).toBe(3)
    expect(result.timed_out).toBe(false)
    expect(result.stdout).toBe(`${box.work}\nout\n`)
    expect(result.stderr).toBe('err\n')
  })

  it("gives the command none of the server's environment", async () => {
    const { client } = await start()
    process.env.SANDBOX_TEST_SECRET = 'do-not-leak'
    try {
      const result = await client.exec('bash', 'env', 10_000)
      expect(result.stdout).not.toContain('do-not-leak')
      expect(result.stdout).not.toContain('SCADBUDDY_')
      expect(Object.keys(commandEnv({ home: '/tmp/home', proxy: 'http://10.0.0.1:3128' })).sort()).toEqual(
        ['ALL_PROXY', 'DOTNET_CLI_TELEMETRY_OPTOUT', 'HOME', 'HTTPS_PROXY', 'LANG', 'PATH', 'POWERSHELL_TELEMETRY_OPTOUT', 'POWERSHELL_UPDATECHECK', 'TERM', 'all_proxy', 'https_proxy'].sort(),
      )
    } finally {
      delete process.env.SANDBOX_TEST_SECRET
    }
  })

  it('kills the whole process group at the deadline', async () => {
    const { box, client } = await start()
    const started = Date.now()
    const result = await client.exec('bash', '(sleep 3; echo late > late.txt) & sleep 30', 500)
    expect(result.timed_out).toBe(true)
    expect(Date.now() - started).toBeLessThan(5_000)
    await new Promise((r) => setTimeout(r, 3_500))
    await expect(readFile(path.join(box.work, 'late.txt'))).rejects.toThrow()
  })

  it('kills what a command leaves running after it exits', async () => {
    const { box, client } = await start()
    const result = await client.exec('bash', '(sleep 2; echo late > late.txt) &', 10_000)
    expect(result.exit_code).toBe(0)
    await new Promise((r) => setTimeout(r, 2_500))
    await expect(readFile(path.join(box.work, 'late.txt'))).rejects.toThrow()
  })

  it('keeps at most STREAM_MAX_BYTES of a stream and counts the rest', async () => {
    const { client } = await start()
    const result = await client.exec('bash', `head -c ${STREAM_MAX_BYTES + 1000} /dev/zero | tr '\\0' a`, 10_000)
    expect(result.stdout.length).toBe(STREAM_MAX_BYTES)
    expect(result.stdout_bytes).toBe(STREAM_MAX_BYTES + 1000)
  })

  it('refuses a call without its token', async () => {
    const { box } = await start()
    const bad = new SandboxClient({ url: box.endpoint.url, token: 'x'.repeat(64) }, AbortSignal.timeout(5_000))
    await expect(bad.manifest()).rejects.toThrow(SandboxError)
    const health = await fetch(`${box.endpoint.url}/healthz`)
    expect(health.status).toBe(200)
  })

  it('puts, lists, reads and deletes files at sync paths only', async () => {
    const { box, client } = await start()
    await client.put('a/b.txt', Buffer.from('hello'))
    await mkdir(path.join(box.work, 'node_modules', 'x'), { recursive: true })
    await writeFile(path.join(box.work, 'node_modules', 'x', 'i.js'), 'x')
    await mkdir(path.join(box.work, '.git'))
    await writeFile(path.join(box.work, '.git', 'HEAD'), 'ref')
    await symlink('/etc/passwd', path.join(box.work, 'link'))
    const manifest = await client.manifest()
    expect(manifest).toEqual({ files: [{ path: 'a/b.txt', sha256: sha('hello'), size: 5 }], truncated: false, unsynced: ['.git/'] })
    expect((await client.get('a/b.txt', 1000)).toString()).toBe('hello')
    await expect(client.get('link', 1000)).rejects.toThrow(/404/)
    await expect(client.put('.git/config', Buffer.from('x'))).rejects.toThrow(/400/)
    await client.delete('a/b.txt')
    expect((await client.manifest()).files).toEqual([])
  })

  it('streams a file to disk and refuses one past its cap, leaving nothing behind', async () => {
    const { box, client } = await start({ fileMaxBytes: 1000 })
    const fits = Buffer.alloc(1000, 'a')
    await client.put('fits.bin', fits)
    expect((await readFile(path.join(box.work, 'fits.bin'))).equals(fits)).toBe(true)
    const big = Buffer.alloc(5000, 'b')
    const res = await fetch(`${box.endpoint.url}/v1/files/big.bin`, {
      method: 'PUT',
      headers: { authorization: `Bearer ${box.endpoint.token}`, 'x-sandbox-sha256': sha(big) },
      body: big,
    }).catch(() => undefined)
    // The server answers 413 and drops the rest of the body; either way nothing is written.
    if (res) expect(res.status).toBe(413)
    expect((await readdir(box.work)).sort()).toEqual(['fits.bin'])
  })

  it('refuses a file whose sha256 does not match', async () => {
    const { box } = await start()
    const res = await fetch(`${box.endpoint.url}/v1/files/a.txt`, {
      method: 'PUT',
      headers: { authorization: `Bearer ${box.endpoint.token}`, 'x-sandbox-sha256': sha('other') },
      body: 'mine',
    })
    expect(res.status).toBe(400)
    expect(await readdir(box.work)).toEqual([])
  })

  it('names the paths that sync', () => {
    expect(syncPath('a.txt')).toBe(true)
    expect(syncPath('a/b/c/d.txt')).toBe(true)
    expect(syncPath('a/b/c/d/e.txt')).toBe(false)
    expect(syncPath('.env')).toBe(false)
    expect(syncPath('node_modules/x.js')).toBe(false)
    expect(syncPath('src/__pycache__/x.pyc')).toBe(false)
    expect(syncPath('../x')).toBe(false)
  })
})
