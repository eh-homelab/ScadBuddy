/**
 * Whether the assistant may render (#256: "hidden when AI is off").
 *
 * The real answer needs two things that don't exist yet: a stored Claude credential
 * (`has_credential` from Settings, #255) and a healthy agent service (#261). Until
 * both land this is a STUB: AI is on only in the mocked build, where the scripted
 * mock agent (`src/mocks/agent.ts`) stands in for the service; everywhere else it is
 * off and the panel renders nothing.
 *
 * TODO(#255, #261): read `has_credential` and the agent service's health here, and
 * keep the signature — the panel only reads `available`.
 */
export interface AiAvailability {
  available: boolean
  /** Why it is off, for Settings to link to setup. */
  reason?: string
}

export function useAiAvailability(): AiAvailability {
  // MOCK: see the module comment.
  if (import.meta.env.VITE_MOCK_API === '1') return { available: true }
  return { available: false, reason: 'The agent service is not set up yet.' }
}
