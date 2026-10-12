import { HttpResponse, http } from 'msw'

/**
 * #1291 — the blank starter template New model's "Start from blank template" fills the
 * editor with, served by the agent service (agent `src/routes/templates.ts`). The source
 * here is a stub of the agent's `BLANK_TEMPLATE`, not a copy of it: nothing keeps the two
 * in sync (#2218).
 */
export const handlers = [
  http.get('/api/v1/ai/templates/blank', () =>
    HttpResponse.json({ source: '// A blank starter\n/* [Hidden] */\n$fn = 64;\n\ncube(10);\n' }),
  ),
]
