/**
 * Wire types for the agent's Claude credentials (#255, #1000, #1093), served by the agent
 * service (`agent/src/routes/credentials.ts`), not the backend, so they are not in the
 * backend's OpenAPI spec and `schema.d.ts`. Keep them in step with that file
 * (`CredentialEntryView`, `CredentialListView`) and with
 * `agent/src/harness/testConnection.ts` (`ConnectionTest`).
 */

/** agent `credentials.ts` `CREDENTIAL_KINDS`. */
export type AiCredentialKind = 'anthropic_api_key' | 'gateway'

/**
 * `cooling_down` until `cooldown_until` (rate limited), then `active` again on its own;
 * `disabled` (refused for good) until reset or saved with a new secret.
 */
export type AiCredentialStatus = 'active' | 'cooling_down' | 'disabled'

/** One credential. The secret itself is never returned. */
export interface AiCredentialEntry {
  id: string
  /** 0 is tried first. */
  priority: number
  kind: AiCredentialKind
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
}

export interface AiCredentialList {
  /** In priority order. */
  credentials: AiCredentialEntry[]
  /** Whether any credential can be used now. */
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
