/**
 * Wire types for `/api/v1/ai/credentials` (#255, #1000), served by the agent service
 * (`agent/src/routes/credentials.ts`), not the backend, so they are not in the
 * backend's OpenAPI spec and `schema.d.ts`. Keep them in step with that file and with
 * `agent/src/harness/testConnection.ts` (`ConnectionTest`).
 */

/** agent `credentials.ts` `CREDENTIAL_KINDS`. */
export type AiCredentialKind = 'anthropic_api_key' | 'gateway'

/** The stored credential as reads give it. The secret itself is never returned. */
export interface AiCredentialView {
  configured: boolean
  kind: AiCredentialKind | null
  base_url: string | null
  last4: string | null
  updated_at: string | null
  /** False when the stored secret was sealed with a key other than the mounted one, or in the old format. */
  usable: boolean
  /** Whether a save with a secret can succeed, and if not, why. */
  can_save: boolean
  cannot_save_reason: string | null
}

export interface AiCredentialUpdate {
  kind: AiCredentialKind
  /** Gateway only. */
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
