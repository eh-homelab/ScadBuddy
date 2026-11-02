/** What a built-in the session provides answers (builtins/server.ts): text, an error flag, and an image. */
export type BuiltinResult = { text: string; isError?: boolean; image?: { data: string; mimeType: string } }
