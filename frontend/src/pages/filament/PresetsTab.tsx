import { XIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import { listTypes } from '../../api/filament'
import { listPresets, mirrorPreset, type PresetMirror, type PresetRow, type PresetSource } from '../../api/filamentPresets'
import { FilamentSearchBox } from '../../components/filament/FilamentSearchBox'
import { filamentLinks } from '../../lib/filamentLinks'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { count } from './plural'
import { canSendAgain, OUTCOME_TEXT, sendFailure, WRITES_OFF_TEXT } from './presetWrites'

const SOURCES: { id: PresetSource; label: string }[] = [
  { id: '3dfp', label: '3DFP' },
  { id: 'bambuddy', label: 'Bambuddy' },
  { id: 'manual', label: 'Manual' },
]

const SOURCE_LABEL: Record<PresetSource, string> = { '3dfp': '3DFP', bambuddy: 'Bambuddy', manual: 'Manual' }

const MIRROR_LABEL: Record<PresetMirror, string> = { sent: 'Sent', pending: 'Pending', failed: 'Failed', bambuddy_owned: 'In Bambuddy' }

const MIRROR_HINT: Record<PresetMirror, string> = {
  sent: 'Bambuddy has this version.',
  pending: 'Bambuddy has not got this version yet.',
  failed: 'The last send to Bambuddy failed.',
  bambuddy_owned: 'Bambuddy owns this preset.',
}

const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The presets could not be loaded. Try again.')

/** The first 8 characters of an id: the name of a type whose code is not loaded. */
const shortId = (id: string) => id.slice(0, 8)

const searchDoc = (row: PresetRow) => [row.name, row.ref, row.printer_model, row.filament_type, row.nozzle_diameter].filter(Boolean).join(' ').toLowerCase()

/** The mirror state of a preset. Its error is shown under it, and kept in the title. */
function MirrorState({ row }: { row: PresetRow }) {
  const title = row.mirror === 'failed' && row.mirror_error ? row.mirror_error : row.mirror_error ? `${MIRROR_HINT[row.mirror]} ${row.mirror_error}` : MIRROR_HINT[row.mirror]
  return (
    <>
      <span title={title} className={cn(row.mirror === 'failed' && 'font-medium text-warn', (row.mirror === 'pending' || row.mirror === 'bambuddy_owned') && 'text-muted-foreground')}>
        {MIRROR_LABEL[row.mirror]}
      </span>
      {row.mirror_error && <p className="mt-0.5 max-w-64 text-[12px] whitespace-normal text-muted-foreground">{row.mirror_error}</p>}
    </>
  )
}

/** #7204 — the Presets tab: ScadBuddy's own filament presets, from the three sources, with where each stands in Bambuddy. */
export function PresetsTab() {
  const [params, setParams] = useSearchParams()
  const source = SOURCES.find((one) => one.id === params.get('source'))?.id ?? null
  const search = params.get('q') ?? ''
  const typeId = params.get('type_id')
  const [rows, setRows] = useState<PresetRow[] | null>(null)
  const [served, setServed] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [typeCodes, setTypeCodes] = useState<Map<string, string>>(new Map())
  const [sending, setSending] = useState<number | null>(null)
  const [writesOff, setWritesOff] = useState(false)
  const [notice, setNotice] = useState<{ text: string; warn: boolean } | null>(null)

  useEffect(() => {
    let live = true
    // A new filter drops the last answer: the old type's rows never show under the new label.
    setRows(null)
    setError(null)
    listPresets({ typeId: typeId ?? undefined }).then(
      (list) => {
        if (!live) return
        setRows(list.rows)
        setServed(list.served)
        setError(null)
      },
      (cause: unknown) => {
        if (live) setError(message(cause))
      },
    )
    // The type codes, beside the list: a type that cannot be read shows by its place in the list only.
    listTypes({ includeArchived: true }).then(
      (types) => {
        if (live) setTypeCodes(new Map(types.map((type) => [type.id, type.code])))
      },
      () => {},
    )
    return () => {
      live = false
    }
  }, [typeId])

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next, { replace: true })
  }

  const sendAgain = async (row: PresetRow) => {
    setSending(row.id)
    setNotice(null)
    try {
      const result = await mirrorPreset(row.id)
      setRows((was) => was && was.map((one) => (one.id === row.id ? result.preset : one)))
      setNotice({ text: `${row.name}: ${OUTCOME_TEXT[result.outcome]}`, warn: result.outcome === 'failed' })
    } catch (cause) {
      const failure = sendFailure(cause)
      if (failure.off) setWritesOff(true)
      setNotice({ text: failure.off ? WRITES_OFF_TEXT : `${row.name}: ${failure.text}`, warn: true })
    } finally {
      setSending(null)
    }
  }

  const typeChip = typeId && (
    <Button type="button" variant="outline" size="sm" aria-label={`Clear the type filter (Type: ${typeCodes.get(typeId) ?? shortId(typeId)})`} onClick={() => setParam('type_id', null)}>
      {`Type: ${typeCodes.get(typeId) ?? shortId(typeId)}`}
      <XIcon />
    </Button>
  )

  if (error) {
    // The type chip stays: a type id the server refuses must not leave the URL as the only way out.
    return (
      <div className="flex flex-col items-start gap-2">
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
        {typeChip}
      </div>
    )
  }
  if (!rows) return <p className="text-[13px] text-muted-foreground">Loading presets…</p>
  if (!served) return <p className="text-[13px] text-muted-foreground">Presets are not on yet on this server.</p>

  const needle = search.trim().toLowerCase()
  const shown = rows.filter((row) => (!source || row.source === source) && (!needle || searchDoc(row).includes(needle)))
  return (
    <section aria-label="Presets" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-3">
        <FilamentSearchBox label="Search presets" placeholder="Search: name, printer, material  /" value={search} onChange={(text) => setParam('q', text || null)} className="w-full sm:w-80" />
        <div role="group" aria-label="Source" className="flex flex-wrap items-center gap-1">
          <Button type="button" variant={source ? 'outline' : 'secondary'} size="sm" aria-pressed={!source} onClick={() => setParam('source', null)}>
            All
          </Button>
          {SOURCES.map((one) => (
            <Button key={one.id} type="button" variant={source === one.id ? 'secondary' : 'outline'} size="sm" aria-pressed={source === one.id} onClick={() => setParam('source', one.id)}>
              {one.label}
            </Button>
          ))}
        </div>
        {typeChip}
      </div>
      {notice && (
        <p role={notice.warn ? 'alert' : 'status'} className={cn('text-[13px]', notice.warn ? 'text-warn' : 'text-muted-foreground')}>
          {notice.text}
        </p>
      )}
      <div className="relative overflow-x-auto rounded-md border">
        <table aria-label="Presets" className="w-full text-left text-[13px]">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th scope="col" className="px-2 py-1.5 font-medium">Name</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Source</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Type</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Printer</th>
              <th scope="col" className="px-2 py-1.5 text-right font-medium">Version</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Bambuddy</th>
              <th scope="col" className="px-2 py-1.5 text-right font-medium">Bambuddy id</th>
              <th scope="col" className="px-2 py-1.5 text-right font-medium">Used by</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((row) => (
              <tr key={row.id} data-preset={row.id} className="border-t">
                <td className="px-2 py-1.5">
                  <Link to={filamentLinks.preset(row.id)} className="font-medium underline-offset-4 hover:underline">
                    {row.name}
                  </Link>
                </td>
                <td className="px-2 py-1.5">{SOURCE_LABEL[row.source]}</td>
                <td className="px-2 py-1.5">
                  {row.type_id ? (
                    <Link to={filamentLinks.type(row.type_id)} aria-label={typeCodes.get(row.type_id) ?? `Type ${row.type_id}`} className="underline-offset-4 hover:underline">
                      {typeCodes.get(row.type_id) ?? shortId(row.type_id)}
                    </Link>
                  ) : (
                    <span className="text-muted-foreground" aria-label="No type">—</span>
                  )}
                </td>
                <td className="px-2 py-1.5">{[row.printer_model, row.nozzle_diameter && `${row.nozzle_diameter} mm`].filter(Boolean).join(', ')}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">{`v${row.version}`}</td>
                <td className="px-2 py-1.5">
                  <MirrorState row={row} />
                  {canSendAgain(row) && !writesOff && (
                    <Button type="button" variant="outline" size="sm" className="mt-1" disabled={sending !== null} onClick={() => sendAgain(row)}>
                      Send again
                    </Button>
                  )}
                </td>
                <td className="px-2 py-1.5 text-right tabular-nums">{row.bb_preset_id ?? <span className="text-muted-foreground">None</span>}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">{`${count(row.used_by_types, 'type')}, ${count(row.used_by_spools, 'spool')}`}</td>
              </tr>
            ))}
            {shown.length === 0 && (
              <tr className="border-t">
                <td colSpan={8} className="px-2 py-3 text-muted-foreground">
                  {rows.length === 0 ? (typeId ? 'No preset has this type.' : 'No presets yet. Fetch a profile on a type, or pull from Bambuddy.') : needle ? `No preset matches “${search}”.` : 'No preset has this source.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}
