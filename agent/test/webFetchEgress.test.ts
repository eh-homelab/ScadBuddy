import { describe, expect, it } from 'vitest'
import { webFetchEgressHook, webFetchRefusal } from '../src/harness/webFetchEgress.js'

// A classic turn's Claude Code WebFetch is checked as the agent's own fetches are.

const publicHost = async () => ['93.184.215.14']
const toMetadata = async () => ['169.254.169.254']

describe('webFetchRefusal', () => {
  it('lets a public http(s) URL through', async () => {
    expect(await webFetchRefusal('https://example.com/page', publicHost)).toBeUndefined()
    expect(await webFetchRefusal('http://192.168.1.10/', publicHost)).toBeUndefined()
  })

  it('refuses metadata hosts and addresses, by name or by what a name resolves to', async () => {
    expect(await webFetchRefusal('http://metadata.google.internal/', publicHost)).toMatch(/cloud metadata service/)
    expect(await webFetchRefusal('http://169.254.169.254/latest/', publicHost)).toMatch(/link-local/)
    expect(await webFetchRefusal('http://rebound.example/', toMetadata)).toMatch(/resolves to 169\.254\.169\.254/)
  })

  it("refuses the pod's loopback, by name, literal or what a name resolves to", async () => {
    expect(await webFetchRefusal('http://localhost:8081/', publicHost)).toMatch(/loopback/)
    expect(await webFetchRefusal('http://127.0.0.1:9/', publicHost)).toMatch(/loopback/)
    expect(await webFetchRefusal('http://[::1]/', publicHost)).toMatch(/loopback/)
    expect(await webFetchRefusal('http://rebound.example/', async () => ['127.0.0.2'])).toMatch(/resolves to 127\.0\.0\.2/)
    expect(await webFetchRefusal('http://rebound.example/', async () => ['::ffff:127.0.0.1'])).toMatch(/loopback/)
  })

  it('refuses what is not a plain http(s) URL', async () => {
    expect(await webFetchRefusal('file:///etc/passwd', publicHost)).toMatch(/^WebFetch refused/)
    expect(await webFetchRefusal('https://user:pw@example.com/', publicHost)).toMatch(/^WebFetch refused/)
    expect(await webFetchRefusal(undefined, publicHost)).toMatch(/^WebFetch refused/)
  })
})

describe('webFetchEgressHook', () => {
  const run = (name: string, input: unknown) =>
    webFetchEgressHook(toMetadata).hooks[0]!(
      { hook_event_name: 'PreToolUse', tool_name: name, tool_input: input, tool_use_id: 't', session_id: 's', transcript_path: '', cwd: '' } as never,
      't',
      { signal: new AbortController().signal },
    )

  it('denies a WebFetch that fails the check, and says nothing about any other tool', async () => {
    expect(await run('WebFetch', { url: 'http://x.example/', prompt: 'p' })).toMatchObject({
      hookSpecificOutput: { permissionDecision: 'deny', permissionDecisionReason: expect.stringMatching(/^WebFetch refused/) },
    })
    expect(await run('Read', { file_path: '/etc/passwd' })).toEqual({})
  })
})
