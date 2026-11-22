import { afterAll } from 'vitest'
import { flushDrops } from './postgres.js'

// The throwaway databases a file's tests dropped later (#7044) are dropped before its process
// ends (#7058). Listed first in `setupFiles`, so this hook runs after every other `afterAll`
// (vitest runs after-hooks in reverse order). With the 2 s `flushDrops` takes to close its
// connection, it must stay under the smallest `hookTimeout` of the configs that load it:
// vitest's default 10 s in vitest.evals.config.ts (vitest.config.ts has 20 s). A DROP still
// waiting then is left behind, as a killed run leaves it.
const DROP_WAIT_MS = 6_000

afterAll(() => flushDrops(DROP_WAIT_MS))
