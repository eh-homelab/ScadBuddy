import type { ReactNode } from 'react'
import { type ImageBase, safeHttpUrl, safeImageSrc } from '../../lib/safeUrl'
import { parseBlocks } from './markdownBlocks'

/**
 * A deliberately small Markdown renderer for assistant replies: paragraphs, `#`–`###`
 * headings, `-`/`*`/`1.` lists, fenced code, GFM tables (#820), and inline `code`,
 * **bold**, *italic*, [links](https://…) and ![images](/api/v1/…, data:image/…). The repo has no
 * Markdown renderer to reuse and a full one is a lot of bundle for chat text.
 *
 * It builds React elements — never HTML strings — so model output cannot inject
 * markup, links render only for http(s) URLs (`safeHttpUrl`), and images only for
 * ScadBuddy's own API paths and inline `data:` images (`safeImageSrc`), so a render
 * view the agent checked can be shown while untrusted text never makes the browser
 * fetch another host; any other image is its alt text. Given a `base` (the model a
 * README belongs to, #951), a relative image such as `thumbnail.png` resolves to that
 * model's image route; without one (agent chat) it stays alt text. It tolerates the
 * half-finished text of a stream: an unclosed fence is code to the end, and an
 * unclosed `**` is plain text until its partner arrives.
 */
export function Markdown({ text, base }: { text: string; base?: ImageBase }) {
  return <div className="space-y-2 break-words">{blocks(text, base)}</div>
}

function blocks(text: string, base: ImageBase | undefined): ReactNode[] {
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
            {inline(block.text, base)}
          </Tag>
        )
      }
      case 'list': {
        const Tag = block.ordered ? 'ol' : 'ul'
        return (
          <Tag key={i} className={`space-y-0.5 pl-5 ${block.ordered ? 'list-decimal' : 'list-disc'}`}>
            {block.items.map((item, j) => (
              <li key={j}>{inline(item, base)}</li>
            ))}
          </Tag>
        )
      }
      case 'para':
        return <p key={i}>{inline(block.text, base)}</p>
      case 'table':
        return (
          <div key={i} className="overflow-x-auto">
            <table className="border-collapse text-[12px]">
              <thead>
                <tr>
                  {block.header.map((cell, c) => (
                    <th
                      scope="col"
                      key={c}
                      style={{ textAlign: block.align[c] ?? undefined }}
                      className="border border-line bg-surface-3 px-2 py-1 font-semibold"
                    >
                      {inline(cell, base)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, r) => (
                  <tr key={r}>
                    {row.map((cell, c) => (
                      <td key={c} style={{ textAlign: block.align[c] ?? undefined }} className="border border-line px-2 py-1">
                        {inline(cell, base)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
    }
  })
}

// One alternation, leftmost match wins: code first so `**` inside backticks stays literal,
// and an image's `!` starts one character before the `[` a link would match.
const INLINE =
  /`([^`]+)`|\*\*([^*]+)\*\*|\*([^*\s][^*]*)\*|_([^_\s][^_]*)_|\[([^\]]+)\]\(([^)\s]+)\)|!\[([^\]]*)\]\(([^)\s]+)\)/g

function inline(text: string, base: ImageBase | undefined): ReactNode[] {
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
      out.push(<strong key={key}>{inline(m[2], base)}</strong>)
    } else if (m[3] !== undefined || m[4] !== undefined) {
      out.push(<em key={key}>{inline(m[3] ?? m[4] ?? '', base)}</em>)
    } else if (m[8] !== undefined) {
      const src = safeImageSrc(m[8], base)
      out.push(
        src ? (
          <img
            key={key}
            src={src}
            alt={m[7] ?? ''}
            loading="lazy"
            className="inline-block max-h-80 max-w-full rounded-[6px] border border-line bg-bg align-middle"
          />
        ) : (
          <span key={key}>{m[7]}</span>
        ),
      )
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
