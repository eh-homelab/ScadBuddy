/** Block-level parse for `Markdown.tsx`. See there for what is supported and why. */
export type Block =
  | { kind: 'code'; lang: string; body: string }
  | { kind: 'heading'; level: 1 | 2 | 3; text: string }
  | { kind: 'list'; ordered: boolean; items: string[] }
  | { kind: 'para'; text: string }

export function parseBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, '\n').split('\n')
  const out: Block[] = []
  let para: string[] = []
  const flush = () => {
    if (para.length) out.push({ kind: 'para', text: para.join(' ') })
    para = []
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? ''
    const fence = /^```\s*([\w+-]*)\s*$/.exec(line)
    if (fence) {
      flush()
      const body: string[] = []
      i++
      // An unclosed fence (mid-stream) runs to the end of the text.
      while (i < lines.length && !/^```\s*$/.test(lines[i] ?? '')) body.push(lines[i++] ?? '')
      out.push({ kind: 'code', lang: fence[1] ?? '', body: body.join('\n') })
      continue
    }
    const heading = /^(#{1,3})\s+(.*)$/.exec(line)
    if (heading) {
      flush()
      out.push({ kind: 'heading', level: (heading[1]?.length ?? 1) as 1 | 2 | 3, text: heading[2] ?? '' })
      continue
    }
    const bullet = /^\s*(?:[-*]|(\d+)\.)\s+(.*)$/.exec(line)
    if (bullet) {
      flush()
      const ordered = bullet[1] !== undefined
      const last = out[out.length - 1]
      if (last?.kind === 'list' && last.ordered === ordered) last.items.push(bullet[2] ?? '')
      else out.push({ kind: 'list', ordered, items: [bullet[2] ?? ''] })
      continue
    }
    if (line.trim() === '') flush()
    else para.push(line.trim())
  }
  flush()
  return out
}
