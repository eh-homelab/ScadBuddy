import type { RouteModule } from './module.js'
import { BLANK_TEMPLATE } from '../tools/templates.js'

// GET /api/v1/ai/templates/blank (#1291): the blank starter template the
// agent's create_from_template tool writes (tools/templates.ts), for New model's
// "Start from blank template". Served from here so the UI and the agent start a
// model from the same string, kept in one place. Unguarded, like /status: it is
// a constant that ships in the image.

export type BlankTemplateView = { source: string }

/** The blank starter template, for the UI. */
export const route: RouteModule = {
  register(app) {
    app.get('/api/v1/ai/templates/blank', (c) => {
      // A constant of the image: a browser may keep it for an hour.
      c.header('Cache-Control', 'public, max-age=3600')
      return c.json({ source: BLANK_TEMPLATE } satisfies BlankTemplateView)
    })
  },
}
