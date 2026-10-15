import type { Sql } from 'postgres'
import { kekFromBase64 } from '../../src/secrets.js'
import { PgPayloadKeys } from '../../src/temporal/payloadCodec.js'

const kek = kekFromBase64(Buffer.alloc(32, 7).toString('base64'))

/** The data keys of a test's sessions, under one fixed key-encryption key, so every replica of a test (each with its own client) agrees. */
export function testPayloadKeys(sql: Sql): PgPayloadKeys {
  return new PgPayloadKeys(sql, kek)
}
