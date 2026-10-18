import { api } from '../../api/client'
import { Markdown } from '../../agent/chat/Markdown'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'

/**
 * #1074 — the template's README, rendered. Its relative images (`thumbnail.png`,
 * `images/…`) load from the template's own files through `Markdown`'s `base`. Those
 * are the current revision's: the readme route takes no revision, so a README opened
 * beside an older version still shows today's images.
 */
export function ReadmeButton({ slug }: { slug: string }) {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button type="button" variant="ghost" size="sm">
          README
        </Button>
      </DialogTrigger>
      {/* Wide, for a README's images; the body scrolls under the title. */}
      <DialogContent className="flex max-h-[calc(100dvh-2rem)] flex-col sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle>README</DialogTitle>
        </DialogHeader>
        <ScrollArea className="-mx-4 min-h-0 flex-1 px-4">
          <Readme slug={slug} />
        </ScrollArea>
      </DialogContent>
    </Dialog>
  )
}

function Readme({ slug }: { slug: string }) {
  const readme = useAsync(() => api.getReadme(slug), [slug], [`model:${slug}`])
  if (readme.error) {
    return (
      <p role="alert" className="text-xs text-destructive">
        Could not load the README: {readme.error.message}
      </p>
    )
  }
  if (readme.data === undefined) {
    return (
      <div role="status" aria-label="Loading the README" className="flex flex-col gap-2">
        <Skeleton className="h-6 w-1/2" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-5/6" />
      </div>
    )
  }
  if (readme.data === null) return <p className="text-sm text-muted-foreground">This template has no README.</p>
  return (
    <div className="text-sm">
      <Markdown text={readme.data} base={{ slug }} />
    </div>
  )
}
