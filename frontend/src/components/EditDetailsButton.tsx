import { useCallback, useRef, useState } from 'react'
import { ApiError, api } from '../api/client'
import type { ModelPatch, ModelSummary } from '../api/types'
import { isMarkdown, isPng } from '../lib/modelFolder'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  slug: string
  /** Called with the record after the last change landed. */
  onSaved?: (model: ModelSummary) => void
}

/** What the form opened on, so a save sends only what changed. */
interface Baseline {
  model: ModelSummary
  readme: string
}

const FIELD =
  'w-full rounded-[6px] border border-line bg-surface-2 px-2.5 py-1.5 text-[13px] text-ink outline-none focus:border-accent'

function parseTags(text: string): string[] {
  return text
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean)
}

/**
 * What a save does to the README. Text left as it was is nothing. Otherwise blank
 * or whitespace-only text is never written as a file: it removes the README a model
 * has, and is nothing on one without.
 */
function readmeChange(
  text: string,
  saved: string,
  hasReadme: boolean,
): 'set' | 'remove' | 'none' {
  // Untouched is untouched, even when what is stored is itself only whitespace.
  if (text === saved) return 'none'
  if (text.trim() === '') return hasReadme ? 'remove' : 'none'
  return text !== saved ? 'set' : 'none'
}

const sameTags = (a: string[], b: string[]) =>
  a.length === b.length && a.every((tag, index) => tag === b[index])

/**
 * #179 — name, description, tags, thumbnail and README, after the model exists. Each
 * change the server accepts is its own revision in the model's history, so a save
 * sends only the parts that differ from what the form opened on.
 */
