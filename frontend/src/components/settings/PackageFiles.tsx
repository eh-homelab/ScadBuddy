import { useState } from 'react'
import { Markdown } from '../../agent/chat/Markdown'
import { aiPlugins, type PackageFilesOf, type PackageReview } from '../../api/aiPlugins'
import { formatBytes } from '../../lib/format'
import { highlight, languageOf, TOKEN_CLASS } from '../../lib/highlight'
import { useAsync } from '../../lib/useAsync'
import { CheckIcon, ChevronRightIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { ScrollArea } from '@/components/ui/scroll-area'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'
import { LoadingLines } from './form'

/**
 * A plugin package's files as the admin reads them before approving it (#1029): the
 * review's "Files to read" (its Markdown and JSON) first, then every other file, each
 * with its size, opened one at a time in a read-only viewer with Previous and Next, and
 * a count of how many have been opened. Contents come from the agent
 * (`GET /api/v1/ai/plugin-packages/:name/file`), which reads only the files of the pin
 * under review (`of.pending`: the re-pin's). A binary file is a placeholder; a long one
 * is cut until "Show all", unless it is too large to send whole (#2151). Markdown is
 * shown raw (what the assistant reads) or rendered.
 */
export function PackageFiles({ review, of }: { review: PackageReview; of: PackageFilesOf }) {
  const [open, setOpen] = useState(false)
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="inline-flex min-h-6 cursor-pointer items-center gap-1 text-muted-foreground hover:text-foreground">
        <ChevronRightIcon aria-hidden="true" className={cn('size-3.5 transition-transform', open && 'rotate-90')} />
        Files to read ({review.files.length})
      </CollapsibleTrigger>
      <CollapsibleContent>
        <FileBrowser review={review} of={of} />
      </CollapsibleContent>
    </Collapsible>
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
        <li key={path} aria-label={path} className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="xs"
            onClick={() => select(path)}
            aria-current={path === selected ? 'true' : undefined}
            className="sb-num h-auto min-h-6 justify-start text-left font-normal break-all whitespace-normal aria-[current=true]:bg-muted aria-[current=true]:text-primary"
          >
            {path}
          </Button>
          {sizes.has(path) && (
            <span className="sb-num shrink-0 text-muted-foreground">{formatBytes(sizes.get(path)!)}</span>
          )}
          {seen.has(path) && (
            <span className="shrink-0 text-ok" role="img" aria-label="opened">
              <CheckIcon aria-hidden="true" className="size-3.5" />
            </span>
          )}
        </li>
      ))}
    </ul>
  )

  return (
    <div className="mt-1 space-y-2">
      <p className="text-muted-foreground">
        Read {[...seen].filter((p) => order.includes(p)).length} of {order.length} files
      </p>
      <ScrollArea className="max-h-64 rounded-lg ring-1 ring-foreground/10 [&>[data-slot=scroll-area-viewport]]:max-h-64">
        <div className="p-1.5">
          {files(`Files to read in ${of.name}`, toRead)}
          {list.loading && <LoadingLines label="Listing the files" lines={['w-1/2', 'w-1/3']} className="mt-1 px-1" />}
      {list.error && (
        <p role="alert" className="text-warn">
          The file list is unavailable: {list.error.message}
        </p>
      )}
          {others.length > 0 && (
            <div className="mt-1.5">
              <p className="px-1 text-muted-foreground">Other files</p>
              {files(`Other files in ${of.name}`, others)}
            </div>
          )}
        </div>
      </ScrollArea>
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
    <Card size="sm" className="gap-0 py-0">
    <section aria-label={`${of.name}: ${path}`}>
      <header className="flex flex-wrap items-center gap-2 border-b px-2.5 py-1.5">
        <span className="sb-num grow break-all font-medium">{path}</span>
        {data && (
          <span className="sb-num text-muted-foreground">
            {formatBytes(data.size)} · {data.media_type}
          </span>
        )}
        {!data && size !== undefined && <span className="sb-num text-muted-foreground">{formatBytes(size)}</span>}
        {language === 'markdown' && content !== null && (
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            spacing={0}
            value={rendered ? 'rendered' : 'raw'}
            onValueChange={(next) => next && setRendered(next === 'rendered')}
            aria-label="Show the file"
          >
            <ToggleGroupItem value="raw">Raw</ToggleGroupItem>
            <ToggleGroupItem value="rendered">Rendered</ToggleGroupItem>
          </ToggleGroup>
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
      <div className="p-2.5">
        {file.loading && <LoadingLines label="Reading the file" lines={['w-full', 'w-5/6', 'w-2/3']} />}
        {file.error && (
          <p role="alert" className="text-warn">
            {file.error.message}
          </p>
        )}
        {data?.binary && (
          <p className="text-muted-foreground">
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
          <div className="mt-2 flex flex-wrap items-center gap-2 text-muted-foreground">
            <p>
              Showing the first {formatBytes(new TextEncoder().encode(content).length)} of {formatBytes(data.size)}.
              {data.too_large && ' Too large to show whole.'}
            </p>
            {!data.too_large && (
              <Button size="sm" variant="outline" onClick={() => setFull(true)}>
                Show all
              </Button>
            )}
          </div>
        )}
      </div>
    </section>
    </Card>
  )
}

/** The frontmatter (the skill's settings) as text, then the body rendered. */
function RenderedMarkdown({ text }: { text: string }) {
  const front = FRONTMATTER.exec(text)?.[0]
  return (
    <div className="max-h-96 space-y-2 overflow-auto text-[13px]">
      {front && (
        <pre className="rounded-md bg-muted px-2 py-1 font-mono text-[12px] whitespace-pre-wrap text-muted-foreground">
          {front}
        </pre>
      )}
      <Markdown text={front ? text.slice(front.length) : text} />
    </div>
  )
}
