import type { Association, AssociationRelation } from '../api/types'

/**
 * #6395 — each relation in words, from both sides. The backend's `RELATIONS`
 * (`print_library/associations.py`) is the one list; this record is typed by it, so tsc
 * names a relation it adds and this misses.
 */
export const RELATION_WORDS: Record<AssociationRelation, { fromFile: string; fromTarget: string }> = {
  output_of: { fromFile: 'Output of', fromTarget: 'Has output' },
  variant_of: { fromFile: 'Variant of', fromTarget: 'Has variant' },
  source_of: { fromFile: 'Source of', fromTarget: 'Made from' },
  related_to: { fromFile: 'Related to', fromTarget: 'Related to' },
}

/** `print_library/ids.py` `NATIVE_ID_BASE`: only ScadBuddy's own library keeps associations. */
const NATIVE_ID_BASE = 1_000_000_000

export function isNativeFileId(id: number): boolean {
  return id >= NATIVE_ID_BASE
}

export const RELATIONS = Object.keys(RELATION_WORDS) as AssociationRelation[]

/** What a page shows associations for: a library file or a model. */
export type AssociationSubject = { kind: 'library'; id: number } | { kind: 'model'; slug: string }

/** The other end of an association, as the subject's page sees it. */
export interface OtherEnd {
  kind: 'library' | 'model'
  /** The file id, or the model's slug. */
  id: string
  name: string
}

export function subjectKey(subject: AssociationSubject): string {
  return subject.kind === 'library' ? `library:${subject.id}` : `model:${subject.slug}`
}

/** The relation said from the subject's side, and the other end. */
export function seenFrom(subject: AssociationSubject, item: Association): { words: string; other: OtherEnd } {
  const fromFile = subject.kind === 'library' && item.library_file_id === subject.id
  const words = RELATION_WORDS[item.relation]
  if (fromFile) {
    const [kind, id] = splitTarget(item.target)
    return { words: words.fromFile, other: { kind, id, name: item.target_name ?? id } }
  }
  return {
    words: words.fromTarget,
    other: { kind: 'library', id: String(item.library_file_id), name: item.library_file_name },
  }
}

function splitTarget(target: string): ['library' | 'model', string] {
  const at = target.indexOf(':')
  return [target.slice(0, at) === 'model' ? 'model' : 'library', target.slice(at + 1)]
}

/** Why an implied association is listed, in words, said from the subject's page. */
export function impliedWhy(subject: AssociationSubject, item: Association): string | null {
  const onModel = subject.kind === 'model'
  if (item.implied_by === 'mesh_source') return onModel ? 'This model was made from the file' : 'The model was made from this file'
  if (item.implied_by === 'output') return onModel ? "A copy of one of this model's outputs" : "A copy of one of the model's outputs"
  return null
}
