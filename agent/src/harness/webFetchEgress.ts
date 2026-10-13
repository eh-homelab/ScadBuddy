import type { HookCallbackMatcher } from '@anthropic-ai/claude-agent-sdk'
import { assertHostAllowed, assertHttpUrl, embeddedIPv4, isLoopbackHost, type Resolver, systemResolver } from '../http/egress.js'

// Claude Code's WebFetch, which a classic turn offers when an allowed plugin package
// names it (run.ts `builtinTools`), fetches inside Claude Code, where none of the
// agent's egress checks apply: measured on 2.1.296, a call to 127.0.0.1:9 tried to
// connect (#2393's probe). Every URL the agent itself fetches is checked
// (http/egress.ts `assertHostAllowed`: no cloud metadata host, no link-local address, no
// user:password), so this PreToolUse hook checks a WebFetch call's URL the same way
// before the call can run or park, and denies one that fails. A hook deny holds whatever
// the permission decision (sdk.d.ts `PreToolUse`). Loopback is refused too, which the
// gateway check allows: WebFetch fetches web pages, and the pod's loopback is the agent's
// own services (the plugin forwarder, the subagent MCP route).
//
// Claude Code resolves the name again when it connects, so a name re-pointed between
// this check and that connection is not caught here; a durable turn's WebFetch (the
// agent's own, builtins/web.ts) connects to the checked address.

export const WEB_FETCH = 'WebFetch'

/** Why `url` may not be fetched, or undefined when it may. */
export async function webFetchRefusal(url: unknown, resolve: Resolver = systemResolver): Promise<string | undefined> {
  try {
    const parsed = assertHttpUrl(String(url), 'url')
    if (isLoopbackHost(parsed.hostname)) return `WebFetch refused: url host ${parsed.hostname} is this pod's loopback, not a web page`
    const addresses = await assertHostAllowed(parsed.href, resolve, 'url', 'an allowed host')
    const loopback = addresses.find((a) => isLoopbackHost(embeddedIPv4(a) ?? a))
    if (loopback !== undefined) return `WebFetch refused: url host ${parsed.hostname} resolves to ${loopback}, this pod's loopback`
    return undefined
  } catch (err) {
    return `WebFetch refused: ${(err as Error).message}`
  }
}

export function webFetchEgressHook(resolve: Resolver = systemResolver): HookCallbackMatcher {
  return {
    matcher: WEB_FETCH,
    hooks: [
      async (input) => {
        if (input.hook_event_name !== 'PreToolUse' || input.tool_name !== WEB_FETCH) return {}
        const reason = await webFetchRefusal((input.tool_input as { url?: unknown } | undefined)?.url, resolve)
        if (reason === undefined) return {}
        return {
          hookSpecificOutput: { hookEventName: 'PreToolUse' as const, permissionDecision: 'deny' as const, permissionDecisionReason: reason },
        }
      },
    ],
  }
}
