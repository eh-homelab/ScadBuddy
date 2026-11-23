import { ChevronLeftIcon, PrinterIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link, useSearchParams } from 'react-router'
import { api } from '../../api/client'
import { getLocation, getSpool, printerLabels, type SpoolDetail } from '../../api/filament'
import { Label, LocationLabel } from '../../components/filament/Label'
import { LABEL_HEIGHT_MM, LABEL_WIDTH_MM, httpsOrigin, labelBase, labelPayload, locationPayload, MAX_LABELS, MAX_PAYLOAD_BYTES, qrSvg } from '../../components/filament/labelSheet'
import { Button } from '@/components/ui/button'
import { Label as FieldLabel } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
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
 * page, so rounding adds no blank page); a sheet packs them on the paper with a margin.
 */
const printCss = (layout: Layout) => `
#label-print-root { display: none; }
@media print {
  body > *:not(#label-print-root) { display: none !important; }
  #label-print-root { display: block !important; background: white; }
  ${
    layout === 'roll'
      ? `@page { size: ${LABEL_WIDTH_MM}mm ${LABEL_HEIGHT_MM}mm; margin: 0; }
  #label-print-root > * { height: ${LABEL_HEIGHT_MM - 0.5}mm; overflow: hidden; break-after: page; }
  #label-print-root > *:last-child { break-after: auto; }`
      : `@page { margin: 10mm; }
  #label-print-root { display: flex !important; flex-wrap: wrap; gap: 2mm; width: 190mm; }`
  }
}`

/** How many spools the page reads at the same time. */
const READ_AT_ONCE = 6

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
  labels: PlaceLabel[]
  /** What could not be read, as the alert says it; no label is drawn then. */
  error: string | null
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
 * again (the ids are the page's only input) and drawn at 50 x 25 mm with its number and a
 * QR code of `scadbuddy:spool:<id>`. The layout, `roll` (one label per page, for a label
 * printer) or `sheet` (packed on paper), is remembered in this browser.
 */
export function LabelsPage() {
  const [params] = useSearchParams()
  const raw = params.get('ids') ?? ''
  const ids = useMemo(() => [...new Set(raw.split(',').map((id) => id.trim()).filter(Boolean))], [raw])
  const tooMany = ids.length > MAX_LABELS
  const key = ids.join(',')
  const [layout, setLayout] = useState<Layout>(storedLayout)
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  // #6955 — `?printers=<id>,<id>|all` (every place of those printers) or `?locations=<id>` (one place).
  const printersParam = params.get('printers')
  const locationParam = params.get('locations')?.trim() || null
  const placesKey = printersParam ? `printers:${printersParam}` : locationParam ? `locations:${locationParam}` : ''
  const [places, setPlaces] = useState<LoadedPlaces | null>(null)

  useEffect(() => {
    if (!placesKey) return
    let live = true
    const draw = async (base: string | null, rows: { id: string; code: string; name: string }[], noReport: string[] = []) => {
      const origin = labelBase(base)
      // A payload past 84 bytes would need a bigger QR code than the 19.8 mm box scans: no label for it.
      const fits = (row: { code: string }) => new TextEncoder().encode(locationPayload(origin, row.code)).length <= MAX_PAYLOAD_BYTES
      const tooLong = rows.filter((row) => !fits(row)).map((row) => row.code)
      const labels = await Promise.all(rows.filter(fits).map(async (row) => ({ ...row, svg: await qrSvg(locationPayload(origin, row.code)) })))
      if (live) setPlaces({ key: placesKey, base: origin, labels, error: null, tooLong, noReport })
    }
    const fail = (error: string) => () => {
      if (live) setPlaces({ key: placesKey, base: labelBase(null), labels: [], error, tooLong: [], noReport: [] })
    }
    if (printersParam) {
      const wanted = printersParam === 'all' ? null : [...new Set(printersParam.split(',').map((id) => id.trim()).filter(Boolean))]
      printerLabels(wanted)
        .then((answer) =>
          draw(
            answer.base_url,
            answer.labels.map((row) => ({ id: row.location_id, code: row.code, name: row.name })),
            answer.no_report,
          ),
        )
        .catch(fail('The printer labels could not be read'))
    } else if (locationParam) {
      // A failed settings read is an error, never "unset": the page's own origin may be the wrong host for a label.
      const base = settingsBase().catch((cause: unknown) => {
        throw new SettingsReadError(cause)
      })
      Promise.all([getLocation(locationParam), base])
        .then(([place, origin]) => draw(origin, [{ id: place.id, code: place.code, name: place.name }]))
        .catch((cause: unknown) =>
          fail(cause instanceof SettingsReadError ? 'The label address in Settings could not be read, so no label is drawn. Try again.' : `The label of ${locationParam} could not be read`)(),
        )
    }
    return () => {
      live = false
    }
  }, [placesKey, printersParam, locationParam])

  useEffect(() => {
    const wanted = key.split(',').filter(Boolean)
    if (tooMany || wanted.length === 0 || placesKey) return
    let live = true
    // At most `READ_AT_ONCE` reads in flight: up to 100 ids must not open 100 requests together.
    const results: (Loaded['labels'][number] | string)[] = new Array(wanted.length)
    let next = 0
    const worker = async () => {
      while (live && next < wanted.length) {
        const at = next++
        const id = wanted[at]!
        try {
          const spool = await getSpool(id)
          results[at] = { spool, svg: await qrSvg(labelPayload(spool.id)) }
        } catch {
          results[at] = id
        }
      }
    }
    void Promise.all(Array.from({ length: Math.min(READ_AT_ONCE, wanted.length) }, worker)).then(() => {
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

  return (
    <div className="h-full overflow-y-auto">
      <style>{printCss(layout)}</style>
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
                  ? placeLabels.length === 0 && readyPlaces.error === null && readyPlaces.tooLong.length === 0
                    ? 'No printers.'
                    : `${placeLabels.length} ready, ${LABEL_WIDTH_MM} x ${LABEL_HEIGHT_MM} mm each.`
                  : 'Loading labels…'
                : tooMany ? '' : ids.length === 0 ? 'No spools chosen.' : `${labels.length} of ${ids.length} ready, ${LABEL_WIDTH_MM} x ${LABEL_HEIGHT_MM} mm each.`}
            </p>
            {readyPlaces && readyPlaces.error === null && <p className="text-[13px] text-muted-foreground">{`Labels open ${readyPlaces.base}`}</p>}
          </div>
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
          <Button type="button" size="sm" disabled={count === 0} onClick={() => window.print()}>
            <PrinterIcon /> Print
          </Button>
        </header>

        {tooMany && (
          <p role="alert" className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
            Print at most {MAX_LABELS} labels at a time
          </p>
        )}
        {readyPlaces?.error && (
          <p role="alert" className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
            {readyPlaces.error}
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
              <Label spool={spool} svg={svg} />
            </div>
          ))}
          {placeLabels.map((one) => (
            <div key={one.id} className="outline outline-1 outline-dashed outline-border">
              <LocationLabel code={one.code} name={one.name} svg={one.svg} />
            </div>
          ))}
        </div>
      </div>
      {createPortal(
        <div id="label-print-root" aria-hidden="true">
          {labels.map(({ spool, svg }) => (
            <div key={spool.id}>
              <Label spool={spool} svg={svg} testId="print-label" />
            </div>
          ))}
          {placeLabels.map((one) => (
            <div key={one.id}>
              <LocationLabel code={one.code} name={one.name} svg={one.svg} testId="print-label" />
            </div>
          ))}
        </div>,
        document.body,
      )}
    </div>
  )
}
