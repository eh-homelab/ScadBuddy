import { Suspense, lazy, useCallback, useEffect, useId, useMemo, useRef, useState, type FormEvent, type ReactNode, type RefObject } from 'react'
import { waitFor } from '../../agent/highlight'
import { api } from '../../api/client'
import { joinInputs, sameJson, splitInputs, type JsonObject } from '../../lib/inputs'
import { sameValues } from '../../lib/params'
import { applyPreset } from '../../lib/presets'
import { useDisplayUnit } from '../../lib/units'
import { useAsync } from '../../lib/useAsync'
import { useDebounced } from '../../lib/useDebounced'
import { useLatest } from '../../lib/useLatest'
import { canRetry, RENDER_DEBOUNCE_MS, useRenderAutoRetry, useRenderJob } from '../../lib/useRenderJob'
import type { HostDeps } from '../../template-ui/host'
import { TemplateUi } from '../../template-ui/TemplateUi'
import type { TemplateUiFailure, UiDeclaration } from '../../template-ui/types'
import { useInputs } from '../flows/inputsForm'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import { designStart, fieldGroups, fieldInputs, fieldValues, pipelineOf, presetInputsAt, type DesignStart } from './designInputs'
import type { DesignSchema, ListingDraft } from './listingDraft'

// three.js: only a dialog with a preview loads it.
const Preview = lazy(async () => ({ default: (await import('../Preview')).Preview }))

/** How long a template's `generate()` waits for the render of the working copy. */
const GENERATE_WAIT_MS = 120_000

interface Props {
  draft: ListingDraft
  schema: DesignSchema
  /** The design's name, under the title. */
  name: string
  onClose: () => void
  /** The working copy (its own inputs, `params` and `v`), and what the template's UI says of it. */
  onUse: (inputs: JsonObject, said: string | null) => void
}

/**
 * #5701 (spec 2026-11-17 §5.2, §5.3) — "Set up the design": the template's own UI on a
 * working copy of the listing's inputs, in the dialog's own chrome, so a person never takes
 * it for the Customize page. Nothing reaches the listing until "Use this design", and
 * nothing saves an output, a preset or a print. Without a UI, or with one that does not
 * mount, the form is drawn from the inputs' own schema.
 */
