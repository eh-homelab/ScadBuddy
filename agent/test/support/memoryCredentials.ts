import {
  type Credential,
  CREDENTIAL_AAD,
  type CredentialRepo,
  type CredentialUpdate,
  planPut,
  type StoredCredential,
} from '../../src/credentials.js'
import { type Envelope, type Kek, openSecret } from '../../src/secrets.js'

/**
 * CredentialStore's behaviour without Postgres: the same `planPut` validation
 * and the same sealing, kept in memory. The Postgres store itself is covered by
 * test/pg.test.ts.
 */
export class MemoryCredentials implements CredentialRepo {
  row: (StoredCredential & { envelope: Envelope }) | undefined
  failing = false

  get(): Promise<StoredCredential | undefined> {
    if (this.failing) return Promise.reject(new Error('connection refused'))
    if (!this.row) return Promise.resolve(undefined)
    const { envelope: _envelope, ...rest } = this.row
    return Promise.resolve(rest)
  }

  reveal(kek: Kek): Promise<Credential | undefined> {
    if (!this.row) return Promise.resolve(undefined)
    const secret = openSecret(kek, this.row.envelope, CREDENTIAL_AAD)
    return Promise.resolve(
      this.row.kind === 'gateway'
        ? { kind: 'gateway', baseUrl: this.row.base_url ?? '', secret }
        : { kind: 'anthropic_api_key', secret },
    )
  }

  async put(update: CredentialUpdate, kek: Kek | undefined): Promise<StoredCredential> {
    const plan = planPut(update, update.secret === undefined ? await this.get() : undefined, kek)
    if ('keep' in plan) return plan.keep
    const { kind, baseUrl, envelope, last4 } = plan.write
    this.row = {
      kind,
      base_url: baseUrl,
      last4,
      updated_at: new Date().toISOString(),
      kekId: envelope.kekId,
      envelope,
    }
    return this.get() as Promise<StoredCredential>
  }

  delete(): Promise<boolean> {
    const had = this.row !== undefined
    this.row = undefined
    return Promise.resolve(had)
  }
}
