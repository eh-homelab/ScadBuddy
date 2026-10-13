import { useCallback, useState } from 'react'
import { api } from '../../api/client'
import { Markdown } from '../../agent/chat/Markdown'
import { useAsync } from '../../lib/useAsync'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

/**
 * #1074 — the template's README, rendered. Its relative images (`thumbnail.png`,
 * `images/…`) load from the template's own files through `Markdown`'s `base`.
 */
export function ReadmeButton({ slug }: { slug: string }) {
  const [open, setOpen] = useState(false)
  // Stable, because `Dialog` refocuses its panel whenever `onClose` changes.
  const close = useCallback(() => setOpen(false), [])

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
      >
        README
      </button>
      <Dialog open={open} title="README" onClose={close} size="wide">
        {open && <Readme slug={slug} />}
      </Dialog>
    </>
  )
}

function Readme({ slug }: { slug: string }) {
  const readme = useAsync(() => api.getReadme(slug), [slug], [`model:${slug}`])
  if (readme.error) {
    return (
      <p role="alert" className="text-[12px] text-warn">
        Could not load the README: {readme.error.message}
      </p>
    )
  }
  if (readme.data === undefined) return <Spinner />
  if (readme.data === null) return <p className="text-[13px] text-muted">This template has no README.</p>
  return (
    <div className="text-[13px] text-ink">
      <Markdown text={readme.data} base={{ slug }} />
    </div>
  )
}
