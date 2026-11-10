import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { APP, APP_LABEL, KubernetesSandboxes, plainApi, resolvedProxy, sandboxPod, SESSION_LABEL } from '../src/sandbox/pods.js'
import { ConfigError, loadConfig } from '../src/config.js'

type FakePod = {
  metadata: { name: string; labels: Record<string, string>; deletionTimestamp?: string }
  spec: { containers: { name: string; env?: { name: string; value?: string }[] }[] } & Record<string, unknown>
  status: { phase: string; podIP?: string; conditions?: { type: string; status: string }[] }
}

/** A fake of the pods part of the Kubernetes API: create, get, list by label, delete. */
class FakeKube {
  pods = new Map<string, FakePod>()
  requests: string[] = []
  created: Record<string, unknown>[] = []
  next = 0
  /** A new pod is ready after this many reads. */
  readyAfter = 1
  reads = new Map<string, number>()
  server: Server = createServer((req, res) => {
    const chunks: Buffer[] = []
    req.on('data', (c: Buffer) => chunks.push(c))
    req.on('end', () => {
      const url = new URL(req.url ?? '/', 'http://kube')
      this.requests.push(`${req.method} ${url.pathname}${url.search}`)
      const send = (status: number, body: unknown) => {
        res.writeHead(status, { 'content-type': 'application/json' })
        res.end(JSON.stringify(body))
      }
      const m = /^\/api\/v1\/namespaces\/sandbox\/pods(?:\/([^/]+))?$/.exec(url.pathname)
      if (!m) return send(404, {})
      const name = m[1]
      if (req.method === 'POST' && !name) {
        const pod = JSON.parse(Buffer.concat(chunks).toString()) as FakePod & { metadata: { generateName: string } }
        this.created.push(pod as unknown as Record<string, unknown>)
        const made: FakePod = { ...pod, metadata: { name: `${pod.metadata.generateName}${++this.next}`, labels: pod.metadata.labels }, status: { phase: 'Pending' } }
        this.pods.set(made.metadata.name, made)
        return send(201, made)
      }
      if (req.method === 'GET' && !name) {
        const want = Object.fromEntries((url.searchParams.get('labelSelector') ?? '').split(',').map((kv) => kv.split('=')))
        const items = [...this.pods.values()].filter((p) => Object.entries(want).every(([k, v]) => p.metadata.labels[k] === v))
        return send(200, { items })
      }
      const pod = name ? this.pods.get(name) : undefined
      if (!pod) return send(404, {})
      if (req.method === 'GET') {
        const n = (this.reads.get(name!) ?? 0) + 1
        this.reads.set(name!, n)
        if (pod.status.phase === 'Pending' && n >= this.readyAfter) {
          pod.status = { phase: 'Running', podIP: '10.42.0.9', conditions: [{ type: 'Ready', status: 'True' }] }
        }
        return send(200, pod)
      }
      if (req.method === 'DELETE') {
        this.pods.delete(name!)
        return send(200, pod)
      }
      return send(405, {})
    })
  })
}

const config = { namespace: 'sandbox', image: 'ghcr.io/eh-homelab/scadbuddy-sandbox:sha-1@sha256:abc', egressProxy: 'http://proxy:3128', tokenFile: '/t', caFile: '/c' }
const SESSION = '00000000-0000-4000-8000-0000000000aa'

