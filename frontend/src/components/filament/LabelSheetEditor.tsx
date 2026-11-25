import { useId, useState } from 'react'
import { ApiError } from '../../api/client'
import { createLabelSheet, replaceLabelSheet, type SavedLabelSheet, type SheetLayout } from '../../api/filament'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label as FieldLabel } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { PAGE_MM } from './labelSheet'

type NumberKey = Exclude<keyof SheetLayout, 'page'>

/** The number fields, in the groups the dialog shows them in. `step` is the field's step: mm take one decimal. */
const GROUPS: { legend: string; fields: { key: NumberKey; label: string; step: number }[] }[] = [
  {
    legend: 'Label',
    fields: [
      { key: 'width_mm', label: 'Label width (mm)', step: 0.1 },
      { key: 'height_mm', label: 'Label height (mm)', step: 0.1 },
    ],
  },
  {
    legend: 'Grid',
    fields: [
      { key: 'cols', label: 'Columns', step: 1 },
      { key: 'rows', label: 'Rows', step: 1 },
    ],
  },
  {
    legend: 'Margins',
    fields: [
      { key: 'top_mm', label: 'Top margin (mm)', step: 0.1 },
      { key: 'left_mm', label: 'Left margin (mm)', step: 0.1 },
    ],
  },
  {
    legend: 'Gaps',
    fields: [
      { key: 'col_gap_mm', label: 'Column gap (mm)', step: 0.1 },
      { key: 'row_gap_mm', label: 'Row gap (mm)', step: 0.1 },
    ],
  },
]

const FIELD_LABELS: Record<string, string> = { name: 'Name', page: 'Page', ...Object.fromEntries(GROUPS.flatMap((group) => group.fields.map((one) => [one.key, one.label]))) }

/** A new sheet starts as a grid that fits A4: three columns of seven labels. */
const NEW_LAYOUT: SheetLayout = { page: 'a4', width_mm: 63.5, height_mm: 38.1, cols: 3, rows: 7, top_mm: 15.1, left_mm: 7.2, col_gap_mm: 2.5, row_gap_mm: 0 }

type Draft = { name: string; page: SheetLayout['page'] } & Record<NumberKey, string>

function draftOf(sheet: SavedLabelSheet | null): Draft {
  const layout = sheet?.layout ?? NEW_LAYOUT
  const numbers = Object.fromEntries(GROUPS.flatMap((group) => group.fields.map(({ key }) => [key, String(layout[key])]))) as Record<NumberKey, string>
  return { name: sheet?.name ?? '', page: layout.page, ...numbers }
}

/** An empty field is sent as `null`, so the server names it; the server's text is the only check (R-7216-11). */
const numberOf = (text: string): number | null => (text.trim() === '' ? null : Number(text))

/** What the API said, as lines: each 422 problem with the field it names, else the refusal's sentence. */
function refusal(error: unknown): string[] {
  if (error instanceof ApiError) {
    const errors = (error.problem as { errors?: { loc?: string[]; msg?: string }[] }).errors
    if (error.status === 422 && errors && errors.length > 0)
      return errors.map((one) => {
        const field = FIELD_LABELS[one.loc?.at(-1) ?? '']
        const text = (one.msg ?? '').replace(/^Value error, /, '')
        return field ? `${field}: ${text}` : text
      })
    return [error.detail]
  }
  return ['The sheet could not be saved. Try again.']
}

/** A small drawing of the page and its grid. A label past the page edge is drawn in the warning colour. */
function SheetPreview({ draft }: { draft: Draft }) {
  const [pageW, pageH] = PAGE_MM[draft.page]
  const n = (key: NumberKey) => Number(draft[key])
  const cols = Math.trunc(n('cols'))
  const rows = Math.trunc(n('rows'))
  const valid = [n('width_mm'), n('height_mm'), n('top_mm'), n('left_mm'), n('col_gap_mm'), n('row_gap_mm')].every((v) => Number.isFinite(v) && v >= 0)
  const drawn = valid && cols >= 1 && rows >= 1 && cols <= 20 && rows <= 40 && n('width_mm') > 0 && n('height_mm') > 0
  const labels = drawn
    ? Array.from({ length: cols * rows }, (_, at) => {
        const x = n('left_mm') + (at % cols) * (n('width_mm') + n('col_gap_mm'))
        const y = n('top_mm') + Math.floor(at / cols) * (n('height_mm') + n('row_gap_mm'))
        return { x, y, out: x + n('width_mm') > pageW + 0.01 || y + n('height_mm') > pageH + 0.01 }
      })
    : []
  // The view takes in the labels past the page too, so the person sees how far they go.
  const viewW = Math.min(Math.max(pageW, ...labels.map((one) => one.x + n('width_mm'))), pageW * 1.5)
  const viewH = Math.min(Math.max(pageH, ...labels.map((one) => one.y + n('height_mm'))), pageH * 1.5)
  const pad = 4
  return (
    <svg
      role="img"
      aria-label={drawn ? `${cols} by ${rows} labels on ${draft.page === 'a4' ? 'A4' : 'Letter'}` : 'No grid to draw'}
      viewBox={`${-pad} ${-pad} ${viewW + 2 * pad} ${viewH + 2 * pad}`}
      className="h-auto w-full"
    >
      <rect x={0} y={0} width={pageW} height={pageH} rx={1.5} className="fill-card stroke-border" strokeWidth={1} />
      {labels.map((one, at) => (
        <rect
          key={at}
          x={one.x}
          y={one.y}
          width={n('width_mm')}
          height={n('height_mm')}
          rx={Math.min(1.5, n('height_mm') / 6)}
          className={one.out ? 'fill-warn/25 stroke-warn' : 'fill-primary/10 stroke-primary/50'}
          strokeWidth={0.6}
        />
      ))}
    </svg>
  )
}

