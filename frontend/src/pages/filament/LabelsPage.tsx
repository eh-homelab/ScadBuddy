import { ChevronLeftIcon, FileDownIcon, PencilIcon, PlusIcon, PrinterIcon, Trash2Icon } from 'lucide-react'
import { type FocusEvent, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link, useSearchParams } from 'react-router'
import { api, ApiError } from '../../api/client'
import {
  fileName,
  deleteLabelSheet,
  getLocation,
  getSpool,
  labelsFile,
  type LabelOutput,
  type LabelTemplateChoice,
  type LabelTemplates,
  listLabelTemplates,
  printerLabels,
  type SavedLabelSheet,
  type SpoolDetail,
} from '../../api/filament'
import { Label, LocationLabel } from '../../components/filament/Label'
import { LabelOutputPicker } from '../../components/filament/LabelOutputPicker'
import { LabelFieldsPicker } from '../../components/filament/LabelFieldsPicker'
import { LabelSheetEditor } from '../../components/filament/LabelSheetEditor'
import { DEFAULT_FIELDS, DEFAULT_SIZE, httpsOrigin, labelBase, type LabelField, labelPayload, type LabelSize, locationPayload, MAX_LABELS, MAX_PAYLOAD_BYTES, PAGE_MM, pagesOf, qrSvg, tickField } from '../../components/filament/labelSheet'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label as FieldLabel } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from '@/components/ui/select'
import { downloadBlob } from '../../lib/embed'
import { useSettingSaver } from '../../lib/useSettingSaver'
import { filamentLinks } from '../../lib/filamentLinks'

type Layout = 'roll' | 'sheet'
const LAYOUT_KEY = 'scadbuddy.labels.layout'

function storedLayout(): Layout {
  try {
    return localStorage.getItem(LAYOUT_KEY) === 'sheet' ? 'sheet' : 'roll'
  } catch {
    return 'roll'
  }
}

/**
 * Print rules: the labels print from a copy in `#label-print-root`, a child of `<body>`
 * (a portal), and every other child of `<body>` is `display: none`, so the app shell, its
 * header and its scroll boxes leave no gap or clip. The copy is hidden on screen. A roll
 * prints one label per page of the label's own size (each wrapper a little shorter than the
 * page, so rounding adds no blank page); a sheet packs them on the paper with a margin. A
 * sheet template (#7157) prints each sheet as one page of its own size, its labels on the
 * sheet's grid, whatever the layout.
 */
const printCss = (layout: Layout, size: LabelSize) => {
  const { sheet } = size
  const rules = sheet
    ? `@page { size: ${PAGE_MM[sheet.page][0]}mm ${PAGE_MM[sheet.page][1]}mm; margin: 0; }
  #label-print-root > [data-sheet-page] { box-sizing: border-box; height: ${PAGE_MM[sheet.page][1] - 0.5}mm; overflow: hidden; break-after: page;
    padding: ${sheet.top_mm}mm 0 0 ${sheet.left_mm}mm; display: grid; align-content: start;
    grid-template-columns: repeat(${sheet.cols}, ${size.widthMm}mm); grid-auto-rows: ${size.heightMm}mm;
    column-gap: ${sheet.col_gap_mm}mm; row-gap: ${sheet.row_gap_mm}mm; }
  #label-print-root > [data-sheet-page]:last-child { break-after: auto; }`
    : layout === 'roll'
      ? `@page { size: ${size.widthMm}mm ${size.heightMm}mm; margin: 0; }
  #label-print-root > * { height: ${size.heightMm - 0.5}mm; overflow: hidden; break-after: page; }
  #label-print-root > *:last-child { break-after: auto; }`
      : `@page { margin: 10mm; }
  #label-print-root { display: flex !important; flex-wrap: wrap; gap: 2mm; width: 190mm; }`
  return `
#label-print-root { display: none; }
@media print {
  body > *:not(#label-print-root) { display: none !important; }
  #label-print-root { display: block !important; background: white; }
  ${rules}
}`
}

/** The template the page starts at, and the one an unreadable setting leaves. */
const DEFAULT_CHOICE: LabelTemplateChoice = { template: 'roll_50x25' }

/** #7216 — the output the page starts at, and the one an unreadable setting leaves: the setting's default (R-7216-8, R-7216-9). */
const DEFAULT_OUTPUT: LabelOutput = { format: 'pdf', dpi: 300, fields: [...DEFAULT_FIELDS] }

/** What the Download button's `title` says it gives. */
const outputTitle = (output: LabelOutput) => (output.format === 'png' ? `Download PNG images at ${output.dpi} dpi` : 'Download a PDF')

