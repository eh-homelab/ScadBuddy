import { z } from 'zod'
import { principalHeader } from './authorship.js'
import { ok } from './call.js'
import { defineTool, json, type Tool } from './registry.js'

// #4975 phase 6 (spec 2026-11-01 §7.4): a browser the agent drives (a Playwright MCP
// plugin, Settings → Tools & plugins; the built-in headless browser of #349 is gone) needs
// a session to load ScadBuddy once sign-in is enforced. This tool asks the backend, with
// the agent's service credential, for a one-time sign-in link for the principal this call
// runs as: opened within a minute, once, it gives that browser a `delegated` session of at
// most an hour, which can never answer, approve or grant what only a person does.

export const delegationTools: readonly Tool[] = [
  defineTool({
    name: 'sign_in_link',
    label: "Sign the assistant's browser in",
    description:
      "A one-time link that signs a browser the assistant drives (a Playwright browser plugin) into ScadBuddy as " +
      'the user this session acts for. Open it in that browser within a minute; it works once, and the session it ' +
      'starts lasts `ttl_seconds` (at most an hour). That session cannot answer, approve or grant anything only a ' +
      "person may. Never give the link to anyone else, and never open it in the user's own tab.",
    input: z.object({
      ttl_seconds: z.number().int().min(60).max(3600).default(3600).describe('How long the browser stays signed in.'),
    }),
    risk: 'write',
    routes: ['POST /api/v1/auth/delegated-sessions'],
    handler: async (args, { backend, principal }) =>
      json(
        await ok(
          backend.POST('/api/v1/auth/delegated-sessions', {
            body: { principal: principalHeader(principal.id), ttl_seconds: args.ttl_seconds },
          }),
          'mint a browser sign-in link',
        ),
      ),
  }),
]
