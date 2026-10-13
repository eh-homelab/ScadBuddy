import { randomUUID } from 'node:crypto'
import { createSdkMcpServer } from '@anthropic-ai/claude-agent-sdk'
import { ROOT_CONTEXT } from '@opentelemetry/api'
import type { ApprovalRequest } from '../../src/harness/permissions.js'
import { builtInPolicy } from '../../src/plugins/builtInTools.js'
import type { ClaimedSession, TurnPrincipal } from '../../src/sessions/manager.js'
import { TurnRunner } from '../../src/sessions/turnRunner.js'
import type { TurnContext } from '../../src/sessions/turnSlot.js'
import type { SessionFiles } from '../../src/workspace/store.js'
import { TurnTrace } from '../../src/telemetry/turn.js'
import { fixedCredentials } from './fixedCredentials.js'
import { browser, tempPaths } from './sessions.js'

/**
 * A TurnRunner over the session tests' fakes (sessions.ts), with every option a
 * turn can be assembled with switched on: a browser-owned session (so a question
 * gate), the in-process registry and http servers, a remote plugin that is a
 * Hindsight bank (so memory hooks), plugin packages, an own plugin and a model.
 * `resume` says whether the session already has a transcript.
 */
export async function turnRunnerForTest(options: { resume?: boolean } = {}) {
  const paths = await tempPaths()
  const now = new Date().toISOString()
  const session: ClaimedSession = {
    id: randomUUID(),
    origin: 'chat',
    owner: browser,
    creator: browser,
    offer: null,
    status: 'running',
    title: 'test',
    tags: [],
    scope: {},
    parentId: null,
    mode: 'classic',
    maxTurns: 5,
    budgetUsd: 1,
    costUsd: 0,
    ownCostUsd: 0,
    turns: 0,
    turnActive: true,
    createdAt: now,
    updatedAt: now,
    archivedAt: null,
    unpricedCostUsd: 0,
  }
  const principal: TurnPrincipal = {}
  const runner = new TurnRunner({
    paths,
    credentials: fixedCredentials({ kind: 'anthropic_api_key', secret: 'sk-ant-test' }),
    settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-test' : undefined) as T | undefined) },
    tierOf: () => 'read',
    mcpServers: () => ({ scadbuddy: createSdkMcpServer({ name: 'scadbuddy', tools: [] }) }),
    builtInTools: () => Promise.resolve(builtInPolicy([])),
    pluginPaths: [],
    ownPlugin: '/plugins/own',
    remotePlugins: () =>
      Promise.resolve({
        plugins: [{ name: 'hindsight', url: 'http://127.0.0.1:1/p/token', toolTiers: {}, disabledTools: [] }],
        problems: [],
        secrets: ['plugin-token'],
        hindsight: {
          plugin: { name: 'hindsight', url: 'https://hindsight.example/mcp/bank/', toolTiers: {}, disabledTools: [] },
          address: '192.0.2.1',
        },
        release: () => {},
      }),
    packagePlugins: () =>
      Promise.resolve({ paths: ['/plugins/a'], allowedPaths: ['/plugins/b'], builtinTools: ['Bash'], loaded: [], problems: [], release: () => {} }),
    httpRequest: {},
    stderr: () => {},
    store: {
      exists: () => Promise.resolve(options.resume ?? false),
      append: () => Promise.resolve(),
      load: () => Promise.resolve(null),
    },
    events: { append: () => Promise.resolve([]) },
    files: {} as SessionFiles,
  })
  const seen: { turnId: string; toolUseId: string }[] = []
  const gateCalls = {
    seen,
    /** A turn whose approval gate records which turn it was asked in. */
    context(turnId: string): TurnContext {
      return {
        turnId,
        principal,
        approvalGate: (request: ApprovalRequest) => {
          seen.push({ turnId, toolUseId: request.toolUseId })
          return Promise.resolve({ approved: false, message: 'denied by the test' })
        },
        trace: new TurnTrace({ sessionId: session.id, turnId, parent: ROOT_CONTEXT, tierOf: () => 'read' }),
        signalInterrupt: () => {},
      }
    },
  }
  return { runner, session, principal, gateCalls }
}