export function EditDetailsButton({ slug, onSaved }: Props) {
  const [open, setOpen] = useState(false)
  const [baseline, setBaseline] = useState<Baseline | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [tags, setTags] = useState('')
  const [readme, setReadme] = useState('')
  const [thumbnail, setThumbnail] = useState<File | null>(null)
  const [removeThumbnail, setRemoveThumbnail] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function show() {
    setOpen(true)
    setBaseline(null)
    setLoadError(null)
    setError(null)
    setThumbnail(null)
    setRemoveThumbnail(false)
    try {
      const model = await api.getModel(slug)
      const text = model.has_readme ? ((await api.getReadme(slug)) ?? '') : ''
      setBaseline({ model, readme: text })
      setName(model.name)
      setDescription(model.description ?? '')
      setTags((model.tags ?? []).join(', '))
      setReadme(text)
    } catch (caught) {
      setLoadError(caught instanceof ApiError ? caught.detail : String(caught))
    }
  }

  // Stable, because `Dialog` refocuses its panel whenever `onClose` changes, and a
  // new function every render would pull focus out of the field being typed in.
  const savingRef = useRef(false)
  const close = useCallback(() => {
    if (!savingRef.current) setOpen(false)
  }, [])

  function chooseThumbnail(file: File | undefined) {
    if (!file) return
    if (!isPng(file)) {
      setError('The thumbnail must be a PNG.')
      return
    }
    setError(null)
    setThumbnail(file)
    setRemoveThumbnail(false)
  }

  async function loadReadme(file: File | undefined) {
    if (!file) return
    if (!isMarkdown(file)) {
      setError('The README must be a Markdown (.md) file.')
      return
    }
    setError(null)
    setReadme(await file.text())
  }

  async function save() {
    if (!baseline) return
    savingRef.current = true
    setSaving(true)
    setError(null)
    // Sequential, and the baseline advances after each step: when one fails, the
    // steps before it have landed, and a retry must not send them a second time.
    let current = baseline
    let record = baseline.model
    try {
      const patch: ModelPatch = {}
      if (name.trim() !== record.name) patch.name = name.trim()
      if (description !== (record.description ?? '')) patch.description = description
      const nextTags = parseTags(tags)
      if (!sameTags(nextTags, record.tags ?? [])) patch.tags = nextTags
      if (Object.keys(patch).length > 0) {
        record = await api.updateModel(slug, patch)
        current = { ...current, model: record }
        setBaseline(current)
      }

      if (thumbnail) {
        record = await api.setThumbnail(slug, thumbnail)
        current = { ...current, model: record }
        setBaseline(current)
        setThumbnail(null)
      } else if (removeThumbnail) {
        record = await api.removeThumbnail(slug)
        current = { ...current, model: record }
        setBaseline(current)
        setRemoveThumbnail(false)
      }

      // A README that is blank or only whitespace is never saved as a file: on a
      // model that has one it is the removal the form says it is (`readmeChange`),
      // and on one that has none there is nothing to do.
      const change = readmeChange(readme, current.readme, record.has_readme)
      if (change === 'set') {
        record = await api.setReadme(slug, readme)
        current = { model: record, readme }
        setBaseline(current)
      } else if (change === 'remove') {
        record = await api.removeReadme(slug)
        current = { model: record, readme: '' }
        setBaseline(current)
      }

      onSaved?.(record)
      setOpen(false)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
    } finally {
      savingRef.current = false
      setSaving(false)
    }
  }

  const model = baseline?.model
  const ownThumbnail = model?.thumbnail_source === 'model' && !removeThumbnail
  const readmeBlank = readme.trim() === ''
  const pendingReadme = baseline
    ? readmeChange(readme, baseline.readme, baseline.model.has_readme)
    : 'none'
  const nameMissing = name.trim() === ''

  return (
    <>
      <button
        type="button"
        onClick={() => void show()}
        className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
      >
        Edit details
      </button>
      <Dialog
        open={open}
        title="Edit details"
        description="Each change is saved as a revision in the model's history."
        onClose={close}
        footer={
          <>
            <Button variant="ghost" onClick={close} disabled={saving}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void save()}
              disabled={!model || saving || nameMissing}
            >
              {saving && <Spinner />}
              {saving ? 'Saving' : 'Save'}
            </Button>
          </>
        }
      >
        {!model && !loadError && (
          <p className="flex items-center gap-2 text-[13px] text-muted">
            <Spinner /> Loading details
          </p>
        )}
        {loadError && (
          <p role="alert" className="text-[13px] text-warn">
            Could not load the model: {loadError}
          </p>
        )}
        {model && (
          <div className="flex flex-col gap-3 text-[13px]">
            <label className="flex flex-col gap-1">
              <span className="text-muted">Name</span>
              <input className={FIELD} value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted">Description</span>
              <textarea
                className={FIELD}
                rows={3}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted">Tags</span>
              <input
                className={FIELD}
                value={tags}
                placeholder="comma, separated"
                onChange={(e) => setTags(e.target.value)}
              />
            </label>

            <fieldset className="flex flex-col gap-1.5">
              <legend className="mb-1 text-muted">Thumbnail</legend>
              <p className="text-[12px] text-faint" data-testid="thumbnail-state">
                {thumbnail
                  ? `${thumbnail.name} replaces the current one on save.`
                  : removeThumbnail
                    ? 'Removed on save. The first generated plate stands in, if there is one.'
                    : model.thumbnail_source === 'model'
                      ? 'Set on this model.'
                      : model.thumbnail_source === 'output'
                        ? 'None set; the first generated plate stands in.'
                        : 'None set. The first generated plate will stand in.'}
              </p>
              <div className="flex items-center gap-2">
                <label className="inline-flex cursor-pointer items-center rounded-[6px] border border-line bg-surface-2 px-2.5 py-1 text-[13px] hover:border-line-strong">
                  {ownThumbnail || thumbnail ? 'Replace PNG' : 'Choose PNG'}
                  <input
                    type="file"
                    accept=".png,image/png"
                    className="sr-only"
                    aria-label="Thumbnail (PNG)"
                    onChange={(event) => {
                      chooseThumbnail(event.target.files?.[0])
                      event.target.value = ''
                    }}
                  />
                </label>
                {(ownThumbnail || thumbnail) && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      if (thumbnail) setThumbnail(null)
                      else setRemoveThumbnail(true)
                    }}
                  >
                    {thumbnail ? 'Keep current' : 'Remove thumbnail'}
                  </Button>
                )}
              </div>
            </fieldset>

            <label className="flex flex-col gap-1">
              <span className="text-muted">README</span>
              <textarea
                className={`${FIELD} sb-num min-h-40 font-mono text-[12px]`}
                rows={8}
                value={readme}
                placeholder="Markdown. Leave empty for no README."
                onChange={(e) => setReadme(e.target.value)}
              />
            </label>
            {pendingReadme === 'remove' && (
              <p role="status" data-testid="readme-state" className="text-[12px] text-warn">
                Saving will remove the README.
              </p>
            )}
            {readmeBlank && readme !== '' && !model.has_readme && (
              <p role="status" data-testid="readme-state" className="text-[12px] text-faint">
                Only whitespace, so no README is saved.
              </p>
            )}
            {model.has_readme && readme !== '' && (
              <Button size="sm" variant="ghost" className="w-fit" onClick={() => setReadme('')}>
                Remove README
              </Button>
            )}
            <label className="inline-flex w-fit cursor-pointer items-center rounded-[6px] border border-line bg-surface-2 px-2.5 py-1 text-[13px] hover:border-line-strong">
              Load README from file
              <input
                type="file"
                accept=".md,.markdown,text/markdown"
                className="sr-only"
                aria-label="README file"
                onChange={(event) => {
                  void loadReadme(event.target.files?.[0])
                  event.target.value = ''
                }}
              />
            </label>
          </div>
        )}
        {error && (
          <p role="alert" className="mt-3 text-[13px] text-warn">
            {error}
          </p>
        )}
      </Dialog>
    </>
  )
}
