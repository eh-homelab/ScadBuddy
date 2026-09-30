import type { McpSdkServerConfigWithInstance } from '@anthropic-ai/claude-agent-sdk'
import { harnessPrincipal, hasTier, type Principal, TIERS } from '../auth/principal.js'
import type { TierResolver } from '../harness/permissions.js'
import type { TurnPrincipal } from '../sessions/manager.js'
import type { Owner } from '../sessions/protocol.js'
import { ALL_TOOLS } from './index.js'
import { createHarnessServer, SERVER_NAME } from './projections.js'
import type { Tool, ToolServices } from './registry.js'

// The registry's harness side, as the session manager takes it
// (sessions/manager.ts SessionManagerDeps `tierOf` and `mcpServers`), so that
// main.ts wires it in one line. Spec §5.1 and D3: the harness gets the same
// tools as /mcp, in-process, as `mcp__scadbuddy__<name>`
// (https://code.claude.com/docs/en/agent-sdk/custom-tools).
//
//   - `tierOf` maps those names to each tool's `risk` (as index.ts `tierOf`
//     does for ALL_TOOLS), for the permission seam
//     (harness/permissions.ts): read and write tools run within the
//     principal's tiers, outward ones park for a human approval (#258). Any
//     other name (a plugin's tool) stays unknown, and so `outward` (spec §8.1).
//   - `mcpServers` builds one in-process server per turn, bound to the
//     session owner's principal (auth/principal.ts `harnessPrincipal`), with
//     only the tools that principal's tiers allow: a tool it could never run
//     is not offered, so no human is asked to approve a call that `runTool`
//     would then refuse for want of the tier. A turn sent over /mcp
//     (tools/sessions.ts, #300) carries the sender's tiers, which replace the
//     `read` default of a non-browser owner.

export type HarnessTools = {
  tierOf: TierResolver
  mcpServers: (session: { id?: string; owner: Owner }, turn?: TurnPrincipal) => Record<string, McpSdkServerConfigWithInstance>
}

export function harnessTools(services: ToolServices, tools: readonly Tool[] = ALL_TOOLS): HarnessTools {
  const risk = new Map(tools.map((t) => [`mcp__${SERVER_NAME}__${t.name}`, t.risk]))
  return {
    tierOf: (name) => risk.get(name),
    mcpServers: (session, turn) => {
      const principal = turnPrincipal(session.owner, turn)
      const allowed = tools.filter((t) => hasTier(principal, t.risk))
      // The browser_* tools reach the tab this session is paired with (#254, bridge/hub.ts).
      const bound =
        services.browser && session.id !== undefined
          ? { ...services, browser: services.browser.forSession(session.id) }
          : services
      // The session id too, so its commits name it (authorship.ts, #252).
      return { [SERVER_NAME]: createHarnessServer(allowed, bound, principal, session.id) }
    },
  }
}

/** The owner's harness principal, with a non-browser sender's own tiers when the turn carries them. */
export function turnPrincipal(owner: Owner, turn?: TurnPrincipal): Principal {
  const base = harnessPrincipal(owner)
  if (owner.kind === 'browser' || !turn?.tiers) return base
  const tiers = turn.tiers
  return { ...base, tiers: TIERS.filter((t) => tiers.includes(t)) }
}
