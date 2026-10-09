import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  resolve: {
    // test/support/frontendProtocol.ts loads the panel's own protocol schema
    // from frontend/src (#340). frontend/node_modules is not installed in the
    // agent job, so its `zod` import resolves to agent's. That check is only
    // faithful while both packages declare the same zod range:
    // test/zodAlias.test.ts fails when agent/package.json and
    // frontend/package.json drift apart, so bump them together.
    alias: [{ find: /^zod$/, replacement: fileURLToPath(import.meta.resolve('zod')) }],
  },
  test: {
    environment: 'node',
    include: ['test/**/*.test.ts'],
    // #1746: polls wait on background turns and Postgres round trips, which a
    // loaded CI runner slows about tenfold (measured: 0.2 s tests took 2 s
    // beside the rest of the suite). vitest's own 1 s poll default was never a
    // chosen limit, and the polls that did choose one chose 5 s, so that is the
    // default. A test may poll several times, so it gets room for a few.
    expect: { poll: { timeout: 5000 } },
    testTimeout: 20_000,
  },
})
