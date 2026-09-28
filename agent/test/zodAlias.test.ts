import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// vitest.config.ts resolves the frontend protocol module's `zod` import to
// agent's own copy (frontend/node_modules is not installed in the agent CI
// job). sessions.protocol.test.ts then checks the agent's events against the
// panel's schema, but only truthfully while both packages run the same zod.
// This guard fails when the two declared ranges drift apart.

const here = path.dirname(fileURLToPath(import.meta.url))

async function zodRange(pkg: string): Promise<string | undefined> {
  const json = JSON.parse(await readFile(path.resolve(here, '..', '..', pkg, 'package.json'), 'utf8')) as {
    dependencies?: Record<string, string>
    devDependencies?: Record<string, string>
  }
  return json.dependencies?.zod ?? json.devDependencies?.zod
}

describe('the zod alias for the frontend protocol schema', () => {
  it('holds only while agent/ and frontend/ declare the same zod range', async () => {
    const [agent, frontend] = await Promise.all([zodRange('agent'), zodRange('frontend')])
    expect(agent, 'agent/package.json declares no zod').toBeDefined()
    expect(
      frontend,
      `zod ranges differ: frontend/package.json has ${frontend}, agent/package.json has ${agent}. ` +
        'vitest.config.ts runs the frontend protocol schema (#340) on agent’s zod, so the ' +
        'sessions protocol test would validate against a zod the panel does not run. ' +
        'Bump both package.json files (and both lockfiles) together.',
    ).toBe(agent)
  })
})
