/** Comments, so a commented-out `use` suggests nothing. An unterminated block comment
 * comments out everything after it. */
const COMMENTS = /\/\*[\s\S]*?(?:\*\/|$)|\/\/[^\n]*/g

/** The first path segment of a `use <DIR/...>` or `include <DIR/...>`. */
const IMPORT = /\b(?:use|include)\s*<\s*([^>/\s]+)\//g

/**
 * #169 — the curated libraries a source reaches for: every name in `catalogue` that
 * a `use`/`include` line opens a directory of, in the order the source first names
 * them. Case-sensitive, as the library path is on the server.
 */
export function detectLibraries(source: string, catalogue: readonly string[]): string[] {
  const known = new Set(catalogue)
  const found = new Set<string>()
  for (const match of source.replace(COMMENTS, '').matchAll(IMPORT)) {
    const name = match[1]
    if (name !== undefined && known.has(name)) found.add(name)
  }
  return [...found]
}
