// What the model is told about a parked call that was not run, in the words a classic turn
// uses (approvals/service.ts `refusal`): SessionGate's (the durable session workflows) and an
// AgentSession turn's, which reads the entry's row as it holds (sessions/turnHost.ts). Free of
// Node imports: the workflow bundle loads it.

/** What the model is told when its call was not approved, in the words a classic turn uses (approvals/service.ts `refusal`). */
export function refusal(tool: string, requestId: string, outcome: string, reason?: string | null): string {
  const what = `${tool} (approval ${requestId})`
  if (outcome === 'denied') return `The user denied ${what}; it was not run. Do not retry it unless the user asks you to.`
  if (outcome === 'flow_denied') {
    return `The flow that sent this turn refused ${what}: it is outside what the run may do (${reason || 'no reason given'}), so it was not run. Do not retry it.`
  }
  if (outcome === 'expired') return `Nobody decided on ${what} before it expired, so it was not run. Tell the user it needs their approval.`
  if (outcome === 'cancelled') return `The approval for ${what} was cancelled (${reason || 'no reason given'}), so it was not run.`
  if (outcome === 'withdrawn') return `The approval for ${what} was withdrawn before it ran (${reason || 'no reason given'}), so it was not run.`
  return `${what} was not approved, so it was not run.`
}
