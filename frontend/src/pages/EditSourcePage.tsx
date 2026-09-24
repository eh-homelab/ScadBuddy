import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { api } from '../api/client'
import { SourceWorkbench } from '../components/SourceWorkbench'
import { Spinner } from '../components/ui/Spinner'
import { useAsync } from '../lib/useAsync'

/** The same editor as "New model", prefilled. Saving overwrites the source in place. */
export function EditSourcePage() {
  const { slug = '' } = useParams()
  const loaded = useAsync(() => api.getSource(slug), [slug])
  const [source, setSource] = useState<string | null>(null)
  const navigate = useNavigate()

  useEffect(() => {
    if (loaded.data !== undefined) setSource(loaded.data)
  }, [loaded.data])

  async function save(force: boolean) {
    await api.replaceSource(slug, source ?? '', force)
    await navigate(`/m/${slug}`)
  }

  if (loaded.loading || (source === null && !loaded.error)) {
    return (
      <p className="flex h-full items-center justify-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading the source
      </p>
    )
  }

  if (loaded.error || source === null) {
    return (
      <div role="alert" className="mx-auto max-w-lg px-4 py-16 text-center">
        <h1 className="text-[15px] font-medium">That source is not here</h1>
        <p className="mt-2 text-[13px] text-muted">{loaded.error?.message}</p>
        <Link to="/" className="mt-4 inline-block text-[13px] text-accent underline">
          Back to models
        </Link>
      </div>
    )
  }

  return (
    <SourceWorkbench
      breadcrumb={
        <>
          <Link to={`/m/${slug}`} className="shrink-0 text-[12px] text-muted hover:text-ink">
            {slug}
          </Link>
          <span className="text-faint">/</span>
          <h1 className="truncate text-[13px] font-medium">Edit source</h1>
        </>
      }
      source={source}
      onSourceChange={setSource}
      saveLabel="Save source"
      canSave
      onSave={save}
    />
  )
}
