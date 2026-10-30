import { useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { McpAuthSetting, McpAuthUpdate, McpTokenTier } from '../api/mcpTokens'
import { useAsync } from '../lib/useAsync'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { ChoiceSelect, LoadingLines, SettingBody, SettingCard } from './settings/form'
import { Spinner } from './ui/spinner'

// #251 — Settings → MCP access → Auth mode: whether an outside MCP client needs a token
// at /mcp, and how far an anonymous caller may go when it does not (AI design spec
// §8.3). The agent service keeps both in `ai_settings` and reads them on every /mcp
// request, so a save applies to the next call.
//
// Turning authentication off is the operator trusting the network: every caller that
// reaches /mcp over HTTPS gets the anonymous cap (full access by default). It takes an
// explicit confirmation here, as does raising the cap while it stays off. The
// confirmation is UI-only: the PUT route does not require it (#258). Outward actions still wait for a person to approve them
// in ScadBuddy whatever the mode (§8.2). Save and the confirmation are USER_ONLY:
// changing the auth mode is an outward settings write the browser agent may not make.

type Mode = McpAuthUpdate['mode']

const CAP_LABEL: Record<McpTokenTier, string> = {
  read: 'Read only',
  write: 'Read and write',
  outward: 'Full access',
}

const CAP_HELP: Record<McpTokenTier, string> = {
  read: 'An anonymous caller can look at models, renders and settings, and change nothing.',
  write: 'An anonymous caller can also make changes that the model history can undo.',
  outward:
    'An anonymous caller can also ask to send, print, delete or change settings; each of those still waits for a person to approve it here.',
}

const CAP_RANK: Record<McpTokenTier, number> = { read: 0, write: 1, outward: 2 }

function describeError(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.detail : fallback
}

interface Props {
  /** Told the saved setting, so the token list can say which mode applies. */
  onSaved?: (setting: McpAuthSetting) => void
}

export function McpAuthSection({ onSaved }: Props) {
  const state = useAsync(() => api.getMcpAuth(), [])
  const current = state.data
  // Edits over what was loaded; undefined means "as loaded".
  const [mode, setMode] = useState<Mode | undefined>(undefined)
  const [cap, setCap] = useState<McpTokenTier | undefined>(undefined)
  const [saving, setSaving] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  // The stored mode, which is what the radios edit. While OIDC is on it is
  // overridden (`current.mode` is `oidc`) and applies once OIDC is turned off.
  const storedMode = current?.configured_mode
  const oidcActive = current?.mode === 'oidc'
  const chosenMode = mode ?? storedMode
  const chosenCap = cap ?? current?.anonymous_cap ?? 'outward'
  const changed =
    current !== undefined &&
    chosenMode !== undefined &&
    (chosenMode !== current.configured_mode || chosenCap !== current.anonymous_cap)
  // Calls without a token are allowed already; this save only raises what they may do.
  const raisingCap =
    storedMode === 'disabled' &&
    current !== undefined &&
    CAP_RANK[chosenCap] > CAP_RANK[current.anonymous_cap]

  async function save() {
    if (!chosenMode || !current) return
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      // `expected` makes the save a compare-and-set: a tab showing an old setting
      // gets a 409 instead of skipping the confirmation the current one needs.
      const next = await api.setMcpAuth({
        mode: chosenMode,
        anonymous_cap: chosenCap,
        expected: { mode: current.configured_mode, anonymous_cap: current.anonymous_cap },
      })
      state.setData(next)
      setMode(undefined)
      setCap(undefined)
      setConfirming(false)
      setSaved(true)
      onSaved?.(next)
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        // Changed elsewhere: show what is stored now, drop the edits made over the
        // old values, and let the operator choose again (and confirm again if needed).
        setConfirming(false)
        setMode(undefined)
        setCap(undefined)
        setError(
          'The MCP authentication setting was changed elsewhere since this page loaded it, so nothing was saved. It now shows the current setting; choose again.',
        )
        try {
          const fresh = await api.getMcpAuth()
          state.setData(fresh)
          onSaved?.(fresh)
        } catch {
          state.reload()
        }
      } else {
        setError(describeError(cause, 'Could not save the MCP authentication setting.'))
      }
    } finally {
      setSaving(false)
    }
  }

  function submit() {
    // Anything that lets an unauthenticated caller do more asks first: turning
    // authentication off, or raising the anonymous cap while it stays off. While
    // OIDC is on it asks too: the stored choice applies once OIDC is turned off.
    if (chosenMode === 'disabled' && (storedMode !== 'disabled' || raisingCap)) {
      setError(null)
      setConfirming(true)
      return
    }
    void save()
  }

  function closeConfirm() {
    if (saving) return
    setConfirming(false)
    setError(null)
  }

  return (
    <SettingCard title="MCP authentication" headingId="mcp-auth-heading">
      <SettingBody>
        <p className="text-sm text-muted-foreground">
          Whether an outside MCP client needs one of the access tokens below to call{' '}
          <code className="sb-num">/mcp</code>. Calls are HTTPS only either way, and an action
          that sends, prints, deletes or changes settings always waits for a person to approve it
          here.
        </p>

        {state.loading && !current ? (
          <LoadingLines label="Loading" lines={['w-2/3', 'w-1/2', 'h-8 w-64']} />
        ) : state.error ? (
          <p role="alert" className="text-sm text-warn">
            The MCP authentication setting could not be loaded:{' '}
            {describeError(state.error, state.error.message)}
          </p>
        ) : (
          current && (
            <>
              {current.mode === 'disabled' && (
                <p
                  className="rounded-lg bg-warn/10 px-3 py-2 text-xs text-warn ring-1 ring-warn/50"
                  data-testid="mcp-auth-disabled-warning"
                >
                  Authentication is off. Anyone who can reach <code>/mcp</code> over HTTPS can call
                  ScadBuddy&rsquo;s tools without a token, with{' '}
                  {CAP_LABEL[current.anonymous_cap].toLowerCase()}.
                </p>
              )}
              {oidcActive && (
                <p
                  className="rounded-lg bg-muted/50 px-3 py-2 text-xs text-muted-foreground ring-1 ring-foreground/10"
                  data-testid="mcp-auth-oidc-note"
                >
                  OIDC sign-in is on, so it overrides the choice below: every call needs an OIDC
                  sign-in or an access token. The choice below is stored, and applies once OIDC is
                  turned off in MCP access &rarr; OIDC sign-in.
                  {current.configured_mode === 'disabled' && (
                    <span className="mt-1 block text-warn" data-testid="mcp-auth-oidc-disabled">
                      The stored choice allows calls without a token: turning OIDC off will let
                      anyone who can reach <code>/mcp</code> call it with{' '}
                      {CAP_LABEL[current.anonymous_cap].toLowerCase()}.
                    </span>
                  )}
                </p>
              )}

              <fieldset>
                <legend className="text-sm">When an MCP client calls</legend>
                <RadioGroup
                  name="mcp-auth-mode"
                  value={chosenMode ?? ''}
                  onValueChange={(next) => setMode(next as Mode)}
                  className="mt-2 gap-3"
                >
                  <div className="flex items-start gap-2.5">
                    <RadioGroupItem id="mcp-auth-mode-bearer" value="bearer" className="mt-0.5" />
                    <Label htmlFor="mcp-auth-mode-bearer" className="block leading-snug font-normal">
                      Require an access token
                      <span className="mt-0.5 block text-xs text-muted-foreground">
                        The default. A call without a valid token is refused.
                      </span>
                    </Label>
                  </div>
                  <div className="flex items-start gap-2.5">
                    <RadioGroupItem id="mcp-auth-mode-disabled" value="disabled" className="mt-0.5" />
                    <Label htmlFor="mcp-auth-mode-disabled" className="block leading-snug font-normal">
                      Allow calls without a token
                      <span className="mt-0.5 block text-xs text-muted-foreground">
                        Only for a network you trust: every caller is anonymous and gets the access
                        chosen below.
                      </span>
                    </Label>
                  </div>
                </RadioGroup>
              </fieldset>

              <div className="grid gap-1.5">
                <Label htmlFor="mcp-anonymous-cap" className="font-normal">
                  Access without a token
                </Label>
                <ChoiceSelect
                  id="mcp-anonymous-cap"
                  value={chosenCap}
                  onValueChange={(next) => setCap(next as McpTokenTier)}
                  choices={(Object.keys(CAP_LABEL) as McpTokenTier[]).map((value) => ({ value, label: CAP_LABEL[value] }))}
                  className="sm:max-w-[16rem]"
                  aria-describedby="mcp-anonymous-cap-help"
                />
                <p id="mcp-anonymous-cap-help" className="text-xs text-muted-foreground">
                  {CAP_HELP[chosenCap]} Applies only while calls without a token are allowed.
                </p>
              </div>

              <div className="flex items-center gap-3">
                <Button
                  onClick={submit}
                  disabled={!changed || saving}
                  aria-busy={saving && !confirming}
                  {...USER_ONLY}
                >
                  {saving && !confirming && <Spinner />}
                  Save
                </Button>
                {saved && !changed && (
                  <span role="status" className="text-xs text-ok">
                    {oidcActive
                      ? 'Saved. OIDC sign-in still applies until it is turned off.'
                      : 'Saved. It applies from the next MCP call.'}
                  </span>
                )}
              </div>
              {error && !confirming && (
                <p role="alert" className="text-sm text-warn">
                  {error}
                </p>
              )}
            </>
          )
        )}
      </SettingBody>

      <AlertDialog open={confirming} onOpenChange={(open) => !open && closeConfirm()}>
        <AlertDialogContent className="sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle>
              {raisingCap ? 'Give MCP calls without a token more access?' : 'Allow MCP calls without a token?'}
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                {oidcActive && (
                  <p data-testid="mcp-auth-confirm-oidc">
                    OIDC sign-in is on and still applies after this save: calls keep needing an OIDC
                    sign-in or an access token until OIDC is turned off. From then on:
                  </p>
                )}
                <p>
                  Anyone who can reach ScadBuddy&rsquo;s <code>/mcp</code> over HTTPS will be able to call
                  its tools without a token, as an anonymous caller with{' '}
                  <span className="font-medium text-foreground">{CAP_LABEL[chosenCap].toLowerCase()}</span>.
                </p>
                <p>
                  Sending, printing, deleting and changing settings still wait for a person to approve
                  them here. Your access tokens are kept, and are checked again once you turn
                  authentication back on.
                </p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          {error && (
            <p role="alert" className="text-sm text-warn">
              {error}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={saving}>Cancel</AlertDialogCancel>
            {/* Not an AlertDialogAction: the dialog stays open until the save answers. */}
            <Button variant="destructive" onClick={() => void save()} disabled={saving} {...USER_ONLY}>
              {saving ? (
                <Spinner />
              ) : raisingCap ? (
                'Raise access'
              ) : oidcActive ? (
                'Allow once OIDC is off'
              ) : (
                'Turn authentication off'
              )}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </SettingCard>
  )
}
