// A leaf module: builtins/server.ts and temporal/turnTools.ts both read it, and
// server.ts importing turnTools.ts would close an import cycle through tools/index.ts.

/** The key a call's `_meta` carries its tool_use id under, to a server whose `callIds` asks for it (#3201). */
export const CALL_ID_META = 'scadbuddy/toolUseId'
