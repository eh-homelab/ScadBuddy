// The declared shapes of a question (harness/questions.ts re-exports them), free of Node
// and SDK imports so the session workflow's bundle can use the validator and the entry.

/** Claude Code's own limits (sdk-tools.d.ts `AskUserQuestionInput`), and caps on what the panel shows. */
export const QUESTIONS_MAX = 4
export const OPTIONS_MIN = 2
export const OPTIONS_MAX = 4
export const QUESTION_TEXT_MAX = 2_000
export const PREVIEW_MAX = 20_000
/** The longest answer the user may type ("Other", or an edited draft). */
export const ANSWER_MAX = 20_000

/** The UTF-8 length of a string (a lone surrogate is 3 bytes, as Buffer encodes it); a workflow has no Buffer. */
export function utf8Length(text: string): number {
  let bytes = 0
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i)
    if (c < 0x80) bytes += 1
    else if (c < 0x800) bytes += 2
    else if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length && (text.charCodeAt(i + 1) & 0xfc00) === 0xdc00) {
      bytes += 4
      i++
    } else bytes += 3
  }
  return bytes
}
