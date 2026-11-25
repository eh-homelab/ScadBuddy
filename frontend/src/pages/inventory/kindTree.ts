import type { ItemKind } from './api'

export interface KindNode {
  kind: ItemKind
  children: KindNode[]
}

/** The kinds as a tree by `parent_id`, each level by name; a kind whose parent is not listed is a root. */
export function kindTree(kinds: ItemKind[]): KindNode[] {
  const ids = new Set(kinds.map((k) => k.id))
  const byName = (a: KindNode, b: KindNode) => a.kind.name.localeCompare(b.kind.name)
  const nodes = new Map(kinds.map((kind) => [kind.id, { kind, children: [] as KindNode[] }]))
  const roots: KindNode[] = []
  for (const node of nodes.values()) {
    const parent = node.kind.parent_id && ids.has(node.kind.parent_id) ? nodes.get(node.kind.parent_id) : undefined
    ;(parent ? parent.children : roots).push(node)
  }
  for (const node of nodes.values()) node.children.sort(byName)
  return roots.sort(byName)
}

/** The kind's name, and its ancestors' before it: "Part / Hardware". */
export function kindPathText(path: string[], byId: Map<string, ItemKind>): string {
  return path.map((id) => byId.get(id)?.name ?? id).join(' / ')
}
