import { useState } from 'react'
import { Markdown } from '../../agent/chat/Markdown'
import { aiPlugins, type PackageFilesOf, type PackageReview } from '../../api/aiPlugins'
import { formatBytes } from '../../lib/format'
import { highlight, languageOf, type TokenKind } from '../../lib/highlight'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'

/**
 * A plugin package's files as the admin reads them before approving it (#1029): the
 * review's "Files to read" (its Markdown and JSON) first, then every other file, each
 * with its size, opened one at a time in a read-only viewer with Previous and Next, and
 * a count of how many have been opened. Contents come from the agent
 * (`GET /api/v1/ai/plugin-packages/:name/file`), which reads only the files of the pin
 * under review (`of.pending`: the re-pin's). A binary file is a placeholder; a long one
 * is cut until "Show all". Markdown is shown raw (what the assistant reads) or rendered.
 */
export function PackageFiles({ review, of }: { review: PackageReview; of: PackageFilesOf }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="cursor-pointer text-muted hover:text-ink"
      >
        <span aria-hidden className="mr-1 inline-block w-2">
          {open ? '▾' : '▸'}
        </span>
        Files to read ({review.files.length})
      </button>
      {open && <FileBrowser review={review} of={of} />}
    </div>
  )
}

function FileBrowser({ review, of }: { review: PackageReview; of: PackageFilesOf }) {
  const list = useAsync(() => aiPlugins.packageFiles(of), [of.name, of.pending])
  const [selected, setSelected] = useState<string | null>(null)
  const [seen, setSeen] = useState<ReadonlySet<string>>(new Set())
  const sizes = new Map((list.data?.files ?? []).map((f) => [f.path, f.size]))
  const toRead = review.files
  const others = (list.data?.files ?? []).map((f) => f.path).filter((p) => !toRead.includes(p))
  const order = [...toRead, ...others]
  const at = selected === null ? -1 : order.indexOf(selected)

  const select = (path: string) => {
    setSelected(path)
    setSeen((before) => new Set(before).add(path))
  }

  const files = (label: string, paths: string[]) => (
    <ul className="mt-1 space-y-0.5" aria-label={label}>
      {paths.map((path) => (
        <li key={path} aria-label={path} className="flex items-baseline gap-2">
          <button
            type="button"
            onClick={() => select(path)}
            aria-current={path === selected ? 'true' : undefined}
            className={`sb-num break-all text-left hover:underline ${path === selected ? 'text-accent' : ''}`}
          >
            {path}
          </button>
          {sizes.has(path) && <span className="sb-num shrink-0 text-muted">{formatBytes(sizes.get(path)!)}</span>}
          {seen.has(path) && (
            <span className="shrink-0 text-ok" aria-label="opened">
              ✓
            </span>
          )}
        </li>
      ))}
    </ul>
  )

  return (
    <div className="mt-1 space-y-2">
      <p className="text-muted">
        Read {[...seen].filter((p) => order.includes(p)).length} of {order.length} files
      </p>
      {files(`Files to read in ${of.name}`, toRead)}
      {list.loading && <Spinner />}
      {list.error && (
        <p role="alert" className="text-warn">
          The file list is unavailable: {list.error.message}
        </p>
      )}
      {others.length > 0 && (
        <div>
          <p className="text-muted">Other files</p>
          {files(`Other files in ${of.name}`, others)}
        </div>
      )}
      {selected !== null && (
        <FileViewer
          key={`${of.name}:${of.pending}:${selected}`}
          of={of}
          path={selected}
          size={sizes.get(selected)}
          onPrevious={at > 0 ? () => select(order[at - 1]!) : undefined}
          onNext={at >= 0 && at < order.length - 1 ? () => select(order[at + 1]!) : undefined}
        />
      )}
    </div>
  )
}

const TOKEN_CLASS: Record<TokenKind, string | undefined> = {
  plain: undefined,
  comment: 'text-muted italic',
  string: 'text-ok',
  number: 'text-accent',
  keyword: 'text-accent font-semibold',
  key: 'text-accent',
  heading: 'font-semibold',
  meta: 'text-muted',
  punct: 'text-muted',
}

const FRONTMATTER = /^---\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/

function FileViewer({
  of,
  path,
  size,
  onPrevious,
  onNext,
}: {
  of: PackageFilesOf
  path: string
  size: number | undefined
  onPrevious?: () => void
  onNext?: () => void
}) {
  const [full, setFull] = useState(false)
  const [rendered, setRendered] = useState(false)
  const file = useAsync(() => aiPlugins.packageFile(of, path, full), [of.name, of.pending, path, full])
  const data = file.data
  const language = languageOf(path, data?.media_type)
  const content = data?.content ?? null

  return (
    <section aria-label={`${of.name}: ${path}`} className="rounded-[6px] border border-line">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-2 py-1.5">
        <span className="sb-num grow break-all font-medium">{path}</span>
        {data && (
          <span className="sb-num text-muted">
            {formatBytes(data.size)} · {data.media_type}
          </span>
        )}
        {!data && size !== undefined && <span className="sb-num text-muted">{formatBytes(size)}</span>}
        {language === 'markdown' && content !== null && (
          <span className="flex gap-1">
            <Button size="sm" variant="ghost" aria-pressed={!rendered} onClick={() => setRendered(false)}>
              Raw
            </Button>
            <Button size="sm" variant="ghost" aria-pressed={rendered} onClick={() => setRendered(true)}>
              Rendered
            </Button>
          </span>
        )}
        <span className="flex gap-1">
          <Button size="sm" variant="ghost" onClick={onPrevious} disabled={!onPrevious}>
            Previous file
          </Button>
          <Button size="sm" variant="ghost" onClick={onNext} disabled={!onNext}>
            Next file
          </Button>
        </span>
      </header>
      <div className="p-2">
        {file.loading && <Spinner />}
        {file.error && (
          <p role="alert" className="text-warn">
            {file.error.message}
          </p>
        )}
        {data?.binary && (
          <p className="text-muted">
            Binary file, not shown: {formatBytes(data.size)} · {data.media_type}
          </p>
        )}
        {content !== null &&
          (rendered && language === 'markdown' ? (
            <RenderedMarkdown text={content} />
          ) : (
            <pre
              data-testid="file-content"
              className="max-h-96 overflow-auto font-mono text-[12px] leading-snug whitespace-pre-wrap break-words"
            >
              {highlight(content, language).map((token, i) => (
                <span key={i} data-token={token.kind} className={TOKEN_CLASS[token.kind]}>
                  {token.text}
                </span>
              ))}
            </pre>
          ))}
        {data?.truncated && content !== null && (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-muted">
            <p>
              Showing the first {formatBytes(new TextEncoder().encode(content).length)} of {formatBytes(data.size)}.
            </p>
            <Button size="sm" onClick={() => setFull(true)}>
              Show all
            </Button>
          </div>
        )}
      </div>
    </section>
  )
}

/** The frontmatter (the skill's settings) as text, then the body rendered. */
function RenderedMarkdown({ text }: { text: string }) {
  const front = FRONTMATTER.exec(text)?.[0]
  return (
    <div className="max-h-96 space-y-2 overflow-auto text-[13px]">
      {front && (
        <pre className="rounded-[6px] border border-line bg-bg px-2 py-1 font-mono text-[12px] text-muted whitespace-pre-wrap">
          {front}
        </pre>
      )}
      <Markdown text={front ? text.slice(front.length) : text} />
    </div>
  )
}
