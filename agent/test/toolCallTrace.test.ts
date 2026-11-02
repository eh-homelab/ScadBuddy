import { MockActivityEnvironment } from '@temporalio/testing'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import { createBackendClient } from '../src/api/backend.js'
import type { Owner } from '../src/sessions/protocol.js'
import { toolCallActivities } from '../src/temporal/toolActivities.js'
import { toolCallActivity, toolCallWorkflowId } from '../src/temporal/toolCall.js'
import { defineTool, json } from '../src/tools/registry.js'
import { BACKEND, services } from './helpers/mcp.js'
import { PARENT_SPAN_ID, TRACE_ID, TRACEPARENT, testTracing } from './support/tracing.js'

// A ToolCall's activity runs under the call's span in the turn's trace (plan 2026-10-13 Step 9.2):
// the traceparent the starter put in its arguments is the parent of its `agent.tool/<name>` span,
// and so of the backend requests the tool sends, as an in-process call's are.

const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const BROWSER: Owner = { kind: 'browser', id: 'browser', label: 'You' }

describe("a ToolCall's trace", () => {
  it("is the call's span in the turn's trace, and so is the backend request it sends", async () => {
    const spans = testTracing()
    spans.reset()
    const headers: Headers[] = []
    const backend = createBackendClient(BACKEND, async (request) => {
      headers.push((request as Request).headers)
      return Response.json({})
    })
    const look = defineTool({
      name: 'look',
      label: 'look',
      description: 'reads the settings',
      input: z.object({}),
      risk: 'read',
      routes: [],
      handler: async (_args, ctx) => {
        await ctx.backend.GET('/api/v1/settings')
        return json({ looked: true })
      },
    })
    const activities = toolCallActivities([look], { services: services({ backend }), sessions: { ownerOf: async (id) => (id === SESSION ? BROWSER : undefined) } })
    const env = new MockActivityEnvironment({
      activityId: '1',
      activityType: toolCallActivity('look'),
      workflowExecution: { workflowId: toolCallWorkflowId(SESSION, 'toolu_01Traced'), runId: 'run-1' },
      taskQueue: 'agent-tools',
    })
    expect(await env.run(activities[toolCallActivity('look')]!, { tool: 'look', input: {}, traceparent: TRACEPARENT })).toMatchObject({ ok: true })
    const span = spans.getFinishedSpans().find((s) => s.name === 'agent.tool/look')
    expect(span?.spanContext().traceId).toBe(TRACE_ID)
    expect(span?.parentSpanContext?.spanId).toBe(PARENT_SPAN_ID)
    expect(span?.attributes['scadbuddy.outcome']).toBe('ok')
    // The queue it ran on, for the agent dashboard's tool activities by queue (Step 9.9).
    expect(span?.attributes['scadbuddy.task_queue']).toBe('agent-tools')
    expect(headers[0]?.get('traceparent')).toContain(TRACE_ID)
  })
})