/**
 * #7216 — a saved sheet grid, new (`sheet` null) or one to change. Save POSTs or PUTs `{name, layout}` with numbers;
 * the server checks the bounds and the fit on the page (R-7216-11), and its 409 and 422 text shows here as it is.
 */
export function LabelSheetEditor({ sheet, onSaved, onClose }: { sheet: SavedLabelSheet | null; onSaved(sheet: SavedLabelSheet): void; onClose(): void }) {
  const [draft, setDraft] = useState<Draft>(() => draftOf(sheet))
  const [problem, setProblem] = useState<string[] | null>(null)
  const [busy, setBusy] = useState(false)
  const id = useId()

  const set = (key: keyof Draft, value: string) => setDraft((was) => ({ ...was, [key]: value }))

  const submit = async () => {
    setBusy(true)
    setProblem(null)
    const numbers = Object.fromEntries(GROUPS.flatMap((group) => group.fields.map(({ key }) => [key, numberOf(draft[key])])))
    const body = { name: draft.name, layout: { page: draft.page, ...numbers } as SheetLayout }
    try {
      const done = sheet ? await replaceLabelSheet(sheet.id, body) : await createLabelSheet(body)
      onSaved(done)
    } catch (error) {
      setProblem(refusal(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(next) => !next && !busy && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{sheet ? 'Edit label sheet' : 'New label sheet'}</DialogTitle>
          <DialogDescription>A sheet is a page of labels on a grid. Measure from the top left corner of the page.</DialogDescription>
        </DialogHeader>
        <form
          id={`${id}-form`}
          className="grid gap-5 sm:grid-cols-[1fr_11rem]"
          onSubmit={(event) => {
            event.preventDefault()
            void submit()
          }}
        >
          <div className="flex min-w-0 flex-col gap-4">
            <div className="grid grid-cols-[1fr_auto] gap-3">
              <span className="flex min-w-0 flex-col gap-1">
                <FieldLabel htmlFor={`${id}-name`} className="text-[13px]">
                  Name
                </FieldLabel>
                <Input id={`${id}-name`} value={draft.name} maxLength={100} autoComplete="off" onChange={(event) => set('name', event.target.value)} />
              </span>
              <span className="flex flex-col gap-1">
                <FieldLabel htmlFor={`${id}-page`} className="text-[13px]">
                  Page
                </FieldLabel>
                <Select value={draft.page} onValueChange={(value) => set('page', value)}>
                  <SelectTrigger id={`${id}-page`} className="w-28">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="a4">A4</SelectItem>
                    <SelectItem value="letter">Letter</SelectItem>
                  </SelectContent>
                </Select>
              </span>
            </div>
            <div className="grid grid-cols-1 gap-x-5 gap-y-3 min-[420px]:grid-cols-2">
              {GROUPS.map((group) => (
                <fieldset key={group.legend} className="flex flex-col gap-1.5">
                  <legend className="mb-1 text-[13px] font-medium">{group.legend}</legend>
                  <div className="grid grid-cols-2 gap-2">
                    {group.fields.map((field) => (
                      <span key={field.key} className="flex min-w-0 flex-col gap-1">
                        <FieldLabel htmlFor={`${id}-${field.key}`} className="text-xs font-normal text-muted-foreground">
                          {field.label}
                        </FieldLabel>
                        <Input
                          id={`${id}-${field.key}`}
                          type="number"
                          inputMode={field.step === 1 ? 'numeric' : 'decimal'}
                          step={field.step}
                          min={0}
                          value={draft[field.key]}
                          onChange={(event) => set(field.key, event.target.value)}
                        />
                      </span>
                    ))}
                  </div>
                </fieldset>
              ))}
            </div>
          </div>
          <div className="order-first mx-auto w-28 sm:order-none sm:w-full sm:pt-6">
            <SheetPreview draft={draft} />
          </div>
        </form>
        {problem && (
          <div role="alert" className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
            {problem.map((line, at) => (
              <p key={at}>{line}</p>
            ))}
          </div>
        )}
        <DialogFooter>
          <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" form={`${id}-form`} disabled={busy}>
            Save sheet
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
