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
  /**
   * May approve or deny any other agent's pending outward action, in any session
   * (#300; never its own calls or sessions). Outward tokens only.
   */
  approval_grant: boolean
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
  /** Only with `tier: 'outward'`; the service answers 400 otherwise. */
  approval_grant?: boolean
}

export interface MintedMcpToken {
  /** The bearer token: in this response only. */
  token: string
  record: McpToken
}

/** The modes Settings stores; `oidc` is on while its configuration is enabled (#262). */
export type ConfiguredMcpAuthMode = Exclude<McpAuthMode, 'oidc'>

/** `/api/v1/ai/mcp/auth` (`agent/src/routes/mcpAuthMode.ts`). */
export interface McpAuthSetting {
  /** What `/mcp` applies: `oidc` while OIDC is enabled, whatever is stored. */
  mode: McpAuthMode
  /** The stored mode, which applies while OIDC is off. */
  configured_mode: ConfiguredMcpAuthMode
  /** The most an anonymous caller may do while the mode is `disabled`. */
  anonymous_cap: McpTokenTier
  /**
   * The networks (IP addresses or CIDR ranges) an anonymous caller must come from while the
   * mode is `disabled` (#7767). A caller from anywhere else needs an access token.
   */
  anonymous_networks: string[]
  /** What `anonymous_networks` is when it was never set. */
  default_anonymous_networks: string[]
}

/**
 * What Settings may save. `expected` is the stored mode, cap and networks the page showed:
 * the service answers 409, and saves nothing, when they have changed since. Without
 * `anonymous_networks` the stored networks stay as they are.
 */
export interface McpAuthUpdate {
  mode: ConfiguredMcpAuthMode
  anonymous_cap: McpTokenTier
  anonymous_networks?: string[]
  expected: { mode: ConfiguredMcpAuthMode; anonymous_cap: McpTokenTier; anonymous_networks?: string[] }
}
