import { createServer, type Server } from 'node:http'
import { connect, createServer as netServer, type AddressInfo, type Server as NetServer, type Socket } from 'node:net'
import { afterEach, describe, expect, it } from 'vitest'
import { connectTarget, type EgressLog, egressProxy, MAX_TUNNELS_PER_SOURCE, notPublic } from '../src/sandbox/egressProxy.js'

/** Sends one CONNECT through the proxy and returns its status line, and the socket. */
function tunnel(port: number, target: string): Promise<{ status: string; socket: Socket }> {
  return new Promise((resolve, reject) => {
    const socket = connect(port, '127.0.0.1', () => socket.write(`CONNECT ${target} HTTP/1.1\r\nhost: ${target}\r\n\r\n`))
    let buf = ''
    const onData = (chunk: Buffer) => {
      buf += chunk.toString('latin1')
      const end = buf.indexOf('\r\n\r\n')
      if (end >= 0) {
        socket.off('data', onData)
        resolve({ status: buf.split('\r\n')[0]!, socket })
      }
    }
    socket.on('data', onData)
    socket.on('error', reject)
  })
}

describe('the sandbox egress proxy', () => {
  const servers: (Server | NetServer)[] = []
  afterEach(async () => {
    for (const s of servers.splice(0)) await new Promise((r) => s.close(() => r(undefined)))
  })
  const listen = async <S extends Server | NetServer>(s: S): Promise<number> => {
    servers.push(s)
    await new Promise<void>((r) => s.listen(0, '127.0.0.1', r))
    return (s.address() as AddressInfo).port
  }

  it('tunnels to a public address it checked, and logs the tunnel', async () => {
    const echo = netServer((sock) => sock.pipe(sock))
    const echoPort = await listen(echo)
    const logs: EgressLog[] = []
    const connected: string[] = []
    const proxy = egressProxy({
      resolve: async () => ['93.184.216.34'],
      log: (e) => logs.push(e),
      // The checked address is public; the test reaches it as the local echo server.
      connect: (address, port) => {
        connected.push(`${address}:${port}`)
        return connect(echoPort, '127.0.0.1')
      },
    })
    const port = await listen(proxy)
    const { status, socket } = await tunnel(port, 'example.com:443')
    expect(status).toBe('HTTP/1.1 200 Connection Established')
    const back = await new Promise<string>((resolve) => {
      socket.once('data', (d: Buffer) => resolve(d.toString()))
      socket.write('ping')
    })
    expect(back).toBe('ping')
    socket.destroy()
    await new Promise((r) => setTimeout(r, 100))
    expect(connected).toEqual(['93.184.216.34:443'])
    expect(logs.at(-1)).toMatchObject({ host: 'example.com', port: 443, outcome: 'connected', address: '93.184.216.34', bytes_out: 4, bytes_in: 4 })
  })

  it.each([
    ['example.com:22', '403', /port 22 is not allowed/],
    ['127.0.0.1:443', '403', /loopback/],
    ['10.0.0.5:443', '403', /private/],
    ['169.254.169.254:80', '403', /link-local or cloud metadata/],
    ['100.64.1.1:443', '403', /not public/],
    ['metadata.google.internal:80', '403', /metadata/],
    ['internal.example:443', '403', /resolves to 192.168.1.10, a private address/],
    ['rebind.example:443', '403', /private/],
    ['[::1]:443', '403', /loopback/],
    ['[::ffff:10.0.0.1]:443', '403', /private/],
    ['nonsense', '400', /host:port/],
  ])('refuses CONNECT %s', async (target, code, reason) => {
    const logs: EgressLog[] = []
    const proxy = egressProxy({
      resolve: async (host) => (host === 'internal.example' ? ['192.168.1.10'] : host === 'rebind.example' ? ['93.184.216.34', '10.1.2.3'] : ['93.184.216.34']),
      log: (e) => logs.push(e),
      connect: () => {
        throw new Error('must not connect')
      },
    })
    const port = await listen(proxy)
    const { status, socket } = await tunnel(port, target)
    socket.destroy()
    expect(status).toContain(code)
    expect(logs).toHaveLength(1)
    expect(logs[0]).toMatchObject({ outcome: 'refused' })
    expect(logs[0]!.reason).toMatch(reason)
  })

  it('holds one source to its own share of the tunnels', async () => {
    const echo = netServer((sock) => sock.pipe(sock))
    const echoPort = await listen(echo)
    const logs: EgressLog[] = []
    const proxy = egressProxy({ resolve: async () => ['93.184.216.34'], log: (e) => logs.push(e), connect: () => connect(echoPort, '127.0.0.1') })
    const port = await listen(proxy)
    const open: Socket[] = []
    try {
      for (let i = 0; i < MAX_TUNNELS_PER_SOURCE; i++) {
        const { status, socket } = await tunnel(port, 'example.com:443')
        open.push(socket)
        expect(status).toBe('HTTP/1.1 200 Connection Established')
      }
      const over = await tunnel(port, 'example.com:443')
      over.socket.destroy()
      expect(over.status).toContain('429')
      // One closed frees one.
      open.shift()!.destroy()
      await expect.poll(async () => {
        const again = await tunnel(port, 'example.com:443')
        open.push(again.socket)
        return again.status
      }).toBe('HTTP/1.1 200 Connection Established')
    } finally {
      for (const socket of open) socket.destroy()
    }
  })

  it('refuses plain absolute-form http requests', async () => {
    const proxy = egressProxy({ log: () => {} })
    const port = await listen(proxy)
    const res = await new Promise<number>((resolve, reject) => {
      const sock = connect(port, '127.0.0.1', () => sock.write('GET http://example.com/ HTTP/1.1\r\nhost: example.com\r\n\r\n'))
      sock.once('data', (d: Buffer) => {
        resolve(Number(d.toString().split(' ')[1]))
        sock.destroy()
      })
      sock.on('error', reject)
    })
    expect(res).toBe(405)
  })

  it('answers 502 when the upstream cannot be reached', async () => {
    const closed = createServer()
    const closedPort = await listen(closed)
    await new Promise((r) => closed.close(() => r(undefined)))
    servers.splice(servers.indexOf(closed), 1)
    const logs: EgressLog[] = []
    const proxy = egressProxy({ resolve: async () => ['93.184.216.34'], log: (e) => logs.push(e), connect: () => connect(closedPort, '127.0.0.1') })
    const port = await listen(proxy)
    const { status, socket } = await tunnel(port, 'example.com:443')
    socket.destroy()
    expect(status).toContain('502')
    expect(logs[0]).toMatchObject({ outcome: 'failed' })
  })

  it('names what is not public', () => {
    expect(notPublic('8.8.8.8')).toBeUndefined()
    expect(notPublic('2606:4700::1111')).toBeUndefined()
    expect(notPublic('127.0.0.1')).toBe('loopback')
    expect(notPublic('172.16.0.1')).toBe('private')
    expect(notPublic('fd00::1')).toBe('private')
    expect(notPublic('100.100.0.1')).toBe('not public')
    expect(notPublic('64:ff9b::a00:1')).toBe('private')
    expect(connectTarget('example.com:443')).toEqual({ host: 'example.com', port: 443 })
    expect(connectTarget('[2606:4700::1111]:443')).toEqual({ host: '2606:4700::1111', port: 443 })
    expect(connectTarget('example.com')).toBeUndefined()
  })
})
