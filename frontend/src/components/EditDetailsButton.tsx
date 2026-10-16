import { useCallback, useRef, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useLatest } from '../lib/useLatest'
import { useSubscription } from '../lib/realtime'
import type { ModelPatch, ModelSummary } from '../api/types'
import { isMarkdown, readmeProblem } from '../lib/modelFolder'
import { addMedia, makeCover, mediaProblem, useUploadLimit } from '../lib/mediaFiles'
import { MediaPicker } from './media/MediaPicker'
import { MEDIA_KEY, mediaIdOf, mediaPickerItems } from './media/pickerItems'
import { Spinner } from './ui/Spinner'
import { FileTextIcon, PencilIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'

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
  const readmeFile = useRef<HTMLInputElement>(null)
  const [baseline, setBaseline] = useState<Baseline | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [tags, setTags] = useState('')
  const [readme, setReadme] = useState('')
  // The media item that becomes the cover on save; the picker sets it.
  const [cover, setCover] = useState<string | null>(null)
  // What an upload was called, for the note: the new item has no caption.
  const [uploadedNames, setUploadedNames] = useState<Record<string, string>>({})
  const [picking, setPicking] = useState(false)
  const uploadLimit = useUploadLimit()
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
    setCover(null)
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

  /**
   * Uploads an image from the picker into the template's media (it is kept there, like
   * any other media item) and makes it the pending cover.
   */
  async function uploadCover(file: File) {
    if (!baseline) return
    const problem = mediaProblem(file, await uploadLimit())
    if (problem) throw new Error(problem)
    const { model: record, id } = await addMedia(slug, file, baseline.model.media ?? [])
    setBaseline((current) => (current ? { ...current, model: record } : current))
    if (id) {
      setUploadedNames((names) => ({ ...names, [id]: file.name }))
      setCover(id)
      setRemoveThumbnail(false)
    }
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

      if (cover) {
        record = (await makeCover(slug, record.media ?? [], cover)) ?? record
        current = { ...current, model: record }
        setBaseline(current)
        setCover(null)
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
  const media = model?.media ?? []
  const coverItems = model ? mediaPickerItems(slug, media, { videos: true }) : []
  const pendingCover = cover ? coverItems.find((item) => mediaIdOf(item) === cover) : undefined
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
      <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={() => void show()}>
        <PencilIcon /> Edit details
      </Button>
      <Dialog open={open} onOpenChange={(next) => !next && close()}>
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Edit details</DialogTitle>
          <DialogDescription>Each change is saved as a revision in the model&apos;s history.</DialogDescription>
        </DialogHeader>
        {!model && !loadError && (
          <div role="status" aria-label="Loading details" className="flex flex-col gap-3">
            <span className="sr-only">Loading details</span>
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-32 w-full" />
          </div>
        )}
        {changedElsewhere && (
          <div
            role="status"
            className="flex items-center gap-3 rounded-lg border border-primary/40 bg-primary/10 px-3 py-2 text-xs"
          >
            <span>These details were changed elsewhere since you opened them.</span>
            <Button size="sm" variant="outline" onClick={() => void show()}>
              Load the latest
            </Button>
          </div>
        )}
        {loadError && (
          <p role="alert" className="text-sm text-destructive">
            Could not load the model: {loadError}
          </p>
        )}
        {model && (
          <div className="flex flex-col gap-4 text-sm">
            <div className="grid gap-2">
              <Label htmlFor="details-name">Name</Label>
              <Input id="details-name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="details-description">Description</Label>
              <Textarea
                id="details-description"
                rows={3}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="details-tags">Tags</Label>
              <Input
                id="details-tags"
                value={tags}
                placeholder="comma, separated"
                onChange={(e) => setTags(e.target.value)}
              />
            </div>

            <fieldset className="flex flex-col gap-1.5">
              <legend className="mb-1 text-sm font-medium">Thumbnail</legend>
              <p className="text-xs text-muted-foreground" data-testid="thumbnail-state">
                {pendingCover
                  ? `${uploadedNames[cover ?? ''] ?? pendingCover.label} becomes the cover on save.`
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
                <Button size="sm" variant="outline" onClick={() => setPicking(true)}>
                  {ownThumbnail || pendingCover ? 'Change…' : 'Choose…'}
                </Button>
                {(ownThumbnail || pendingCover) && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      if (pendingCover) setCover(null)
                      else setRemoveThumbnail(true)
                    }}
                  >
                    {pendingCover ? 'Keep current' : 'Remove thumbnail'}
                  </Button>
                )}
                <span className="text-xs text-muted-foreground">From the template&apos;s media</span>
              </div>
              <MediaPicker
                open={picking}
                title="Choose the thumbnail"
                description="The template's first image is its cover. Pick one, or upload a new one to the template's media."
                onClose={() => setPicking(false)}
                sections={[
                  {
                    title: "Template's images",
                    items: coverItems,
                    empty: 'None yet. Upload one, or save a rendered image from Generate.',
                  },
                ]}
                selected={cover ? `${MEDIA_KEY}${cover}` : ownThumbnail ? coverItems[0]?.key : undefined}
                onPick={(item) => {
                  const id = mediaIdOf(item)
                  if (!id) return
                  // Already the cover: nothing to change, unless a removal was pending.
                  setCover(id === media[0]?.id && ownThumbnail ? null : id)
                  setRemoveThumbnail(false)
                }}
                upload={{
                  accept: 'image/png,image/jpeg,image/webp',
                  hint: 'PNG, JPEG or WebP, up to 10 MB. Added to the template’s media.',
                  onFile: uploadCover,
                }}
              />
            </fieldset>

            <div className="grid gap-2">
              <Label htmlFor="details-readme">README</Label>
              <Textarea
                id="details-readme"
                className="sb-num min-h-40 font-mono text-xs"
                rows={8}
                value={readme}
                placeholder="Markdown. Leave empty for no README."
                onChange={(e) => setReadme(e.target.value)}
              />
            </div>
            {readmeTooLong && (
              <p role="alert" data-testid="readme-limit" className="text-xs text-destructive">
                {readmeTooLong}
              </p>
            )}
            {pendingReadme === 'remove' && (
              <p role="status" data-testid="readme-state" className="text-xs text-destructive">
                Saving will remove the README.
              </p>
            )}
            {readmeBlank && readme !== '' && !model.has_readme && (
              <p role="status" data-testid="readme-state" className="text-xs text-muted-foreground">
                Only whitespace, so no README is saved.
              </p>
            )}
            {model.has_readme && readme !== '' && (
              <Button size="sm" variant="ghost" className="w-fit" onClick={() => setReadme('')}>
                Remove README
              </Button>
            )}
            <Button size="sm" variant="outline" className="w-fit" onClick={() => readmeFile.current?.click()}>
              <FileTextIcon /> Load README from file
            </Button>
            <input
              ref={readmeFile}
              type="file"
              accept=".md,.markdown,text/markdown"
              className="sr-only"
              tabIndex={-1}
              aria-label="README file"
              onChange={(event) => {
                void loadReadme(event.target.files?.[0])
                event.target.value = ''
              }}
            />
          </div>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={close} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={() => void save()} disabled={!model || saving || nameMissing || readmeTooLong !== null}>
            {saving && <Spinner />}
            {saving ? 'Saving' : 'Save'}
          </Button>
        </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
