import { HttpResponse, http } from 'msw'

/**
 * The agent sidecar's status (agent `src/app.ts` `AiStatusView`), which the UI's
 * assistant gate reads (`src/agent/chat/availability.ts`). The mocked build's agent
 * is the scripted one in `../agent.ts`, and it is always there.
 */
export const handlers = [
  http.get('/api/v1/ai/status', () =>
    HttpResponse.json(
      { available: true, state: 'enabled', ai: 'enabled' },
      { headers: { 'X-ScadBuddy-Service': 'agent' } },
    ),
  ),
]
