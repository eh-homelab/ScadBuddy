import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { ApprovedBrowserOrigin } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { Button } from '@/components/ui/button'
import { CardContent } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { SaveFooter, SettingCard } from './settings/form'

const lines = (text: string) =>
  text
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)

const same = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((x, i) => x === b[i])

/**
 * #982 — where the assistant's headless browser (the Playwright plugin) may go, stored
 * by the agent service (`/api/v1/ai/settings/browser-origins`): sites on the first list
 * open at once, sites on the second never do, and any other asks in the chat first.
 * Below them, the sites approved in each chat, each with Revoke.
 *
 * Hidden when the agent service or its database is not there. Every control is
 * user-only: an agent must not widen where its own browser may go (AI design spec §8.1).
 */
export function BrowserOriginsSetting() {
  const setting = useAsync(() => api.getBrowserOrigins(), [])
  const [allow, setAllow] = useState<string | null>(null)
  const [deny, setDeny] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [revoking, setRevoking] = useState<string | null>(null)

  if (setting.error || !setting.data) return null
  const current = setting.data
  const allowText = allow ?? current.allow.join('\n')
  const denyText = deny ?? current.deny.join('\n')
  const changed = !same(lines(allowText), current.allow) || !same(lines(denyText), current.deny)
  const defaults = same(lines(allowText), current.defaults.allow) && same(lines(denyText), current.defaults.deny)

  async function save(lists: { allow: string[]; deny: string[] }) {
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      setting.setData(await api.putBrowserOrigins(lists))
      setAllow(null)
      setDeny(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the lists')
    } finally {
      setSaving(false)
    }
  }

  async function revoke(item: ApprovedBrowserOrigin) {
    const key = `${item.session_id} ${item.origin}`
    setRevoking(key)
    setError(null)
    try {
      await api.revokeBrowserOrigin(item.session_id, item.origin)
      setting.setData({ ...current, approved: current.approved.filter((a) => `${a.session_id} ${a.origin}` !== key) })
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not revoke it')
    } finally {
      setRevoking(null)
    }
  }

  return (
    <SettingCard title="AI headless browser" data-testid="browser-origins">
      <form
        onSubmit={(event: FormEvent) => {
          event.preventDefault()
          void save({ allow: lines(allowText), deny: lines(denyText) })
        }}
      >
        <CardContent className="space-y-4 py-4">
          <p id="browser-origins-help" className="text-xs text-muted-foreground">
            For the assistant&rsquo;s own browser (the Playwright plugin in Settings &rarr; Plugins). Sites it may open
            without asking, sites it may never open (even if you approve), and anything else asks you in the chat first,
            where you can allow it once, for that chat, or always. One per line: a site such as{' '}
            <code>makerworld.com</code> (https only), all its subdomains as <code>*.example.com</code>, an exact origin
            such as <code>http://printer.lan:8080</code>, or an address range such as <code>10.0.0.0/8</code>.
          </p>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="grid gap-1.5">
              <Label htmlFor="browser-origins-allow" className="font-normal">
                Opens without asking
              </Label>
              <Textarea
                id="browser-origins-allow"
                value={allowText}
                onChange={(event) => setAllow(event.target.value)}
                rows={8}
                spellCheck={false}
                className="min-h-40 font-mono text-xs"
                aria-describedby="browser-origins-help"
                {...USER_ONLY}
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="browser-origins-deny" className="font-normal">
                Never opens
              </Label>
              <Textarea
                id="browser-origins-deny"
                value={denyText}
                onChange={(event) => setDeny(event.target.value)}
                rows={8}
                spellCheck={false}
                className="min-h-40 font-mono text-xs"
                aria-describedby="browser-origins-help"
                {...USER_ONLY}
              />
            </div>
          </div>
        </CardContent>
        <SaveFooter
          saving={saving}
          disabled={!changed}
          saved={saved && !changed ? 'Saved. The next page the browser opens uses these lists.' : undefined}
          error={error}
        >
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={defaults || saving}
            onClick={() => void save(current.defaults)}
            {...USER_ONLY}
          >
            Restore defaults
          </Button>
        </SaveFooter>
      </form>
      <CardContent className="border-t py-4">
        <h3 className="text-sm font-medium">Approved in chats</h3>
        {current.approved.length === 0 ? (
          <p className="mt-1 text-xs text-muted-foreground">None yet. Sites you allow for one chat are listed here.</p>
        ) : (
          <ul className="mt-2 flex flex-col gap-1.5" aria-label="Sites approved in chats">
            {current.approved.map((item) => {
              const key = `${item.session_id} ${item.origin}`
              return (
                <li key={key} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
                  <span className="min-w-0 font-mono text-xs break-all">{item.origin}</span>
                  <span className="min-w-0 text-muted-foreground">
                    {item.scope === 'session' ? 'for' : 'opened once in'} &ldquo;{item.title || 'Untitled chat'}&rdquo;
                  </span>
                  <Button
                    type="button"
                    variant="outline"
                    size="xs"
                    disabled={revoking === key}
                    aria-busy={revoking === key}
                    aria-label={`Revoke ${item.origin} in ${item.title || 'Untitled chat'}`}
                    onClick={() => void revoke(item)}
                    {...USER_ONLY}
                  >
                    Revoke
                  </Button>
                </li>
              )
            })}
          </ul>
        )}
      </CardContent>
    </SettingCard>
  )
}
