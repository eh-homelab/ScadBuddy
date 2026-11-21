import type { Sql } from 'postgres'
import { PG_CHANNEL } from '../../../src/events/bus.js'
import type { SessionBusEvent } from '../../../src/sessions/busEvents.js'
import type { Publisher } from '../../../src/sessions/publish.js'
import { WARM_EVICT_CHANNEL } from '../../../src/sessions/warmEvict.js'

// `Publisher` straight on the test database: the NOTIFYs the API's publish routes send (#5299).

export class PgPublisher implements Publisher {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async sessionEvent(event: SessionBusEvent): Promise<void> {
    await this.#sql`SELECT pg_notify(${PG_CHANNEL}, ${JSON.stringify(event)})`
  }

  async warmEvict(target: string): Promise<void> {
    await this.#sql`SELECT pg_notify(${WARM_EVICT_CHANNEL}, ${target})`
  }
}
