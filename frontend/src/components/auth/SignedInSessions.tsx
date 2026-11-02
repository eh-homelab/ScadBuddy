import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api } from '../../api/client'
import { Button } from '@/components/ui/button'
import { timeAgo } from '../../lib/format'
import { refreshAuthSession, useAuthSession } from '../../lib/signIn'
import { useAsync } from '../../lib/useAsync'

/** #4975 — the signed-in person's own sessions, with revoke and Sign out everywhere (spec §5.2). */
export function SignedInSessions() {
  const session = useAuthSession()
  const signedIn = Boolean(session?.signed_in)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const listed = useAsync(() => (signedIn ? api.listAuthSessions() : Promise.resolve([])), [signedIn])

  if (!signedIn) {
    return (
      <p className="text-sm text-muted-foreground">
        {session?.login_enabled ? 'Sign in to see your sessions.' : 'Sign-in is off.'}
      </p>
    )
  }

  async function run(id: string, action: () => Promise<unknown>) {
    setBusy(id)
    setError(null)
    try {
      await action()
      listed.reload()
      await refreshAuthSession()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const rows = listed.data ?? []
  return (
    <div className="space-y-3">
      <ul className="divide-y rounded-md border" aria-label="Signed-in sessions">
        {rows.map((row) => (
          <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
            <div>
              <p>
                {row.user_agent ?? 'Unknown browser'}
                {row.current && <span className="ml-2 text-xs text-ok">This browser</span>}
              </p>
              <p className="text-xs text-muted-foreground">
                {row.client_address ? `${row.client_address} · ` : ''}last used {timeAgo(row.last_seen_at)}
                {row.kind !== 'login' && ` · ${row.kind.replace('_', ' ')}`}
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              disabled={busy !== null}
              onClick={() => void run(row.id, () => api.revokeAuthSession(row.id))}
              {...USER_ONLY}
            >
              {row.current ? 'Sign out' : 'Revoke'}
            </Button>
          </li>
        ))}
      </ul>
      {error && (
        <p role="alert" className="text-sm text-warn">
          {error}
        </p>
      )}
      <Button
        variant="outline"
        disabled={busy !== null || rows.length === 0}
        onClick={() => void run('all', () => api.revokeAllAuthSessions())}
        {...USER_ONLY}
      >
        Sign out everywhere
      </Button>
    </div>
  )
}
