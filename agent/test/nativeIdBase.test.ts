import { readFileSync } from 'node:fs'
import { expect, it } from 'vitest'
import { NATIVE_ID_BASE } from '../src/sessions/touched.js'

// The agent tells a native library file from a Bambuddy one by the backend's own id
// base (#4512): read it from backend/scadbuddy/print_library/ids.py, so the two cannot drift.
const IDS_PY = new URL('../../backend/scadbuddy/print_library/ids.py', import.meta.url)

it("uses the backend's NATIVE_ID_BASE", () => {
  const literal = /^NATIVE_ID_BASE = ([0-9_]+)$/m.exec(readFileSync(IDS_PY, 'utf8'))?.[1]
  expect(literal, 'ids.py defines NATIVE_ID_BASE as an integer literal').toBeDefined()
  expect(NATIVE_ID_BASE).toBe(Number(literal?.replaceAll('_', '')))
})
