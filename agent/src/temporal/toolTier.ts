import { Context } from '@temporalio/activity'
import { ApplicationFailure } from '@temporalio/common'
import type { ToolAccess } from '../access/model.js'
import type { AccessResolver } from '../access/resolver.js'
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
    if (tool === undefined) {
      throw ApplicationFailure.nonRetryable(`${name} is not one of ScadBuddy's own tools`, 'ToolError')
    }
    const { risk, disabled } = await effectiveTool(tool, services)
    return { tier: risk, disabled }
  }
}

/**
 * `builtin_tier` (#4757): a durable session asks, before an engine-parked built-in's call
 * (Bash, PowerShell), its access across `packages`, the listing's packages that name it, as
 * a classic turn's seam reads it per call (access/live.ts): resolver.ts `builtin`, the
 * strictest of each package's `builtin:<tool>`, an unset one `outward`. The caller names
 * the packages, so both runtimes ask about the same set. Without a resolver, `outward`.
 * The caller never passes an empty `packages` for a package's built-in (workflow.py
 * `_builtin_tier` asks only about a built-in a turn package names): `resolver.builtin`
 * answers an empty list `outward` without reading anything, which classic never asks.
 */
export const BUILTIN_TIER_ACTIVITY = 'builtin_tier'

export function builtinTier(deps: { access?: Pick<AccessResolver, 'builtin'> | undefined }): (args: { tool: string; packages: string[] }) => Promise<{ tier: ToolAccess }> {
  return async (args) => {
    const { tool, packages } = (args ?? {}) as { tool?: unknown; packages?: unknown }
    if (typeof tool !== 'string' || !Array.isArray(packages) || !packages.every((p): p is string => typeof p === 'string')) {
      throw ApplicationFailure.nonRetryable(`${BUILTIN_TIER_ACTIVITY} takes {tool: string, packages: string[]}`, 'ToolError')
    }
    return { tier: deps.access ? await deps.access.builtin(tool, packages) : 'outward' }
  }
}
