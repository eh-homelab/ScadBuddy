import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { PlusIcon, XIcon } from 'lucide-react'
import { api, ApiError } from '../../api/client'
import type { Association, AssociationRelation } from '../../api/types'
import {
  impliedWhy,
  RELATION_WORDS,
  RELATIONS,
  seenFrom,
  subjectKey,
  type AssociationSubject,
  type OtherEnd,
} from '../../lib/associations'
import { modelPath } from '../../lib/deeplink'
import { LIBRARY_ROOT } from '../../lib/libraryPath'
import { useAsync } from '../../lib/useAsync'
import { Spinner } from '../ui/spinner'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'

/** How many search matches the picker lists. */
const PICKS_SHOWN = 8

function problem(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.detail : fallback
}

function otherPath(other: OtherEnd): string {
  return other.kind === 'model' ? modelPath(other.id) : `${LIBRARY_ROOT}?details=${other.id}`
}

/**
 * #6395 — what a library file or a model is associated with: each association with its
 * relation said from this side, a link to the other end, and its note. A stored one has a
 * remove button; an implied one (read from the model's source or the output's copy) says
 * why it is listed instead. "Associate…" adds one.
 */
export function RelatedAssociations({ subject }: { subject: AssociationSubject }) {
  const key = subjectKey(subject)
  const read = useAsync(
    () =>
      subject.kind === 'library'
        ? api.getLibraryAssociations(subject.id)
        : api.getModelAssociations(subject.slug),
    [key],
  )
  const [adding, setAdding] = useState(false)
  const [removing, setRemoving] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const items = read.data?.associations ?? []

  async function remove(item: Association) {
    if (!item.id) return
    setRemoving(item.id)
    setError(null)
    try {
      await api.removeAssociation(item.id)
      read.reload()
    } catch (caught) {
      setError(problem(caught, 'Could not remove the association.'))
    } finally {
      setRemoving(null)
    }
  }

  return (
    <div data-testid="related-associations" className="space-y-2">
      {read.loading && !read.data ? (
        <Skeleton role="status" aria-label="Reading" className="h-10 w-full" />
      ) : read.error ? (
        <p className="text-sm text-muted-foreground">{problem(read.error, 'Could not read what it is related to.')}</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Nothing is associated yet. Associate a model or a library file to keep them together.
        </p>
      ) : (
        <ul className="divide-y rounded-md border text-sm">
          {items.map((item) => {
            const { words, other } = seenFrom(subject, item)
            const why = impliedWhy(subject, item)
            const rowKey = item.id ?? `${item.implied_by}:${item.library_file_id}:${item.target}`
            return (
              <li key={rowKey} className="flex items-start gap-2 px-3 py-2">
                <div className="min-w-0 flex-1 space-y-0.5">
                  <p className="flex flex-wrap items-baseline gap-x-1.5">
                    <span className="text-muted-foreground">{words}</span>{' '}
                    <Link to={otherPath(other)} className="min-w-0 break-all text-primary underline-offset-4 hover:underline">
                      {other.name}
                    </Link>
                    {other.kind === 'model' && ' '}
                    {other.kind === 'model' && <span className="text-xs text-muted-foreground">(model)</span>}
                  </p>
                  {item.note && <p className="break-words text-xs">{item.note}</p>}
                  {why && <p className="text-xs text-muted-foreground">{why}</p>}
                </div>
                {item.id && (
                  <Button
                    size="icon-xs"
                    variant="ghost"
                    aria-label={`Remove ${words.toLowerCase()} ${other.name}`}
                    disabled={removing === item.id}
                    onClick={() => void remove(item)}
                  >
                    {removing === item.id ? <Spinner /> : <XIcon />}
                  </Button>
                )}
              </li>
            )
          })}
        </ul>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <Button size="sm" variant="outline" onClick={() => setAdding(true)}>
        <PlusIcon /> Associate…
      </Button>
      <AssociateDialog
        subject={subject}
        open={adding}
        onClose={() => setAdding(false)}
        onAdded={() => {
          setAdding(false)
          read.reload()
        }}
      />
    </div>
  )
}

/** A pick: a model by slug, or a library file by id. */
interface Pick {
  kind: 'model' | 'library'
  id: string
  name: string
}

function AssociateDialog({
  subject,
  open,
  onClose,
  onAdded,
}: {
  subject: AssociationSubject
  open: boolean
  onClose: () => void
  onAdded: () => void
}) {
  // On a model's page the other end is always a library file; on a file's, either.
  const [kind, setKind] = useState<'model' | 'library'>(subject.kind === 'model' ? 'library' : 'model')
  const [query, setQuery] = useState('')
  const [picked, setPicked] = useState<Pick | null>(null)
  const [relation, setRelation] = useState<AssociationRelation>('related_to')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setKind(subject.kind === 'model' ? 'library' : 'model')
    setQuery('')
    setPicked(null)
    setRelation('related_to')
    setNote('')
    setError(null)
  }, [open, subject.kind])

  const models = useAsync(
    () => (open && kind === 'model' ? api.listModels() : Promise.resolve(null)),
    [open, kind],
  )
  const words = query.trim()
  const files = useAsync(
    () => (open && kind === 'library' && words !== '' ? api.searchLibrary({ q: words, all: true }) : Promise.resolve(null)),
    [open, kind, words],
  )
  const picks = useMemo<Pick[]>(() => {
    if (kind === 'model') {
      const needle = words.toLowerCase()
      return (models.data ?? [])
        .filter((model) => !needle || model.name.toLowerCase().includes(needle) || model.slug.includes(needle))
        .slice(0, PICKS_SHOWN)
        .map((model) => ({ kind: 'model', id: model.slug, name: model.name }))
    }
    return (files.data?.files ?? [])
      .filter((file) => !(subject.kind === 'library' && file.id === subject.id))
      .slice(0, PICKS_SHOWN)
      .map((file) => ({ kind: 'library', id: String(file.id), name: file.filename }))
  }, [kind, words, models.data, files.data, subject])

  // Said from the file's side: on a model's page the picked file is that side.
  const fileSide = subject.kind === 'model' ? (picked?.name ?? 'The file') : 'This file'
  const targetSide = subject.kind === 'model' ? 'this model' : (picked?.name ?? 'it')

  async function confirm() {
    if (!picked) return
    setBusy(true)
    setError(null)
    try {
      if (subject.kind === 'model') {
        await api.associateLibraryFile(Number(picked.id), {
          target: `model:${subject.slug}`,
          relation,
          note: note.trim(),
        })
      } else {
        await api.associateLibraryFile(subject.id, {
          target: `${picked.kind}:${picked.id}`,
          relation,
          note: note.trim(),
        })
      }
      onAdded()
    } catch (caught) {
      setError(problem(caught, 'Could not associate them.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !busy && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Associate</DialogTitle>
          <DialogDescription>
            {subject.kind === 'model'
              ? 'Pick a library file and say how it relates to this model. To associate any other 3MF, upload it to the library first.'
              : 'Pick a model or another library file and say how this file relates to it.'}
          </DialogDescription>
        </DialogHeader>
        <form
          id="associate-form"
          className="grid gap-3"
          onSubmit={(event) => {
            event.preventDefault()
            void confirm()
          }}
        >
          {subject.kind === 'library' && (
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              aria-label="Associate with"
              value={kind}
              onValueChange={(value) => {
                if (value !== 'model' && value !== 'library') return
                setKind(value)
                setPicked(null)
              }}
              className="justify-start"
            >
              <ToggleGroupItem value="model">A model</ToggleGroupItem>
              <ToggleGroupItem value="library">A library file</ToggleGroupItem>
            </ToggleGroup>
          )}
          <div className="grid gap-1.5">
            <Label htmlFor="associate-search">{kind === 'model' ? 'Model' : 'Library file'}</Label>
            <Input
              id="associate-search"
              autoFocus
              placeholder={kind === 'model' ? 'Filter models by name' : 'Search the library by name'}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            {(models.loading || files.loading) && <Skeleton className="h-8 w-full" />}
            {picks.length > 0 && (
              <RadioGroup
                aria-label="Matches"
                value={picked ? `${picked.kind}:${picked.id}` : ''}
                onValueChange={(value) => setPicked(picks.find((pick) => `${pick.kind}:${pick.id}` === value) ?? null)}
                className="max-h-48 gap-0 overflow-y-auto rounded-md border text-sm"
              >
                {picks.map((pick) => {
                  const value = `${pick.kind}:${pick.id}`
                  return (
                    <Label
                      key={value}
                      htmlFor={`associate-pick-${value}`}
                      className="flex cursor-pointer items-center gap-2 px-2 py-1.5 font-normal hover:bg-muted has-data-[state=checked]:bg-primary/10"
                    >
                      <RadioGroupItem id={`associate-pick-${value}`} value={value} />
                      <span className="min-w-0 truncate">{pick.name}</span>
                    </Label>
                  )
                })}
              </RadioGroup>
            )}
            {kind === 'library' && words !== '' && !files.loading && picks.length === 0 && (
              <p className="text-xs text-muted-foreground">No library file matches. Upload it to the library first.</p>
            )}
          </div>
          <div className="grid gap-1.5">
            <Label id="associate-relation">How they relate</Label>
            <RadioGroup
              aria-labelledby="associate-relation"
              value={relation}
              onValueChange={(value) => setRelation(value as AssociationRelation)}
              className="grid-cols-2 gap-1.5"
            >
              {RELATIONS.map((option) => (
                <Label key={option} htmlFor={`associate-relation-${option}`} className="flex items-center gap-2 font-normal">
                  <RadioGroupItem id={`associate-relation-${option}`} value={option} />
                  {RELATION_WORDS[option].fromFile}
                </Label>
              ))}
            </RadioGroup>
            <p className="text-xs text-muted-foreground" data-testid="associate-sentence">
              {fileSide} is {RELATION_WORDS[relation].fromFile.toLowerCase()} {targetSide}.
            </p>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="associate-note">Note (optional)</Label>
            <Textarea
              id="associate-note"
              rows={2}
              maxLength={500}
              value={note}
              onChange={(event) => setNote(event.target.value)}
            />
          </div>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </form>
        <DialogFooter>
          <Button type="button" variant="outline" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" form="associate-form" disabled={!picked || busy}>
            {busy && <Spinner />} Associate
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** #6395 — the model page's "Related": the library files associated with the model. */
export function RelatedButton({ slug }: { slug: string }) {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button type="button" variant="ghost" size="sm">
          Related
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Related</DialogTitle>
          <DialogDescription>Library files associated with this model, and how each relates to it.</DialogDescription>
        </DialogHeader>
        <RelatedAssociations subject={{ kind: 'model', slug }} />
      </DialogContent>
    </Dialog>
  )
}
