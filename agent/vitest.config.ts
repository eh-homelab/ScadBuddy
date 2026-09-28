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
  },
})
