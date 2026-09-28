import { useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { McpAuthSetting, McpAuthUpdate, McpTokenTier } from '../api/mcpTokens'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

// #251 — Settings → "MCP authentication": whether an outside MCP client needs a token
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

  const loadedMode: Mode | undefined =
    current && current.mode !== 'oidc' ? current.mode : undefined
  const chosenMode = mode ?? loadedMode
  const chosenCap = cap ?? current?.anonymous_cap ?? 'outward'
  const changed =
    current !== undefined &&
    chosenMode !== undefined &&
    (chosenMode !== current.mode || chosenCap !== current.anonymous_cap)
  // Calls without a token are allowed already; this save only raises what they may do.
  const raisingCap =
    current?.mode === 'disabled' && CAP_RANK[chosenCap] > CAP_RANK[current.anonymous_cap]

  async function save() {
    if (!chosenMode) return
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const next = await api.setMcpAuth({ mode: chosenMode, anonymous_cap: chosenCap })
      state.setData(next)
      setMode(undefined)
      setCap(undefined)
      setConfirming(false)
      setSaved(true)
      onSaved?.(next)
    } catch (cause) {
      setError(describeError(cause, 'Could not save the MCP authentication setting.'))
    } finally {
      setSaving(false)
    }
  }

  function submit() {
    // Anything that lets an unauthenticated caller do more asks first: turning
    // authentication off, or raising the anonymous cap while it stays off.
    if (chosenMode === 'disabled' && (current?.mode !== 'disabled' || raisingCap)) {
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
    <section
      className="mt-4 rounded-[6px] border border-line bg-surface"
      aria-labelledby="mcp-auth-heading"
    >
      <h2 id="mcp-auth-heading" className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
        MCP authentication
      </h2>
      <div className="space-y-4 p-4">
        <p className="text-[13px] text-muted">
          Whether an outside MCP client needs one of the access tokens below to call{' '}
          <code className="sb-num">/mcp</code>. Calls are HTTPS only either way, and an action
          that sends, prints, deletes or changes settings always waits for a person to approve it
          here.
        </p>

        {state.loading && !current ? (
          <p className="flex items-center gap-2 text-[13px] text-muted">
            <Spinner /> Loading
          </p>
        ) : state.error ? (
          <p role="alert" className="text-[13px] text-warn">
            The MCP authentication setting could not be loaded:{' '}
            {describeError(state.error, state.error.message)}
          </p>
        ) : (
          current && (
            <>
              {current.mode === 'disabled' && (
                <p
                  className="rounded-[6px] border border-warn/50 bg-warn/10 px-3 py-2 text-[12px] text-warn"
                  data-testid="mcp-auth-disabled-warning"
                >
                  Authentication is off. Anyone who can reach <code>/mcp</code> over HTTPS can call
                  ScadBuddy&rsquo;s tools without a token, with{' '}
                  {CAP_LABEL[current.anonymous_cap].toLowerCase()}.
                </p>
              )}
              {current.mode === 'oidc' && (
                <p className="text-[12px] text-muted" data-testid="mcp-auth-oidc-note">
                  Sign-in is through your identity provider (OIDC), and access tokens keep working
                  alongside it. Choosing a mode here and saving switches OIDC off.
                </p>
              )}

              <fieldset>
                <legend className="text-[13px]">When an MCP client calls</legend>
                <div className="mt-1.5 space-y-1.5">
                  <label className="flex items-start gap-2 text-[13px]">
                    <input
                      type="radio"
                      name="mcp-auth-mode"
                      value="bearer"
                      checked={chosenMode === 'bearer'}
                      onChange={() => setMode('bearer')}
                      className="mt-0.5"
                    />
                    <span>
                      Require an access token
                      <span className="block text-[12px] text-muted">
                        The default. A call without a valid token is refused.
                      </span>
                    </span>
                  </label>
                  <label className="flex items-start gap-2 text-[13px]">
                    <input
                      type="radio"
                      name="mcp-auth-mode"
                      value="disabled"
                      checked={chosenMode === 'disabled'}
                      onChange={() => setMode('disabled')}
                      className="mt-0.5"
                    />
                    <span>
                      Allow calls without a token
                      <span className="block text-[12px] text-muted">
                        Only for a network you trust: every caller is anonymous and gets the access
                        chosen below.
                      </span>
                    </span>
                  </label>
                </div>
              </fieldset>

              <div>
                <label htmlFor="mcp-anonymous-cap" className="block text-[13px]">
                  Access without a token
                </label>
                <select
                  id="mcp-anonymous-cap"
                  value={chosenCap}
                  onChange={(event) => setCap(event.target.value as McpTokenTier)}
                  className="sb-field mt-1.5 cursor-pointer sm:max-w-[16rem]"
                  aria-describedby="mcp-anonymous-cap-help"
                >
                  {(Object.keys(CAP_LABEL) as McpTokenTier[]).map((value) => (
                    <option key={value} value={value}>
                      {CAP_LABEL[value]}
                    </option>
                  ))}
                </select>
                <p id="mcp-anonymous-cap-help" className="mt-1.5 text-[12px] text-muted">
                  {CAP_HELP[chosenCap]} Applies only while calls without a token are allowed.
                </p>
              </div>

              <div className="flex items-center gap-3">
                <Button
                  variant="primary"
                  onClick={submit}
                  disabled={!changed || saving}
                  aria-busy={saving && !confirming}
                  {...USER_ONLY}
                >
                  {saving && !confirming && <Spinner />}
                  Save
                </Button>
                {saved && !changed && (
                  <span role="status" className="text-[12px] text-ok">
                    Saved. It applies from the next MCP call.
                  </span>
                )}
              </div>
              {error && !confirming && (
                <p role="alert" className="text-[13px] text-warn">
                  {error}
                </p>
              )}
            </>
          )
        )}
      </div>

      <Dialog
        open={confirming}
        title={
          raisingCap ? 'Give MCP calls without a token more access?' : 'Allow MCP calls without a token?'
        }
        onClose={closeConfirm}
        footer={
          <>
            <Button variant="ghost" onClick={closeConfirm} disabled={saving}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => void save()} disabled={saving} {...USER_ONLY}>
              {saving ? <Spinner /> : raisingCap ? 'Raise access' : 'Turn authentication off'}
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-muted">
          Anyone who can reach ScadBuddy&rsquo;s <code>/mcp</code> over HTTPS will be able to call
          its tools without a token, as an anonymous caller with{' '}
          <span className="font-medium text-ink">{CAP_LABEL[chosenCap].toLowerCase()}</span>.
        </p>
        <p className="mt-2 text-[13px] text-muted">
          Sending, printing, deleting and changing settings still wait for a person to approve
          them here. Your access tokens are kept, and are checked again once you turn
          authentication back on.
        </p>
        {error && (
          <p role="alert" className="mt-3 text-[13px] text-warn">
            {error}
          </p>
        )}
      </Dialog>
    </section>
  )
}
