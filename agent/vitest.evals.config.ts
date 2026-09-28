import { defineConfig } from 'vitest/config'

// `pnpm evals`: the opt-in live evals in evals/*.eval.ts (issue #259,
// docs/ai/evals.md). They call a real Claude model, so `pnpm test`
// (vitest.config.ts, test/**/*.test.ts) never includes them.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['evals/**/*.eval.ts'],
    // One scenario at a time: each starts its own Claude Code process.
    fileParallelism: false,
    testTimeout: 360_000,
    // Verbose, so a skip shows its reason (the suite name carries it).
    reporters: ['verbose'],
  },
})