describe('sandbox pods on Kubernetes', () => {
  let kube: FakeKube
  let sandboxes: KubernetesSandboxes
  beforeEach(async () => {
    kube = new FakeKube()
    await new Promise<void>((r) => kube.server.listen(0, '127.0.0.1', r))
    const base = new URL(`http://127.0.0.1:${(kube.server.address() as AddressInfo).port}`)
    sandboxes = new KubernetesSandboxes(config, plainApi(base), { resolveProxy: async () => 'http://10.43.0.20:3128', pollMs: 10 })
  })
  afterEach(async () => {
    await new Promise((r) => kube.server.close(() => r(undefined)))
  })

  it('makes one pod for a session, waits until it is ready, and reuses it', async () => {
    kube.readyAfter = 3
    const endpoint = await sandboxes.acquire(SESSION, AbortSignal.timeout(5_000))
    expect(endpoint.url).toBe('http://10.42.0.9:8090')
    expect(endpoint.token).toMatch(/^[0-9a-f]{64}$/)
    expect(kube.created).toHaveLength(1)
    expect(await sandboxes.acquire(SESSION, AbortSignal.timeout(5_000))).toEqual(endpoint)
    expect(kube.created).toHaveLength(1)
  })

  it("finds a session's pod another replica made, with its token", async () => {
    const other = await sandboxes.acquire(SESSION, AbortSignal.timeout(5_000))
    const base = new URL(`http://127.0.0.1:${(kube.server.address() as AddressInfo).port}`)
    const replica = new KubernetesSandboxes(config, plainApi(base), { resolveProxy: async () => 'http://10.43.0.20:3128', pollMs: 10 })
    expect(await replica.acquire(SESSION, AbortSignal.timeout(5_000))).toEqual(other)
    expect(kube.created).toHaveLength(1)
  })

  it('looks again after a release, and sweeps every pod that has ended', async () => {
    const endpoint = await sandboxes.acquire(SESSION, AbortSignal.timeout(5_000))
    const pod = [...kube.pods.values()][0]!
    pod.status.phase = 'Succeeded'
    expect(await sandboxes.sweep()).toBe(1)
    expect(kube.pods.size).toBe(0)
    sandboxes.release(SESSION, endpoint)
    await sandboxes.acquire(SESSION, AbortSignal.timeout(5_000))
    expect(kube.created).toHaveLength(2)
  })

  it('makes a pod with no secret, no token, no DNS, its own user namespace and limits', () => {
    const pod = sandboxPod(config, SESSION, 't'.repeat(64), 'http://10.43.0.20:3128') as {
      metadata: { labels: Record<string, string> }
      spec: Record<string, unknown> & { containers: Record<string, unknown>[]; volumes: Record<string, unknown>[] }
    }
    expect(pod.metadata.labels).toEqual({ [APP_LABEL]: APP, [SESSION_LABEL]: SESSION })
    expect(pod.spec).toMatchObject({
      automountServiceAccountToken: false,
      enableServiceLinks: false,
      hostUsers: false,
      restartPolicy: 'Never',
      dnsPolicy: 'None',
      securityContext: { runAsNonRoot: true, seccompProfile: { type: 'RuntimeDefault' } },
    })
    const container = pod.spec.containers[0]!
    expect(container.securityContext).toEqual({ allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: ['ALL'] } })
    expect(container.resources).toMatchObject({ limits: { cpu: '1', memory: '1Gi' } })
    expect(pod.spec.volumes.every((v) => Object.keys(v).sort().join() === 'emptyDir,name')).toBe(true)
    expect(JSON.stringify(pod)).not.toMatch(/secretKeyRef|serviceAccountToken|hostPath/)
  })

  it('gives the pod the proxy as an address, since it has no DNS', async () => {
    expect(await resolvedProxy('http://scadbuddy-sandbox-egress.sandbox:3128', async () => '10.43.0.20')).toBe('http://10.43.0.20:3128')
    expect(await resolvedProxy('http://10.43.0.21:3128')).toBe('http://10.43.0.21:3128')
  })
})

describe('the sandbox configuration', () => {
  const all = {
    SCADBUDDY_SANDBOX_NAMESPACE: 'scadbuddy-sandbox',
    SCADBUDDY_SANDBOX_IMAGE: 'ghcr.io/eh-homelab/scadbuddy-sandbox:sha-1',
    SCADBUDDY_SANDBOX_EGRESS_PROXY: 'http://scadbuddy-sandbox-egress.scadbuddy-sandbox:3128',
    SCADBUDDY_SANDBOX_KUBE_TOKEN_FILE: '/var/run/sandbox/token',
    SCADBUDDY_SANDBOX_KUBE_CA_FILE: '/var/run/sandbox/ca.crt',
  }
  it('is all five variables or none', () => {
    expect(loadConfig({}).sandbox).toBeUndefined()
    expect(loadConfig(all).sandbox).toEqual({
      namespace: 'scadbuddy-sandbox',
      image: all.SCADBUDDY_SANDBOX_IMAGE,
      egressProxy: all.SCADBUDDY_SANDBOX_EGRESS_PROXY,
      tokenFile: '/var/run/sandbox/token',
      caFile: '/var/run/sandbox/ca.crt',
    })
    expect(() => loadConfig({ SCADBUDDY_SANDBOX_NAMESPACE: 'x' })).toThrow(ConfigError)
    expect(() => loadConfig({ ...all, SCADBUDDY_SANDBOX_EGRESS_PROXY: 'https://proxy:3128' })).toThrow(/http:\/\/<host>:<port>/)
    expect(() => loadConfig({ ...all, SCADBUDDY_SANDBOX_NAMESPACE: 'Not_A_Namespace' })).toThrow(/namespace/)
    expect(() => loadConfig({ ...all, SCADBUDDY_SANDBOX_KUBE_CA_FILE: 'ca.crt' })).toThrow(/absolute/)
  })
})
