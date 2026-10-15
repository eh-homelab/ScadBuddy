import { useRef, useState, type FormEvent, type ReactNode } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { McpAuthMode, McpToken, McpTokenTier, MintedMcpToken } from '../api/mcpTokens'
import { copyText, selectContents } from '../lib/clipboard'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/useAsync'
import { McpConnectSnippet } from './McpConnectSnippet'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ChoiceSelect, LoadingLines, SettingBody, SettingCard, Tip } from './settings/form'
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

// #804: the approval grant is instance-wide, not a small per-token extra. A holder can
// decide every pending outward approval, in every session (ApprovalService.visible() and
// authorize(); docs/ai/agent-sessions.md §3), so the label states that scope itself.
const GRANT_LABEL = "This token can approve or deny any agent's outward action, in any session"
const GRANT_BADGE = 'Approves for any session'

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

/** A date, with its full timestamp on hover and focus. */
function When({ iso, children }: { iso: string | null | undefined; children: ReactNode }) {
  if (!iso) return <span>{children}</span>
  return (
    <Tip content={iso}>
      <span tabIndex={0} className="rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/50">
        {children}
      </span>
    </Tip>
  )
}

function TokenRow({ token, onRevoke }: { token: McpToken; onRevoke: (token: McpToken) => void }) {
  const inactive = token.status !== 'active'
  return (
    <li className="flex items-start justify-between gap-3 py-2.5" data-testid="mcp-token">
      <div className="min-w-0">
        <p className={`truncate text-sm font-medium ${inactive ? 'text-muted-foreground line-through' : ''}`}>
          {token.name}
        </p>
        <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <Badge variant="outline">{TIER_LABEL[token.tier]}</Badge>
          {token.approval_grant && (
            <Tip content={`${GRANT_LABEL}.`}>
              <Badge variant="destructive" tabIndex={0} data-testid="mcp-token-grant">
                {GRANT_BADGE}
              </Badge>
            </Tip>
          )}
          {token.status === 'revoked' && token.revoked_at && (
            <span className="text-warn">Revoked {day(token.revoked_at)}</span>
          )}
          {token.status === 'expired' && token.expires_at && (
            <span className="text-warn">Expired {day(token.expires_at)}</span>
          )}
          <When iso={token.created_at}>Created {day(token.created_at)}</When>
          {token.status === 'active' && (
            <When iso={token.expires_at}>{token.expires_at ? `Expires ${day(token.expires_at)}` : 'Never expires'}</When>
          )}
          <When iso={token.last_used_at}>
            {token.last_used_at ? `Last used ${timeAgo(token.last_used_at)}` : 'Never used'}
          </When>
        </p>
      </div>
      {token.status !== 'revoked' && (
        <Button
          variant="destructive"
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

interface Props {
  /**
   * The mode just saved in "MCP authentication" (McpAuthSection), which is newer than
   * the one this list was loaded with.
   */
  authMode?: McpAuthMode | undefined
  /** SCADBUDDY_PUBLIC_URL as Settings has it, for the connect snippet (#1910); this page's origin when unset. */
  publicUrl?: string | null | undefined
}

export function McpTokensSection({ authMode: savedMode, publicUrl }: Props = {}) {
  const listState = useAsync(() => api.listMcpTokens(), [])
  const [name, setName] = useState('')
  const [tier, setTier] = useState<McpTokenTier>('read')
  const [expiry, setExpiry] = useState('90')
  const [grant, setGrant] = useState(false)
  const [creating, setCreating] = useState(false)
  const [minted, setMinted] = useState<MintedMcpToken | null>(null)
  const [copied, setCopied] = useState<'copied' | 'manual' | null>(null)
  const [confirming, setConfirming] = useState<McpToken | null>(null)
  const [revoking, setRevoking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [revokeError, setRevokeError] = useState<string | null>(null)
  const tokenRef = useRef<HTMLElement>(null)

  const list = listState.data
  const authMode = savedMode ?? list?.auth_mode ?? null

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
        ...(grant && tier === 'outward' ? { approval_grant: true } : {}),
      })
      setMinted(result)
      setCopied(null)
      setName('')
      setGrant(false)
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
    <SettingCard title="MCP access tokens" headingId="mcp-tokens-heading">
      <SettingBody>
        <p className="text-sm text-muted-foreground">
          An outside MCP client, such as Claude Desktop or Claude Code, calls ScadBuddy&rsquo;s
          tools at <code className="sb-num">/mcp</code> with one of these as its bearer token.
          ScadBuddy keeps only a hash of each token, so a new one is shown once, here, when it is
          created.
        </p>

        {authMode === 'disabled' && (
          <p
            className="rounded-lg bg-warn/10 px-3 py-2 text-xs text-warn ring-1 ring-warn/50"
            data-testid="mcp-auth-note"
          >
            MCP authentication is turned off: <code>/mcp</code> accepts calls without a token, as
            an anonymous caller. Tokens made here are kept, and are checked again once
            authentication is back on.
          </p>
        )}
        {authMode === 'oidc' && (
          <p className="text-xs text-muted-foreground" data-testid="mcp-auth-note">
            MCP sign-in is through your identity provider; these bearer tokens keep working
            alongside it.
          </p>
        )}

        {minted && (
          <Card
            size="sm"
            className="bg-warn/10 ring-warn/50"
            data-testid="minted-token"
            aria-labelledby="minted-token-heading"
            role="group"
          >
            <CardHeader>
              <p id="minted-token-heading" className="text-sm font-medium">
                New token: {minted.record.name}
              </p>
              <p className="text-xs text-warn">
                Copy it now. This is the only time it is shown: ScadBuddy stores a hash, not the
                token. Anyone who has it can use ScadBuddy at its tier. If it is lost, revoke it and
                create another.
              </p>
            </CardHeader>
            <CardContent className="space-y-2">
              <code
                ref={tokenRef}
                className="sb-num block cursor-text rounded-md bg-background px-2.5 py-2 text-xs break-all select-all ring-1 ring-foreground/10"
                data-testid="minted-token-value"
                onClick={(event) => selectContents(event.currentTarget)}
                {...USER_ONLY}
              >
                {minted.token}
              </code>
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" onClick={() => void copy()} {...USER_ONLY}>
                  Copy token
                </Button>
                <Button size="sm" variant="ghost" onClick={done}>
                  Done
                </Button>
                {copied === 'copied' && (
                  <span role="status" className="text-xs text-ok">
                    Copied to the clipboard.
                  </span>
                )}
                {copied === 'manual' && (
                  <span role="status" className="text-xs text-warn">
                    This page may not use the clipboard here. The token is selected: press Ctrl+C
                    (⌘C on a Mac).
                  </span>
                )}
              </div>
            </CardContent>
          </Card>
        )}

        <McpConnectSnippet publicUrl={publicUrl} authMode={authMode} token={minted?.token} />

        <form className="grid gap-3 sm:grid-cols-[1fr_auto_auto_auto] sm:items-end" onSubmit={(event) => void create(event)}>
          <div className="grid gap-1.5">
            <Label htmlFor="mcp-token-name" className="font-normal">
              Token name
            </Label>
            <Input
              id="mcp-token-name"
              type="text"
              value={name}
              maxLength={100}
              required
              autoComplete="off"
              onChange={(event) => setName(event.target.value)}
              placeholder="Claude Desktop on my laptop"
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="mcp-token-tier" className="font-normal">
              Access
            </Label>
            <ChoiceSelect
              id="mcp-token-tier"
              value={tier}
              onValueChange={(raw) => {
                const next = raw as McpTokenTier
                setTier(next)
                if (next !== 'outward') setGrant(false)
              }}
              choices={(Object.keys(TIER_LABEL) as McpTokenTier[]).map((value) => ({ value, label: TIER_LABEL[value] }))}
              className="sm:w-32"
              aria-describedby="mcp-token-tier-help"
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="mcp-token-expiry" className="font-normal">
              Expires after
            </Label>
            <ChoiceSelect
              id="mcp-token-expiry"
              value={expiry}
              onValueChange={setExpiry}
              choices={EXPIRY.map((option) => ({ value: option.value, label: option.label }))}
              className="sm:w-32"
            />
          </div>
          <Button type="submit" disabled={creating || name.trim() === ''} aria-busy={creating} {...USER_ONLY}>
            {creating && <Spinner />}
            Create token
          </Button>
        </form>
        <p id="mcp-token-tier-help" className="-mt-2 text-xs text-muted-foreground">
          {TIER_HELP[tier]}
        </p>
        {tier === 'outward' && (
          <div className="flex items-start gap-2.5">
            <Checkbox
              id="mcp-token-grant"
              checked={grant}
              onCheckedChange={(next) => setGrant(next === true)}
              className="mt-0.5"
              aria-describedby="mcp-token-grant-help"
              {...USER_ONLY}
            />
            <div>
              <Label htmlFor="mcp-token-grant" className="leading-snug font-normal">
                {GRANT_LABEL}
              </Label>
              <p id="mcp-token-grant-help" className="mt-1 text-xs text-muted-foreground">
                System-wide: whoever holds this token sees every pending send, print, delete or
                settings change any agent asks for, in every session on this ScadBuddy, not only
                sessions this token started, and can decide it in place of a person, though never
                its own: not the actions it asked for, nor those in sessions it started or owns.
                Leave this off unless that client is meant to review other agents. It cannot be
                changed later; revoke the token to withdraw it.
              </p>
            </div>
          </div>
        )}

        {error && (
          <p role="alert" className="text-sm text-warn">
            {error}
          </p>
        )}

        {listState.loading && !list ? (
          <LoadingLines label="Loading tokens" lines={['w-1/2', 'w-1/3', 'w-1/2', 'w-1/3']} />
        ) : listState.error ? (
          <p role="alert" className="text-sm text-warn">
            The tokens could not be loaded: {describeError(listState.error, listState.error.message)}
          </p>
        ) : list && list.tokens.length === 0 ? (
          <p className="text-sm text-muted-foreground">No tokens yet.</p>
        ) : (
          list && (
            <ul className="divide-y" aria-label="Tokens">
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
      </SettingBody>

      <AlertDialog open={confirming !== null} onOpenChange={(open) => !open && closeConfirm()}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{`Revoke ${confirming?.name ?? 'token'}?`}</AlertDialogTitle>
            <AlertDialogDescription>
              Any MCP client using <span className="font-medium text-foreground">{confirming?.name}</span> is
              refused from its next request. This cannot be undone; the client needs a new token.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {revokeError && (
            <p role="alert" className="text-sm text-warn">
              {revokeError}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={revoking}>Cancel</AlertDialogCancel>
            {/* Not an AlertDialogAction: the dialog stays open until the revoke answers. */}
            <Button variant="destructive" onClick={() => void revoke()} disabled={revoking} {...USER_ONLY}>
              {revoking ? <Spinner /> : 'Revoke token'}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </SettingCard>
  )
}
