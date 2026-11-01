import type { Sql, TransactionSql } from 'postgres'

// #1383: whether an `ai_questions` row waits on the user, written once. A `done`
// summary (#815 §4) has no timer (`expires_at` NULL): nothing is parked on it, it
// outlives its turn and stays until dismissed, so it is not a wait. Every other
// unresolved row is, including a `done` row with a timer, which only an older
// replica wrote and whose turn is parked on it like any attention request.
//
// The rule is NULL-safe, so its negation is too: a plain question's `attention_reason`
// is NULL, and `attention_reason <> 'done'` would be NULL for it, so a WHERE would drop it.
// test/waiting.test.ts fails on a query elsewhere in src/ that spells the rule itself.

type Query = Sql | TransactionSql

/** True for a posted `done` summary: reason `done`, no timer. `alias` is the table's alias in the query, if any. */
export function isDoneSummary(sql: Query, alias?: string) {
  const column = (name: string) => sql(alias ? `${alias}.${name}` : name)
  return sql`(${column('attention_reason')} IS NOT DISTINCT FROM 'done' AND ${column('expires_at')} IS NULL)`
}

/** True for every row but a posted `done` summary: with `outcome IS NULL`, a row that waits on the user. */
export function isNotDoneSummary(sql: Query, alias?: string) {
  return sql`(NOT ${isDoneSummary(sql, alias)})`
}
