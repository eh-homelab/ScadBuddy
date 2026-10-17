import { useEffect, useState, type ReactNode } from 'react'
import { USER_ONLY } from '../agent/dom'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { LoadingLines, SwitchField } from './settings/form'
import { Spinner } from './ui/spinner'

// Settings → "MCP sign-in (OIDC)" (#262). Talks to the agent service's
// /api/v1/ai/mcp/oidc (agent/src/routes/mcpAuth.ts), which the ingress routes to
// the agent (AI spec §4.2). That API is not in the backend's OpenAPI document, so
// the shapes are declared here. Nothing in it is secret.

export const OIDC_API = '/api/v1/ai/mcp/oidc'

type Tier = 'read' | 'write' | 'outward'
const TIERS: Tier[] = ['read', 'write', 'outward']

export interface OidcConfig {
  enabled: boolean
  issuer: string
  audience: string | null
  client_id: string | null
  scopes: Record<Tier, string>
  tier_claim: string | null
  algorithms: string[]
}

export interface OidcView {
  config: OidcConfig
  saved: boolean
  resource: string | null
  resource_metadata_url: string | null
  supported_algorithms: string[]
  can_enable: boolean
  cannot_enable_reason: string | null
}

interface DiscoveryReport {
  issuer: string
  jwks_uri: string
  keys: number
  dynamic_registration: boolean
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${OIDC_API}${path}`, {
    method,
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const data = (await res.json().catch(() => ({}))) as { detail?: string }
  if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
  return data as T
}

const blankToNull = (value: string) => (value.trim() === '' ? null : value.trim())

export function McpOidcSettings() {
  const [view, setView] = useState<OidcView | null>(null)
  const [form, setForm] = useState<OidcConfig | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState<string | null>(null)
  const [busy, setBusy] = useState<'test' | 'save' | null>(null)

  useEffect(() => {
    let live = true
    call<OidcView>('GET', '')
      .then((v) => {
        if (!live) return
        setView(v)
        setForm(v.config)
      })
      .catch((err: Error) => live && setError(err.message))
    return () => {
      live = false
    }
  }, [])

  if (!form || !view) {
    return error ? (
      <p role="alert" className="text-[13px] text-warn">
        {error}
      </p>
    ) : (
      <LoadingLines label="Loading the sign-in settings" lines={['w-2/3', 'h-8 w-full', 'h-8 w-full']} />
    )
  }

  const update = (change: Partial<OidcConfig>) => setForm({ ...form, ...change })

  async function test() {
    setBusy('test')
    setError(null)
    setStatus(null)
    try {
      const report = await call<DiscoveryReport>('POST', '/test', { issuer: form!.issuer })
      setStatus(
        `Found ${report.issuer}: ${report.keys} signing key${report.keys === 1 ? '' : 's'}; ` +
          (report.dynamic_registration
            ? 'MCP clients can register themselves.'
            : 'no dynamic registration, so register each MCP client in the IdP.'),
      )
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function save() {
    setBusy('save')
    setError(null)
    setStatus(null)
    try {
      const saved = await call<OidcView>('PUT', '', form)
      setView(saved)
      setForm(saved.config)
      setStatus(saved.config.enabled ? 'Saved. /mcp now accepts sign-ins from this issuer.' : 'Saved.')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const field = (id: string, label: ReactNode, input: ReactNode, help?: ReactNode) => (
    <div className="grid gap-1.5">
      <Label htmlFor={id} className="font-normal">
        {label}
      </Label>
      {input}
      {help && <p className="text-xs text-muted-foreground">{help}</p>}
    </div>
  )

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <SwitchField
          id="oidc-enabled"
          label="Let MCP clients sign in with this identity provider"
          checked={form.enabled}
          disabled={!view.can_enable && !form.enabled}
          onCheckedChange={(enabled) => update({ enabled })}
          describedBy="oidc-enabled-help"
        />
        <p id="oidc-enabled-help" className="text-xs text-muted-foreground">
          Bearer tokens keep working. Switching this on runs a discovery check against the issuer
          first, so a typo cannot lock every client out.
          {view.cannot_enable_reason && <> {view.cannot_enable_reason}.</>}
        </p>
      </div>

      {field(
        'oidc-issuer',
        'Issuer URL',
        <Input
          id="oidc-issuer"
          type="url"
          value={form.issuer}
          onChange={(event) => update({ issuer: event.target.value })}
          className="sb-num"
        />,
        'Exactly as the IdP writes it, trailing slash included.',
      )}

      {field(
        'oidc-audience',
        'Audience',
        <Input
          id="oidc-audience"
          value={form.audience ?? ''}
          placeholder={view.resource ?? ''}
          onChange={(event) => update({ audience: blankToNull(event.target.value) })}
          className="sb-num"
        />,
        <>
          Leave empty to require this server&rsquo;s resource URI
          {view.resource && (
            <>
              {' '}
              (<span className="sb-num">{view.resource}</span>)
            </>
          )}
          , as the MCP authorization spec asks.
        </>,
      )}

      {field(
        'oidc-client-id',
        <>Client ID for ScadBuddy&rsquo;s own login (optional)</>,
        <Input
          id="oidc-client-id"
          value={form.client_id ?? ''}
          onChange={(event) => update({ client_id: blankToNull(event.target.value) })}
          className="sb-num"
        />,
      )}

      <fieldset>
        <legend className="text-sm">Scope for each tier</legend>
        <div className="mt-1.5 grid gap-2 sm:grid-cols-3">
          {TIERS.map((tier) => (
            <div key={tier} className="grid gap-1">
              <Label htmlFor={`oidc-scope-${tier}`} className="text-xs font-normal text-muted-foreground">
                {tier}
              </Label>
              <Input
                id={`oidc-scope-${tier}`}
                aria-label={`Scope for ${tier}`}
                value={form.scopes[tier]}
                onChange={(event) => update({ scopes: { ...form.scopes, [tier]: event.target.value } })}
                className="sb-num"
              />
            </div>
          ))}
        </div>
      </fieldset>

      {field(
        'oidc-tier-claim',
        'Also read tiers from claim (optional)',
        <Input
          id="oidc-tier-claim"
          value={form.tier_claim ?? ''}
          placeholder="groups"
          onChange={(event) => update({ tier_claim: blankToNull(event.target.value) })}
          className="sb-num"
        />,
      )}

      <fieldset>
        <legend className="text-sm">Allowed signing algorithms</legend>
        <div className="mt-2 flex flex-wrap gap-4">
          {view.supported_algorithms.map((alg) => (
            <div key={alg} className="flex items-center gap-2">
              <Checkbox
                id={`oidc-alg-${alg}`}
                checked={form.algorithms.includes(alg)}
                onCheckedChange={(next) =>
                  update({
                    algorithms: next === true ? [...form.algorithms, alg] : form.algorithms.filter((a) => a !== alg),
                  })
                }
              />
              <Label htmlFor={`oidc-alg-${alg}`} className="sb-num text-xs font-normal">
                {alg}
              </Label>
            </div>
          ))}
        </div>
      </fieldset>

      {view.resource_metadata_url && (
        <p className="text-xs text-muted-foreground">
          MCP clients discover the IdP at{' '}
          <span className="sb-num">{view.resource_metadata_url}</span>.
        </p>
      )}

      {error && (
        <p role="alert" className="text-sm text-warn">
          {error}
        </p>
      )}
      {status && (
        <p role="status" className="text-xs text-ok">
          {status}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          onClick={() => void test()}
          disabled={busy !== null}
          aria-busy={busy === 'test'}
          {...USER_ONLY}
        >
          {busy === 'test' && <Spinner />}
          Test discovery
        </Button>
        <Button onClick={() => void save()} disabled={busy !== null} aria-busy={busy === 'save'} {...USER_ONLY}>
          {busy === 'save' && <Spinner />}
          Save sign-in settings
        </Button>
      </div>
    </div>
  )
}
