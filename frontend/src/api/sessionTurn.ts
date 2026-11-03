// The values the agent takes for each turn setting (agent `sessions/turnSettings.ts`, #5221,
// #5222), copied for the Settings section and the msw mock. agent
// `test/turnSettings.frontend.test.ts` loads this file and fails when the copies drift.

/** agent `harness/toolSearch.ts` TOOL_SEARCH_MODES. */
export const TOOL_SEARCH_MODES = ['off', 'auto', 'always'] as const

/** agent `sessions/turnSettings.ts` SystemPromptBase. */
export const SYSTEM_PROMPT_BASES = ['claude_code', 'minimal'] as const

/** agent `sessions/turnSettings.ts` SYSTEM_PROMPT_APPEND_MAX. */
export const SYSTEM_PROMPT_APPEND_MAX = 20_000
