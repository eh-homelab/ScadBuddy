import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api } from '../../api/client'
import type { LegacyCaller, ServiceCaller, ServiceCredentialView } from '../../api/types'
import { Button } from '@/components/ui/button'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'

// #4975 phase 4 — Settings → System → Service credentials (spec 2026-11-01 §7.2): the bearer
// credentials the API issues each service. Never a secret: created, expires, last used, with
// revoke. The agent reads a sealed one, rotated on its own and by "Rotate now"; the print,
// projects and render workers, the storefront and the intake worker get none of those
// (#5298, #5282, #6133): they exchange a proof for each, listed here to be revoked.

type Section =
  | { caller: LegacyCaller; label: string; sealed: true }
  | { caller: Exclude<ServiceCaller, LegacyCaller>; label: string; sealed: false }

const CALLERS: Section[] = [
  { caller: 'agent', label: 'Agent', sealed: true },
  { caller: 'print', label: 'Print worker', sealed: false },
  { caller: 'projects', label: 'Projects worker', sealed: false },
  { caller: 'render', label: 'Render worker', sealed: false },
  { caller: 'storefront', label: 'Storefront', sealed: false },
  { caller: 'intake', label: 'Intake worker', sealed: false },
]

const day = (iso: string) => new Date(iso).toLocaleDateString()

export function ServiceCredentials() {
  const listed = useAsync(() => api.listServiceCredentials(), [])
  const [rows, setRows] = useState<ServiceCredentialView[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const shown = rows ?? listed.data ?? []

  async function run(key: string, action: () => Promise<ServiceCredentialView[]>) {
    setBusy(key)
    setError(null)
    try {
      setRows(await action())
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(null)
    }
  }

  if (listed.error && !rows) {
    return (
      <p role="alert" className="text-sm text-warn">
        {listed.error.message}
      </p>
    )
  }

  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        Issued and rotated by ScadBuddy. The agent's each last 30 days and the next is issued a week before, so a
        rotation never cuts it off; revoking the last live one issues a replacement at once.
        The other services exchange a proof for each credential, which lasts 15 minutes; a revoked one is replaced at
        the service's next exchange.
      </p>
      {CALLERS.map((section) => {
        const { caller, label } = section
        const own = shown.filter((row) => row.caller === caller)
        return (
          <section key={caller} aria-label={label} className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-sm">{label}</h4>
              {section.sealed && (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={busy !== null}
                  onClick={() => void run(`rotate-${caller}`, () => api.rotateServiceCredential(section.caller))}
                  {...USER_ONLY}
                >
                  Rotate now
                </Button>
              )}
            </div>
            <ul className="divide-y rounded-md border" aria-label={`${label} credentials`}>
              {own.length === 0 && <li className="px-3 py-2 text-sm text-muted-foreground">None issued.</li>}
              {own.map((row) => (
                <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
                  <div>
                    <p className="sb-num">
                      {row.id}
                      {!row.live && (
                        <span className="ml-2 text-xs text-muted-foreground">{row.revoked_at ? 'revoked' : 'expired'}</span>
                      )}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      created {day(row.created_at)} · expires {day(row.expires_at)} · {row.last_used_at ? `last used ${timeAgo(row.last_used_at)}` : 'never used'}
                    </p>
                  </div>
                  {row.live && (
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy !== null}
                      onClick={() => void run(row.id, () => api.revokeServiceCredential(row.id))}
                      {...USER_ONLY}
                    >
                      Revoke
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )
      })}
      {error && (
        <p role="alert" className="text-sm text-warn">
          {error}
        </p>
      )}
    </div>
  )
}
