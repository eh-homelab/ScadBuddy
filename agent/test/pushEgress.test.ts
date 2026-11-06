import { describe, expect, it } from 'vitest'
import { EgressError } from '../src/http/egress.js'
import { checkPushEndpoint, DEFAULT_PUSH_HOSTS, hostOnAllowlist, isHostPattern, isPublicAddress, pushHostsOf } from '../src/notify/pushEgress.js'

// Where a push may go (#815): https, an allowlisted push service, public addresses only.

const PUBLIC = (host: string) => Promise.resolve(host === 'fcm.googleapis.com' ? ['142.250.1.95'] : ['203.0.113.5'])

describe('isPublicAddress', () => {
  it('refuses loopback, RFC 1918, ULA, CGNAT, link-local, unspecified, multicast and IPv6 forms that embed them', () => {
    for (const a of [
      '127.0.0.1', '10.1.2.3', '172.16.0.1', '192.168.1.1', '100.64.0.1', '100.127.255.254', '169.254.169.254', '0.0.0.0',
      '224.0.0.1', '::1', '::', 'fe80::1', 'fd00::1', 'fc00::1', 'ff02::1', '::ffff:10.0.0.1', '64:ff9b::a9fe:a9fe', '2002:c0a8:0101::1',
    ]) {
      expect(isPublicAddress(a), a).toBe(false)
    }
    for (const a of ['142.250.1.95', '203.0.113.5', '100.128.0.1', '2607:f8b0:4004:800::200e']) expect(isPublicAddress(a), a).toBe(true)
  })
})

describe('the push-service allowlist', () => {
  it('matches a host exactly, or under a *. suffix but never the bare suffix', () => {
    expect(hostOnAllowlist('fcm.googleapis.com', DEFAULT_PUSH_HOSTS)).toBe(true)
    expect(hostOnAllowlist('wns2-by3p.notify.windows.com', DEFAULT_PUSH_HOSTS)).toBe(true)
    expect(hostOnAllowlist('api.push.apple.com', DEFAULT_PUSH_HOSTS)).toBe(true)
    expect(hostOnAllowlist('notify.windows.com', DEFAULT_PUSH_HOSTS)).toBe(false)
    expect(hostOnAllowlist('evilnotify.windows.com', DEFAULT_PUSH_HOSTS)).toBe(false)
    expect(hostOnAllowlist('fcm.googleapis.com.evil.example', DEFAULT_PUSH_HOSTS)).toBe(false)
  })

  it('is the default until a valid list is stored', () => {
    expect(pushHostsOf(undefined)).toBe(DEFAULT_PUSH_HOSTS)
    expect(pushHostsOf(['Bad Host'])).toBe(DEFAULT_PUSH_HOSTS)
    expect(pushHostsOf(['push.example.net'])).toEqual(['push.example.net'])
    expect(isHostPattern('*.push.example.net')).toBe(true)
    expect(isHostPattern('*')).toBe(false)
  })
})

describe('checkPushEndpoint', () => {
  it('pins an allowlisted https endpoint to its public address', async () => {
    expect(await checkPushEndpoint('https://fcm.googleapis.com/fcm/send/abc', DEFAULT_PUSH_HOSTS, PUBLIC)).toEqual({
      url: 'https://fcm.googleapis.com/fcm/send/abc',
      address: '142.250.1.95',
    })
  })

  it('refuses http (loopback included), a private address, a name resolving to one, and a host off the list', async () => {
    const refused = async (url: string, hosts: readonly string[], resolve = PUBLIC) =>
      expect(checkPushEndpoint(url, hosts, resolve)).rejects.toThrow(EgressError)
    await refused('http://fcm.googleapis.com/x', DEFAULT_PUSH_HOSTS)
    await refused('http://127.0.0.1:8080/x', ['127.0.0.1'])
    await refused('https://10.0.0.5/x', ['10.0.0.5'])
    await refused('https://fcm.googleapis.com/x', DEFAULT_PUSH_HOSTS, () => Promise.resolve(['192.168.1.20']))
    await refused('https://fcm.googleapis.com/x', DEFAULT_PUSH_HOSTS, () => Promise.resolve(['142.250.1.95', '100.64.0.9']))
    await refused('https://push.example.net/x', DEFAULT_PUSH_HOSTS)
    await refused('https://user:pw@fcm.googleapis.com/x', DEFAULT_PUSH_HOSTS)
  })
})
