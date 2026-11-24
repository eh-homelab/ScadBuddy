import { z } from 'zod'
import type { SessionBrowser } from './rows.js'

// A session's browser options (#7027) as the tool, the session route and the chat socket take
// them: the shape only. The bounds are the API's alone (backend api/crud_agent_sessions.py
// `SessionBrowser`); a value it refuses comes back as `SessionError('invalid_browser')`, 422.

export const SessionBrowserInput = z
  .strictObject({
    user_agent: z.string().optional().describe('The User-Agent header and navigator.userAgent the browser reports.'),
    viewport: z
      .strictObject({ width: z.number().int(), height: z.number().int() })
      .optional()
      .describe("The page's viewport in CSS pixels."),
    locale: z.string().optional().describe('The BCP 47 locale the browser reports (navigator.language, Accept-Language), such as en-GB.'),
  })
  .describe(
    "What the session's browser (its Playwright MCP) reports: user agent, viewport and locale; a key left out is the browser's default. " +
      'The session keeps them for its life, and its forks keep them too.',
  ) satisfies z.ZodType<SessionBrowser>
