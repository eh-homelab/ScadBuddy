/**
 * #6133 — the order `store.py` `charges` lists an order's charges in (`ORDER BY auto,
 * kind = 'handling', kind = 'purchase', position, id`): the typed rows, the purchases, the
 * handling fee, then the auto tax. The summary and the mocks share it.
 */
type Ranked = { id: string; auto?: boolean | null; kind: string; position: number }

function group(c: Ranked): number {
  return c.auto ? 3 : c.kind === 'handling' ? 2 : c.kind === 'purchase' ? 1 : 0
}

export function compareCharges(a: Ranked, b: Ranked): number {
  return group(a) - group(b) || a.position - b.position || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
}
