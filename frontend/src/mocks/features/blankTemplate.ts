import { HttpResponse, http } from 'msw'

/**
 * #1291 — the blank starter template New model's "Start from blank template" fills the
 * editor with, served by the agent service (agent `src/routes/templates.ts`), which
 * keeps the one copy of it (`BLANK_TEMPLATE`).
 */
export const handlers = [
  http.get('/api/v1/ai/templates/blank', () =>
    HttpResponse.json({ source: '// A blank starter\n/* [Hidden] */\n$fn = 64;\n\ncube(10);\n' }),
  ),
]
