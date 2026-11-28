/** The next number of a numbered row set: one more than the highest now there, as the database sequences hand out (a type, an item, a kind, a place, a spool). */
export const nextNumber = (rows: readonly { number: number }[]): number => Math.max(0, ...rows.map((row) => row.number)) + 1
