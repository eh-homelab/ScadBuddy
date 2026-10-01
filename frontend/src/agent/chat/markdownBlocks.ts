/** Block-level parse for `Markdown.tsx`. See there for what is supported and why. */
export type Block =
  | { kind: 'code'; lang: string; body: string }
  | { kind: 'heading'; level: 1 | 2 | 3; text: string }
  | { kind: 'list'; ordered: boolean; items: string[] }
  | { kind: 'para'; text: string }
  | { kind: 'table'; align: Align[]; header: string[]; rows: string[][] }

export type Align = 'left' | 'center' | 'right' | null

const DELIMITER_CELL = /^\s*(:?)-+(:?)\s*$/

/** A GFM table row's cells: edge pipes dropped, split on unescaped `|`, `\|` unescaped. */
function cells(line: string): string[] {
  let row = line.trim()
  if (row.startsWith('|')) row = row.slice(1)
  if (row.endsWith('|') && !row.endsWith('\\|')) row = row.slice(0, -1)
  return row.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, '|'))
}

/** The delimiter row's alignments, or null when `line` is not one for `width` columns. */
function delimiter(line: string | undefined, width: number): Align[] | null {
  if (line === undefined || !line.includes('|')) return null
  const parts = cells(line)
  if (parts.length !== width) return null
  const align: Align[] = []
  for (const part of parts) {
    const m = DELIMITER_CELL.exec(part)
    if (!m) return null
    align.push(m[1] && m[2] ? 'center' : m[2] ? 'right' : m[1] ? 'left' : null)
  }
  return align
}

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
    if (line.includes('|')) {
      const header = cells(line)
      const align = delimiter(lines[i + 1], header.length)
      if (align) {
        flush()
        const rows: string[][] = []
        i += 2
        while (i < lines.length && (lines[i] ?? '').includes('|') && (lines[i] ?? '').trim() !== '') {
          const row = cells(lines[i++] ?? '')
          rows.push(header.map((_, c) => row[c] ?? ''))
        }
        i--
        out.push({ kind: 'table', align, header, rows })
        continue
      }
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
