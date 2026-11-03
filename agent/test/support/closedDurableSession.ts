import { proxyActivities } from '@temporalio/workflow'

// A stand-in for a DurableSession that ran and closed (agent-durable's Python workflow on the
// temporalio-claude-agent-sdk plugin, retired in plan Step 8.1): its history holds what
// recoverDurable.ts reads, each model segment a `run_claude_segment` activity (its input a
// SegmentInput, its result a SegmentOutput) and each tool call an activity `tool-<tool_use_id>`.
// The activities of test/recoverDurable.temporal.test.ts answer with what each step names.

/** One step: a model segment, its input and the output it returns, or a tool call and its result. */
export type Step =
  | { segment: { input: Record<string, unknown>; output: Record<string, unknown> } }
  | { tool: { name: string; id: string; input: Record<string, unknown>; result: unknown } }

export async function DurableSession(steps: Step[]): Promise<void> {
  for (const step of steps) {
    if ('segment' in step) {
      const { run_claude_segment } = proxyActivities<{ run_claude_segment(input: unknown): Promise<unknown> }>({
        startToCloseTimeout: '1 minute',
        retry: { maximumAttempts: 1 },
      })
      await run_claude_segment({ ...step.segment.input, reply: step.segment.output })
    } else {
      const tools = proxyActivities<Record<string, (input: unknown) => Promise<unknown>>>({
        startToCloseTimeout: '1 minute',
        retry: { maximumAttempts: 1 },
        activityId: `tool-${step.tool.id}`,
      })
      await tools[step.tool.name]!({ ...step.tool.input, reply: step.tool.result })
    }
  }
}
