// agent/test/telemetry.turn.test.ts
import type { SDKResultMessage } from '@anthropic-ai/claude-agent-sdk'
import { ROOT_CONTEXT, SpanStatusCode, trace, TraceFlags } from '@opentelemetry/api'
import { beforeEach, describe, expect, it } from 'vitest'
import { PROTOCOL_VERSION } from '../src/sessions/protocol.js'
import { TurnTrace } from '../src/telemetry/turn.js'
import { toolContextFor, tracer, traceparentOf } from '../src/telemetry/trace.js'
import { exportedText, flushTracing, PARENT_SPAN_ID, resetTracing, TRACE_ID, testTracing } from './support/tracing.js'

// The turn's segments (spec 2026-10-01 §5.4): a park ends the open segment and
// the parked tool span at once; each decision is its own trace; the next
// segment is `agent.turn.resume`, the last decision's child. Spans export
// asynchronously (test/support/tracing.ts), so each read of the exporter
// follows an `await flushTracing()`.

const spans = testTracing()
beforeEach(resetTracing)
const SENTINEL = 's3ntinel-9e5b'

const named = (name: string) => spans.getFinishedSpans().filter((s) => s.name === name)
const one = (name: string) => {
  const all = named(name)
  expect(all, name).toHaveLength(1)
  return all[0]!
}
const segment = (n: number) =>
  [...named('agent.turn'), ...named('agent.turn.resume')].find((s) => s.attributes['scadbuddy.segment'] === n)!
const success = { kind: 'result', subtype: 'success', costUsd: 0.02, turns: 2 } as const

function turn(parent = ROOT_CONTEXT): TurnTrace {
  return new TurnTrace({ sessionId: 's-1', turnId: 't-1', parent, tierOf: () => 'outward' })
}

/** A decision span as ApprovalService.settle makes one, and the traceparent the row stores. */
function decision() {
  const span = tracer().startSpan('agent.approval', { root: true })
  span.end()
  return { traceparent: traceparentOf(span)!, spanId: span.spanContext().spanId, traceId: span.spanContext().traceId }
}
const approved = (id: string, decisionTraceparent: string | null) => ({ id, decision: 'approved' as const, decisionTraceparent })
const execution = (toolUseId: string) => trace.getSpan(toolContextFor({ _meta: { 'claudecode/toolUseId': toolUseId } }))!.spanContext()

