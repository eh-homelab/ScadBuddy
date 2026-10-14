import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { ApprovedBrowserOrigin } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

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
    <section className="mt-4 rounded-[6px] border border-line bg-surface" data-testid="browser-origins">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">AI headless browser</h2>
      <form
        className="flex flex-col gap-3 p-4"
        onSubmit={(event: FormEvent) => {
          event.preventDefault()
          void save({ allow: lines(allowText), deny: lines(denyText) })
        }}
      >
        <p id="browser-origins-help" className="text-[12px] text-muted">
          For the assistant&rsquo;s own browser (the Playwright plugin in Settings &rarr; Plugins). Sites it may open
          without asking, sites it may never open (even if you approve), and anything else asks you in the chat first,
          where you can allow it once, for that chat, or always. One per line: a site such as{' '}
          <code>makerworld.com</code> (https only), all its subdomains as <code>*.example.com</code>, an exact origin
          such as <code>http://printer.lan:8080</code>, or an address range such as <code>10.0.0.0/8</code>.
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-[13px]">
            Opens without asking
            <textarea
              value={allowText}
              onChange={(event) => setAllow(event.target.value)}
              rows={8}
              spellCheck={false}
              className="sb-field font-mono text-[12px]"
              aria-describedby="browser-origins-help"
              {...USER_ONLY}
            />
          </label>
          <label className="flex flex-col gap-1 text-[13px]">
            Never opens
            <textarea
              value={denyText}
              onChange={(event) => setDeny(event.target.value)}
              rows={8}
              spellCheck={false}
              className="sb-field font-mono text-[12px]"
              aria-describedby="browser-origins-help"
              {...USER_ONLY}
            />
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" variant="primary" size="sm" disabled={!changed || saving} aria-busy={saving} {...USER_ONLY}>
            {saving && <Spinner />}
            Save
          </Button>
          <Button
            type="button"
            variant="default"
            size="sm"
            disabled={defaults || saving}
            onClick={() => void save(current.defaults)}
            {...USER_ONLY}
          >
            Restore defaults
          </Button>
          {saved && !changed && (
            <span role="status" className="text-[12px] text-ok">
              Saved. The next page the browser opens uses these lists.
            </span>
          )}
        </div>
        {error && (
          <p role="alert" className="text-[12px] text-warn">
            {error}
          </p>
        )}
      </form>
      <div className="border-t border-line p-4">
        <h3 className="text-[12.5px] font-medium">Approved in chats</h3>
        {current.approved.length === 0 ? (
          <p className="mt-1 text-[12px] text-muted">None yet. Sites you allow for one chat are listed here.</p>
        ) : (
          <ul className="mt-2 flex flex-col gap-1.5" aria-label="Sites approved in chats">
            {current.approved.map((item) => {
              const key = `${item.session_id} ${item.origin}`
              return (
                <li key={key} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px]">
                  <span className="min-w-0 break-all font-mono text-[12px]">{item.origin}</span>
                  <span className="min-w-0 text-muted">
                    {item.scope === 'session' ? 'for' : 'opened once in'} &ldquo;{item.title || 'Untitled chat'}&rdquo;
                  </span>
                  <Button
                    type="button"
                    variant="default"
                    size="sm"
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
      </div>
    </section>
  )
}
