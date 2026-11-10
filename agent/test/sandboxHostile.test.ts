import { createHash } from 'node:crypto'
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { SandboxClient, syncBack, syncIn } from '../src/sandbox/client.js'
import { reapStrays } from '../src/sandbox/server.js'
import { writeDirFile } from '../src/workspace/directory.js'

// The sandbox is untrusted (#5864, security review): a command there controls what its exec
// server answers. These tests stand in for a sandbox that lies.

const sha = (b: Buffer | string) => createHash('sha256').update(b).digest('hex')

type Fake = { manifest: unknown; files: Record<string, Buffer>; deleted: string[]; got: string[] }

/** A sandbox server that answers whatever `fake` says. */
async function hostile(fake: Fake): Promise<{ server: Server; url: string }> {
  const server = createServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://x')
    if (url.pathname === '/v1/manifest') {
      res.writeHead(200, { 'content-type': 'application/json' })
      return res.end(JSON.stringify(fake.manifest))
    }
    const rel = decodeURIComponent(url.pathname.slice('/v1/files/'.length))
    if (req.method === 'DELETE') {
      fake.deleted.push(rel)
      return res.end('{}')
    }
    if (req.method === 'GET') {
      fake.got.push(rel)
      const body = fake.files[rel] ?? Buffer.alloc(0)
      res.writeHead(200, { 'x-sandbox-sha256': sha(body) })
      return res.end(body)
    }
    req.resume()
    req.on('end', () => res.end('{}'))
  })
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r))
  return { server, url: `http://127.0.0.1:${(server.address() as AddressInfo).port}` }
}

describe('a sandbox that lies', () => {
  let dir: string
  let outside: string
  let box: { server: Server; url: string } | undefined
  beforeEach(async () => {
    const root = await mkdtemp(path.join(tmpdir(), 'hostile-'))
    dir = path.join(root, 'session')
    outside = path.join(root, 'outside')
    await writeFile(path.join(root, 'marker'), 'x')
    const { mkdir } = await import('node:fs/promises')
    await mkdir(dir)
    await mkdir(outside)
    await writeFile(path.join(outside, 'victim.txt'), 'keep me')
  })
  afterEach(async () => {
    if (box) await new Promise((r) => box!.server.close(() => r(undefined)))
    box = undefined
    await rm(path.dirname(dir), { recursive: true, force: true })
  })
  const client = (url: string) => new SandboxClient({ url, token: 't' }, AbortSignal.timeout(10_000))
  const limits = { fileMaxBytes: 1000, workspaceMaxBytes: 5000 }

  it('never writes a file at a manifest path outside the workspace form', async () => {
    const evil = Buffer.from('pwned')
    const fake: Fake = { manifest: { files: [{ path: '../outside/victim.txt', sha256: sha(evil), size: 5 }], truncated: false }, files: { '../outside/victim.txt': evil }, deleted: [], got: [] }
    box = await hostile(fake)
    const back = await syncBack(client(box.url), dir, new Map(), limits)
    expect(await readFile(path.join(outside, 'victim.txt'), 'utf8')).toBe('keep me')
    expect(fake.got).toEqual([])
    expect(back.written).toEqual([])
    expect(back.left[0]).toMatchObject({ path: '../outside/victim.txt' })
  })

  it('never removes a file for a manifest path outside the workspace form', async () => {
    const fake: Fake = { manifest: { files: [{ path: '../outside/victim.txt', sha256: 'a'.repeat(64), size: 1 }], truncated: false }, files: {}, deleted: [], got: [] }
    box = await hostile(fake)
    // syncIn: the sandbox "has" a file the session does not, which would be deleted there; a bad path is ignored.
    const before = await syncIn(client(box.url), dir, limits)
    expect(fake.deleted).toEqual([])
    expect([...before.keys()]).toEqual([])
    // syncBack: a bad path in `before` that the sandbox no longer lists is never removed here.
    fake.manifest = { files: [], truncated: false }
    const back = await syncBack(client(box.url), dir, new Map([['../outside/victim.txt', 'a'.repeat(64)]]), limits)
    expect(back.removed).toEqual([])
    expect(await readFile(path.join(outside, 'victim.txt'), 'utf8')).toBe('keep me')
  })

  it('refuses a write whose path climbs out of the directory', async () => {
    await expect(writeDirFile(dir, '../outside/victim.txt', Buffer.from('pwned'))).rejects.toThrow()
    // A climbing path whose parent is a file outside: nothing is removed there before the refusal.
    await expect(writeDirFile(dir, '../outside/victim.txt/x', Buffer.from('pwned'))).rejects.toThrow()
    const { symlink } = await import('node:fs/promises')
    await symlink(outside, path.join(dir, 'link'))
    await expect(writeDirFile(dir, 'link/../../outside/victim.txt/y', Buffer.from('pwned'))).rejects.toThrow()
    await rm(path.join(dir, 'link'))
    expect(await readFile(path.join(outside, 'victim.txt'), 'utf8')).toBe('keep me')
    expect(await readdir(dir)).toEqual([])
  })

  it('stops reading a file past the per-file cap whatever size the manifest claims', async () => {
    const big = Buffer.alloc(50_000, 'a')
    const fake: Fake = { manifest: { files: [{ path: 'big.txt', sha256: sha(big), size: 10 }], truncated: false }, files: { 'big.txt': big }, deleted: [], got: [] }
    box = await hostile(fake)
    const back = await syncBack(client(box.url), dir, new Map(), limits)
    expect(back.written).toEqual([])
    expect(back.left[0]).toMatchObject({ path: 'big.txt', reason: expect.stringContaining('cap') })
    await expect(readFile(path.join(dir, 'big.txt'))).rejects.toThrow()
  })

  it("counts the directory's own bytes against the workspace cap", async () => {
    await writeFile(path.join(dir, 'already.bin'), Buffer.alloc(4_800))
    const body = Buffer.alloc(900, 'b')
    const fake: Fake = { manifest: { files: [{ path: 'new.txt', sha256: sha(body), size: 1 }], truncated: false }, files: { 'new.txt': body }, deleted: [], got: [] }
    box = await hostile(fake)
    const back = await syncBack(client(box.url), dir, new Map(), limits)
    expect(back.written).toEqual([])
    expect(back.left[0]).toMatchObject({ path: 'new.txt', reason: expect.stringContaining('cap') })
  })
})

describe('reaping what a command left behind', () => {
  it("kills every process of the sandbox's uid but the server and PID 1, until none is left", async () => {
    const proc = await mkdtemp(path.join(tmpdir(), 'proc-'))
    const status = (uid: number) => `Name:\tx\nUid:\t${uid}\t${uid}\t${uid}\t${uid}\n`
    const { mkdir } = await import('node:fs/promises')
    for (const [pid, uid] of [[1, 10001], [7, 10001], [42, 10001], [43, 10001], [50, 0], [51, 999]] as const) {
      await mkdir(path.join(proc, String(pid)))
      await writeFile(path.join(proc, String(pid), 'status'), status(uid))
    }
    await mkdir(path.join(proc, 'self'))
    const killed: number[] = []
    const alive = new Set([42, 43])
    const n = await reapStrays({
      uid: 10001,
      keep: [7],
      procRoot: proc,
      kill: async (pid) => {
        killed.push(pid)
        alive.delete(pid)
        await rm(path.join(proc, String(pid)), { recursive: true })
      },
    })
    expect(killed.sort()).toEqual([42, 43])
    expect(n).toBe(2)
    expect(alive.size).toBe(0)
    await rm(proc, { recursive: true, force: true })
  })
})
