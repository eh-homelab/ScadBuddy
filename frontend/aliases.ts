import { fileURLToPath } from 'node:url'

const ui = (file: string) => fileURLToPath(new URL(`./src/components/ui/${file}`, import.meta.url))

/**
 * Module aliases for vite.config.ts and vitest.config.ts. `@/` is src/, the shadcn/ui
 * import style (components.json, tsconfig.app.json), except for shadcn's button and dialog.
 * Those live in shadcn-button.tsx and shadcn-dialog.tsx while the legacy ui/Button and
 * ui/Dialog exist (#3653): on a case-insensitive file system, `./ui/Button` would
 * otherwise find button.tsx before Button/index.tsx. When the last legacy import goes,
 * rename them back and drop these two entries (and the matching ones in tsconfig).
 */
export const alias = [
  { find: /^@\/components\/ui\/button$/, replacement: ui('shadcn-button.tsx') },
  { find: /^@\/components\/ui\/dialog$/, replacement: ui('shadcn-dialog.tsx') },
  { find: '@', replacement: fileURLToPath(new URL('./src', import.meta.url)) },
]
