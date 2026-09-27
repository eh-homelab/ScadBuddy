import { api } from '../api/client'
import { keychainSource } from '../mocks/fixtures'

/** The duplicate every #160 scenario is built on, and its upstream. */
export const COPY = 'keychain-for-nova'
export const UPSTREAM = 'name-keychain'

/** What the upstream changes to: its text size, on one line. */
export const theirs = keychainSource.replace('text_size = 14;', 'text_size = 16;')
/** What the copy changes to when it conflicts: the same line, differently. */
export const ours = keychainSource.replace('text_size = 14;', 'text_size = 12;')

/**
 * A duplicate of `name-keychain` whose upstream has since moved, through the mock API
 * as the app would do it. `conflict` edits the copy's same line too.
 */
export async function duplicateWithUpdate({ conflict = false } = {}): Promise<void> {
  await api.duplicateModel(UPSTREAM, 'Keychain for Nova')
  if (conflict) await api.replaceSource(COPY, ours)
  await api.replaceSource(UPSTREAM, theirs)
}
