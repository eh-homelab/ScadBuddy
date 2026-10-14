import { Context } from '@temporalio/activity'
import { ApplicationFailure } from '@temporalio/common'
import { DURABLE_ONLY_NAMES } from '../tools/answerTools.js'
import { effectiveTool, type Risk, type Tool, type ToolServices } from '../tools/registry.js'
import { flowOf } from './flowRuns.js'

// A flow's tools and their tiers (#2704), as the agent's own turns resolve them: each
// tool's definition with Settings' raised tiers and disabled tools applied, read when
// asked (`effectiveTool`), never from a file built beside another image. Two ways in:
//
//   - `flow_tool_tier`, an activity on `agent-tools`: before a flow's `tool(name, args)`
//     may run or park, ProjectWorkflow (backend/scadbuddy/workflows/flow_tools.py) asks
//     it for the tool's tier now, as it asks `turn_tool_tier` for a turn tool's;
//   - `GET /internal/flows/tools` (routes/flowTools.ts), on the pod's loopback: the API
//     beside this sidecar checks a grant's tools and lists the editor's with it.
//
// Both answer from the one function below. Neither decides a call: the call's own
// activity resolves the tool again as it runs (toolActivities.ts `runAsActivity`).

export const FLOW_TOOL_TIER_ACTIVITY = 'flow_tool_tier'

/** A tool a flow may call: none of a session's answer tools, and no browser tool (a flow has no tab). */
export function forFlows(tool: Tool): boolean {
  return !DURABLE_ONLY_NAMES.has(tool.name) && !tool.name.startsWith('browser_')
}

export type FlowToolTier = { name: string; tier: Risk; disabled: boolean }

async function tierOf(tool: Tool, services: Pick<ToolServices, 'toolOverrides'>): Promise<FlowToolTier> {
  const effective = await effectiveTool(tool, services)
  return { name: tool.name, tier: effective.risk, disabled: effective.disabled }
}

/** Every tool a flow may call, at its effective tier now. */
export async function flowToolTiers(tools: readonly Tool[], services: Pick<ToolServices, 'toolOverrides'>): Promise<FlowToolTier[]> {
  return Promise.all(tools.filter(forFlows).map((tool) => tierOf(tool, services)))
}

/** `flow_tool_tier`: `{tool}` to `{tier, disabled}`, for a `flow-<run id>` workflow only. */
export function flowToolTierActivity(
  tools: readonly Tool[],
  services: Pick<ToolServices, 'toolOverrides'>,
): (input: unknown) => Promise<{ tier: Risk; disabled: boolean }> {
  const byName = new Map(tools.map((t) => [t.name, t]))
  return async (input: unknown) => {
    const workflowId = Context.current().info.workflowExecution?.workflowId
    if (flowOf(workflowId) === undefined) {
      throw ApplicationFailure.nonRetryable(`${FLOW_TOOL_TIER_ACTIVITY} answers only a flow run; ${workflowId ?? 'this workflow'} is none`, 'UnknownFlow')
    }
    const name = (input as { tool?: unknown } | null | undefined)?.tool
    if (typeof name !== 'string') throw ApplicationFailure.nonRetryable(`${FLOW_TOOL_TIER_ACTIVITY} takes {tool: string}`, 'ToolError')
    const tool = byName.get(name)
    if (tool === undefined) throw ApplicationFailure.nonRetryable(`the agent has no tool ${name}`, 'ToolError')
    if (!forFlows(tool)) {
      throw ApplicationFailure.nonRetryable(`${name} is not for flows: a flow has no session and no browser tab`, 'NotForFlows')
    }
    const { tier, disabled } = await tierOf(tool, services)
    return { tier, disabled }
  }
}
