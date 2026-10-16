import { fileURLToPath } from 'node:url'

/** Module aliases for vite.config.ts and vitest.config.ts: `@/` is src/, the shadcn/ui import style (components.json, tsconfig.app.json). */
export const alias = [{ find: '@', replacement: fileURLToPath(new URL('./src', import.meta.url)) }]
