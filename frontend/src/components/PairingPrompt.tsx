import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import type { TabLinkState } from '../agent/link'
import type { PairingEntry } from '../agent/linkProtocol'
import { Button } from './ui/Button'

/**
 * The pairing prompt of AI spec §8.5 (#254): an agent outside ScadBuddy (an MCP client)
 * asks to drive this tab, and only the user can let it, here in the tab, by typing the
 * code the agent was given (agent `src/bridge/pairings.ts`). Every connected tab shows the
 * request; the one the user types the code into is the one the agent then drives. A
 * paired agent is listed until the user disconnects it.
 *
 * The whole prompt is user-only (`USER_ONLY`): an agent already driving the tab cannot
 * accept another one, nor keep itself paired, through `click` and `fill`.
 */

interface Props {
  state: TabLinkState
  onAccept: (id: string, code: string) => void
  onDeny: (id: string) => void
  onEnd: (id: string) => void
}

function until(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? '' : at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function Request({
  entry,
  result,
  onAccept,
  onDeny,
}: {
  entry: PairingEntry
  result?: { ok: boolean; message: string }
  onAccept: Props['onAccept']
  onDeny: Props['onDeny']
}) {
  const [code, setCode] = useState('')
  const inputId = `pairing-code-${entry.id}`
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (code.trim()) onAccept(entry.id, code.trim())
  }
  return (
    <form
      onSubmit={submit}
      aria-label={`Pairing request from ${entry.label}`}
      className="rounded-[8px] border border-accent/40 bg-surface p-3 text-[12.5px] shadow-2xl"
    >
      <p className="text-ink">
        <strong className="font-semibold">{entry.label}</strong> asks to use this tab. It could see what is on
        screen and change it the way you can, but never press Print, Send, Delete or Save for you.
      </p>
      <p className="mt-1 text-muted">
        Allow it only if you asked it to: type the code it gave you. The request ends at {until(entry.expiresAt)}.
      </p>
      <div className="mt-2 flex items-end gap-2">
        <label htmlFor={inputId} className="flex flex-1 flex-col gap-1 text-muted">
          Pairing code
          <input
            id={inputId}
            value={code}
            onChange={(event) => setCode(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            placeholder="ABCD-EFGH"
            className="sb-field font-mono uppercase text-ink"
          />
        </label>
        <Button type="submit" variant="primary" size="sm" disabled={!code.trim()}>
          Allow
        </Button>
        <Button variant="ghost" size="sm" onClick={() => onDeny(entry.id)}>
          Deny
        </Button>
      </div>
      {result && !result.ok && (
        <p role="alert" className="mt-2 text-warn">
          {result.message}
        </p>
      )}
    </form>
  )
}

export function PairingPrompt({ state, onAccept, onDeny, onEnd }: Props) {
  if (state.pending.length === 0 && state.paired.length === 0) return null
  return (
    <section
      {...USER_ONLY}
      aria-label="Agent pairing"
      className="pointer-events-none fixed bottom-3 left-3 z-40 flex w-[min(420px,calc(100%-24px))] flex-col gap-2"
    >
      {state.pending.map((entry) => (
        <div key={entry.id} className="pointer-events-auto">
          <Request entry={entry} result={state.results[entry.id]} onAccept={onAccept} onDeny={onDeny} />
        </div>
      ))}
      {state.paired.map((entry) => (
        <div
          key={entry.id}
          role="status"
          className="pointer-events-auto flex items-center gap-2 rounded-[8px] border border-line bg-surface px-3 py-2 text-[12.5px] shadow-xl"
        >
          <span className="flex-1 text-ink">
            <strong className="font-semibold">{entry.label}</strong> can use this tab until {until(entry.expiresAt)}.
          </span>
          <Button variant="ghost" size="sm" onClick={() => onEnd(entry.id)}>
            Disconnect
          </Button>
        </div>
      ))}
    </section>
  )
}
