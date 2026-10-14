import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// #2259: test/fixtures/backend-flow-migrations holds copies of the backend's migrations,
// which the agent's flow tests apply (the agent never migrates those tables itself). A
// copy that drifts from its backend file tests the agent against a schema no deployment
// has, so each must match the backend's file of the same name byte for byte.

const FIXTURES = fileURLToPath(new URL('./fixtures/backend-flow-migrations/', import.meta.url))
const BACKEND = fileURLToPath(new URL('../../backend/scadbuddy/migrations/', import.meta.url))

describe('backend flow migration fixtures', () => {
  const names = readdirSync(FIXTURES).filter((name) => name.endsWith('.sql'))

  it('are there', () => {
    expect(names.length).toBeGreaterThan(0)
  })

  // A backend flow migration with no copy here tests the agent against a schema without it.
  // Found by what it touches, not its name: a migration on these tables named otherwise
  // (`_run_grants_index.sql`, say) needs a copy too.
  it('include every backend migration that touches the flow tables', () => {
    const touches = /\b(workflow_definitions|workflow_runs|workflow_run_decisions|workflow_run_proposals|workflow_flow_states|flow_grants|flow_grant_uses)\b/
    const backend = readdirSync(BACKEND).filter(
      (name) => name.endsWith('.sql') && touches.test(readFileSync(`${BACKEND}${name}`, 'utf8')),
    )
    expect(backend.length).toBeGreaterThan(0)
    expect(backend.filter((name) => !names.includes(name))).toEqual([])
  })

  it.each(names)('%s matches the backend migration of the same name', (name) => {
    const backend = `${BACKEND}${name}`
    expect(existsSync(backend), `${name} is not a backend migration`).toBe(true)
    expect(readFileSync(`${FIXTURES}${name}`, 'utf8')).toBe(readFileSync(backend, 'utf8'))
  })
})
