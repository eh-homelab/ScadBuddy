import { randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { type ChannelKind, type ChannelView, ChannelError, type CheckedChannel, CrudChannelRows, type OpenChannel } from '../../../src/notify/channels.js'
import { type Envelope, type Kek, last4, openSecret, sealSecret } from '../../../src/secrets.js'

// Test-only: `ai_notify_channels` over SQL, each token sealed with the test's KEK as the API
// seals it (backend api/crud_agent_notify.py), for the tests that run on a throwaway database.
// The agent itself goes through the API (src/notify/channels.ts CrudChannelRows).

type Row = {
  id: string
  kind: ChannelKind
  name: string
  url: string
  target: string | null
  enabled: boolean
  secret_sealed: Buffer | null
  dek_sealed: Buffer | null
  kek_id: string | null
  token_last4: string
  last_error: string | null
  last_sent_at: Date | null
}

const aad = (id: string, kind: string, url: string) => `notify-channel:${id}:${kind}:${url}`

function view(row: Row): ChannelView {
  return {
    id: row.id,
    kind: row.kind,
    name: row.name,
    url: row.url,
    target: row.target,
    enabled: row.enabled,
    has_token: row.secret_sealed !== null,
    token_last4: row.token_last4,
    last_error: row.last_error,
    last_sent_at: row.last_sent_at?.toISOString() ?? null,
  }
}

export class SqlChannelRows extends CrudChannelRows {
  readonly #sql: Sql
  readonly #kek: Kek

  constructor(sql: Sql, kek: Kek) {
    super({ baseUrl: 'http://no-api.invalid' })
    this.#sql = sql
    this.#kek = kek
  }

  #seal(id: string, checked: CheckedChannel): { sealed: Envelope | undefined; tail: string } {
    return checked.token
      ? { sealed: sealSecret(this.#kek, checked.token, aad(id, checked.kind, checked.url)), tail: last4(checked.token) }
      : { sealed: undefined, tail: '' }
  }

  #open(row: Row): OpenChannel {
    const token = row.secret_sealed
      ? openSecret(this.#kek, { secretSealed: row.secret_sealed, dekSealed: row.dek_sealed!, kekId: row.kek_id! }, aad(row.id, row.kind, row.url))
      : undefined
    return { id: row.id, kind: row.kind, url: row.url, target: row.target, token }
  }

  override async list(): Promise<ChannelView[]> {
    return (await this.#sql<Row[]>`SELECT * FROM ai_notify_channels ORDER BY created_at, id`).map(view)
  }

  override async view(id: string): Promise<ChannelView | undefined> {
    const [row] = await this.#sql<Row[]>`SELECT * FROM ai_notify_channels WHERE id = ${id}`
    return row ? view(row) : undefined
  }

  override async create(checked: CheckedChannel, enabled: boolean): Promise<string> {
    const id = randomUUID()
    const { sealed, tail } = this.#seal(id, checked)
    await this.#sql`
      INSERT INTO ai_notify_channels (id, kind, name, url, target, enabled, secret_sealed, dek_sealed, kek_id, token_last4)
      VALUES (${id}, ${checked.kind}, ${checked.name}, ${checked.url}, ${checked.target}, ${enabled},
              ${sealed?.secretSealed ?? null}, ${sealed?.dekSealed ?? null}, ${sealed?.kekId ?? null}, ${tail})`
    return id
  }

  override async update(id: string, checked: CheckedChannel, enabled: boolean, keepToken: boolean): Promise<boolean> {
    const [row] = await this.#sql<Row[]>`SELECT * FROM ai_notify_channels WHERE id = ${id}`
    if (!row) return false
    if (keepToken && (row.kind !== checked.kind || row.url !== checked.url)) throw new ChannelError('the channel moved; give its token again')
    const token = keepToken && row.secret_sealed ? this.#open(row).token! : checked.token
    const { sealed, tail } = this.#seal(id, { ...checked, token: token ?? null })
    await this.#sql`
      UPDATE ai_notify_channels SET kind = ${checked.kind}, name = ${checked.name}, url = ${checked.url}, target = ${checked.target},
             enabled = ${enabled}, secret_sealed = ${sealed?.secretSealed ?? null}, dek_sealed = ${sealed?.dekSealed ?? null},
             kek_id = ${sealed?.kekId ?? null}, token_last4 = ${tail}, last_error = NULL
       WHERE id = ${id}`
    return true
  }

  override async delete(id: string): Promise<boolean> {
    return (await this.#sql`DELETE FROM ai_notify_channels WHERE id = ${id}`).count > 0
  }

  override async open(id: string): Promise<OpenChannel | undefined> {
    const [row] = await this.#sql<Row[]>`SELECT * FROM ai_notify_channels WHERE id = ${id}`
    return row ? this.#open(row) : undefined
  }

  override async enabled(): Promise<OpenChannel[]> {
    const rows = await this.#sql<Row[]>`SELECT * FROM ai_notify_channels WHERE enabled ORDER BY created_at, id`
    return rows.flatMap((row) => {
      try {
        return [this.#open(row)]
      } catch {
        return []
      }
    })
  }

  override async record(id: string, error: string | null): Promise<void> {
    if (error === null) await this.#sql`UPDATE ai_notify_channels SET last_error = NULL, last_sent_at = now() WHERE id = ${id}`
    else await this.#sql`UPDATE ai_notify_channels SET last_error = ${error.slice(0, 500)} WHERE id = ${id}`
  }
}
