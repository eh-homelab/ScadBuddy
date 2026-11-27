/**
 * Wire types for the agent's Claude credentials (#255, #1000, #1093), served by the agent
 * service (`agent/src/routes/credentials.ts`), not the backend, so they are not in the
 * backend's OpenAPI spec and `schema.d.ts`. Keep them in step with that file
 * (`CredentialEntryView`, `CredentialListView`) and with
 * `agent/src/harness/testConnection.ts` (`ConnectionTest`).
 */

/** agent `credentials.ts` `AGENT_CREDENTIAL_KINDS`: the kinds the assistant runs, and the kinds Settings saves. */
export type AiCredentialKind = 'anthropic_api_key' | 'claude_oauth_token' | 'gateway'

/**
 * agent `credentials.ts` `CREDENTIAL_KINDS`: every kind the list can show. The pool is shared
 * with the fleet's login rotator (#7983), so it also holds `claude_login` rows, which the
 * assistant never runs or saves.
 */
export type AiStoredCredentialKind = AiCredentialKind | 'claude_login'

/** agent `credentials.ts` `CREDENTIAL_SCOPES`: who may use a credential (#7983). */
export type AiCredentialScope = 'agent' | 'fleet' | 'both'

/**
 * `cooling_down` until `cooldown_until` (rate limited), then `active` again on its own;
 * `disabled` (refused for good) until reset or saved with a new secret.
 */
export type AiCredentialStatus = 'active' | 'cooling_down' | 'disabled'

/** agent `credentials.ts` `CREDENTIAL_NAME_MAX`: the most characters a name has, after trimming. */
export const AI_CREDENTIAL_NAME_MAX = 60

/** One credential. The secret itself is never returned. */
export interface AiCredentialEntry {
  id: string
  /** 0 is tried first. */
  priority: number
  kind: AiStoredCredentialKind
  /** The person's label for it (#3710), or null. Plain text: show it as text, never as markup. */
  name: string | null
  base_url: string | null
  /** Empty for a secret shorter than 12 characters. */
  last4: string
  updated_at: string
  /** False when the stored secret was sealed with a key other than the mounted one, or in the old format. */
  usable: boolean
  status: AiCredentialStatus
  cooldown_until: string | null
  /** Why it was last refused, without the secret. */
  last_error: string | null
  last_error_at: string | null
  last_used_at: string | null
  /** Who may use it (#7983): the assistant, the fleet's machines, or both. */
  scope: AiCredentialScope
  /** Whether the assistant uses it: false for a `claude_login` or a `fleet` row. */
  agent_uses: boolean
  /** A `claude_login`'s account (#7983), in clear: null on every other kind. */
  email: string | null
  subscription_type: string | null
}

export interface AiCredentialList {
  /** In priority order. */
  credentials: AiCredentialEntry[]
  /** Whether any credential the assistant uses can be used now. */
  usable_now: boolean
  /** When none can: the soonest a rate-limited one is usable again; null if every one needs a reset. */
  recovers_at: string | null
  /** Whether a save with a secret can succeed, and if not, why. */
  can_save: boolean
  cannot_save_reason: string | null
}

export interface AiCredentialCreate {
  kind: AiCredentialKind
  /** Gateway only. */
  base_url?: string | null
  secret: string
  /** Optional (#3710); trimmed, and empty is no name. */
  name?: string | null
}

export interface AiCredentialSave {
  kind: AiCredentialKind
  base_url?: string | null
  /** Omitted keeps the stored secret, for the same kind and base URL only. */
  secret?: string
}

export interface AiConnectionTest {
  ok: boolean
  /** Why it failed; the credential is redacted from it. */
  detail: string
  duration_ms: number
  model: string | null
}
