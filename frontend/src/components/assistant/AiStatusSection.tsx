import { useState } from 'react'
import { recheckAiAvailability, useAiAvailability, type AiState } from '../../agent/chat/availability'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Spinner } from '../ui/spinner'

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
    <Card role="region" className="mt-4" aria-labelledby="ai-status-heading">
      {/* #2456 — not "Assistant": it sits inside the Assistant section, and two landmarks
          of one name cannot be told apart. */}
      <CardHeader className="border-b">
        <h2 id="ai-status-heading" className="font-heading text-sm font-medium">
          Assistant status
        </h2>
      </CardHeader>
      <CardContent className="space-y-2 text-[13px]">
        <p aria-live="polite" data-testid="ai-status" data-state={state} className="flex flex-wrap items-center gap-1.5">
          <Badge
            variant="outline"
            className={state === 'configured' ? 'border-ok/50 text-ok' : state === 'checking' ? 'text-muted-foreground' : 'border-warn/50 text-warn'}
          >
            {LABEL[state]}
          </Badge>
          {ai.reason && <span className="text-muted-foreground"> — {ai.reason}</span>}
        </p>
        <p className="text-xs text-muted-foreground">
          The assistant runs in ScadBuddy&rsquo;s agent service, which holds the Claude
          credential; your browser never reads it back.
        </p>
        <Button variant="outline" onClick={() => void again()} disabled={checking} aria-busy={checking}>
          {checking && <Spinner />}
          Check again
        </Button>
      </CardContent>
    </Card>
  )
}