/** A choice as the setting and the PDF take it: the sizes only for `custom`, the sheet's id only for `saved` (#7216). */
function cleanChoice(choice: LabelTemplateChoice): LabelTemplateChoice {
  if (choice.template === 'custom') return { template: 'custom', width_mm: choice.width_mm, height_mm: choice.height_mm }
  if (choice.template === 'saved') return { template: 'saved', sheet_id: choice.sheet_id }
  return { template: choice.template }
}

/** The picker's value of a saved sheet: `saved:<id>`, apart from the template ids. */
const SAVED_PREFIX = 'saved:'

/** The saved sheet a choice names, when the list has it. */
const savedSheetOf = (choice: LabelTemplateChoice, templates: LabelTemplates | null): SavedLabelSheet | null =>
  (choice.template === 'saved' && templates?.saved.find((one) => one.id === choice.sheet_id)) || null

/**
 * The size a choice draws at; a template not yet listed draws at the default until the list arrives. A saved sheet
 * (#7216) draws on its own grid; one the list no longer has draws at the default with `gone` (R-7216-12).
 */
function sizeOf(choice: LabelTemplateChoice, templates: LabelTemplates | null): LabelSize & { gone: boolean } {
  if (choice.template === 'custom' && choice.width_mm != null && choice.height_mm != null) return { widthMm: choice.width_mm, heightMm: choice.height_mm, sheet: null, gone: false }
  if (choice.template === 'saved') {
    const sheet = savedSheetOf(choice, templates)
    if (!sheet) return { ...DEFAULT_SIZE, gone: templates !== null }
    const { page, width_mm, height_mm, cols, rows, top_mm, left_mm, col_gap_mm, row_gap_mm } = sheet.layout
    return { widthMm: width_mm, heightMm: height_mm, sheet: { page, cols, rows, top_mm, left_mm, col_gap_mm, row_gap_mm }, gone: false }
  }
  const found = templates?.templates.find((one) => one.id === choice.template)
  return found ? { widthMm: found.width_mm, heightMm: found.height_mm, sheet: found.sheet ?? null, gone: false } : { ...DEFAULT_SIZE, gone: false }
}

/** How many spools or places the page reads at the same time. */
const READ_AT_ONCE = 6

/** The ids of a comma-separated parameter, trimmed, each once, in order. */
function splitIds(raw: string): string[] {
  return [...new Set(raw.split(',').map((id) => id.trim()).filter(Boolean))]
}

/**
 * `read` of each id, at most `READ_AT_ONCE` in flight (up to 100 ids must not open 100
 * requests together), in the order of `ids`; an id whose read failed stays as its id.
 */
async function readEach<T>(ids: string[], read: (id: string) => Promise<T>, live: () => boolean): Promise<(T | string)[]> {
  const results: (T | string)[] = new Array(ids.length)
  let next = 0
  const worker = async () => {
    while (live() && next < ids.length) {
      const at = next++
      const id = ids[at]!
      try {
        results[at] = await read(id)
      } catch {
        results[at] = id
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(READ_AT_ONCE, ids.length) }, worker))
  // A read stopped by `live()` leaves a hole: only what was read is returned.
  return results.filter((result) => result !== undefined)
}

interface Loaded {
  key: string
  labels: { spool: SpoolDetail; svg: string }[]
  failed: string[]
}

interface PlaceLabel {
  id: string
  code: string
  name: string
  svg: string
}

interface LoadedPlaces {
  key: string
  base: string
  /** Whether `base` came from Settings (the label address, or an https public URL). Without it the
   * labels open the page's own origin, and the PDF route, which never takes a request's host, refuses them. */
  hasBase: boolean
  labels: PlaceLabel[]
  /** What could not be read, as the alert says it; no label is drawn then. */
  error: string | null
  /** More than `MAX_LABELS` labels: none is drawn. */
  tooMany: boolean
  /** The `?locations=` ids that could not be read; the others are drawn. */
  failed: string[]
  tooLong: string[]
  /** The printers with no report that lists their AMS units (`no_report`): their empty slots have no label yet. */
  noReport: string[]
}

/** The settings could not be read, so the label base is not known. */
class SettingsReadError extends Error {
  constructor(cause: unknown) {
    super('settings read failed', { cause })
  }
}

/** The label base of the settings (`filament_label_base_url`, else `public_url`), or null. */
async function settingsBase(): Promise<string | null> {
  const settings = await api.getSettings()
  return httpsOrigin(settings.filament_label_base_url) ?? httpsOrigin(settings.public_url)
}

