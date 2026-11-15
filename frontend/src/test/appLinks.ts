import type { BootstrapValue } from '../api/types'

/** The settings' `SCADBUDDY_ALLOWED_ORIGINS` row, as `appOrigins` reads it. */
export function allowedOrigins(value: string | null): BootstrapValue {
  return { name: 'allowed_origins', env_var: 'SCADBUDDY_ALLOWED_ORIGINS', value, source: 'env', reason: '' }
}
