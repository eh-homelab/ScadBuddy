import { Context } from '@temporalio/activity'
import { ApplicationFailure } from '@temporalio/common'
import { DURABLE_ONLY_NAMES } from '../tools/answerTools.js'
import { effectiveTool, type Risk, type Tool, type ToolServices } from '../tools/registry.js'
import { sessionOf } from './toolActivities.js'

// `tool_tier` (#2171): a durable session asks, before each call of one of ScadBuddy's own
// tools, the tool's tier and disabled flag as Settings has them now (`effectiveTool`, the
// read every other gate makes). It decides nothing: the call's own activity resolves the
// tool again as it runs. `flow_tool_tier` (flowTools.ts) is the same question for a flow.

export const TOOL_TIER_ACTIVITY = 'tool_tier'

/** `tool_tier`: `{tool}` to `{tier, disabled}`, for a `session-<uuid>` workflow only. */
export function toolTierActivity(
  tools: readonly Tool[],
  services: Pick<ToolServices, 'toolOverrides'>,
): (input: unknown) => Promise<{ tier: Risk; disabled: boolean }> {
  const byName = new Map(tools.map((t) => [t.name, t]))
  return async (input: unknown) => {
    const workflowId = Context.current().info.workflowExecution?.workflowId
    if (sessionOf(workflowId) === undefined) {
      throw ApplicationFailure.nonRetryable(`${TOOL_TIER_ACTIVITY} answers only a durable session; ${workflowId ?? 'this workflow'} is none`, 'UnknownSession')
    }
    const name = (input as { tool?: unknown } | null | undefined)?.tool
    if (typeof name !== 'string') throw ApplicationFailure.nonRetryable(`${TOOL_TIER_ACTIVITY} takes {tool: string}`, 'ToolError')
    const tool = byName.get(name)
    if (tool === undefined || DURABLE_ONLY_NAMES.has(name)) {
      throw ApplicationFailure.nonRetryable(`${name} is not one of ScadBuddy's own tools`, 'ToolError')
    }
    const { risk, disabled } = await effectiveTool(tool, services)
    return { tier: risk, disabled }
  }
}
