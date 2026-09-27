import type { ReactNode } from 'react'
import { safeHttpUrl } from '../../lib/safeUrl'
import { parseBlocks } from './markdownBlocks'

/**
 * A deliberately small Markdown renderer for assistant replies: paragraphs, `#`–`###`
 * headings, `-`/`*`/`1.` lists, fenced code, and inline `code`, **bold**, *italic*
 * and [links](https://…). The repo has no Markdown renderer to reuse and a full one
 * is a lot of bundle for chat text.
 *
 * It builds React elements — never HTML strings — so model output cannot inject
 * markup, and links render only for http(s) URLs (`safeHttpUrl`). It tolerates the
 * half-finished text of a stream: an unclosed fence is code to the end, and an
 * unclosed `**` is plain text until its partner arrives.
 */
export function Markdown({ text }: { text: string }) {
  return <div className="space-y-2 break-words">{blocks(text)}</div>
}

function blocks(text: string): ReactNode[] {
  return parseBlocks(text).map((block, i) => {
    switch (block.kind) {
      case 'code':
        return (
          <pre
            key={i}
            className="overflow-x-auto rounded-[6px] border border-line bg-bg px-2.5 py-2 font-mono text-[12px]"
          >
            <code data-lang={block.lang || undefined}>{block.body}</code>
          </pre>
        )
      case 'heading': {
        const Tag = (['h3', 'h4', 'h5'] as const)[block.level - 1] ?? 'h5'
        return (
          <Tag key={i} className="text-[13px] font-semibold">
            {inline(block.text)}
          </Tag>
        )
      }
      case 'list': {
        const Tag = block.ordered ? 'ol' : 'ul'
        return (
          <Tag key={i} className={`space-y-0.5 pl-5 ${block.ordered ? 'list-decimal' : 'list-disc'}`}>
            {block.items.map((item, j) => (
              <li key={j}>{inline(item)}</li>
            ))}
          </Tag>
        )
      }
      case 'para':
        return <p key={i}>{inline(block.text)}</p>
    }
  })
}

// One alternation, leftmost match wins: code first so `**` inside backticks stays literal.
const INLINE = /`([^`]+)`|\*\*([^*]+)\*\*|\*([^*\s][^*]*)\*|_([^_\s][^_]*)_|\[([^\]]+)\]\(([^)\s]+)\)/g

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  for (const m of text.matchAll(INLINE)) {
    const at = m.index
    if (at > last) out.push(text.slice(last, at))
    const key = `${at}`
    if (m[1] !== undefined) {
      out.push(
        <code key={key} className="rounded bg-surface-3 px-1 font-mono text-[12px]">
          {m[1]}
        </code>,
      )
    } else if (m[2] !== undefined) {
      out.push(<strong key={key}>{inline(m[2])}</strong>)
    } else if (m[3] !== undefined || m[4] !== undefined) {
      out.push(<em key={key}>{inline(m[3] ?? m[4] ?? '')}</em>)
    } else {
      const href = safeHttpUrl(m[6])
      out.push(
        href ? (
          <a key={key} href={href} target="_blank" rel="noreferrer noopener" className="text-accent underline">
            {m[5]}
          </a>
        ) : (
          <span key={key}>{m[5]}</span>
        ),
      )
    }
    last = at + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}
