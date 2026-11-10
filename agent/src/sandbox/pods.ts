import { randomBytes } from 'node:crypto'
import { lookup } from 'node:dns/promises'
import { readFile } from 'node:fs/promises'
import { request as httpsRequest } from 'node:https'
import { request as httpRequest } from 'node:http'
import { isIP } from 'node:net'
import type { SandboxEndpoint, Sandboxes } from './client.js'
import { SANDBOX_PORT } from './protocol.js'

// A pod per session in the sandbox namespace (#5864, issue comment "Design" §1), made and found
// through the Kubernetes API with the worker's own ServiceAccount token (a Role in that namespace:
// pods create, get, list, watch, delete; nothing else). The pod holds no secret: no service-account
// token, no Secret, no service links; a read-only root, its own user namespace, and limits. The
// cluster's admission policy for the namespace refuses any other pod shape. Its exec server exits
// after 15 idle minutes, so the pod ends; `sweep` deletes every pod that has ended. Kept apart from
// the shell tool, so #779's stdio MCP servers can use the same runtime.

export const APP_LABEL = 'app.kubernetes.io/name'
export const APP = 'scadbuddy-sandbox'
export const SESSION_LABEL = 'scadbuddy.eh-homelab.io/session'
/** How long a new pod may take to be ready. */
export const READY_MS = 90_000
/** A pod's hard lifetime (activeDeadlineSeconds): past it the kubelet ends it, whatever runs. */
export const ACTIVE_DEADLINE_S = 4 * 60 * 60

export type SandboxConfig = {
  namespace: string
  image: string
  /** The egress proxy, `http://<host>:<port>`; its host is resolved here, since the pod has no DNS. */
  egressProxy: string
  tokenFile: string
  caFile: string
}

/** The Kubernetes API, as this worker reaches it. */
export type KubeApi = (method: string, path: string, body?: unknown) => Promise<{ status: number; body: unknown }>

/** The in-cluster API: KUBERNETES_SERVICE_HOST, the token file read on each call (a projected token rotates), and the cluster CA. */
export function inClusterApi(config: Pick<SandboxConfig, 'tokenFile' | 'caFile'>, env: NodeJS.ProcessEnv = process.env): KubeApi {
  const host = env.KUBERNETES_SERVICE_HOST
  const port = env.KUBERNETES_SERVICE_PORT ?? '443'
  if (!host) throw new Error('KUBERNETES_SERVICE_HOST is not set: the sandbox needs the Kubernetes API')
  return async (method, path, body) => {
    const [token, ca] = await Promise.all([readFile(config.tokenFile, 'utf8'), readFile(config.caFile)])
    return send(httpsRequest, { host: isIP(host) === 6 ? `[${host}]` : host, port, ca, token: token.trim() }, method, path, body)
  }
}

/** A plain-http API (tests' fake). */
export function plainApi(base: URL, token = 'test'): KubeApi {
  return (method, path, body) => send(httpRequest, { host: base.hostname, port: base.port, token }, method, path, body)
}

function send(
  request: typeof httpsRequest,
  target: { host: string; port: string; token: string; ca?: Buffer },
  method: string,
  path: string,
  body: unknown,
): Promise<{ status: number; body: unknown }> {
  const payload = body === undefined ? undefined : Buffer.from(JSON.stringify(body))
  return new Promise((resolve, reject) => {
    const req = request(
      {
        host: target.host.replace(/^\[|\]$/g, ''),
        port: target.port,
        method,
        path,
        ...(target.ca ? { ca: target.ca } : {}),
        headers: {
          authorization: `Bearer ${target.token}`,
          accept: 'application/json',
          ...(payload ? { 'content-type': 'application/json', 'content-length': payload.length } : {}),
        },
        timeout: 30_000,
      },
      (res) => {
        const chunks: Buffer[] = []
        res.on('data', (c: Buffer) => chunks.push(c))
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8')
          let parsed: unknown = text
          try {
            parsed = text ? JSON.parse(text) : undefined
          } catch {
            // Not JSON: kept as text for the error.
          }
          resolve({ status: res.statusCode ?? 0, body: parsed })
        })
        res.on('error', reject)
      },
    )
    req.on('timeout', () => req.destroy(new Error('the Kubernetes API did not answer in 30 s')))
    req.on('error', reject)
    req.end(payload)
  })
}

type Pod = {
  metadata: { name: string; labels?: Record<string, string>; deletionTimestamp?: string; creationTimestamp?: string }
  spec: { containers: { name: string; env?: { name: string; value?: string }[] }[] }
  status?: { phase?: string; podIP?: string; conditions?: { type: string; status: string }[] }
}

