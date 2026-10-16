// A leaf module: fallback.ts sends it, and memory/hindsight.ts leaves it out of what it
// retains (#3701); hindsight.ts importing fallback.ts would close an import cycle.

/** Sent as the next credential's prompt when it resumes a turn another one started. */
export const CONTINUE_PROMPT =
  'The request to the model failed and was sent again with another Claude credential. Continue where you left off.'
