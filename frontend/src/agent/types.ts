import type * as z from 'zod'
import type { TOOLS } from './catalog'

/**
 * The approval tiers of the AI design spec (`docs/superpowers/specs/2026-09-27-ai-integration-design.md`
 * §8.1): `read`; `write`, reversible through history; and `outward` (send, print, delete,
 * settings or credential writes), which always needs a human approval (§8.2). In the tab,
 * an outward tool stops at the confirmation dialog — only the user can confirm it.
 */
export type Risk = 'read' | 'write' | 'outward'

/**
 * Which part of the app provides a tool. A tool is only callable while something in its
 * scope is mounted; calling it anywhere else answers `unavailable`, never silence.
 */
export type Scope = 'global' | 'catalogue' | 'customize' | 'source' | 'settings'

export type ToolName = keyof typeof TOOLS

/** A tool's arguments, as its schema parses them (defaults applied). */
export type ToolArgs<N extends ToolName> = z.output<(typeof TOOLS)[N]['input']>

export type ToolImpl<N extends ToolName> = (args: ToolArgs<N>) => unknown

export type ToolImpls = { [N in ToolName]?: ToolImpl<N> }

export type ErrorCode =
  /** No tool by that name exists at all. */
  | 'unknown_tool'
  /** The tool exists, but nothing that provides it is mounted on this route. */
  | 'unavailable'
  /** The arguments do not match the tool's schema, or the page rejected them. */
  | 'invalid_args'
  /** The page will not do this for an agent (an outward confirmation, a credential). */
  | 'refused'
  /** Waited for the page to settle and it did not. */
  | 'timeout'
  /** The tool ran and failed. */
  | 'failed'

export interface ToolError {
  code: ErrorCode
  message: string
  /** Schema issues, one per offending path, for `invalid_args`. */
  issues?: { path: string; message: string }[]
}

export type CallResult = { ok: true; result: unknown } | { ok: false; error: ToolError }

/** A handler throws this to answer with a specific error code rather than `failed`. */
export class AgentToolError extends Error {
  readonly code: ErrorCode

  constructor(code: ErrorCode, message: string) {
    super(message)
    this.name = 'AgentToolError'
    this.code = code
  }
}

/** One tool as the agent sees it: what `listTools()` and WebMCP publish. */
export interface ToolInfo {
  name: ToolName
  description: string
  risk: Risk
  scope: Scope
  /** JSON Schema for the arguments. */
  inputSchema: Record<string, unknown>
  /** Whether something that provides it is mounted right now. */
  live: boolean
}
