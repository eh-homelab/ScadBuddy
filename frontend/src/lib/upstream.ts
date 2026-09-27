import { modelPath } from './deeplink'

/** A line git's `--diff3` conflict markers open with: one per conflict. */
export function countConflicts(source: string): number {
  return source.match(/^<{7}(?: |$)/gm)?.length ?? 0
}

/** #160 — a duplicate's source page, opened on its conflicted upstream merge to resolve. */
export function resolvePath(slug: string): string {
  return `${modelPath(slug, 'source')}?merge`
}
