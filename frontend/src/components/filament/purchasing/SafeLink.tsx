import { webUrl } from './webUrl'

/** A stored address as a link that opens in a new tab, or as plain text when it is not `http(s)`; a dash for none. */
export function SafeLink({ url }: { url: string | null | undefined }) {
  if (!url) return <span className="text-muted-foreground">—</span>
  const href = webUrl(url)
  if (!href) return <span className="break-all">{url}</span>
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="break-all underline-offset-4 hover:underline">
      {href.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '')}
    </a>
  )
}
