import { afterAll } from 'vitest'
import { flushDrops } from './postgres.js'

// The throwaway databases a file's tests dropped later (#7044) are dropped before its process
// ends (#7058). Listed first in `setupFiles`, so this hook runs after every other `afterAll`
// (vitest runs after-hooks in reverse order). It must stay under `hookTimeout`
// (vitest.config.ts, 20 s): a DROP still waiting then is left behind, as a killed run leaves it.
const DROP_WAIT_MS = 10_000

afterAll(() => flushDrops(DROP_WAIT_MS))
