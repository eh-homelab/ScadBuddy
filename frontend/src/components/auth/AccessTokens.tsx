import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api } from '../../api/client'
import type { AccessTokenScope, AccessTokenView } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'

// #4975 phase 6 — Settings → System → Access tokens (spec 2026-11-01 §7.5; D6): a signed-in
// person's personal access tokens, for bambuddy-fs and the Go client. A new token is shown
// once, here; ScadBuddy keeps only its SHA-256. `read` reaches every read, `write` every
// change; neither reaches what only a person does in the browser.

const day = (iso: string) => new Date(iso).toLocaleDateString()

export function AccessTokens() {
  const listed = useAsync(() => api.listAccessTokens(), [])
  const [rows, setRows] = useState<AccessTokenView[] | null>(null)
  const [name, setName] = useState('')
  const [scope, setScope] = useState<AccessTokenScope>('read')
  const [days, setDays] = useState(90)
  const [minted, setMinted] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const shown = rows ?? listed.data ?? []

  async function refresh() {
    setRows(await api.listAccessTokens())
  }

  async function mint() {
    setBusy(true)
    setError(null)
    try {
      const created = await api.createAccessToken({ name: name.trim(), scope, expires_in_days: days })
      setMinted(created.token)
      setName('')
      await refresh()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function revoke(id: string) {
    setBusy(true)
    setError(null)
    try {
      await api.revokeAccessToken(id)
      await refresh()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
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
        For bambuddy-fs and other programs that call ScadBuddy as you: pass the token with{' '}
        <code>--token-file</code> or <code>$BAMBUDDY_FS_TOKEN_FILE</code>. A read token reads only.
      </p>
      <form
        className="flex flex-wrap items-end gap-2"
        aria-label="New access token"
        onSubmit={(event) => {
          event.preventDefault()
          void mint()
        }}
      >
        <label className="grid gap-1 text-xs">
          Name
          <Input value={name} maxLength={100} onChange={(event) => setName(event.target.value)} />
        </label>
        <div className="grid gap-1 text-xs">
          <span id="access-token-scope">Scope</span>
          <Select value={scope} onValueChange={(value) => setScope(value as AccessTokenScope)}>
            <SelectTrigger size="sm" aria-labelledby="access-token-scope">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="read">Read</SelectItem>
              <SelectItem value="write">Read and write</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <label className="grid gap-1 text-xs">
          Expires in days
          <Input
            type="number"
            min={1}
            max={365}
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
          />
        </label>
        <Button type="submit" size="sm" disabled={busy || name.trim() === ''} {...USER_ONLY}>
          Create token
        </Button>
      </form>
      {minted && (
        <div role="status" className="space-y-1 rounded-md border p-3 text-sm">
          <p>Copy it now: it is not shown again.</p>
          <code className="block break-all sb-num" aria-label="New token">
            {minted}
          </code>
          <Button variant="outline" size="sm" onClick={() => setMinted(null)}>
            Done
          </Button>
        </div>
      )}
      <ul className="divide-y rounded-md border" aria-label="Access tokens">
        {shown.length === 0 && <li className="px-3 py-2 text-sm text-muted-foreground">No tokens.</li>}
        {shown.map((row) => (
          <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
            <div>
              <p>
                {row.name} <span className="text-xs text-muted-foreground">{row.scope === 'write' ? 'read and write' : 'read'}</span>
                {!row.live && (
                  <span className="ml-2 text-xs text-muted-foreground">{row.revoked_at ? 'revoked' : 'expired'}</span>
                )}
              </p>
              <p className="text-xs text-muted-foreground">
                created {day(row.created_at)} · expires {day(row.expires_at)} ·{' '}
                {row.last_used_at ? `last used ${timeAgo(row.last_used_at)}` : 'never used'}
              </p>
            </div>
            {row.live && (
              <Button variant="outline" size="sm" disabled={busy} onClick={() => void revoke(row.id)} {...USER_ONLY}>
                Revoke
              </Button>
            )}
          </li>
        ))}
      </ul>
      {error && (
        <p role="alert" className="text-sm text-warn">
          {error}
        </p>
      )}
    </div>
  )
}
