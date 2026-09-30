import { safeHttpUrl } from '../lib/safeUrl'

/**
 * "From {host}", linking an imported template back to where it came from, on a card
 * and a list row. Only an http(s) origin is linked at all; anything else is not shown (#179).
 */
export function ModelOrigin({ url, className = '' }: { url: string | null | undefined; className?: string }) {
  const origin = safeHttpUrl(url)
  if (!origin) return null
  return (
    <p className={`truncate text-[12px] text-faint ${className}`}>
      From{' '}
      <a
        href={origin}
        target="_blank"
        rel="noreferrer"
        className="text-muted underline decoration-line-strong underline-offset-2 hover:text-ink"
      >
        {hostOf(origin)}
      </a>
    </p>
  )
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname
  } catch {
    return url
  }
}
