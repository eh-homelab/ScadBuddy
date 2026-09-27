import { useRef, useState, type DragEvent } from 'react'
import { api } from '../../api/client'
import type { Asset, Param } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Field } from './Field'

const ASSET_ID = /^[0-9a-f]{64}$/

const PICKER_ACCEPT: Record<string, string> = {
  svg: '.svg,image/svg+xml',
  png: '.png,image/png',
}

/**
 * #204 — a `// file:svg,png` parameter. The value is an uploaded asset's id (the
 * SHA-256 the server stored it under), never a file name or a path: the render stages
 * the file beside the model under a name of its own. The original name is only shown.
 *
 * The empty string means no file. Any other non-id value is a file that ships beside
 * the model: one of its `samples` (offered as a row of thumbnails under the drop
 * zone, so a viewer can pick one without downloading and re-uploading it), or the
 * model's own default.
 */
export function FileWidget({
  param,
  value,
  slug,
  version,
  onChange,
}: {
  param: Param
  value: string
  slug: string
  /** The revision being customized: its samples are read as they were then. */
  version?: string
  onChange: (next: string) => void
}) {
  const id = `p-${param.name}`
  const label = param.caption ?? param.name
  const accept = param.accept ?? []
  const samples = param.samples ?? []
  const input = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string>()
  const [dragging, setDragging] = useState(false)
  // What an upload already answered, so the widget does not fetch it straight back.
  const [known, setKnown] = useState<Record<string, Asset>>({})

  const isAsset = ASSET_ID.test(value)
  const lookup = useAsync(
    () => (isAsset && !known[value] ? api.getAsset(slug, value) : Promise.resolve(undefined)),
    [slug, value, isAsset],
  )
  const asset = isAsset ? (known[value] ?? lookup.data) : undefined
  const isSample = !isAsset && samples.includes(value)
  const previewUrl = isAsset
    ? api.assetContentUrl(slug, value)
    : isSample
      ? api.sampleContentUrl(slug, value, version)
      : undefined

  async function upload(file: File): Promise<void> {
    setError(undefined)
    setUploading(true)
    try {
      const stored = await api.uploadAsset(slug, file)
      if (!accept.includes(stored.kind)) {
        setError(`${label} takes ${accept.join(' or ').toUpperCase()}, not ${stored.kind.toUpperCase()}`)
        return
      }
      setKnown((current) => ({ ...current, [stored.id]: stored }))
      onChange(stored.id)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setUploading(false)
    }
  }

  function onDrop(event: DragEvent<HTMLDivElement>): void {
    event.preventDefault()
    setDragging(false)
    const file = event.dataTransfer.files[0]
    if (file) void upload(file)
  }

  return (
    <Field
      id={id}
      label={label}
      name={param.name}
      readout={<span className="uppercase">{accept.join(' · ')}</span>}
    >
      <div
        data-testid={`drop-${param.name}`}
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={`flex items-center gap-2.5 rounded-[6px] border border-dashed p-2 transition-colors ${
          dragging ? 'border-accent bg-accent/10' : 'border-line bg-surface-2/60'
        }`}
      >
        <div className="grid size-12 shrink-0 place-items-center overflow-hidden rounded-[4px] border border-line bg-[repeating-conic-gradient(var(--color-surface-3)_0_25%,var(--color-surface-2)_0_50%)] bg-[length:12px_12px]">
          {previewUrl ? (
            <img
              src={previewUrl}
              alt={isSample ? `Preview of ${value}` : asset ? `Preview of ${asset.name}` : 'Preview'}
              className="size-full object-contain"
            />
          ) : (
            <span aria-hidden className="text-[10px] text-faint">
              {accept.join('/').toUpperCase()}
            </span>
          )}
        </div>

        <div className="min-w-0 flex-1 text-[12px]">
          {uploading ? (
            <span className="text-muted">Uploading…</span>
          ) : isAsset ? (
            <>
              <span className="block truncate text-ink" title={asset?.name}>
                {asset?.name ?? (lookup.error ? 'Uploaded file (unavailable)' : 'Uploaded file')}
              </span>
              {asset && (
                <span className="sb-num text-faint">
                  {asset.kind.toUpperCase()}
                  {asset.width && asset.height ? ` · ${asset.width}×${asset.height}` : ''}
                </span>
              )}
            </>
          ) : isSample ? (
            <>
              <span className="block truncate text-ink" title={value}>
                {value}
              </span>
              <span className="text-faint">
                {value === param.initial ? 'Model default · template sample' : 'Template sample'}
              </span>
            </>
          ) : value ? (
            <span className="block truncate text-muted" title={value}>
              Model default: {value}
            </span>
          ) : (
            <span className="text-muted">Drop a file here</span>
          )}
        </div>

        <input
          ref={input}
          id={id}
          type="file"
          className="sr-only"
          accept={accept.map((kind) => PICKER_ACCEPT[kind] ?? `.${kind}`).join(',')}
          onChange={(event) => {
            const file = event.target.files?.[0]
            event.target.value = ''
            if (file) void upload(file)
          }}
        />
        <Button size="sm" disabled={uploading} onClick={() => input.current?.click()}>
          Choose…
        </Button>
        {value !== '' && (
          <Button
            size="sm"
            variant="ghost"
            aria-label={`Clear ${label}`}
            disabled={uploading}
            onClick={() => {
              setError(undefined)
              onChange('')
            }}
          >
            Clear
          </Button>
        )}
      </div>
      {samples.length > 0 && (
        <div
          role="group"
          aria-labelledby={`${id}-samples`}
          data-testid={`samples-${param.name}`}
          className="mt-1.5"
        >
          <span id={`${id}-samples`} className="mb-1 block text-[11px] text-faint">
            Samples
          </span>
          <div className="flex flex-wrap gap-1.5">
            {samples.map((name) => {
              const chosen = name === value
              return (
                <button
                  key={name}
                  type="button"
                  aria-pressed={chosen}
                  aria-label={`Use sample ${name}`}
                  title={name}
                  disabled={uploading}
                  onClick={() => {
                    setError(undefined)
                    onChange(name)
                  }}
                  className={`flex w-[72px] flex-col items-center gap-1 rounded-[6px] border p-1 text-[10px] transition-colors disabled:opacity-50 ${
                    chosen
                      ? 'border-accent bg-accent/10 text-ink'
                      : 'border-line bg-surface-2/60 text-muted hover:border-accent/60'
                  }`}
                >
                  <span className="grid size-10 place-items-center overflow-hidden rounded-[4px] bg-[repeating-conic-gradient(var(--color-surface-3)_0_25%,var(--color-surface-2)_0_50%)] bg-[length:10px_10px]">
                    <img
                      src={api.sampleContentUrl(slug, name, version)}
                      alt=""
                      loading="lazy"
                      className="size-full object-contain"
                    />
                  </span>
                  <span className="w-full truncate text-center">{name}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-1.5 text-[12px] text-warn">
          {error}
        </p>
      )}
    </Field>
  )
}