describe('TurnTrace', async () => {
  it('a parked call ends its tool span and the turn at once, outcome parked', async () => {
    const t = turn()
    t.toolStarted('toolu_1', 'mcp__scadbuddy__print')
    const park = t.park('toolu_1', 'mcp__scadbuddy__print')
    park.parked('appr-1')
    await flushTracing()
    const tool = one('agent.tool/mcp__scadbuddy__print')
    const seg0 = one('agent.turn')
    expect(tool.attributes).toMatchObject({
      'scadbuddy.outcome': 'parked',
      'scadbuddy.approval_id': 'appr-1',
      'scadbuddy.tier': 'outward',
      'scadbuddy.tool': 'mcp__scadbuddy__print',
    })
    expect(seg0.attributes).toMatchObject({
      'scadbuddy.outcome': 'parked',
      'scadbuddy.approval_id': 'appr-1',
      'scadbuddy.segment': 0,
      'scadbuddy.turn_id': 't-1',
      'scadbuddy.session_id': 's-1',
      'scadbuddy.tool_calls': 1,
    })
    expect(tool.parentSpanContext?.spanId).toBe(seg0.spanContext().spanId)
    expect(park.traceparent).toBe(`00-${tool.spanContext().traceId}-${tool.spanContext().spanId}-01`)
  })

  it('is the browser’s child when started under its context', async () => {
    const parent = trace.setSpanContext(ROOT_CONTEXT, { traceId: TRACE_ID, spanId: PARENT_SPAN_ID, traceFlags: TraceFlags.SAMPLED, isRemote: true })
    turn(parent).finish(success)
    await flushTracing()
    const seg0 = one('agent.turn')
    expect(seg0.spanContext().traceId).toBe(TRACE_ID)
    expect(seg0.parentSpanContext?.spanId).toBe(PARENT_SPAN_ID)
    expect(seg0.attributes).toMatchObject({ 'scadbuddy.outcome': 'success', 'scadbuddy.cost_usd': 0.02, 'scadbuddy.turns': 2 })
  })

  it('the decision starts agent.turn.resume, and the approved call runs inside it', async () => {
    const t = turn()
    const park = t.park('toolu_1', 'x')
    park.parked('a1')
    const d = decision()
    park.decided(approved('a1', d.traceparent), true)
    const exec = execution('toolu_1')
    t.toolEnded('toolu_1', true)
    t.finish(success, { total_cost_usd: 0.02, num_turns: 2, usage: { input_tokens: 10, output_tokens: 3 } } as unknown as SDKResultMessage)
    await flushTracing()
    const resume = one('agent.turn.resume')
    expect(resume.parentSpanContext?.spanId).toBe(d.spanId)
    expect(resume.spanContext().traceId).toBe(d.traceId)
    expect(resume.links).toEqual([])
    expect(resume.attributes).toMatchObject({
      'scadbuddy.segment': 1,
      'scadbuddy.outcome': 'success',
      'scadbuddy.turn_id': 't-1',
      'scadbuddy.input_tokens': 10,
      'scadbuddy.output_tokens': 3,
    })
    const ran = named('agent.tool/x').find((s) => s.spanContext().spanId === exec.spanId)!
    expect(ran.parentSpanContext?.spanId).toBe(resume.spanContext().spanId)
    expect(ran.attributes).toMatchObject({ 'scadbuddy.outcome': 'ok', 'scadbuddy.approval_id': 'a1' })
  })

  it('a turn that parks twice yields segments 0, 1 and 2, each ending as the next begins', async () => {
    const t = turn()
    const p1 = t.park('toolu_1', 'x')
    p1.parked('a1')
    const d1 = decision()
    p1.decided(approved('a1', d1.traceparent), true)
    t.toolEnded('toolu_1', true)
    await flushTracing()
    expect(named('agent.turn.resume')).toHaveLength(0)
    const p2 = t.park('toolu_2', 'x')
    p2.parked('a2')
    await flushTracing()
    expect(segment(1).attributes).toMatchObject({ 'scadbuddy.outcome': 'parked', 'scadbuddy.approval_id': 'a2' })
    const d2 = decision()
    p2.decided(approved('a2', d2.traceparent), true)
    t.toolEnded('toolu_2', true)
    t.finish(success)
    await flushTracing()
    expect([0, 1, 2].map((n) => segment(n).attributes['scadbuddy.turn_id'])).toEqual(['t-1', 't-1', 't-1'])
    expect(segment(1).parentSpanContext?.spanId).toBe(d1.spanId)
    expect(segment(2).parentSpanContext?.spanId).toBe(d2.spanId)
    expect(segment(2).name).toBe('agent.turn.resume')
    expect(segment(2).attributes['scadbuddy.outcome']).toBe('success')
  })

  it('two calls parked at once end the segment once; the resume is the last decision’s child, linked to the other', async () => {
    const t = turn()
    t.toolStarted('toolu_r', 'mcp__scadbuddy__list_models')
    const pa = t.park('toolu_a', 'x')
    const pb = t.park('toolu_b', 'x')
    pa.parked('a')
    pb.parked('b')
    await flushTracing()
    expect(named('agent.turn')).toHaveLength(1)
    expect(named('agent.tool/x')).toHaveLength(2)
    expect(named('agent.tool/mcp__scadbuddy__list_models')).toHaveLength(0)
    const da = decision()
    pa.decided(approved('a', da.traceparent), true)
    await flushTracing()
    expect(named('agent.turn.resume')).toHaveLength(0)
    const execA = execution('toolu_a')
    t.toolEnded('toolu_a', true)
    const db = decision()
    pb.decided(approved('b', db.traceparent), true)
    t.toolEnded('toolu_b', true)
    t.toolEnded('toolu_r', true)
    t.finish(success)
    await flushTracing()
    const ranA = named('agent.tool/x').find((s) => s.spanContext().spanId === execA.spanId)!
    expect(ranA.parentSpanContext?.spanId).toBe(da.spanId)
    const resume = one('agent.turn.resume')
    expect(resume.parentSpanContext?.spanId).toBe(db.spanId)
    expect(resume.links.map((l) => l.context.spanId)).toEqual([da.spanId])
    expect(one('agent.tool/mcp__scadbuddy__list_models').parentSpanContext?.spanId).toBe(one('agent.turn').spanContext().spanId)
  })

  it('an expired or denied call opens the resume but runs nothing', async () => {
    const t = turn()
    const park = t.park('toolu_1', 'x')
    park.parked('a1')
    const d = decision()
    park.decided({ id: 'a1', decision: 'expired', decisionTraceparent: d.traceparent }, false)
    t.finish(success)
    await flushTracing()
    expect(named('agent.tool/x')).toHaveLength(1)
    expect(one('agent.turn.resume').parentSpanContext?.spanId).toBe(d.spanId)
  })

  it('a decision with no trace context starts the resume as a root', async () => {
    const t = turn()
    const park = t.park('toolu_1', 'x')
    park.parked('a1')
    park.decided(approved('a1', null), true)
    t.finish(success)
    await flushTracing()
    const resume = one('agent.turn.resume')
    expect(resume.parentSpanContext).toBeUndefined()
    expect(resume.links).toEqual([])
  })

  it('an interrupt while parked ends nothing twice, and ends an open sibling as unfinished', async () => {
    const t = turn()
    t.toolStarted('toolu_s', 'y')
    const park = t.park('toolu_1', 'x')
    park.parked('a1')
    t.finish({ kind: 'interrupted' })
    await flushTracing()
    expect(one('agent.turn').attributes['scadbuddy.outcome']).toBe('parked')
    expect(one('agent.tool/x').attributes['scadbuddy.outcome']).toBe('parked')
    expect(one('agent.tool/y').attributes['scadbuddy.outcome']).toBe('unfinished')
  })

  it('under an unsampled parent nothing is stored on the row', async () => {
    const parent = trace.setSpanContext(ROOT_CONTEXT, { traceId: TRACE_ID, spanId: PARENT_SPAN_ID, traceFlags: TraceFlags.NONE, isRemote: true })
    const park = turn(parent).park('toolu_1', 'x')
    expect(park.traceparent).toBeUndefined()
    park.parked('a1')
    await flushTracing()
    expect(spans.getFinishedSpans()).toEqual([])
  })

  it('a failed turn records the class and nothing of the message', async () => {
    const t = turn()
    t.fail(new TypeError(`could not start: ${SENTINEL}`))
    t.finish({ kind: 'failed', message: SENTINEL })
    await flushTracing()
    const seg0 = one('agent.turn')
    expect(seg0.status.code).toBe(SpanStatusCode.ERROR)
    expect(seg0.attributes).toMatchObject({ 'scadbuddy.outcome': 'failed', 'scadbuddy.failure_class': 'TypeError' })
    expect(exportedText(spans)).not.toContain(SENTINEL)
  })

  it('its hooks and events start and end tool spans, and never record an input or a result', async () => {
    const t = turn()
    const hooks = t.hooks()
    const signal = new AbortController().signal
    const base = { session_id: 's', transcript_path: '', cwd: '' }
    await hooks.PreToolUse![0]!.hooks[0]!(
      { ...base, hook_event_name: 'PreToolUse', tool_name: 'x', tool_input: { job: SENTINEL }, tool_use_id: 'toolu_h' },
      'toolu_h',
      { signal },
    )
    await hooks.PostToolUse![0]!.hooks[0]!(
      { ...base, hook_event_name: 'PostToolUse', tool_name: 'x', tool_input: { job: SENTINEL }, tool_response: SENTINEL, tool_use_id: 'toolu_h' },
      'toolu_h',
      { signal },
    )
    t.observe({ v: PROTOCOL_VERSION, type: 'tool.call', sessionId: 's-1', id: 'toolu_e', name: 'z', input: { job: SENTINEL }, risk: 'read' })
    t.observe({ v: PROTOCOL_VERSION, type: 'tool.result', sessionId: 's-1', id: 'toolu_e', ok: false, summary: SENTINEL })
    t.finish(success)
    await flushTracing()
    expect(one('agent.tool/x').attributes['scadbuddy.outcome']).toBe('ok')
    expect(one('agent.tool/z').attributes['scadbuddy.outcome']).toBe('error')
    expect(one('agent.tool/z').status.code).toBe(SpanStatusCode.ERROR)
    expect(exportedText(spans)).not.toContain(SENTINEL)
  })
  it('only a failed outcome makes the turn an error: a failure before an interrupt is not one', async () => {
    const t = turn()
    t.fail(new TypeError(SENTINEL))
    t.finish({ kind: 'interrupted' })
    const failedWithoutError = turn()
    failedWithoutError.finish({ kind: 'failed', message: SENTINEL })
    const errored = turn()
    errored.finish({ ...success, subtype: 'error_max_turns' })
    await flushTracing()
    const [interrupted, failed, maxTurns] = named('agent.turn')
    expect(interrupted!.status.code).toBe(SpanStatusCode.UNSET)
    expect(interrupted!.attributes).toMatchObject({ 'scadbuddy.outcome': 'interrupted' })
    expect(interrupted!.attributes['scadbuddy.failure_class']).toBeUndefined()
    expect(interrupted!.events).toEqual([])
    expect(failed!.status.code).toBe(SpanStatusCode.ERROR)
    expect(failed!.attributes).toMatchObject({ 'scadbuddy.outcome': 'failed' })
    expect(maxTurns!.status.code).toBe(SpanStatusCode.UNSET)
    expect(maxTurns!.attributes).toMatchObject({ 'scadbuddy.outcome': 'error_max_turns' })
    expect(exportedText(spans)).not.toContain(SENTINEL)
  })

  it('an ended tool span is no longer bound under its tool_use id', () => {
    const t = turn()
    t.toolStarted('toolu_1', 'x')
    expect(execution('toolu_1').spanId).not.toBe(trace.getSpan(t.context())!.spanContext().spanId)
    t.toolEnded('toolu_1', true)
    expect(trace.getSpan(toolContextFor({ _meta: { 'claudecode/toolUseId': 'toolu_1' } }))).toBeUndefined()
    t.toolStarted('toolu_2', 'x')
    t.finish(success)
    expect(trace.getSpan(toolContextFor({ _meta: { 'claudecode/toolUseId': 'toolu_2' } }))).toBeUndefined()
  })

  it('after finish nothing opens: a late decision, hook or park leaves no span unended', async () => {
    const t = turn()
    const park = t.park('toolu_1', 'x')
    park.parked('a1')
    t.finish({ kind: 'interrupted' })
    park.decided({ id: 'a1', decision: 'cancelled', decisionTraceparent: decision().traceparent }, false)
    t.toolStarted('toolu_2', 'late')
    const latePark = t.park('toolu_3', 'late')
    expect(latePark.traceparent).toBeUndefined()
    latePark.parked('a3')
    latePark.decided(approved('a3', null), true)
    await flushTracing()
    expect(named('agent.turn.resume')).toHaveLength(0)
    expect(named('agent.tool/late')).toHaveLength(0)
    expect(trace.getSpan(t.context())!.isRecording()).toBe(false)
  })
})
