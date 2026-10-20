import type { Sql } from 'postgres'
import { randomUUID } from 'node:crypto'
import { createSdkMcpServer } from '@anthropic-ai/claude-agent-sdk'
import { ROOT_CONTEXT } from '@opentelemetry/api'
import { SETTING_HTTP_REQUEST } from '../../src/harness/httpRequest.js'
import { SETTING_RECALL_MAX_QUERY_CHARS } from '../../src/memory/hindsight.js'
import type { ApprovalRequest, RiskTier } from '../../src/harness/permissions.js'
import { builtInPolicy } from '../../src/plugins/builtInTools.js'
import { forwardForRun, type PluginForwarder } from '../../src/plugins/forwarder.js'
import type { ClaimedSession, TurnPrincipal } from '../../src/sessions/manager.js'
import { TurnRunner, type TurnRunnerDeps } from '../../src/sessions/turnRunner.js'
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
 * `options` change one input each (TurnRunnerTestOptions).
 */
export type TurnRunnerTestOptions = {
  resume?: boolean
  /** The Hindsight plugin's configured endpoint. */
  pluginUrl?: string
  /** The Hindsight plugin's tool tiers and disabled tools. */
  pluginToolTiers?: Record<string, RiskTier>
  pluginDisabledTools?: string[]
  /** Settings → Plugins' raised tiers for ScadBuddy's own tools (#1953), by bare name. */
  raisedTiers?: Record<string, RiskTier>
  /** The stored http_request setting (#827); on when undefined. */
  httpRequest?: boolean
  /** Whether the loaded package was approved with `allow_refused`. */
  allowRefused?: boolean
  /** The stored Hindsight recall query cap (#2655); the default when undefined. */
  recallMaxQueryChars?: number
  /** The preparer each assembly binds (#4442); none resolves when undefined. */
  prepareApproval?: TurnRunnerDeps['prepareApproval']
}

/** The runner's `sql`, which only http_request's saved bodies would use: these tests save none. */
export const unusedSql = new Proxy(
  {},
  {
    get() {
      throw new Error('turnRunnerForTest has no database')
    },
  },
) as unknown as Sql

export async function turnRunnerForTest(options: TurnRunnerTestOptions = {}) {
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
    agentModel: null,
    agentEffort: null,
    agentType: null,
    claimedAt: now,
  }
  const principal: TurnPrincipal = {}
  const runner = new TurnRunner({
    sql: unusedSql,
    paths,
    credentials: fixedCredentials({ kind: 'anthropic_api_key', secret: 'sk-ant-test' }),
    settings: {
      get: <T>(key: string) => {
        const stored: Record<string, unknown> = {
          model: 'claude-test',
          [SETTING_HTTP_REQUEST]: options.httpRequest,
          [SETTING_RECALL_MAX_QUERY_CHARS]: options.recallMaxQueryChars,
        }
        return Promise.resolve(stored[key] as T | undefined)
      },
    },
    tierOf: () => 'read',
    mcpServers: () => ({ scadbuddy: createSdkMcpServer({ name: 'scadbuddy', tools: [] }) }),
    prepareApproval: options.prepareApproval ?? (() => async () => undefined),
    builtInTools: () =>
      Promise.resolve(
        builtInPolicy([
          {
            set: { name: 'scadbuddy', tool_prefix: 'mcp__scadbuddy__', tools: [], switchable: false },
            overrides: { tool_tiers: options.raisedTiers ?? {}, disabled_tools: [] },
          },
        ]),
      ),
    pluginPaths: [],
    ownPlugin: '/plugins/own',
    // The real forwardForRun over a forwarder that, like the real one, gives
    // every registration a loopback URL of its own.
    remotePlugins: () =>
      Promise.resolve(
        forwardForRun(
          {
            plugins: [
              {
                plugin: {
                  name: 'hindsight',
                  url: options.pluginUrl ?? 'https://hindsight.example/mcp/bank/',
                  header: { name: 'Authorization', value: 'Bearer plugin-token' },
                  toolTiers: options.pluginToolTiers ?? { recall: 'read' },
                  disabledTools: options.pluginDisabledTools ?? [],
                },
                address: '192.0.2.1',
              },
            ],
            problems: [],
          },
          { register: () => ({ url: `http://127.0.0.1:1/p/${randomUUID()}`, release: () => {} }) } as unknown as PluginForwarder,
        ),
      ),
    packagePlugins: () =>
      Promise.resolve({
        paths: ['/plugins/a'],
        allowedPaths: ['/plugins/b'],
        builtinTools: ['Bash'],
        // Not one of `paths`, so nothing is copied to strip its servers.
        loaded: [{ name: 'c', contentHash: 'hash-c', dir: '/plugins/c', allowed: options.allowRefused ?? false, builtinTools: [] }],
        problems: [],
        release: () => {},
      }),
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
        trace: new TurnTrace({
          sessionId: session.id,
          turnId,
          parent: ROOT_CONTEXT,
          tierOf: () => 'read',
          before: { costUsd: 0, turns: 0 },
        }),
        signalInterrupt: () => {},
      }
    },
  }
  return { runner, session, principal, gateCalls }
}
