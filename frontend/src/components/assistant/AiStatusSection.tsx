import { useState } from 'react'
import { recheckAiAvailability, useAiAvailability, type AiState } from '../../agent/chat/availability'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'

const LABEL: Record<AiState, string> = {
  checking: 'Checking…',
  configured: 'Ready',
  not_configured: 'Not set up',
  unavailable: 'Unavailable',
  unreachable: 'Unreachable',
}

/**
 * Settings' view of the assistant (#256): whether the agent service is reachable and
 * set up, with the agent's own reason when it isn't (`useAiAvailability`). It shows
 * status only; the credential is set in `AiCredentialSection` (#1000), and the agent
 * never returns it to the browser.
 */
export function AiStatusSection() {
  const ai = useAiAvailability()
  const [checking, setChecking] = useState(false)
  const state = ai.state ?? (ai.available ? 'configured' : 'unreachable')
  const again = async () => {
    setChecking(true)
    try {
      await recheckAiAvailability()
    } finally {
      setChecking(false)
    }
  }
  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface" aria-labelledby="ai-status-heading">
      <h2 id="ai-status-heading" className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
        Assistant
      </h2>
      <div className="space-y-2 p-4 text-[13px]">
        <p aria-live="polite" data-testid="ai-status" data-state={state}>
          <span className={state === 'configured' ? 'text-ok' : state === 'checking' ? 'text-muted' : 'text-warn'}>
            {LABEL[state]}
          </span>
          {ai.reason && <span className="text-muted"> — {ai.reason}</span>}
        </p>
        <p className="text-[12px] text-muted">
          The assistant runs in ScadBuddy&rsquo;s agent service, which holds the Claude
          credential; your browser never reads it back.
        </p>
        <Button onClick={() => void again()} disabled={checking} aria-busy={checking}>
          {checking && <Spinner />}
          Check again
        </Button>
      </div>
    </section>
  )
}
