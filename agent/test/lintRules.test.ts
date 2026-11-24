import { ESLint } from 'eslint'
import { describe, expect, it } from 'vitest'

// Only src/temporal/pollers.ts may call DescribeTaskQueue (#7306, agent/CLAUDE.md "Poller
// reads"); the rule must catch each way a call can name it (#7416).
const eslint = new ESLint({ cwd: new URL('..', import.meta.url).pathname })
const restricted = async (code: string, filePath = 'src/example.ts') =>
  (await eslint.lintText(code, { filePath })).flatMap((r) => r.messages).filter((m) => m.ruleId === 'no-restricted-syntax').length

describe('the describeTaskQueue rule', () => {
  it.each([
    ['a member call', 'declare const svc: any\nvoid svc.describeTaskQueue({})\n'],
    ['a destructured call', 'declare const svc: any\nconst { describeTaskQueue } = svc\nvoid describeTaskQueue({})\n'],
    ['a computed call', "declare const svc: any\nvoid svc['describeTaskQueue']({})\n"],
    ['a reference called later', 'declare const svc: any\nconst f = svc.describeTaskQueue\nvoid f({})\n'],
    ['a call through .call', 'declare const svc: any\nvoid svc.describeTaskQueue.call(svc, {})\n'],
    ['a template-literal key', 'declare const svc: any\nvoid svc[`describeTaskQueue`]({})\n'],
    ['a renamed destructuring', 'declare const svc: any\nconst { describeTaskQueue: f } = svc\nvoid f({})\n'],
  ])('reports %s', async (_, code) => {
    expect(await restricted(code)).toBeGreaterThanOrEqual(1)
  })

  it.each([
    ['a stub key', 'export const svc = { describeTaskQueue: () => Promise.resolve({ pollers: [] }) }\n'],
    ['a quoted stub key', "export const svc = { 'describeTaskQueue': () => Promise.resolve({ pollers: [] }) }\n"],
  ])('allows %s in a test fake', async (_, code) => {
    expect(await restricted(code, 'test/example.test.ts')).toBe(0)
  })

  it('allows the call in src/temporal/pollers.ts', async () => {
    expect(await restricted('declare const svc: any\nvoid svc.describeTaskQueue({})\n', 'src/temporal/pollers.ts')).toBe(0)
  })
})