const ready = (pod: Pod) => pod.status?.phase === 'Running' && !!pod.status.podIP && !!pod.status.conditions?.some((c) => c.type === 'Ready' && c.status === 'True')
const ended = (pod: Pod) => pod.status?.phase === 'Succeeded' || pod.status?.phase === 'Failed'
/** The oldest first, then by name: every replica orders a session's pods the same way. */
const oldestFirst = (a: Pod, b: Pod) =>
  (a.metadata.creationTimestamp ?? '').localeCompare(b.metadata.creationTimestamp ?? '') || a.metadata.name.localeCompare(b.metadata.name)
const tokenOf = (pod: Pod) => pod.spec.containers.find((c) => c.name === 'sandbox')?.env?.find((e) => e.name === 'SCADBUDDY_SANDBOX_TOKEN')?.value

/** The pod a session's sandbox runs in. Every field the cluster's admission policy checks is set here. */
export function sandboxPod(config: Pick<SandboxConfig, 'image'>, sessionId: string, token: string, proxy: string): Record<string, unknown> {
  return {
    apiVersion: 'v1',
    kind: 'Pod',
    metadata: {
      generateName: `${APP}-`,
      labels: { [APP_LABEL]: APP, [SESSION_LABEL]: sessionId },
    },
    spec: {
      restartPolicy: 'Never',
      // The image is built for amd64 only (build-image.yml); the cluster's admission policy allows this selector and no other.
      nodeSelector: { 'kubernetes.io/arch': 'amd64' },
      automountServiceAccountToken: false,
      enableServiceLinks: false,
      hostUsers: false,
      activeDeadlineSeconds: ACTIVE_DEADLINE_S,
      terminationGracePeriodSeconds: 5,
      // No cluster DNS: the proxy is given as an address, and every name a command uses is resolved at the proxy.
      dnsPolicy: 'None',
      dnsConfig: { nameservers: ['127.0.0.1'] },
      securityContext: {
        runAsNonRoot: true,
        runAsUser: 10001,
        runAsGroup: 10001,
        fsGroup: 10001,
        seccompProfile: { type: 'RuntimeDefault' },
      },
      containers: [
        {
          name: 'sandbox',
          image: config.image,
          imagePullPolicy: 'IfNotPresent',
          env: [
            { name: 'SCADBUDDY_SANDBOX_ROLE', value: 'exec' },
            { name: 'SCADBUDDY_SANDBOX_TOKEN', value: token },
            { name: 'SCADBUDDY_SANDBOX_PROXY', value: proxy },
          ],
          ports: [{ name: 'exec', containerPort: SANDBOX_PORT, protocol: 'TCP' }],
          readinessProbe: { httpGet: { path: '/healthz', port: SANDBOX_PORT }, periodSeconds: 2 },
          resources: {
            requests: { cpu: '100m', memory: '256Mi', 'ephemeral-storage': '256Mi' },
            limits: { cpu: '1', memory: '1Gi', 'ephemeral-storage': '3Gi' },
          },
          securityContext: {
            allowPrivilegeEscalation: false,
            readOnlyRootFilesystem: true,
            capabilities: { drop: ['ALL'] },
          },
          volumeMounts: [
            { name: 'work', mountPath: '/work' },
            { name: 'tmp', mountPath: '/tmp' },
          ],
        },
      ],
      volumes: [
        { name: 'work', emptyDir: { sizeLimit: '2Gi' } },
        { name: 'tmp', emptyDir: { sizeLimit: '512Mi' } },
      ],
    },
  }
}

/** `proxy` with its host resolved to an address: the sandbox pod has no DNS. */
export async function resolvedProxy(proxy: string, resolve: (host: string) => Promise<string> = async (h) => (await lookup(h)).address): Promise<string> {
  const url = new URL(proxy)
  const host = url.hostname.replace(/^\[|\]$/g, '')
  if (isIP(host) !== 0) return proxy
  const address = await resolve(host)
  url.hostname = isIP(address) === 6 ? `[${address}]` : address
  return url.href.replace(/\/$/, '')
}

export class KubernetesSandboxes implements Sandboxes {
  readonly #api: KubeApi
  readonly #config: SandboxConfig
  readonly #known = new Map<string, SandboxEndpoint & { pod: string }>()
  readonly #resolveProxy: () => Promise<string>
  readonly #pollMs: number

  constructor(config: SandboxConfig, api: KubeApi, options: { resolveProxy?: () => Promise<string>; pollMs?: number } = {}) {
    this.#config = config
    this.#api = api
    this.#resolveProxy = options.resolveProxy ?? (() => resolvedProxy(config.egressProxy))
    this.#pollMs = options.pollMs ?? 500
  }

