import { readdirSync } from 'node:fs'
import { basename, dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import type { RouteModule } from './module.js'

/**
 * Every file in this directory that exports `route` (`module.ts`), in file-name order.
 * Found at load time, from the `.ts` sources under vitest and the built `.js` in
 * `dist/`, so adding a route group edits no shared list. The order does not decide
 * which route answers: `test/routes.test.ts` fails if two groups register the same one.
 */
export type RouteEntry = { file: string; route: RouteModule }

const here = dirname(fileURLToPath(import.meta.url))
const self = basename(fileURLToPath(import.meta.url))

const files = readdirSync(here)
  .filter((file) => /\.(ts|js)$/.test(file) && !/\.(d|test)\.(ts|js)$/.test(file) && file !== self)
  .sort()

// Imported together, then kept in file-name order.
const modules = await Promise.all(
  files.map(async (file) => ({
    file,
    module: (await import(pathToFileURL(join(here, file)).href)) as { route?: RouteModule },
  })),
)

export const ROUTES: RouteEntry[] = modules.flatMap(({ file, module }) =>
  module.route ? [{ file, route: module.route }] : [],
)
