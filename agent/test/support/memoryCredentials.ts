import {
  type Credential,
  type CredentialRepo,
  type CredentialUpdate,
  openCredential,
  planPut,
  type StoredCredential,
} from '../../src/credentials.js'
import { type Envelope, type Kek, SEAL_V1, sealedVersion } from '../../src/secrets.js'

/**
 * CredentialStore's behaviour without Postgres: the same `planPut` validation,
 * the same sealing and the same `openCredential`, kept in memory. The Postgres
 * store itself is covered by test/pg.test.ts.
 */
export class MemoryCredentials implements CredentialRepo {
  row: (Omit<StoredCredential, 'legacyFormat'> & { envelope: Envelope }) | undefined
  failing = false
  /** When set, get() waits on it (a database that hangs). */
  hang: Promise<void> | undefined

  async get(): Promise<StoredCredential | undefined> {
    if (this.hang) await this.hang
    if (this.failing) throw new Error('connection refused')
    if (!this.row) return undefined
    const { envelope, ...rest } = this.row
    return { ...rest, legacyFormat: sealedVersion(envelope.secretSealed) === SEAL_V1 }
  }

  reveal(kek: Kek): Promise<Credential | undefined> {
    if (!this.row) return Promise.resolve(undefined)
    try {
      return Promise.resolve(openCredential(kek, this.row))
    } catch (err) {
      return Promise.reject(err as Error)
    }
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
    return (await this.get()) as StoredCredential
  }

  delete(): Promise<boolean> {
    const had = this.row !== undefined
    this.row = undefined
    return Promise.resolve(had)
  }
}