/**
 * #6264 — `/filament/labels?ids=<id>,<id>`: spool labels to print. Each spool is read
 * again (the ids are the page's only input) and drawn at the label template (#7157, the setting `filament_label_template`; 50 x 25 mm by default) with its number and a
 * QR code of `scadbuddy:spool:<id>`. The layout, `roll` (one label per page, for a label
 * printer) or `sheet` (packed on paper), is remembered in this browser.
 */
export function LabelsPage() {
  const [params] = useSearchParams()
  const raw = params.get('ids') ?? ''
  const ids = useMemo(() => splitIds(raw), [raw])
  const tooMany = ids.length > MAX_LABELS
  const key = ids.join(',')
  const [layout, setLayout] = useState<Layout>(storedLayout)
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  // #6955 — `?printers=<id>,<id>|all` (every place of those printers) or `?locations=<id>,<id>` (those places).
  // Both print at most `MAX_LABELS` labels, as spools do (#7081).
  const printersParam = params.get('printers')
  const locationsParam = params.get('locations')?.trim() || null
  const placesKey = printersParam ? `printers:${printersParam}` : locationsParam ? `locations:${locationsParam}` : ''
  const [places, setPlaces] = useState<LoadedPlaces | null>(null)
  // #7157 — the label template: the setting `filament_label_template`, saved at each change.
  const [templates, setTemplates] = useState<LabelTemplates | null>(null)
  const [choice, setChoice] = useState<LabelTemplateChoice>(DEFAULT_CHOICE)
  const [editingCustom, setEditingCustom] = useState(false)
  const [draft, setDraft] = useState({ width: '', height: '' })
  const [customProblem, setCustomProblem] = useState<string | null>(null)
  const [pdfProblem, setPdfProblem] = useState<string | null>(null)
  const [pdfBusy, setPdfBusy] = useState(false)
  const size = sizeOf(choice, templates)
  // #7216 — the saved sheet chosen, the editor open (`new` or a sheet), and the sheet a delete waits to confirm.
  const chosenSheet = savedSheetOf(choice, templates)
  const [editing, setEditing] = useState<SavedLabelSheet | 'new' | null>(null)
  const [deleting, setDeleting] = useState<SavedLabelSheet | null>(null)
  const [sheetProblem, setSheetProblem] = useState<string | null>(null)
  // A choice saved on this page outranks the stored one that arrives after it. Opening the
  // custom fields saves nothing, so the stored choice still shows under them, and the
  // person's typing is kept.
  const customOpened = useRef(false)
  // R-7157-9, R-7216-16: one PUT at a time, the newest choice stored last.
  const templateSaver = useSettingSaver('filament_label_template')
  // #7216 — the file's format and dpi: the setting `filament_label_output`, saved at each change.
  const [output, setOutput] = useState<LabelOutput>(DEFAULT_OUTPUT)
  const outputSaver = useSettingSaver('filament_label_output')
  const fields = output.fields ?? DEFAULT_FIELDS
  // R-7216-17: a pick saves only the part it picked (the format, the dpi or one field's box), with
  // the other parts read from the setting. `value` is the stored output with every pick since the
  // read. Until the read answers, the picked format and dpi wait in `waiting` and the field ticks in
  // `ticks` (R-7216-18: one box each, applied in order to the stored fields); after a failed read,
  // no pick is saved.
  const outputRead = useRef<{ state: 'reading' | 'read' | 'failed'; value: LabelOutput; waiting: Partial<LabelOutput> | null; ticks: { field: LabelField; on: boolean }[] }>({
    state: 'reading',
    value: DEFAULT_OUTPUT,
    waiting: null,
    ticks: [],
  })
  const [outputUnsaved, setOutputUnsaved] = useState(false)
  const picker = useRef<HTMLButtonElement>(null)
  const pickerList = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let live = true
    // A list that cannot be read leaves the default label, which needs none.
    listLabelTemplates()
      .then((list) => live && setTemplates(list))
      .catch(() => {})
    // An unreadable setting leaves the default too; the place labels say so themselves.
    api
      .getSettings()
      .then((settings) => {
        if (!live) return
        const read = outputRead.current
        const stored = settings.filament_label_output ?? DEFAULT_OUTPUT
        read.state = 'read'
        // The parts picked before the read are kept (R-7216-16), and saved now over the stored rest:
        // the format and dpi picked, and each field tick applied to the stored fields (R-7216-18).
        const picked = read.waiting
        const ticks = read.ticks
        read.waiting = null
        read.ticks = []
        let value: LabelOutput = { ...stored, ...picked }
        for (const { field, on } of ticks) value = { ...value, fields: tickField(value.fields ?? DEFAULT_FIELDS, field, on) }
        read.value = value
        setOutput(value)
        if (picked || ticks.length > 0) outputSaver.save(value)
        const template = settings.filament_label_template
        if (!template || templateSaver.savedBeforeRead) return
        setChoice(cleanChoice(template))
        if (template.template === 'custom' && !customOpened.current) setDraft({ width: String(template.width_mm ?? ''), height: String(template.height_mm ?? '') })
      })
      .catch(() => {
        if (!live) return
        // The page cannot know the stored output, so it saves no pick; a pick holds on this page only.
        const read = outputRead.current
        read.state = 'failed'
        if (read.waiting || read.ticks.length > 0) setOutputUnsaved(true)
        read.waiting = null
        read.ticks = []
      })
    return () => {
      live = false
    }
    // The settings are read once. `savedBeforeRead` is a getter over the saver's own ref, so the first render's saver reads it current.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const save = (next: LabelTemplateChoice) => {
    setChoice(next)
    templateSaver.save(cleanChoice(next))
  }

  /**
   * One saver for the whole output: a pick changes only the part it picked (the format or the dpi)
   * and keeps the other parts, stored or picked.
   */
  const pickOutput = (part: Pick<LabelOutput, 'format'> | Pick<LabelOutput, 'dpi'>) => {
    setOutput((was) => ({ ...was, ...part }))
    const read = outputRead.current
    if (read.state === 'read') {
      read.value = { ...read.value, ...part }
      outputSaver.save(read.value)
    } else if (read.state === 'reading') read.waiting = { ...read.waiting, ...part }
    else setOutputUnsaved(true)
  }

  /**
   * R-7216-18: a field tick changes one box of the fields the page has (the stored ones, or the
   * default before the read). Before the read it waits as a change, and the read applies it to the
   * stored fields.
   */
  const pickField = (field: LabelField, on: boolean) => {
    const tick = (was: LabelOutput): LabelOutput => ({ ...was, fields: tickField(was.fields ?? DEFAULT_FIELDS, field, on) })
    setOutput(tick)
    const read = outputRead.current
    if (read.state === 'read') {
      read.value = tick(read.value)
      outputSaver.save(read.value)
    } else if (read.state === 'reading') read.ticks.push({ field, on })
    else setOutputUnsaved(true)
  }

  const pick = (value: string) => {
    setCustomProblem(null)
    if (value === 'custom') {
      customOpened.current = true
      setEditingCustom(true)
      setDraft({ width: String(size.widthMm), height: String(size.heightMm) })
      return
    }
    setEditingCustom(false)
    save(value.startsWith(SAVED_PREFIX) ? { template: 'saved', sheet_id: value.slice(SAVED_PREFIX.length) } : { template: value as LabelTemplateChoice['template'] })
  }

  /** The template list read again after a sheet changed; a failed read keeps the list the page has. */
  const readTemplates = () => {
    listLabelTemplates()
      .then(setTemplates)
      .catch(() => {})
  }

  /** #7216 — a sheet saved in the editor is in the list at once, and it becomes the choice. */
  const sheetSaved = (sheet: SavedLabelSheet) => {
    setEditing(null)
    setEditingCustom(false)
    setTemplates((was) => was && { ...was, saved: [...was.saved.filter((one) => one.id !== sheet.id), sheet] })
    save({ template: 'saved', sheet_id: sheet.id })
    readTemplates()
  }

  /** #7216 — deletes a sheet after the confirm. When it was the choice, the page goes back to the default label. */
  const removeSheet = async (sheet: SavedLabelSheet) => {
    setDeleting(null)
    setSheetProblem(null)
    try {
      await deleteLabelSheet(sheet.id)
    } catch (cause) {
      // A 404 is a sheet that is gone already: the result the person asked for.
      if (!(cause instanceof ApiError && cause.status === 404)) {
        setSheetProblem(cause instanceof Error ? cause.message : 'The sheet could not be deleted. Try again.')
        return
      }
    }
    setTemplates((was) => was && { ...was, saved: was.saved.filter((one) => one.id !== sheet.id) })
    if (choice.template === 'saved' && choice.sheet_id === sheet.id) save(DEFAULT_CHOICE)
    readTemplates()
  }

  /** A field left for the template picker saves nothing: the pick that follows decides. */
  const leaveCustom = (event: FocusEvent<HTMLInputElement>) => {
    const to = event.relatedTarget
    if (to instanceof Node && (picker.current?.contains(to) || pickerList.current?.contains(to))) return
    commitCustom()
  }

  /** A custom size is saved once both lengths are numbers within the bounds. */
  const commitCustom = () => {
    const bounds = templates?.custom ?? { min_width_mm: 40, min_height_mm: 25, max_width_mm: 190, max_height_mm: 190 }
    const width = Number(draft.width)
    const height = Number(draft.height)
    const fits =
      draft.width.trim() !== '' && draft.height.trim() !== '' && width >= bounds.min_width_mm && width <= bounds.max_width_mm && height >= bounds.min_height_mm && height <= bounds.max_height_mm
    if (!fits) {
      setCustomProblem(`A custom label is ${bounds.min_width_mm} to ${bounds.max_width_mm} mm wide and ${bounds.min_height_mm} to ${bounds.max_height_mm} mm tall.`)
      return
    }
    setCustomProblem(null)
    if (choice.template === 'custom' && choice.width_mm === width && choice.height_mm === height) return
    save({ template: 'custom', width_mm: width, height_mm: height })
  }

  useEffect(() => {
    if (!placesKey) return
    let live = true
    const empty = { labels: [], error: null, tooMany: false, failed: [], tooLong: [], noReport: [], hasBase: false }
    const draw = async (base: string | null, rows: { id: string; code: string; name: string }[], more: { noReport?: string[]; failed?: string[] } = {}) => {
      const origin = labelBase(base)
      // The cap stops drawing only: in `?printers=` mode the labels request has already made
      // the printers' places by now (`POST /filament/printer-labels` makes them, #6955).
      if (rows.length > MAX_LABELS) {
        if (live) setPlaces({ ...empty, key: placesKey, base: origin, tooMany: true })
        return
      }
      // A payload past 84 bytes would need a bigger QR code than the 19.8 mm box scans: no label for it.
      const fits = (row: { code: string }) => new TextEncoder().encode(locationPayload(origin, row.code)).length <= MAX_PAYLOAD_BYTES
      const tooLong = rows.filter((row) => !fits(row)).map((row) => row.code)
      const labels = await Promise.all(rows.filter(fits).map(async (row) => ({ ...row, svg: await qrSvg(locationPayload(origin, row.code)) })))
      if (live) setPlaces({ ...empty, key: placesKey, base: origin, hasBase: base !== null, labels, tooLong, noReport: more.noReport ?? [], failed: more.failed ?? [] })
    }
    const fail = (error: string) => () => {
      if (live) setPlaces({ ...empty, key: placesKey, base: labelBase(null), error })
    }
    if (printersParam) {
      const wanted = printersParam === 'all' ? null : splitIds(printersParam)
      // Each printer has a label of its own, so more printers than the cap are refused before any place is made.
      if (wanted && wanted.length > MAX_LABELS) {
        setPlaces({ ...empty, key: placesKey, base: labelBase(null), tooMany: true })
        return
      }
      printerLabels(wanted)
        .then((answer) =>
          draw(
            answer.base_url,
            answer.labels.map((row) => ({ id: row.location_id, code: row.code, name: row.name })),
            { noReport: answer.no_report },
          ),
        )
        .catch(fail('The printer labels could not be read'))
    } else if (locationsParam) {
      const wanted = splitIds(locationsParam)
      if (wanted.length > MAX_LABELS) {
        setPlaces({ ...empty, key: placesKey, base: labelBase(null), tooMany: true })
        return
      }
      // A failed settings read is an error, never "unset": the page's own origin may be the wrong host for a label.
      const base = settingsBase().catch((cause: unknown) => {
        throw new SettingsReadError(cause)
      })
      Promise.all([readEach(wanted, getLocation, () => live), base])
        .then(([read, origin]) => {
          const found = read.filter((r): r is Exclude<typeof r, string> => typeof r !== 'string')
          return draw(
            origin,
            found.map((place) => ({ id: place.id, code: place.code, name: place.name })),
            { failed: read.filter((r): r is string => typeof r === 'string') },
          )
        })
        .catch((cause: unknown) =>
          fail(cause instanceof SettingsReadError ? 'The label address in Settings could not be read, so no label is drawn. Try again.' : 'The place labels could not be drawn')(),
        )
    }
    return () => {
      live = false
    }
  }, [placesKey, printersParam, locationsParam])

  useEffect(() => {
    const wanted = key.split(',').filter(Boolean)
    if (tooMany || wanted.length === 0 || placesKey) return
    let live = true
    const read = async (id: string) => {
      const spool = await getSpool(id)
      return { spool, svg: await qrSvg(labelPayload(spool.id)) }
    }
    void readEach(wanted, read, () => live).then((results) => {
      if (!live) return
      setLoaded({
        key,
        labels: results.filter((r): r is Loaded['labels'][number] => typeof r !== 'string'),
        failed: results.filter((r): r is string => typeof r === 'string'),
      })
    })
    return () => {
      live = false
    }
  }, [key, tooMany, placesKey])

  const choose = (next: string) => {
    const chosen: Layout = next === 'sheet' ? 'sheet' : 'roll'
    setLayout(chosen)
    try {
      localStorage.setItem(LAYOUT_KEY, chosen)
    } catch {
      // Blocked storage: the choice holds for this visit only.
    }
  }

  const placeMode = placesKey !== ''
  const ready = !placeMode && !tooMany && loaded?.key === key ? loaded : null
  const labels = ready?.labels ?? []
  const readyPlaces = placeMode && places?.key === placesKey ? places : null
  const placeLabels = readyPlaces?.labels ?? []
  const count = placeMode ? placeLabels.length : labels.length
  const pickerValue = editingCustom || choice.template === 'custom' ? 'custom' : choice.template === 'saved' ? `${SAVED_PREFIX}${choice.sheet_id ?? ''}` : choice.template
  const savedSheets = templates?.saved ?? []
  /** The print copy's labels, in order: a sheet template lays them out a page at a time. */
  const printed: { key: string; node: ReactNode }[] = [
    ...labels.map(({ spool, svg }) => ({ key: spool.id, node: <Label spool={spool} svg={svg} size={size} fields={fields} testId="print-label" /> })),
    ...placeLabels.map((one) => ({ key: one.id, node: <LocationLabel code={one.code} name={one.name} svg={one.svg} size={size} testId="print-label" /> })),
  ]

  // A place label's PDF needs an address from Settings; a spool label needs none.
  const pdfNeedsBase = placeLabels.length > 0 && readyPlaces !== null && !readyPlaces.hasBase

  /**
   * #7157, #7216 — the server's file of the labels drawn, at the template and output shown; saved through
   * `downloadBlob`, which works inside Bambuddy's frame. The name follows the type of the answer.
   */
  const download = () => {
    setPdfProblem(null)
    setPdfBusy(true)
    const body = {
      spool_ids: labels.map(({ spool }) => spool.id),
      location_ids: placeLabels.map((one) => one.id),
      template: cleanChoice(choice),
      layout,
      format: output.format,
      dpi: output.dpi,
      fields: [...fields],
    }
    let file: Blob | null = null
    downloadBlob(
      async () => (file = await labelsFile(body)),
      () => (file ? fileName(file) : 'labels.pdf'),
    )
      .catch((cause: unknown) => setPdfProblem(cause instanceof Error ? cause.message : 'The labels file could not be made. Try again.'))
      .finally(() => setPdfBusy(false))
  }

  return (
    <div className="h-full overflow-y-auto">
      <style>{printCss(layout, size)}</style>
      <div className="mx-auto max-w-4xl px-4 py-6">
        <Link to={placeMode ? filamentLinks.locations : filamentLinks.spools} className="mb-3 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
          <ChevronLeftIcon className="size-3.5" /> {placeMode ? 'Locations' : 'Spools'}
        </Link>
        <header className="mb-5 flex flex-wrap items-center gap-3">
          <div className="min-w-0 flex-1">
            <h1 className="text-lg font-semibold tracking-tight">{placeMode ? 'Printer labels' : 'Spool labels'}</h1>
            <p className="text-[13px] text-muted-foreground">
              {placeMode
                ? readyPlaces
                  ? readyPlaces.tooMany
                    ? ''
                    : placeLabels.length === 0 && readyPlaces.error === null && readyPlaces.tooLong.length === 0 && readyPlaces.failed.length === 0
                      ? 'No printers.'
                      : `${placeLabels.length} ready, ${size.widthMm} x ${size.heightMm} mm each.`
                  : 'Loading labels…'
                : tooMany ? '' : ids.length === 0 ? 'No spools chosen.' : `${labels.length} of ${ids.length} ready, ${size.widthMm} x ${size.heightMm} mm each.`}
            </p>
            {readyPlaces && readyPlaces.error === null && !readyPlaces.tooMany && <p className="text-[13px] text-muted-foreground">{`Labels open ${readyPlaces.base}`}</p>}
          </div>
          <Select value={pickerValue} onValueChange={pick}>
            <SelectTrigger ref={picker} aria-label="Label" className="w-auto min-w-52">
              <SelectValue />
            </SelectTrigger>
            <SelectContent ref={pickerList}>
              {(templates?.templates ?? [{ id: 'roll_50x25' as const, name: 'Roll label 50 x 25 mm' }]).map((one) => (
                <SelectItem key={one.id} value={one.id}>
                  {one.name}
                </SelectItem>
              ))}
              <SelectItem value="custom">Custom size</SelectItem>
              {(savedSheets.length > 0 || size.gone) && (
                <>
                  <SelectSeparator />
                  <SelectGroup>
                    <SelectLabel>Saved sheets</SelectLabel>
                    {savedSheets.map((one) => (
                      <SelectItem key={one.id} value={`${SAVED_PREFIX}${one.id}`}>
                        {one.name}
                      </SelectItem>
                    ))}
                    {size.gone && (
                      <SelectItem value={pickerValue} disabled>
                        Deleted sheet
                      </SelectItem>
                    )}
                  </SelectGroup>
                </>
              )}
            </SelectContent>
          </Select>
          {!size.sheet && (
            <RadioGroup value={layout} onValueChange={choose} aria-label="Layout" className="flex w-auto items-center gap-4">
              <span className="flex items-center gap-1.5">
                <RadioGroupItem value="roll" id="layout-roll" />
                <FieldLabel htmlFor="layout-roll" className="text-[13px]">
                  Roll
                </FieldLabel>
              </span>
              <span className="flex items-center gap-1.5">
                <RadioGroupItem value="sheet" id="layout-sheet" />
                <FieldLabel htmlFor="layout-sheet" className="text-[13px]">
                  Sheet
                </FieldLabel>
              </span>
            </RadioGroup>
          )}
          <LabelOutputPicker value={output} onChange={(next) => pickOutput(next.format !== output.format ? { format: next.format } : { dpi: next.dpi })} />
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={count === 0 || pdfBusy || pdfNeedsBase}
            aria-describedby={pdfNeedsBase ? 'labels-pdf-needs-base' : undefined}
            title={outputTitle(output)}
            onClick={download}
          >
            <FileDownIcon /> Download
          </Button>
          <Button type="button" size="sm" disabled={count === 0} onClick={() => window.print()}>
            <PrinterIcon /> Print
          </Button>
          {pdfNeedsBase && (
            <p id="labels-pdf-needs-base" className="basis-full text-right text-[13px] text-muted-foreground">
              A download of place labels needs a label address. Set one in Settings, or an https public URL.
            </p>
          )}
        </header>
        {pickerValue === 'custom' && (
          <div className="-mt-2 mb-5 flex flex-wrap items-end gap-3">
            <span className="flex flex-col gap-1">
              <FieldLabel htmlFor="label-width" className="text-[13px]">
                Width (mm)
              </FieldLabel>
              <Input
                id="label-width"
                type="number"
                inputMode="decimal"
                className="w-24"
                value={draft.width}
                aria-invalid={customProblem !== null}
                onChange={(event) => setDraft((was) => ({ ...was, width: event.target.value }))}
                onBlur={leaveCustom}
                onKeyDown={(event) => event.key === 'Enter' && commitCustom()}
              />
            </span>
            <span className="flex flex-col gap-1">
              <FieldLabel htmlFor="label-height" className="text-[13px]">
                Height (mm)
              </FieldLabel>
              <Input
                id="label-height"
                type="number"
                inputMode="decimal"
                className="w-24"
                value={draft.height}
                aria-invalid={customProblem !== null}
                onChange={(event) => setDraft((was) => ({ ...was, height: event.target.value }))}
                onBlur={leaveCustom}
                onKeyDown={(event) => event.key === 'Enter' && commitCustom()}
              />
            </span>
            {customProblem && <p className="pb-1.5 text-[13px] text-warn">{customProblem}</p>}
          </div>
        )}
        <div className="-mt-2 mb-5 flex flex-wrap items-center gap-x-2 gap-y-1">
          {chosenSheet && (
            <p className="mr-auto text-[13px] text-muted-foreground">
              {chosenSheet.layout.cols} x {chosenSheet.layout.rows} labels on {chosenSheet.layout.page === 'a4' ? 'A4' : 'Letter'}.
            </p>
          )}
          <Button type="button" size="sm" variant="ghost" className={chosenSheet ? '' : 'ml-auto'} onClick={() => setEditing('new')}>
            <PlusIcon /> New sheet…
          </Button>
          {chosenSheet && (
            <>
              <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(chosenSheet)}>
                <PencilIcon /> Edit sheet…
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setDeleting(chosenSheet)}>
                <Trash2Icon /> Delete
              </Button>
            </>
          )}
        </div>
        {size.gone && (
          <p className="mb-4 rounded-md border border-border bg-muted/40 px-3 py-2 text-[13px] text-muted-foreground">The saved sheet was deleted. Choose another template.</p>
        )}
        {sheetProblem && (
          <p role="alert" className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
            {sheetProblem}
          </p>
        )}
        {placeMode ? (
          <p className="mb-5 text-[13px] text-muted-foreground">A place label prints its code, its name and its QR code.</p>
        ) : (
          <LabelFieldsPicker value={fields} onTick={pickField} />
        )}
        {templateSaver.failed && <p className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">The label choice could not be saved. It holds on this page only.</p>}
        {(outputSaver.failed || outputUnsaved) && <p className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">The file choice could not be saved. It holds on this page only.</p>}
        {pdfProblem && (
          <p role="alert" className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
            {pdfProblem}
          </p>
        )}

        {(tooMany || readyPlaces?.tooMany) && (
          <p role="alert" className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
            Print at most {MAX_LABELS} labels at a time
          </p>
        )}
        {readyPlaces?.error && (
          <p role="alert" className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
            {readyPlaces.error}
          </p>
        )}
        {readyPlaces && readyPlaces.failed.length > 0 && (
          <p role="alert" className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
            The {readyPlaces.failed.length === 1 ? 'label' : 'labels'} of {readyPlaces.failed.join(', ')} could not be read
          </p>
        )}
        {readyPlaces && readyPlaces.noReport.length > 0 && (
          <p className="mb-4 rounded-md border border-border bg-muted/40 px-3 py-2 text-[13px] text-muted-foreground">
            No AMS report for {readyPlaces.noReport.join(', ')}: an AMS slot of {readyPlaces.noReport.length === 1 ? 'it' : 'them'} gets a label once the printer reports or a
            spool has been loaded in it.
          </p>
        )}
        {readyPlaces && readyPlaces.tooLong.length > 0 && (
          <p role="alert" className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
            The address for {readyPlaces.tooLong.join(', ')} is too long for a QR code on this label. Shorten the label address in Settings.
          </p>
        )}
        {ready && ready.failed.length > 0 && (
          <p role="alert" className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
            {ready.failed.length === 1 ? 'One spool' : `${ready.failed.length} spools`} could not be read and {ready.failed.length === 1 ? 'has' : 'have'} no label: {ready.failed.join(', ')}
          </p>
        )}

        <div className="flex flex-wrap gap-3 rounded-md bg-muted/40 p-3">
          {labels.map(({ spool, svg }) => (
            <div key={spool.id} className="outline outline-1 outline-dashed outline-border">
              <Label spool={spool} svg={svg} size={size} fields={fields} />
            </div>
          ))}
          {placeLabels.map((one) => (
            <div key={one.id} className="outline outline-1 outline-dashed outline-border">
              <LocationLabel code={one.code} name={one.name} svg={one.svg} size={size} />
            </div>
          ))}
        </div>
      </div>
      {editing && <LabelSheetEditor sheet={editing === 'new' ? null : editing} onSaved={sheetSaved} onClose={() => setEditing(null)} />}
      <AlertDialog open={deleting !== null} onOpenChange={(open) => !open && setDeleting(null)}>
        <AlertDialogContent>
          {deleting && (
            <>
              <AlertDialogHeader>
                <AlertDialogTitle>Delete the sheet “{deleting.name}”?</AlertDialogTitle>
                <AlertDialogDescription>The sheet is deleted for every person on this ScadBuddy. When it is the label chosen, this page goes back to the roll label of 50 x 25 mm.</AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Keep it</AlertDialogCancel>
                <AlertDialogAction variant="destructive" onClick={() => void removeSheet(deleting)}>
                  Delete sheet
                </AlertDialogAction>
              </AlertDialogFooter>
            </>
          )}
        </AlertDialogContent>
      </AlertDialog>
      {createPortal(
        <div id="label-print-root" aria-hidden="true">
          {size.sheet
            ? pagesOf(printed, size.sheet.cols * size.sheet.rows).map((page, at) => (
                <div key={at} data-sheet-page="">
                  {page.map((one) => (
                    <div key={one.key}>{one.node}</div>
                  ))}
                </div>
              ))
            : printed.map((one) => <div key={one.key}>{one.node}</div>)}
        </div>,
        document.body,
      )}
    </div>
  )
}
