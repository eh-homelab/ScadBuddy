import { z } from 'zod'
import type { SettingsRepo } from '../credentials.js'
import { TOOL_SEARCH_MODES, type ToolSearchMode } from '../harness/toolSearch.js'

// What a session turn's Claude Code is spawned with, set in Settings → Assistant → Runtime
// beside the runtime settings (routes/sessionRuntime.ts) and stored in `ai_settings` under
// these names. Unlike those, each one is read as a turn is assembled (turnRunner.ts) and is a
// fingerprint input of the warm process (`FingerprintParts`), so a change recycles a warm
// process at its next turn, and a write tells every cache to evict (warmEvict.ts).
//
//   tool_search           off | auto | always (#5221, harness/toolSearch.ts)
//   system_prompt_base    claude_code: Claude Code's preset, ScadBuddy's text appended; minimal:
//                         the SDK's own one-line default, ScadBuddy's text after it (#5222)
//   system_prompt_append  the user's own text, after ScadBuddy's fixed part (turnShared.ts
//                         `systemAppend`), which always goes in

export type SystemPromptBase = 'claude_code' | 'minimal'

export type TurnSettings = {
  tool_search: ToolSearchMode
  system_prompt_base: SystemPromptBase
  system_prompt_append: string
}

/** The most text `system_prompt_append` holds: a page or two of instructions, not a document. */
export const SYSTEM_PROMPT_APPEND_MAX = 20_000

export const TURN_DEFAULTS: TurnSettings = {
  tool_search: 'auto',
  system_prompt_base: 'claude_code',
  system_prompt_append: '',
}

const SCHEMAS = {
  tool_search: z.enum(TOOL_SEARCH_MODES as [ToolSearchMode, ...ToolSearchMode[]]),
  system_prompt_base: z.enum(['claude_code', 'minimal']),
  system_prompt_append: z.string().max(SYSTEM_PROMPT_APPEND_MAX),
} satisfies { [K in keyof TurnSettings]: z.ZodType<TurnSettings[K]> }

export const TURN_KEYS = Object.keys(TURN_DEFAULTS) as (keyof TurnSettings)[]

/** A partial body: only the keys present are written. */
export const TurnPutShape = {
  tool_search: SCHEMAS.tool_search.optional(),
  system_prompt_base: SCHEMAS.system_prompt_base.optional(),
  system_prompt_append: SCHEMAS.system_prompt_append.optional(),
}

export const isTurnKey = (key: string): key is keyof TurnSettings => (TURN_KEYS as string[]).includes(key)

/** Each setting as stored; the default for a missing one or one that does not parse (written by hand). */
export async function readTurnSettings(settings: Pick<SettingsRepo, 'get'>): Promise<TurnSettings> {
  const values = await Promise.all(TURN_KEYS.map((key) => settings.get<unknown>(key)))
  const out: Record<string, unknown> = { ...TURN_DEFAULTS }
  TURN_KEYS.forEach((key, i) => {
    const parsed = SCHEMAS[key].safeParse(values[i])
    if (parsed.success) out[key] = parsed.data
  })
  return out as TurnSettings
}
