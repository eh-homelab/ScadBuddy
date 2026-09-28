import { describe, expect, it } from 'vitest'
import { features } from './features'
import * as mcpTokens from './features/mcpTokens'
import { handlers } from './handlers'

describe('mock features', () => {
  it('picks up every features/*.ts module without a line in handlers.ts', () => {
    expect(features).toContain(mcpTokens)
  })

  it('serves every feature handler from the one handler list', () => {
    for (const feature of features) {
      for (const handler of feature.handlers) expect(handlers).toContain(handler)
    }
  })
})