  #pods(): string {
    return `/api/v1/namespaces/${encodeURIComponent(this.#config.namespace)}/pods`
  }

  async #list(selector: string): Promise<Pod[]> {
    const res = await this.#api('GET', `${this.#pods()}?labelSelector=${encodeURIComponent(selector)}`)
    if (res.status !== 200) throw new Error(`listing sandbox pods answered ${res.status}: ${JSON.stringify(res.body).slice(0, 300)}`)
    return (res.body as { items: Pod[] }).items
  }

  async #get(name: string): Promise<Pod | undefined> {
    const res = await this.#api('GET', `${this.#pods()}/${encodeURIComponent(name)}`)
    if (res.status === 404) return undefined
    if (res.status !== 200) throw new Error(`reading sandbox pod ${name} answered ${res.status}`)
    return res.body as Pod
  }

  async #delete(name: string): Promise<void> {
    const res = await this.#api('DELETE', `${this.#pods()}/${encodeURIComponent(name)}`)
    if (res.status !== 200 && res.status !== 202 && res.status !== 404) throw new Error(`deleting sandbox pod ${name} answered ${res.status}`)
  }

  #endpoint(pod: Pod): (SandboxEndpoint & { pod: string }) | undefined {
    const token = tokenOf(pod)
    if (!token || !pod.status?.podIP) return undefined
    const ip = pod.status.podIP
    return { url: `http://${isIP(ip) === 6 ? `[${ip}]` : ip}:${SANDBOX_PORT}`, token, pod: pod.metadata.name }
  }

  async #waitReady(name: string, signal: AbortSignal): Promise<SandboxEndpoint & { pod: string }> {
    const deadline = Date.now() + READY_MS
    for (;;) {
      signal.throwIfAborted()
      const pod = await this.#get(name)
      if (!pod) throw new Error(`sandbox pod ${name} is gone`)
      if (ended(pod)) throw new Error(`sandbox pod ${name} ended (${pod.status?.phase}) before it was ready`)
      const endpoint = ready(pod) ? this.#endpoint(pod) : undefined
      if (endpoint) return endpoint
      if (Date.now() > deadline) {
        await this.#delete(name).catch(() => {})
        throw new Error(`sandbox pod ${name} was not ready within ${READY_MS / 1000} s`)
      }
      await new Promise((r) => setTimeout(r, this.#pollMs))
    }
  }

  async acquire(sessionId: string, signal: AbortSignal): Promise<SandboxEndpoint> {
    const known = this.#known.get(sessionId)
    if (known) return known
    let pod = await this.#oldest(sessionId)
    if (!pod) {
      const token = randomBytes(32).toString('hex')
      const res = await this.#api('POST', this.#pods(), sandboxPod(this.#config, sessionId, token, await this.#resolveProxy()))
      if (res.status !== 201 && res.status !== 200) throw new Error(`creating the sandbox pod answered ${res.status}: ${JSON.stringify(res.body).slice(0, 500)}`)
      // Another replica may have made one at the same time: both keep the older, so they settle on one.
      pod = (await this.#oldest(sessionId)) ?? (res.body as Pod)
    }
    const endpoint = await this.#waitReady(pod.metadata.name, signal)
    this.#known.set(sessionId, endpoint)
    return endpoint
  }

  /** The session's oldest live pod; any younger one (two replicas raced) is deleted, never the one another replica uses. */
  async #oldest(sessionId: string): Promise<Pod | undefined> {
    const pods = (await this.#list(`${APP_LABEL}=${APP},${SESSION_LABEL}=${sessionId}`))
      .filter((p) => !p.metadata.deletionTimestamp && !ended(p))
      .sort(oldestFirst)
    const [first, ...younger] = pods
    for (const pod of younger) await this.#delete(pod.metadata.name).catch(() => {})
    return first
  }

  release(sessionId: string, endpoint: SandboxEndpoint): void {
    const known = this.#known.get(sessionId)
    if (known && known.url === endpoint.url) this.#known.delete(sessionId)
  }

  /** Deletes every sandbox pod that has ended (idle, past its deadline, or failed); returns how many. */
  async sweep(): Promise<number> {
    const pods = await this.#list(`${APP_LABEL}=${APP}`)
    let n = 0
    for (const pod of pods.filter(ended)) {
      await this.#delete(pod.metadata.name)
      n++
    }
    // Forget every pod that is gone or ended, so a long-lived worker keeps no entry per session it ever served.
    const live = new Set(pods.filter((p) => !ended(p) && !p.metadata.deletionTimestamp).map((p) => p.metadata.name))
    for (const [session, known] of this.#known) if (!live.has(known.pod)) this.#known.delete(session)
    return n
  }
}
