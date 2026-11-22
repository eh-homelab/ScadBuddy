import type { Sql } from 'postgres'
import { CrudOAuthRows } from '../../../src/plugins/oauthStore.js'
import { type Kek, openSecret, SealError } from '../../../src/secrets.js'

// Test-only: the plugin sign-ins' rows a test seeds in `ai_plugin_oauth`, sealed as the API
// seals them. Only `storedSecrets` reads them (the redaction list); every other call goes to
// an API that is not there.

type GrantColumns = { plugin: string; plugin_url: string; resource: string; issuer: string; token_endpoint: string; client_id: string }

/** The AAD the API seals a grant under (backend api/crud_agent_plugins.py). */
export function grantAad(r: GrantColumns): string {
  return `ai_plugin_oauth:${JSON.stringify({
    plugin: r.plugin,
    plugin_url: r.plugin_url,
    resource: r.resource,
    issuer: r.issuer,
    token_endpoint: r.token_endpoint,
    client_id: r.client_id,
  })}`
}

export class SqlOAuthRows extends CrudOAuthRows {
  readonly #sql: Sql
  readonly #kek: Kek

  constructor(sql: Sql, kek: Kek) {
    super({ baseUrl: 'http://no-api.invalid' })
    this.#sql = sql
    this.#kek = kek
  }

  override async storedSecrets(): Promise<{ access_token: string; refresh_token?: string; client_secret?: string }[]> {
    const rows = await this.#sql<(GrantColumns & { secret_sealed: Buffer; dek_sealed: Buffer; kek_id: string })[]>`SELECT * FROM ai_plugin_oauth`
    return rows.flatMap((row) => {
      try {
        const envelope = { secretSealed: row.secret_sealed, dekSealed: row.dek_sealed, kekId: row.kek_id }
        return [JSON.parse(openSecret(this.#kek, envelope, grantAad(row))) as { access_token: string; refresh_token?: string; client_secret?: string }]
      } catch (err) {
        if (err instanceof SealError) return []
        throw err
      }
    })
  }
}
