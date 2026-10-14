import { redact } from '../secrets.js'

// Claude Code's stderr arrives in arbitrary chunks, so a secret can be split
// across two of them and survive a per-chunk replace. This buffers to line
// boundaries and redacts whole lines with the shared `redact()`.
//
// A line longer than `maxBuffer` is emitted in pieces so a runaway line cannot
// grow the buffer without bound. The cut is placed so no occurrence of a
// secret, and no prefix of one at the end, is split: the last
// (longest secret - 1) characters are always held back.

export type LineRedactor = {
  write(chunk: string): void
  /** Emits what is left (a last line without a newline). Idempotent. */
  flush(): void
}

export function lineRedactor(
  secrets: readonly (string | undefined)[],
  sink: (line: string) => void,
  maxBuffer = 64 * 1024,
): LineRedactor {
  const known = secrets.filter((s): s is string => typeof s === 'string' && s.length >= 4)
  const holdBack = Math.max(0, ...known.map((s) => s.length - 1))
  let buffer = ''

  const emit = (text: string) => {
    if (text) sink(redact(text, known))
  }

  return {
    write(chunk) {
      buffer += chunk
      let newline = buffer.indexOf('\n')
      while (newline !== -1) {
        emit(buffer.slice(0, newline + 1))
        buffer = buffer.slice(newline + 1)
        newline = buffer.indexOf('\n')
      }
      if (buffer.length > maxBuffer) {
        let cut = buffer.length - holdBack
        // Move the cut back to the start of any secret it would split.
        for (const secret of known) {
          let at = buffer.indexOf(secret)
          while (at !== -1) {
            if (at < cut && at + secret.length > cut) cut = at
            at = buffer.indexOf(secret, at + 1)
          }
        }
        emit(buffer.slice(0, cut))
        buffer = buffer.slice(cut)
      }
    },
    flush() {
      const rest = buffer
      buffer = ''
      emit(rest)
    },
  }
}
