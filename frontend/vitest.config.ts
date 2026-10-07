import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
    // Page tests render ~1000-element pages in jsdom and React's development build, and
    // take 1-3 s alone. On a shared host (the WSL dev box runs at load 20-50 from other
    // work) they ran into the 5 s default although nothing was wrong (#1485).
    testTimeout: 15_000,
  },
})