export function DesignDialog({ draft, schema, name, onClose, onUse }: Props) {
  // What it opened on: a save that answers meanwhile never restarts it.
  const [opened] = useState({ draft, schema })
  const [start, setStart] = useState<DesignStart | 'loading' | { error: string }>('loading')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let live = true
    setStart('loading')
    designStart(opened.schema, opened.draft).then(
      (ready) => live && setStart(ready),
      (caught: unknown) => live && setStart({ error: caught instanceof Error ? caught.message : String(caught) }),
    )
    return () => {
      live = false
    }
  }, [attempt, opened])

  const changed = useRef(false)
  const [leaving, setLeaving] = useState(false)
  const noteChanged = useCallback((now: boolean) => {
    changed.current = now
  }, [])

  return (
    <>
      <Dialog
        open
        onOpenChange={(open) => {
          if (open) return
          // Escape with work in it asks first; Cancel says what it does.
          if (changed.current) setLeaving(true)
          else onClose()
        }}
      >
        <DialogContent
          showCloseButton={false}
          overlayClassName="max-sm:p-0"
          className={cn(
            'flex h-full max-h-full flex-col gap-0 overflow-hidden p-0 max-sm:rounded-none',
            // Room for the template's own interface; a form is as tall as its fields.
            schema.ui ? 'sm:max-w-5xl 2xl:max-w-7xl' : 'sm:h-auto sm:max-w-xl',
          )}
          // A press beside it is most often a miss: the work in it never goes that way.
          onInteractOutside={(event) => event.preventDefault()}
        >
          <DialogHeader className="shrink-0 gap-1 border-b px-4 py-3">
            <DialogTitle>Set up the design</DialogTitle>
            <DialogDescription className="truncate">{name}</DialogDescription>
          </DialogHeader>
          {start === 'loading' ? (
            <div role="status" aria-label="Loading the design" className="flex min-h-0 flex-1 flex-col gap-2 p-4">
              <Skeleton className="h-8 w-2/3" />
              <Skeleton className="min-h-0 w-full flex-1" />
            </div>
          ) : 'error' in start ? (
            <div className="flex min-h-0 flex-1 flex-col items-start gap-2 p-4">
              <p role="alert" className="text-[13px] text-warn">
                The design could not be opened: {start.error}
              </p>
              <Button variant="outline" size="sm" onClick={() => setAttempt((n) => n + 1)}>
                Try again
              </Button>
            </div>
          ) : (
            <Design start={start} slug={opened.draft.slug} commit={opened.draft.commit} schema={opened.schema} onChanged={noteChanged} onClose={onClose} onUse={onUse} />
          )}
          {(start === 'loading' || 'error' in start) && <Footer onClose={onClose} />}
        </DialogContent>
      </Dialog>
      <AlertDialog open={leaving} onOpenChange={setLeaving}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Drop your changes to the design?</AlertDialogTitle>
            <AlertDialogDescription>The listing keeps the design it had.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <Button type="button" variant="destructive" onClick={onClose}>
              Drop changes
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

function Footer({ children, onClose }: { children?: ReactNode; onClose: () => void }) {
  return (
    <DialogFooter className="mx-0 mb-0 shrink-0 flex-row justify-end rounded-none">
      <Button type="button" variant="outline" onClick={onClose}>
        Cancel
      </Button>
      {children}
    </DialogFooter>
  )
}

/** The template's UI, or the schema's form, over the working copy. */
function Design({
  start,
  slug,
  commit,
  schema,
  onChanged,
  onClose,
  onUse,
}: {
  start: DesignStart
  slug: string
  commit: string
  schema: DesignSchema
  onChanged: (changed: boolean) => void
  onClose: () => void
  onUse: Props['onUse']
}) {
  const [inputs, setInputs] = useState<JsonObject>(start.inputs)
  /** The working copy as of the last write, which a template reads back at once. */
  const latest = useRef(inputs)
  const write = useCallback(
    (next: JsonObject) => {
      latest.current = next
      setInputs(next)
      onChanged(!sameJson(next, start.inputs))
    },
    [onChanged, start],
  )
  const [failure, setFailure] = useState<TemplateUiFailure | null>(null)
  const describe = useRef<(() => string) | null>(null)
  const onDescribe = useCallback((fn: (() => string) | null) => {
    describe.current = fn
  }, [])

  /** What the template says of the working copy; one that cannot say loses nothing else. */
  function said(): string | null {
    try {
      const text: unknown = describe.current?.()
      return typeof text === 'string' && text.trim() ? text.trim() : null
    } catch (cause) {
      console.error('the template’s describe() threw', cause)
      return null
    }
  }

  const note = start.note && (
    <p role="note" className="shrink-0 border-b border-warn/50 bg-warn/5 px-4 py-2 text-[13px]">
      {start.note}
    </p>
  )
  if (schema.ui && !failure) {
    return (
      <>
        {note}
        <TemplateDesign
          slug={slug}
          commit={commit}
          schema={schema}
          ui={schema.ui as UiDeclaration}
          inputs={inputs}
          latest={latest}
          onInputs={write}
          onDescribe={onDescribe}
          onFailure={setFailure}
        />
        <Footer onClose={onClose}>
          <Button type="button" onClick={() => onUse(latest.current, said())}>
            Use this design
          </Button>
        </Footer>
      </>
    )
  }
  return (
    <>
      {note}
      {(failure || schema.ui_error) && (
        <p role="note" className="shrink-0 border-b px-4 py-2 text-[13px] text-muted-foreground">
          The template’s own interface did not open ({failure ? `${failure.file}: ${failure.message}` : schema.ui_error}). Set the design up here instead.
        </p>
      )}
      <FormDesign schema={schema} inputs={inputs} onChanged={onChanged} onClose={onClose} onUse={(next) => onUse(next, null)} />
    </>
  )
}

/** The template's UI (host API v1) with the preview: in its `<sb-preview>`, or beside a panel. */
function TemplateDesign({
  slug,
  commit,
  schema,
  ui,
  inputs,
  latest,
  onInputs,
  onDescribe,
  onFailure,
}: {
  slug: string
  commit: string
  schema: DesignSchema
  ui: UiDeclaration
  inputs: JsonObject
  latest: RefObject<JsonObject>
  onInputs: (next: JsonObject) => void
  onDescribe: (fn: (() => string) | null) => void
  onFailure: (failure: TemplateUiFailure) => void
}) {
  const fonts = useAsync(() => api.listFonts(), [])
  const unit = useDisplayUnit()
  const { params, extra } = splitInputs(inputs)
  // A write to the template's own state alone renders nothing again, as on the Customize page.
  const [values, setValues] = useState(params)
  if (values !== params && !sameValues(values, params)) setValues(params)
  // `generate()` asks for the render of the whole working copy as it is now.
  const [asked, setAsked] = useState(0)
  const wanted = useMemo(() => ({ params: { ...values }, asked }), [values, asked])
  const debounced = useDebounced(wanted, RENDER_DEBOUNCE_MS)
  const settled = debounced === wanted
  const { job, rendering, error, busy, retry, settledFor, stage } = useRenderJob(slug, settled ? debounced.params : undefined, commit, extra)
  useRenderAutoRetry(error, rendering, retry, debounced, settled)

  const live = useLatest({ schema, job, error, asked, done: settled && settledFor === debounced.params })
  const requests = useRef(0)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const deps: HostDeps = useMemo(
    () => ({
      slug,
      version: commit,
      getSchema: () => live.current.schema,
      getInputs: () => latest.current,
      setInputs: onInputs,
      generate: async () => {
        const mine = ++requests.current
        setAsked(mine)
        await waitFor(() => (!mounted.current || (live.current.asked >= mine && live.current.done) ? true : undefined), {
          timeout: GENERATE_WAIT_MS,
          what: 'the preview render of the current inputs',
        })
        if (!mounted.current) throw new Error('the design’s dialog was closed')
        const { job: done, error: failed } = live.current
        if (failed) throw failed
        if (!done || done.status !== 'done') throw new Error(done?.error || 'the render failed')
        // No output is saved here, so there is none to name.
        return { jobId: done.id, outputId: '' }
      },
      openPrint: () => undefined,
      presets: {
        list: () => api.listPresets(slug),
        save: () => Promise.reject(new Error('a preset is saved on the template’s own page, not while a listing is set up')),
        load: async (id: string) => {
          const preset = (await api.listPresets(slug)).find((candidate) => candidate.id === id)
          if (!preset) throw new Error(`no preset ${id}`)
          const applied = applyPreset(live.current.schema, preset)
          const next = await presetInputsAt(live.current.schema, slug, commit, joinInputs(applied.values, applied.extra))
          if (mounted.current) onInputs(next)
        },
      },
      onDescribe,
    }),
    [slug, commit, live, latest, onInputs, onDescribe],
  )

  const preview = (
    <Suspense fallback={<Skeleton className="size-full rounded-none" />}>
      <Preview job={job} unit={unit} rendering={rendering || !settled} stage={stage} rejected={Boolean(error)} />
    </Suspense>
  )
  const template = (
    <TemplateUi
      slug={slug}
      ui={ui}
      version={commit}
      deps={deps}
      inputs={inputs}
      onFailure={onFailure}
      elementContext={{
        schema,
        slug,
        version: commit,
        fonts: fonts.data ?? [],
        inputs,
        getInputs: deps.getInputs,
        onInputs: deps.setInputs,
        preview,
        // Every change renders here already, and no output is kept: there is nothing to press.
        generate: null,
      }}
    />
  )
  return (
    <>
      {(error || busy) && (
        <p role={error ? 'alert' : 'status'} className="flex shrink-0 flex-wrap items-center gap-2 border-b px-4 py-2 text-[13px] text-warn">
          {error ? `The preview could not be rendered: ${error.message}` : 'The render service is busy. The preview follows.'}
          {canRetry(error) && (
            <Button variant="link" size="sm" className="h-auto p-0" onClick={retry}>
              Try again
            </Button>
          )}
        </p>
      )}
      {ui.slot === 'page' ? (
        <div className="min-h-0 flex-1">{template}</div>
      ) : (
        <div className="grid min-h-0 flex-1 max-md:grid-rows-[40%_minmax(0,1fr)] md:grid-cols-[minmax(0,24rem)_minmax(0,1fr)]">
          <div className="min-h-0 max-md:order-2 max-md:border-t md:border-r">{template}</div>
          <div className="min-h-0">{preview}</div>
        </div>
      )}
    </>
  )
}

/** The form drawn from the inputs' own schema, for a design with no UI of its own (§5.2). */
function FormDesign({
  schema,
  inputs,
  onChanged,
  onClose,
  onUse,
}: {
  schema: DesignSchema
  inputs: JsonObject
  onChanged: (changed: boolean) => void
  onClose: () => void
  onUse: (inputs: JsonObject) => void
}) {
  const id = useId()
  const form = useRef<HTMLFormElement>(null)
  const [groups] = useState(() => fieldGroups(pipelineOf(schema)?.inputs))
  const [starts] = useState(() => fieldValues(groups, inputs))
  // The starting values hold the schema's defaults, so a field cleared here is left out.
  const [fields] = useState(() => groups.flatMap((group) => group.fields.map(({ default: _start, ...field }) => field)))
  const { parse, rendered, errors } = useInputs(fields, { values: starts, names: false, onChange: () => onChanged(true) })
  useEffect(() => {
    if (Object.keys(errors).length > 0) form.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus()
  }, [errors])

  function submit(event: FormEvent) {
    event.preventDefault()
    // The browser shows a number it could not read as typed, and its value is blank.
    const unread = Array.from(form.current?.querySelectorAll<HTMLInputElement>('input[data-field]') ?? []).filter((input) => input.validity.badInput)
    const values = parse(unread.map((input) => input.dataset['field']!))
    const { params = {}, v = 0, ...own } = inputs
    if (values) onUse({ ...fieldInputs(groups, values, own), params, v })
  }

  const drawn = new Map(fields.map((field, index) => [field.name, rendered[index]]))
  return (
    <>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <form ref={form} id={id} noValidate className="flex max-w-xl flex-col gap-4" onSubmit={submit}>
          {groups.length === 0 && <p className="text-sm text-muted-foreground">This design has nothing to set up.</p>}
          {groups.map((group) => {
            const controls = group.fields.map((field) => drawn.get(field.name))
            if (group.title === null) return controls
            return (
              <fieldset key={group.key} className="flex min-w-0 flex-col gap-4 rounded-md border p-3">
                <legend className="px-1 text-sm font-semibold">{group.title}</legend>
                {group.description && <p className="text-[12px] text-muted-foreground">{group.description}</p>}
                {controls}
              </fieldset>
            )
          })}
        </form>
      </div>
      <Footer onClose={onClose}>
        <Button type="submit" form={id}>
          Use this design
        </Button>
      </Footer>
    </>
  )
}
