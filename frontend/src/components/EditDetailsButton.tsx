import { useCallback, useRef, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useLatest } from '../lib/useLatest'
import { useSubscription } from '../lib/realtime'
import type { ModelPatch, ModelSummary } from '../api/types'
import {
  MAX_THUMBNAIL_SIZE,
  isMarkdown,
  readmeProblem,
  thumbnailProblem,
} from '../lib/modelFolder'
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
  return 'set'
}

const sameTags = (a: string[], b: string[]) =>
  a.length === b.length && a.every((tag, index) => tag === b[index])

/**
 * #179 — name, description, tags, thumbnail and README, after the model exists. Each
 * change the server accepts is its own revision in the model's history, so a save
 * sends only the parts that differ from what the form opened on.
 */
/** Whether the fields this form edits are the same in both. */
function sameDetails(a: Baseline, b: Baseline) {
  return (
    a.model.name === b.model.name &&
    (a.model.description ?? '') === (b.model.description ?? '') &&
    (a.model.tags ?? []).join('\n') === (b.model.tags ?? []).join('\n') &&
    a.model.has_thumbnail === b.model.has_thumbnail &&
    a.model.thumbnail_source === b.model.thumbnail_source &&
    // Which output or default render it is. An uploaded image replaced by another
    // shows no difference in the record (tracked as a follow-up to #442).
    a.model.thumbnail_output_id === b.model.thumbnail_output_id &&
    a.model.thumbnail_preview_id === b.model.thumbnail_preview_id &&
    a.readme === b.readme
  )
}

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

  // #269 — the details changed elsewhere while this form is open: say so, and let the
  // user load them; never overwrite what is being typed.
  const [changedElsewhere, setChangedElsewhere] = useState(false)
  // `model.updated` also follows pins, source saves and upstream changes, so it only
  // counts when the details this form edits differ from the ones it opened with.
  const opened = useLatest(baseline)
  useSubscription(open && baseline ? `model:${slug}` : undefined, (signal) => {
    if (signal === 'resync' || signal.kind !== 'model.updated' || saving) return
    void (async () => {
      const model = await api.getModel(slug)
      const text = model.has_readme ? ((await api.getReadme(slug)) ?? '') : ''
      const was = opened.current
      if (was && !sameDetails(was, { model, readme: text })) setChangedElsewhere(true)
    })().catch(() => {
      // The form keeps what it shows; the next change reads again.
    })
  })

  /** Bumped by every show(), so a slower earlier load never lands over a later one. */
  const loads = useRef(0)

  async function show() {
    const mine = ++loads.current
    setOpen(true)
    setChangedElsewhere(false)
    setBaseline(null)
    setLoadError(null)
    setError(null)
    setThumbnail(null)
    setRemoveThumbnail(false)
    try {
      const model = await api.getModel(slug)
      const text = model.has_readme ? ((await api.getReadme(slug)) ?? '') : ''
      if (loads.current !== mine) return
      setBaseline({ model, readme: text })
      setName(model.name)
      setDescription(model.description ?? '')
      setTags((model.tags ?? []).join(', '))
      setReadme(text)
    } catch (caught) {
      if (loads.current !== mine) return
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
    const problem = thumbnailProblem(file)
    if (problem) {
      setError(problem)
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
  // Checked here as the server checks it, so a README it would refuse never goes
  // up -- and nor, with Save disabled, do the other fields' changes beside it.
  const readmeTooLong = readmeProblem(readme)

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
              disabled={!model || saving || nameMissing || readmeTooLong !== null}
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
        {changedElsewhere && (
          <div
            role="status"
            className="mb-3 flex items-center gap-3 rounded-[6px] border border-accent/40 bg-accent/8 px-3 py-2 text-[12px]"
          >
            <span>These details were changed elsewhere since you opened them.</span>
            <Button size="sm" onClick={() => void show()}>
              Load the latest
            </Button>
          </div>
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
                    ? 'Removed on save. A generated plate, or else a render of the default settings, stands in.'
                    : model.thumbnail_source === 'model'
                      ? 'Set on this model.'
                      : model.thumbnail_source === 'output'
                        ? 'None set; the first generated plate stands in.'
                        : model.thumbnail_source === 'preview'
                          ? 'None set; a render of the default settings stands in.'
                          : 'None set. A render of the default settings stands in once it is ready; a generated plate takes precedence.'}
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
                <span className="text-[12px] text-faint">PNG, up to {MAX_THUMBNAIL_SIZE}</span>
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
            {readmeTooLong && (
              <p role="alert" data-testid="readme-limit" className="text-[12px] text-warn">
                {readmeTooLong}
              </p>
            )}
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
