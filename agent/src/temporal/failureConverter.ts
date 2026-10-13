import { DefaultFailureConverter } from '@temporalio/common'

// The agent-tools worker's failure converter (spec 2026-10-01 §6.5, plan 5c Ruling 11):
// a failure's message and stack trace go into an encoded payload, so the payload codec
// seals them for a durable subject like any other payload. Without it, a tool's error
// text (which can quote what the tool returned) would sit in a session's history, and
// in Archival, as plaintext after forgetSubject. Loaded by path, as the SDK requires,
// by the worker (main.ts) and the workflow bundle (scripts/bundle-workflows.mjs).
export const failureConverter = new DefaultFailureConverter({ encodeCommonAttributes: true })
