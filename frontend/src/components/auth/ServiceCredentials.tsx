import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api } from '../../api/client'
import type { LegacyCaller, ServiceCredentialView } from '../../api/types'
import { Button } from '@/components/ui/button'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'

// #4975 phase 4 — Settings → System → Service credentials (spec 2026-11-01 §7.2): the bearer
// credentials the API issues the agent and the print worker, rotated on their own. Never a
// secret: created, expires, last used, with revoke and "Rotate now". The projects and render
// workers exchange theirs (#5298) and get none of these.

const CALLERS: { caller: LegacyCaller; label: string }[] = [
  { caller: 'agent', label: 'Agent' },
  { caller: 'print', label: 'Print worker' },
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
        Issued and rotated by ScadBuddy: each lasts 30 days and the next is issued a week before, so a rotation
        never cuts a service off. Revoking the last live one issues a replacement at once.
      </p>
      {CALLERS.map(({ caller, label }) => {
        const own = shown.filter((row) => row.caller === caller)
        return (
          <section key={caller} aria-label={label} className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-sm">{label}</h4>
              <Button
                variant="outline"
                size="sm"
                disabled={busy !== null}
                onClick={() => void run(`rotate-${caller}`, () => api.rotateServiceCredential(caller))}
                {...USER_ONLY}
              >
                Rotate now
              </Button>
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
