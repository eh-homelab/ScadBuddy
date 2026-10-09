import { Link } from 'react-router'
import { api } from '../api/client'
import type { McpTokenTier } from '../api/mcpTokens'
import { useMcpAuthVersion } from '../lib/mcpAuthChanges'
import { useAsync } from '../lib/useAsync'

const CAP_WORDS: Record<McpTokenTier, string> = {
  read: 'read-only access',
  write: 'read and write access',
  outward: 'full access',
}

/**
 * #1921 (spec §8.3, #258) — a persistent warning while `/mcp` serves callers without a
 * token: the effective auth mode (`GET /api/v1/ai/mcp/auth` `mode`, so not while OIDC
 * overrides a stored `disabled`) is `disabled`. Shown at the top of Settings and in the
 * assistant panel; it cannot be dismissed, only ended by turning authentication back on.
 * Says nothing when the agent cannot answer: the sections that read the mode say why.
 *
 * Read again whenever Settings saves the mode (lib/mcpAuthChanges.ts), so the panel's,
 * which stays mounted while hidden, never outlives the change.
 */
export function McpAuthBanner({ link = false, className = '' }: { link?: boolean; className?: string }) {
  const version = useMcpAuthVersion()
  const auth = useAsync(() => api.getMcpAuth(), [version])
  if (auth.data?.mode !== 'disabled') return null
  return (
    <div
      role="alert"
      data-testid="mcp-auth-banner"
      className={`rounded-[6px] border border-warn/50 bg-warn/10 px-3 py-2 text-[12px] text-warn ${className}`}
    >
      <strong className="font-medium">MCP authentication is off.</strong> Anyone who can reach{' '}
      <code>/mcp</code> over HTTPS can call ScadBuddy&rsquo;s tools without a token, with{' '}
      {CAP_WORDS[auth.data.anonymous_cap]}.{' '}
      {link ? (
        <>
          Turn it back on in{' '}
          <Link to="/settings#assistant" className="underline">
            Settings
          </Link>
          .
        </>
      ) : (
        <>Turn it back on under Assistant &rarr; MCP authentication.</>
      )}
    </div>
  )
}
