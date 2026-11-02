import { useEffect, useState, type ReactNode } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api } from '../../api/client'
import type { LoginConfigUpdate, LoginConfigView } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { refreshAuthSession } from '../../lib/signIn'
import { LoadingLines, SwitchField } from '../settings/form'
import { Spinner } from '../ui/spinner'

// #4975 — Settings → System → Sign-in (spec 2026-11-01 §9.2; D1, D3, D7): ScadBuddy's own
// login through an OIDC issuer. While SCADBUDDY_AUTH_MODE is off nobody is refused for not
// signing in; a signed-in person is shown in the header.

const blank = (value: string) => (value.trim() === '' ? null : value.trim())

function subjectsOf(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((subject) => subject.trim())
    .filter(Boolean)
}

/** What Settings may change: the issuer and client id are the deployment's. */
function editable(config: LoginConfigView['config']): LoginConfigUpdate {
  return {
    enabled: config.enabled,
    allowed_subjects: config.allowed_subjects,
    group_claim: config.group_claim,
    group_value: config.group_value,
    rp_logout: config.rp_logout,
    client_secret: null,
  }
}

export function LoginSettings() {
  const [view, setView] = useState<LoginConfigView | null>(null)
  const [form, setForm] = useState<LoginConfigUpdate | null>(null)
  const [subjects, setSubjects] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState<string | null>(null)
  const [busy, setBusy] = useState<'test' | 'save' | null>(null)

  useEffect(() => {
    let live = true
    void (async () => {
      try {
        const loaded = await api.getLoginConfig()
        const config = editable(loaded.config)
        if (!live) return
        setView(loaded)
        setForm(config)
        setSubjects((config.allowed_subjects ?? []).join('\n'))
      } catch (err) {
        if (live) setError((err as Error).message)
      }
    })()
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

  const update = (change: Partial<LoginConfigUpdate>) => setForm({ ...form, ...change })
  const secretSaved = view.has_client_secret
  const { issuer, client_id: clientId } = view.config

  async function test() {
    setBusy('test')
    setError(null)
    setStatus(null)
    try {
      const report = await api.testLoginConfig()
      setStatus(
        `Found ${report.issuer}: ${report.keys} signing key${report.keys === 1 ? '' : 's'}` +
          (report.end_session ? '; it can end its own session on sign-out.' : '.'),
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
      const saved = await api.putLoginConfig({ ...form!, allowed_subjects: subjectsOf(subjects) })
      setView(saved)
      setForm(editable(saved.config))
      setSubjects((saved.config.allowed_subjects ?? []).join('\n'))
      setStatus(saved.config.enabled ? 'Saved. The header now offers Sign in.' : 'Saved.')
      await refreshAuthSession()
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
          id="login-enabled"
          label="Let people sign in to ScadBuddy with this identity provider"
          checked={Boolean(form.enabled)}
          disabled={view.cannot_enable_reason !== null && !form.enabled}
          onCheckedChange={(enabled) => update({ enabled })}
          describedBy="login-enabled-help"
        />
        <p id="login-enabled-help" className="text-xs text-muted-foreground">
          Nobody is refused for not signing in yet. Switching this on checks the issuer first, so a typo
          cannot lock the sign-in out.
          {view.cannot_enable_reason && <> {view.cannot_enable_reason}.</>}
        </p>
      </div>

      {field(
        'login-issuer',
        'Issuer URL',
        <Input id="login-issuer" value={issuer || 'Not set'} readOnly className="sb-num" />,
        <>
          Set by the deployment, <span className="sb-num">SCADBUDDY_AUTH_OIDC_ISSUER</span>, so nobody can point the
          sign-in elsewhere.
        </>,
      )}

      {field(
        'login-client-id',
        'Client ID',
        <Input id="login-client-id" value={clientId || 'Not set'} readOnly className="sb-num" />,
        view.redirect_uri && (
          <>
            Register a confidential client with the redirect URI <span className="sb-num">{view.redirect_uri}</span>
            .
          </>
        ),
      )}

      {field(
        'login-client-secret',
        'Client secret',
        <Input
          id="login-client-secret"
          type="password"
          autoComplete="off"
          value={form.client_secret ?? ''}
          placeholder={secretSaved ? 'Saved; leave empty to keep it' : ''}
          onChange={(event) => update({ client_secret: blank(event.target.value) })}
          className="sb-num"
        />,
        'Kept encrypted; never shown again. Saved for this issuer and client ID only.',
      )}

      <fieldset className="space-y-3">
        <legend className="text-sm">Who may sign in</legend>
        {field(
          'login-subjects',
          'Subjects',
          <Textarea
            id="login-subjects"
            value={subjects}
            rows={3}
            onChange={(event) => setSubjects(event.target.value)}
            className="sb-num"
          />,
          "One per line. A refused sign-in names its subject, so the first one tells you yours.",
        )}
        <div className="grid gap-2 sm:grid-cols-2">
          {field(
            'login-group-claim',
            'Or a group claim',
            <Input
              id="login-group-claim"
              value={form.group_claim ?? ''}
              placeholder="groups"
              onChange={(event) => update({ group_claim: blank(event.target.value) })}
              className="sb-num"
            />,
          )}
          {field(
            'login-group-value',
            'holding',
            <Input
              id="login-group-value"
              value={form.group_value ?? ''}
              onChange={(event) => update({ group_value: blank(event.target.value) })}
              className="sb-num"
            />,
          )}
        </div>
      </fieldset>

      <SwitchField
        id="login-rp-logout"
        label="Signing out of ScadBuddy also signs out of the identity provider"
        checked={Boolean(form.rp_logout)}
        onCheckedChange={(rp_logout) => update({ rp_logout })}
      />

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
