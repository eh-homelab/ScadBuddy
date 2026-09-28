import { useRef, useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { McpToken, McpTokenTier, MintedMcpToken } from '../api/mcpTokens'
import { copyText, selectContents } from '../lib/clipboard'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

// #251 — Settings → "MCP access tokens": mint, list and revoke the bearer tokens an
// outside MCP client presents at /mcp (AI design spec §8.1, §8.3). The agent service
// serves `/api/v1/ai/mcp-tokens` and keeps only a token's SHA-256, so the plaintext
// exists in the browser once, in the answer to the create request, and only in this
// component's state until Done.
//
// The plaintext is rendered as text in a <code>, not as a field's value: the browser
// agent's snapshot (src/agent/snapshot.ts) reads field values and role=status/alert
// text, and a token must never reach it. Create, Copy and Revoke are USER_ONLY: minting
// or revoking a credential is an outward write (spec §8.1) the agent may not make.

const TIER_LABEL: Record<McpTokenTier, string> = {
  read: 'Read',
  write: 'Write',
  outward: 'Outward',
}

const TIER_HELP: Record<McpTokenTier, string> = {
  read: 'Read: looks at models, renders and settings, and changes nothing.',
  write: 'Write: can also make changes that the model history can undo.',
  outward:
    'Outward: can also ask to send, print, delete or change settings; each of those waits for a person to approve it in ScadBuddy.',
}

const DAY = 24 * 60 * 60

const EXPIRY: { value: string; label: string; seconds: number | undefined }[] = [
  { value: '30', label: '30 days', seconds: 30 * DAY },
  { value: '90', label: '90 days', seconds: 90 * DAY },
  { value: '365', label: '1 year', seconds: 365 * DAY },
  { value: 'never', label: 'Never', seconds: undefined },
]

function day(iso: string): string {
  return iso.slice(0, 10)
}

function describeError(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.detail : fallback
}

function TokenRow({ token, onRevoke }: { token: McpToken; onRevoke: (token: McpToken) => void }) {
  const inactive = token.status !== 'active'
  return (
    <li className="flex items-start justify-between gap-3 py-2.5" data-testid="mcp-token">
      <div className="min-w-0">
        <p className={`truncate text-[13px] font-medium ${inactive ? 'text-muted line-through' : ''}`}>
          {token.name}
        </p>
        <p className="mt-0.5 text-[12px] text-muted">
          <span className="rounded-[4px] border border-line px-1 py-px">{TIER_LABEL[token.tier]}</span>
          {token.status === 'revoked' && token.revoked_at && (
            <span className="ml-2 text-warn">Revoked {day(token.revoked_at)}</span>
          )}
          {token.status === 'expired' && token.expires_at && (
            <span className="ml-2 text-warn">Expired {day(token.expires_at)}</span>
          )}
          <span className="ml-2" title={token.created_at}>
            Created {day(token.created_at)}
          </span>
          {token.status === 'active' && (
            <span className="ml-2" title={token.expires_at ?? undefined}>
              {token.expires_at ? `Expires ${day(token.expires_at)}` : 'Never expires'}
            </span>
          )}
          <span className="ml-2" title={token.last_used_at ?? undefined}>
            {token.last_used_at ? `Last used ${timeAgo(token.last_used_at)}` : 'Never used'}
          </span>
        </p>
      </div>
      {token.status !== 'revoked' && (
        <Button
          variant="danger"
          size="sm"
          onClick={() => onRevoke(token)}
          aria-label={`Revoke ${token.name}`}
          {...USER_ONLY}
        >
          Revoke
        </Button>
      )}
    </li>
  )
}

export function McpTokensSection() {
  const listState = useAsync(() => api.listMcpTokens(), [])
  const [name, setName] = useState('')
  const [tier, setTier] = useState<McpTokenTier>('read')
  const [expiry, setExpiry] = useState('90')
  const [creating, setCreating] = useState(false)
  const [minted, setMinted] = useState<MintedMcpToken | null>(null)
  const [copied, setCopied] = useState<'copied' | 'manual' | null>(null)
  const [confirming, setConfirming] = useState<McpToken | null>(null)
  const [revoking, setRevoking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [revokeError, setRevokeError] = useState<string | null>(null)
  const tokenRef = useRef<HTMLElement>(null)

  const list = listState.data
  const authMode = list?.auth_mode ?? null

  async function create(event: FormEvent) {
    event.preventDefault()
    setCreating(true)
    setError(null)
    try {
      const seconds = EXPIRY.find((option) => option.value === expiry)?.seconds
      const result = await api.createMcpToken({
        name: name.trim(),
        tier,
        ...(seconds === undefined ? {} : { expires_in: seconds }),
      })
      setMinted(result)
      setCopied(null)
      setName('')
      listState.reload()
    } catch (cause) {
      setError(describeError(cause, 'Could not create the token.'))
    } finally {
      setCreating(false)
    }
  }

  async function copy() {
    if (!minted) return
    setCopied((await copyText(minted.token, tokenRef.current)) ? 'copied' : 'manual')
  }

  function done() {
    // The plaintext leaves the page here; nothing can show it again.
    setMinted(null)
    setCopied(null)
    window.getSelection()?.removeAllRanges()
  }

  function closeConfirm() {
    if (revoking) return
    setConfirming(null)
    setRevokeError(null)
  }

  async function revoke() {
    if (!confirming) return
    setRevoking(true)
    setRevokeError(null)
    try {
      await api.revokeMcpToken(confirming.id)
      setConfirming(null)
      listState.reload()
    } catch (cause) {
      setRevokeError(describeError(cause, 'Could not revoke the token.'))
    } finally {
      setRevoking(false)
    }
  }

  return (
    <section
      className="mt-4 rounded-[6px] border border-line bg-surface"
      aria-labelledby="mcp-tokens-heading"
    >
      <h2 id="mcp-tokens-heading" className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
        MCP access tokens
      </h2>
      <div className="space-y-4 p-4">
        <p className="text-[13px] text-muted">
          An outside MCP client, such as Claude Desktop or Claude Code, calls ScadBuddy&rsquo;s
          tools at <code className="sb-num">/mcp</code> with one of these as its bearer token.
          ScadBuddy keeps only a hash of each token, so a new one is shown once, here, when it is
          created.
        </p>

        {authMode === 'disabled' && (
          <p className="rounded-[6px] border border-warn/50 bg-warn/10 px-3 py-2 text-[12px] text-warn" data-testid="mcp-auth-note">
            MCP authentication is turned off: <code>/mcp</code> accepts calls without a token, as
            an anonymous caller. Tokens made here are kept, and are checked again once
            authentication is back on.
          </p>
        )}
        {authMode === 'oidc' && (
          <p className="text-[12px] text-muted" data-testid="mcp-auth-note">
            MCP sign-in is through your identity provider; these bearer tokens keep working
            alongside it.
          </p>
        )}

        {minted && (
          <div
            className="rounded-[6px] border border-warn/50 bg-warn/10 p-3"
            data-testid="minted-token"
            aria-labelledby="minted-token-heading"
            role="group"
          >
            <p id="minted-token-heading" className="text-[13px] font-medium">
              New token: {minted.record.name}
            </p>
            <p className="mt-1 text-[12px] text-warn">
              Copy it now. This is the only time it is shown: ScadBuddy stores a hash, not the
              token. Anyone who has it can use ScadBuddy at its tier. If it is lost, revoke it and
              create another.
            </p>
            <code
              ref={tokenRef}
              className="sb-num mt-2 block cursor-text select-all break-all rounded-[4px] border border-line bg-surface-2 px-2 py-1.5 text-[12px]"
              data-testid="minted-token-value"
              onClick={(event) => selectContents(event.currentTarget)}
              {...USER_ONLY}
            >
              {minted.token}
            </code>
            <div className="mt-2 flex items-center gap-2">
              <Button size="sm" onClick={() => void copy()} {...USER_ONLY}>
                Copy token
              </Button>
              <Button size="sm" variant="ghost" onClick={done}>
                Done
              </Button>
              {copied === 'copied' && (
                <span role="status" className="text-[12px] text-ok">
                  Copied to the clipboard.
                </span>
              )}
              {copied === 'manual' && (
                <span role="status" className="text-[12px] text-warn">
                  This page may not use the clipboard here. The token is selected: press Ctrl+C
                  (⌘C on a Mac).
                </span>
              )}
            </div>
          </div>
        )}

        <form className="grid gap-3 sm:grid-cols-[1fr_auto_auto_auto] sm:items-end" onSubmit={(event) => void create(event)}>
          <div>
            <label htmlFor="mcp-token-name" className="block text-[13px]">
              Token name
            </label>
            <input
              id="mcp-token-name"
              type="text"
              value={name}
              maxLength={100}
              required
              autoComplete="off"
              onChange={(event) => setName(event.target.value)}
              placeholder="Claude Desktop on my laptop"
              className="sb-field mt-1.5"
            />
          </div>
          <div>
            <label htmlFor="mcp-token-tier" className="block text-[13px]">
              Access
            </label>
            <select
              id="mcp-token-tier"
              value={tier}
              onChange={(event) => setTier(event.target.value as McpTokenTier)}
              className="sb-field mt-1.5 cursor-pointer"
              aria-describedby="mcp-token-tier-help"
            >
              {(Object.keys(TIER_LABEL) as McpTokenTier[]).map((value) => (
                <option key={value} value={value}>
                  {TIER_LABEL[value]}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="mcp-token-expiry" className="block text-[13px]">
              Expires after
            </label>
            <select
              id="mcp-token-expiry"
              value={expiry}
              onChange={(event) => setExpiry(event.target.value)}
              className="sb-field mt-1.5 cursor-pointer"
            >
              {EXPIRY.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <Button
            type="submit"
            variant="primary"
            disabled={creating || name.trim() === ''}
            aria-busy={creating}
            {...USER_ONLY}
          >
            {creating && <Spinner />}
            Create token
          </Button>
        </form>
        <p id="mcp-token-tier-help" className="-mt-2 text-[12px] text-muted">
          {TIER_HELP[tier]}
        </p>

        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}

        {listState.loading && !list ? (
          <p className="flex items-center gap-2 text-[13px] text-muted">
            <Spinner /> Loading tokens
          </p>
        ) : listState.error ? (
          <p role="alert" className="text-[13px] text-warn">
            The tokens could not be loaded: {describeError(listState.error, listState.error.message)}
          </p>
        ) : list && list.tokens.length === 0 ? (
          <p className="text-[13px] text-muted">No tokens yet.</p>
        ) : (
          list && (
            <ul className="divide-y divide-line" aria-label="Tokens">
              {list.tokens.map((token) => (
                <TokenRow
                  key={token.id}
                  token={token}
                  onRevoke={(next) => {
                    setRevokeError(null)
                    setConfirming(next)
                  }}
                />
              ))}
            </ul>
          )
        )}
      </div>

      <Dialog
        open={confirming !== null}
        title={`Revoke ${confirming?.name ?? 'token'}?`}
        onClose={closeConfirm}
        footer={
          <>
            <Button variant="ghost" onClick={closeConfirm} disabled={revoking}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => void revoke()} disabled={revoking} {...USER_ONLY}>
              {revoking ? <Spinner /> : 'Revoke token'}
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-muted">
          Any MCP client using{' '}
          <span className="font-medium text-ink">{confirming?.name}</span> is refused from its next
          request. This cannot be undone; the client needs a new token.
        </p>
        {revokeError && (
          <p role="alert" className="mt-3 text-[13px] text-warn">
            {revokeError}
          </p>
        )}
      </Dialog>
    </section>
  )
}
