/**
 * Wire types for `/api/v1/ai/mcp-tokens` (#251), served by the agent service
 * (`agent/src/routes/mcpTokens.ts`), not the backend, so they are not in the
 * backend's OpenAPI spec and `schema.d.ts`. Keep them in step with that file.
 */

export type McpTokenTier = 'read' | 'write' | 'outward'
export type McpAuthMode = 'bearer' | 'disabled' | 'oidc'
export type McpTokenStatus = 'active' | 'expired' | 'revoked'

/** A token's metadata. The token itself, and its hash, are never returned. */
export interface McpToken {
  id: string
  name: string
  tier: McpTokenTier
  created_at: string
  expires_at: string | null
  last_used_at: string | null
  revoked_at: string | null
  status: McpTokenStatus
}

export interface McpTokenList {
  /** Null when the agent could not read its auth settings. */
  auth_mode: McpAuthMode | null
  /** Newest first. */
  tokens: McpToken[]
}

export interface McpTokenCreate {
  name: string
  tier: McpTokenTier
  /** Seconds from now; left out, the token never expires. */
  expires_in?: number
}

export interface MintedMcpToken {
  /** The bearer token: in this response only. */
  token: string
  record: McpToken
}

/** `/api/v1/ai/mcp/auth` (`agent/src/routes/mcpAuthMode.ts`): what `/mcp` applies. */
export interface McpAuthSetting {
  mode: McpAuthMode
  /** The most an anonymous caller may do while the mode is `disabled`. */
  anonymous_cap: McpTokenTier
}

/** What Settings may save; `oidc` is switched on with its own configuration (#262). */
export interface McpAuthUpdate {
  mode: Exclude<McpAuthMode, 'oidc'>
  anonymous_cap: McpTokenTier
}
