/**
 * #155: a built-in template's id is `builtin:<slug>`, and the server refuses every
 * write to one with a 403. The prefix is the server's `BUILTIN_PREFIX`.
 */
export const BUILTIN_PREFIX = 'builtin:'

export function isBuiltin(modelId: string): boolean {
  return modelId.startsWith(BUILTIN_PREFIX)
}
