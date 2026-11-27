import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError } from '../../../api/client'
import {
  getResetPreview,
  getRfidStatus,
  getSyncStatus,
  importBambuddyPresets,
  importBambuddySpools,
  listSyncPasses,
  reconcileRfid,
  resetSyncLinks,
  runSync,
  setOverrideMode,
  type LinksResetCounts,
  type LinksResetDone,
  type LinksResetPreview,
  type PresetImport,
  type PullReport,
  type RfidReconcileReport,
  type RfidStatus,
  type SyncError,
  type SyncPass,
  type SyncStatus,
} from '../../../api/filament'
import { statusAfterKeptReset } from './resetStatus'
import { AppLink } from '../../../components/AppLink'
import { filamentLinks } from '../../../lib/filamentLinks'
import { ChoiceSelect, SwitchField } from '../../../components/settings/form'
import { shortId, timeAgo } from '../../../lib/format'
import { useAsync, type AsyncState } from '../../../lib/useAsync'
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
import { Spinner } from '@/components/ui/spinner'
import { Checkbox } from '@/components/ui/checkbox'
import { Switch } from '@/components/ui/switch'
import { Input } from '@/components/ui/input'
import { FieldRow } from '../controls'
import { docOf } from '../fields'
import { FieldBadge, RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings, type BackendSettings } from '../useBackendSettings'
import {
  FETCH_NOZZLES,
  fieldsOf,
  nozzlesValue,
  SYNC_KINDS,
  SYNC_PASS_TTL,
  syncParts,
  syncValue,
  toPatchValue,
  type SyncDirection,
  type SyncKind,
} from '../values'

export const id = 'filament'
export const area: AreaId = 'printing'
export const title = 'Filament'
export const order = 30

const LINK = 'underline underline-offset-2 hover:text-foreground'

/** #6265 — the four directions each data type takes. */
const DIRECTION_CHOICES = [
  { value: 'off', label: 'Off' },
  { value: 'sb_to_bb', label: 'ScadBuddy → Bambuddy' },
  { value: 'bb_to_sb', label: 'Bambuddy → ScadBuddy' },
  { value: 'two_way', label: 'Both ways (newer change wins)' },
] as const satisfies readonly { value: SyncDirection; label: string }[]

/** Each data type's row: its label and what it moves. */
const SYNC_ROWS: Record<SyncKind, { label: string; help: string }> = {
  spools: {
    label: 'Spools and types',
    help: "A spool's own fields, new spools and the spool catalog. With spools off, no spool is created or linked, so weights, presets, suppliers per spool and a spool's location move only on spools linked earlier. Supplier and location rows themselves sync on their own setting.",
  },
  weights: {
    label: 'Weights',
    help: 'Weight readings, scale weighings, dryings and the weight lock of spools ScadBuddy already tracks. A new spool still gets its first Bambuddy weight reading.',
  },
  tags: {
    label: 'Tags',
    help: 'Tag bindings. A new Bambuddy spool takes its tags whatever this says.',
  },
  suppliers: {
    label: 'Suppliers and prices',
    help: "Supplier rows. Each spool's suppliers and prices only ever go to Bambuddy, where they replace Bambuddy's; they are never taken from it.",
  },
  locations: {
    label: 'Locations',
    help: 'Location rows and where each spool is.',
  },
  presets: {
    label: 'Presets and K values',
    help: "Each spool's filament presets and K values. While Bambuddy's changes come in (Bambuddy → ScadBuddy or Both ways), each pull also makes the presets your spools and types name by Bambuddy's id ScadBuddy's own, and presets taken from Bambuddy earlier take its later edits. Otherwise nothing copies them unless you import them below.",
  },
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** What the override mode's status says, in a sentence. */
function statusLine(status: RfidStatus): string {
  switch (status.state) {
    case 'applied':
      // #7156 — the setting holds Bambuddy's auto-add off too; say which hold it is.
      return status.override_mode || status.auto_add_unknown_rfid !== false
        ? 'Bambuddy no longer adds unknown RFID spools'
        : `Bambuddy no longer adds unknown RFID spools, because "${AUTO_ADD_LABEL}" is off`
    case 'blocked':
      return `Blocked: ${status.detail ?? 'Bambuddy refused the change'}`
    case 'pending':
      return status.detail === 'busy'
        ? 'Another update is running; it applies on the next refresh'
        : `Not applied yet${status.detail ? `: ${status.detail}` : '. It applies on the next refresh'}`
    default:
      return 'Bambuddy adds a spool for each RFID tag it does not know'
  }
}

function reportLine(report: RfidReconcileReport): string {
  const parts = [plural(report.tags.pushed, 'tag sent', 'tags sent')]
  if (report.tags.failed > 0) parts.push(`${plural(report.tags.failed, 'tag was', 'tags were')} not sent; they stay queued for the next refresh`)
  const failedSlots = report.slots?.failed ?? 0
  if (failedSlots > 0) parts.push(`${plural(failedSlots, 'slot was', 'slots were')} not set; ${failedSlots === 1 ? 'it is' : 'they are'} tried again on the next refresh`)
  const unconfirmed = report.slots?.unconfirmed ?? 0
  if (unconfirmed > 0) parts.push(`${plural(unconfirmed, 'slot was', 'slots were')} sent, but the printer did not confirm in time; ${unconfirmed === 1 ? 'it is' : 'they are'} checked again on the next refresh`)
  const waiting = report.slots?.waiting ?? 0
  if (waiting > 0) parts.push(`${plural(waiting, 'slot was', 'slots were')} not sent yet (the printer was printing or its AMS busy, or time ran out); ${waiting === 1 ? 'it is' : 'they are'} set on a later refresh`)
  const noTemps = report.slots?.no_temps ?? 0
  if (noTemps > 0) parts.push(`${plural(noTemps, 'spool has', 'spools have')} no nozzle temperatures, so its slot is not set`)
  const noPreset = report.slots?.no_preset ?? 0
  if (noPreset > 0) parts.push(`${plural(noPreset, 'spool has', 'spools have')} no preset for the nozzle, so ${noPreset === 1 ? 'its slot is' : 'their slots are'} not set`)
  const invalidTemps = report.slots?.invalid_temps ?? 0
  if (invalidTemps > 0) parts.push(`${plural(invalidTemps, 'spool has', 'spools have')} a minimum nozzle temperature above the maximum, so ${invalidTemps === 1 ? 'its slot is' : 'their slots are'} not set`)
  const unsupported = report.slots?.unsupported_unit ?? 0
  if (unsupported > 0) parts.push(`${plural(unsupported, 'slot is', 'slots are')} on a unit ScadBuddy cannot configure`)
  const noNozzle = report.slots?.no_nozzle ?? 0
  if (noNozzle > 0) parts.push(`${plural(noNozzle, 'slot', 'slots')}: the printer does not report the nozzle for ${noNozzle === 1 ? 'its tray, so it is' : 'their trays, so they are'} not set`)
  return `${parts.join('. ')}.`
}

/**
 * #6265 — which way each kind of filament data moves between ScadBuddy and Bambuddy. The six
 * directions are one setting, `filament_sync`, held in the form as one string (`syncParts`,
 * `syncValue`) and saved with "Save Filament". The first row carries the setting's badge,
 * environment variable and error.
 */
function SyncRows({ form }: { form: BackendSettings }) {
  const sync = syncParts(form.value('filament_sync'))
  const doc = docOf(form.settings, 'filament_sync')
  return (
    <>
      {SYNC_KINDS.map((kind, i) => {
        const id = i === 0 ? 'setting-filament_sync' : `setting-filament-sync-${kind}`
        return (
          <FieldRow
            key={kind}
            id={id}
            label={SYNC_ROWS[kind].label}
            help={SYNC_ROWS[kind].help}
            {...(i === 0 ? { env: doc.env, badge: <FieldBadge form={form} name="filament_sync" />, error: form.errors.filament_sync } : {})}
          >
            <ChoiceSelect
              id={id}
              value={sync[kind]}
              onValueChange={(next) => form.setField('filament_sync', syncValue({ ...sync, [kind]: next as SyncDirection }))}
              choices={DIRECTION_CHOICES}
            />
          </FieldRow>
        )
      })}
    </>
  )
}

/** #7244 — what the presets import found, in a sentence. */
function importLine(done: PresetImport): string {
  const unresolved = done.unresolved ?? 0
  if (done.adopted === 0 && done.updated === 0 && unresolved === 0)
    return "Nothing to import: no spool or type names a Bambuddy preset, and none taken from Bambuddy changed since."
  const parts = [
    done.adopted > 0 && `${plural(done.adopted, 'new preset', 'new presets')} from Bambuddy`,
    done.updated > 0 && `${plural(done.updated, 'preset', 'presets')} updated to Bambuddy's version`,
  ].filter(Boolean)
  const imported = parts.length > 0 ? `Imported: ${parts.join(', ')}.` : 'Nothing imported.'
  if (unresolved === 0) return imported
  const one = unresolved === 1
  return (
    `${imported} ${plural(unresolved, 'Bambuddy preset', 'Bambuddy presets')} that spools or types name could not be imported: ` +
    `for example, Bambuddy no longer holds ${one ? 'it' : 'them'}, or ${one ? 'it is' : 'they are'} not a filament preset. ` +
    'Choose a ScadBuddy preset for those spools and types.'
  )
}

/** What the presets import says when the server refuses it, in plain words. */
function importProblem(cause: unknown): ReactNode {
  if (!(cause instanceof ApiError)) return "Bambuddy's presets could not be imported. Try again."
  if (cause.status === 409 && cause.problem.code === 'instance_changed')
    return 'The Bambuddy links belong to another Bambuddy. Reset them under Sync status, then try again.'
  if (cause.status === 409)
    return (
      <>
        No Bambuddy is set up. Add its address and API key in{' '}
        <AppLink href="/settings/general#connection" className={LINK}>
          Connection
        </AppLink>
        , then try again.
      </>
    )
  return cause.detail
}

/**
 * #7244 — a one-time import of Bambuddy's filament presets into ScadBuddy's own, whatever the
 * presets direction says (the pull imports them only while it is Bambuddy → ScadBuddy or Both
 * ways). It saves nothing of the form.
 */
function PresetsImportRow() {
  const [pending, setPending] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [problem, setProblem] = useState<ReactNode>(null)

  async function importNow() {
    setPending(true)
    setNote(null)
    setProblem(null)
    try {
      setNote(importLine(await importBambuddyPresets()))
    } catch (cause) {
      setProblem(importProblem(cause))
    } finally {
      setPending(false)
    }
  }

  return (
    <FieldRow
      id="filament-presets-import"
      label="Bambuddy's presets"
      help="Copies Bambuddy's filament presets into ScadBuddy's own once, whatever the presets direction says: presets your spools and types name by Bambuddy's id become ScadBuddy's, and presets taken from Bambuddy earlier take its later edits. Nothing is sent to Bambuddy."
    >
      <div id="filament-presets-import" className="flex flex-col gap-2 text-sm">
        <div>
          <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => void importNow()}>
            {pending && <Spinner />}
            Import presets from Bambuddy now
          </Button>
        </div>
        <p role="status" aria-live="polite" className="text-muted-foreground empty:hidden">
          {note}
        </p>
        {problem && (
          <p role="alert" className="text-warn">
            {problem}
          </p>
        )}
      </div>
    </FieldRow>
  )
}

/** What the spool import found, in plain words (#6261). */
function spoolImportLine(done: PullReport): string {
  const unread = done.invalid + (done.failed_bb_ids?.length ?? 0)
  const parts = [
    done.created > 0 && plural(done.created, 'new spool', 'new spools'),
    done.linked > 0 && `${plural(done.linked, 'spool', 'spools')} linked`,
    done.updated > 0 && `${plural(done.updated, 'spool', 'spools')} updated`,
  ].filter(Boolean)
  const lines = [
    parts.length > 0 ? `Imported: ${parts.join(', ')}.` : null,
    done.needs_type > 0 && `${plural(done.needs_type, 'spool needs', 'spools need')} a type: choose one in Filament.`,
    unread > 0 && `${plural(unread, 'Bambuddy spool', 'Bambuddy spools')} could not be read.`,
    done.rfid_duplicates > 0 && `${plural(done.rfid_duplicates, 'RFID duplicate', 'RFID duplicates')} skipped.`,
  ].filter(Boolean)
  if (lines.length === 0) return 'Nothing to import: ScadBuddy has every Bambuddy spool, and none changed since the last pull.'
  if (parts.length === 0) lines.unshift('Nothing imported.')
  return lines.join(' ')
}

/** What the spool import says when the server refuses it: the presets import's words, plus its own 503. */
function spoolImportProblem(cause: unknown): ReactNode {
  if (!(cause instanceof ApiError)) return "Bambuddy's spools could not be imported. Try again."
  if (cause.status === 503) return 'The import did not finish in time and may still be running. Check the spools before you import again.'
  return importProblem(cause)
}

/**
 * #6261 — a one-time pull of Bambuddy's spools into ScadBuddy's own, whatever the spools direction
 * says (the refresh Schedule does the same). It never writes to Bambuddy and saves nothing of the form.
 */
function SpoolsImportRow() {
  const [pending, setPending] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [problem, setProblem] = useState<ReactNode>(null)

  async function importNow() {
    setPending(true)
    setNote(null)
    setProblem(null)
    try {
      setNote(spoolImportLine(await importBambuddySpools()))
    } catch (cause) {
      setProblem(spoolImportProblem(cause))
    } finally {
      setPending(false)
    }
  }

  return (
    <FieldRow
      id="filament-spools-import"
      label="Bambuddy's spools"
      help="Pulls Bambuddy's spools, archived ones too, into ScadBuddy's own once, whatever the spools direction says. A spool already linked takes what Bambuddy changed since the last pull; any other becomes a new spool. It can take a few minutes. Nothing is sent to Bambuddy."
    >
      <div id="filament-spools-import" className="flex flex-col gap-2 text-sm">
        <div>
          <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => void importNow()}>
            {pending && <Spinner />}
            Import spools from Bambuddy now
          </Button>
        </div>
        <p role="status" aria-live="polite" className="text-muted-foreground empty:hidden">
          {note}
        </p>
        {problem && (
          <p role="alert" className="text-warn">
            {problem}
          </p>
        )}
      </div>
    </FieldRow>
  )
}

const KIND_LABELS: Record<SyncError['kind'], string> = {
  spool: 'Spool',
  supplier: 'Supplier',
  location: 'Location',
  insert_type: 'Insert type',
  filament: 'Spool type',
}

/** Where an item that failed to sync is shown (`lib/filamentLinks.ts`), from its id. */
function itemHref(error: SyncError): string {
  switch (error.kind) {
    case 'spool':
      return filamentLinks.spool(error.sb_id)
    case 'supplier':
      return filamentLinks.supplier(error.sb_id)
    case 'filament':
      return filamentLinks.type(error.sb_id)
    case 'location':
      return filamentLinks.location(error.sb_id)
    case 'insert_type':
      return filamentLinks.catalog('insert-types')
  }
}

const PROBLEMS = 'https://scadbuddy.dev/problems/'

/** An error label (`rfid_push.error_label`: a problem type, `:<status>` when Bambuddy answered one) in plain words. */
function syncErrorText(label: string): string {
  if (label === 'name_taken') return 'Not sent: another Bambuddy row has this name. Rename one of them.'
  if (label === 'internal') return 'ScadBuddy failed while it wrote this.'
  // #6905 — a pass that failed on ScadBuddy's own API (`bb_sync_run.failure_label`).
  if (label.startsWith('crud_unavailable:'))
    return label.endsWith(':transport') ? "ScadBuddy's own API did not answer in time." : "ScadBuddy's own API could not be reached."
  if (label.startsWith('crud_refused:')) return `ScadBuddy's own API refused a step (HTTP ${label.slice('crud_refused:'.length)}).`
  const [type, status] = label.startsWith(PROBLEMS) ? label.slice(PROBLEMS.length).split(':') : [label, undefined]
  const code = status ? ` (HTTP ${status})` : ''
  switch (type) {
    case 'bambuddy-rejected':
      return `Bambuddy refused it${code}.`
    case 'bambuddy-unavailable':
      return `Bambuddy did not answer${code}.`
    case 'bambuddy-not-found':
      return `Bambuddy no longer has it${code}.`
    case 'bambuddy-conflict':
      return `Bambuddy reported a conflict${code}.`
    case 'bambuddy-scope':
      return `The Bambuddy API key may not make this change${code}.`
    case 'bambuddy-not-configured':
      return 'No Bambuddy is set up.'
    default:
      return label
  }
}

const SKIPPED_TEXT: Record<NonNullable<SyncStatus['skipped']>, string> = {
  no_bambuddy: 'No pass runs: no Bambuddy is set up.',
  nothing_pushes: 'No pass runs: no data type but tags is set to send to Bambuddy (tags go with the refresh).',
}

/** What "Sync now" says when the server refuses it, in plain words. */
function runProblem(cause: unknown): ReactNode {
  if (!(cause instanceof ApiError)) return 'The sync could not be asked for. Try again.'
  if (cause.status === 409 && cause.problem.code === 'no_bambuddy')
    return (
      <>
        No Bambuddy is set up. Add its address and API key in{' '}
        <AppLink href="/settings/general#connection" className={LINK}>
          Connection
        </AppLink>
        , then try again.
      </>
    )
  if (cause.status === 409 && cause.problem.code === 'nothing_pushes')
    return 'Nothing is set to go to Bambuddy. Set a data type to "ScadBuddy → Bambuddy" or "Both ways", save Filament, then try again.'
  if (cause.status === 503) return 'The sync could not be started. Try again later.'
  return cause.detail
}

/** R-T11-2: after "Sync now", how often the status is read until the pass lands, and for how long. */
const SYNC_POLL_MS = 3_000
const SYNC_POLL_LIMIT_MS = 60_000

function When({ at }: { at: string }) {
  return (
    <time dateTime={at} title={new Date(at).toLocaleString()}>
      {timeAgo(at)}
    </time>
  )
}

/** The last pass's counts, those above zero beyond the written and failed. */
function countsLine(counts: SyncStatus['counts']): string {
  const parts = [`${counts.written ?? 0} written`, `${counts.failed ?? 0} failed`]
  if (counts.deferred) parts.push(`${counts.deferred} deferred to the next pass`)
  if (counts.conflicts) parts.push(plural(counts.conflicts, 'conflict', 'conflicts'))
  if (counts.unmapped_k) parts.push(`${plural(counts.unmapped_k, 'K value', 'K values')} with no Bambuddy match`)
  return `${parts.join(', ')}.`
}

/**
 * #6773 — each thing a reset of the Bambuddy links deletes, in the order the dialog lists it; the
 * first `LINK_ROWS` are links (a colour's link to Bambuddy's colour catalog too, #6907, and a
 * ScadBuddy filament preset's Bambuddy id, #7176, which the mirror then creates on the new Bambuddy).
 */
const RESET_ROWS: { key: keyof LinksResetCounts; one: string; many: string }[] = [
  { key: 'spools', one: 'spool link', many: 'spool links' },
  { key: 'filaments', one: 'spool type link', many: 'spool type links' },
  { key: 'insert_types', one: 'insert type link', many: 'insert type links' },
  { key: 'suppliers', one: 'supplier link', many: 'supplier links' },
  { key: 'locations', one: 'location link', many: 'location links' },
  { key: 'colours', one: 'colour link', many: 'colour links' },
  { key: 'presets', one: 'filament preset link', many: 'filament preset links' },
  { key: 'intents', one: 'unfinished create', many: 'unfinished creates' },
  { key: 'errors', one: 'listed sync error', many: 'listed sync errors' },
  { key: 'ignored', one: 'ignored Bambuddy spool', many: 'ignored Bambuddy spools' },
]

const LINK_ROWS = 7

/** What a refused reset says, in plain words. */
function resetProblem(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'The reset could not be sent. Try again.'
  switch (cause.status === 409 ? cause.problem.code : undefined) {
    case 'confirm_mismatch':
      return 'The links changed since this dialog opened. Nothing was deleted. Close it and try again.'
    case 'same_instance':
      return 'The links already belong to the Bambuddy that is set up now. Nothing was deleted.'
    case 'nothing_to_reset':
      return 'There are no Bambuddy links to reset.'
    case 'proof_changed':
      return 'The Bambuddy set up now is no longer proven to be the same one. Nothing was deleted. Read what a reset now deletes before you reset.'
    default:
      return cause.detail
  }
}

/**
 * #6773 — after the Bambuddy URL changes, the links (Bambuddy ids) belong to the old Bambuddy and
 * the sync stops (`instance_changed`). "Reset Bambuddy links" reads what a reset would delete
 * (`GET /filament/sync/reset-links`), names it in a confirm dialog, and resets only the instance
 * that dry run read, so a stale page cannot reset another Bambuddy's links. No ScadBuddy row is
 * deleted. Then a full pass links the new Bambuddy afresh.
 * #6845 — when the dry run says `same_bambuddy` (the Bambuddy set up now holds the linked spools as
 * ScadBuddy last saw them; with no spool linked, the linked filament presets, #7343), the reset sends `expect_kept`, deletes nothing and keeps every link, and the
 * dialog says so. When the server can no longer prove it (`proof_changed`, R-6845-6), nothing is deleted:
 * the dialog reads the dry run again and says why. A Bambuddy the dry run could not read
 * (`bambuddy_unreadable`) gets the delete, with a note.
 * #7544 — the warning that each filament preset is copied to Bambuddy again shows only while the
 * saved preset mirror (`mirror`) is on: with it off, nothing is copied after the delete.
 * The backend also needs a Bambuddy URL (`preset_mirror.py`); this dialog shows only on
 * `instance_changed`, which a configured URL implies, so `mirror` alone is enough here (#7738).
 */
function ResetLinks({ mirror, onReset }: { mirror: boolean; onReset: (done: LinksResetDone) => void }) {
  const [opening, setOpening] = useState(false)
  const [preview, setPreview] = useState<LinksResetPreview | null>(null)
  const [resetting, setResetting] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  async function open() {
    setOpening(true)
    setProblem(null)
    try {
      setPreview(await getResetPreview())
    } catch (cause) {
      setProblem(cause instanceof ApiError ? cause.detail : 'What a reset deletes could not be read. Try again.')
    } finally {
      setOpening(false)
    }
  }

  async function reset() {
    if (!preview?.instance) return
    setResetting(true)
    setProblem(null)
    try {
      const done = await resetSyncLinks(preview.instance, preview.same_bambuddy)
      setPreview(null)
      onReset(done)
    } catch (cause) {
      setProblem(resetProblem(cause))
      if (cause instanceof ApiError && cause.status === 409 && cause.problem.code === 'proof_changed') {
        try {
          setPreview(await getResetPreview())
        } catch {
          // The message already says nothing was deleted; the old counts stay shown.
        }
      }
    } finally {
      setResetting(false)
    }
  }

  const rows = preview ? RESET_ROWS.filter((row) => preview.counts[row.key] > 0) : []
  const same = preview?.same_bambuddy === true
  return (
    <div className="flex flex-col gap-2">
      <p className="text-warn">
        The links belong to another Bambuddy than the one set up now, so the sync is stopped. Reset the links to link the new
        Bambuddy.
      </p>
      <div>
        <Button type="button" size="sm" variant="outline" disabled={opening} onClick={() => void open()}>
          {opening ? <Spinner /> : 'Reset Bambuddy links'}
        </Button>
      </div>
      {problem && !preview && (
        <p role="alert" className="text-warn">
          {problem}
        </p>
      )}
      <AlertDialog open={preview !== null} onOpenChange={(next) => {
          if (next || resetting) return
          setPreview(null)
          setProblem(null)
        }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{same ? 'Keep the Bambuddy links?' : 'Reset the Bambuddy links?'}</AlertDialogTitle>
            <AlertDialogDescription>
              {same
                ? 'The Bambuddy set up now holds the linked spools (or, with no spool linked, the linked filament presets) as ScadBuddy last saw them, so it is the same Bambuddy at a new address. Nothing is deleted: every link now belongs to it.'
                : 'ScadBuddy forgets which Bambuddy rows its own rows match. No ScadBuddy spool, type, supplier or location is deleted, and nothing is deleted in either Bambuddy.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {preview && !same && (
            <div className="flex flex-col gap-2 text-sm">
              {preview.changed_since_pull && (
                <p className="text-warn">
                  Every linked spool (or, with no spool linked, filament preset) ScadBuddy could compare (a time on both
                  sides) changed in Bambuddy since ScadBuddy last saw it, so ScadBuddy cannot prove this is the same
                  Bambuddy. Only the reset is offered. Reset only if it is another Bambuddy.
                </p>
              )}
              {mirror && preview.counts.presets > 0 && (
                <p className="text-warn">
                  Each filament preset is then copied to the Bambuddy set up now again. If it is the same Bambuddy at a new
                  address, it then holds those presets twice, and its spools and AMS slots keep naming the old copies.
                </p>
              )}
              {preview.bambuddy_unreadable && (
                <p className="text-warn">
                  The Bambuddy set up now could not be read, so ScadBuddy cannot tell whether it is the same one at a new
                  address. Reset only if it is another Bambuddy, or close this and try again later.
                </p>
              )}
              {rows.length > 0 ? (
                <>
                  <p>This deletes:</p>
                  <ul aria-label="What the reset deletes" className="list-disc pl-5">
                    {rows.map((row) => (
                      <li key={row.key}>
                        <span className="sb-num">{preview.counts[row.key]}</span>{' '}
                        {preview.counts[row.key] === 1 ? row.one : row.many}
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <p>No link is left; this clears the old Bambuddy so the sync can start again.</p>
              )}
              <p className="text-muted-foreground">
                Then the sync links the new Bambuddy afresh. A spool is linked to the Bambuddy spool that holds its RFID tag. When
                suppliers and locations come from Bambuddy, each is linked to the Bambuddy row of the same name. Other spools are created on the side the sync sends
                them to, so a spool with no tag can end up twice.
              </p>
            </div>
          )}
          {problem && (
            <p role="alert" className="text-sm text-warn">
              {problem}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={resetting}>Cancel</AlertDialogCancel>
            {/* Not an AlertDialogAction: the dialog stays open until the reset answers. */}
            <Button variant={same ? 'default' : 'destructive'} onClick={() => void reset()} disabled={resetting || !preview?.instance}>
              {resetting ? <Spinner /> : same ? 'Keep links' : 'Reset links'}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

/**
 * #6265 — how the sync stands (`GET /filament/sync/status`): the last pass and the last clean
 * one, why no pass runs, and each item whose last write failed or was held back. "Sync now"
 * asks for a full pass, which runs in the background, then reads the status again.
 */
function SyncStatusRow({ status, mirror }: { status: AsyncState<SyncStatus>; mirror: boolean }) {
  const [pending, setPending] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [problem, setProblem] = useState<ReactNode>(null)
  /** While a pass asked for has not landed: the server's `requested_at` from the run's 202, as an ISO string. */
  const [waiting, setWaiting] = useState<{ requestedAt: string } | null>(null)
  const data = status.data
  const { setData } = status

  // R-T11-2 — the pass runs in the background: read the status every SYNC_POLL_MS until
  // a pass has started at or after the run's `requested_at` (both the server's clock, never the
  // browser's), for at most SYNC_POLL_LIMIT_MS. A timer pass or an earlier nudge's pass that
  // started before the request does not end it (Decision 162).
  // Unmounting stops it.
  useEffect(() => {
    if (!waiting) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let reads = 0
    const tick = async () => {
      reads += 1
      try {
        const next = await getSyncStatus()
        if (stopped) return
        setData(next, { supersede: true })
        if (next.last_pass_started_at && new Date(next.last_pass_started_at) >= new Date(waiting.requestedAt)) {
          setNote('Sync finished.')
          setWaiting(null)
          return
        }
      } catch {
        if (stopped) return
      }
      if (reads * SYNC_POLL_MS >= SYNC_POLL_LIMIT_MS) {
        setNote('The pass is still running. Check back later.')
        setWaiting(null)
        return
      }
      timer = setTimeout(() => void tick(), SYNC_POLL_MS)
    }
    timer = setTimeout(() => void tick(), SYNC_POLL_MS)
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [waiting, setData])

  /** #6773 — the reset nudged a full pass: say so, show the status without the old links (#6845: or with the kept ones), and wait for that pass. */
  function resetDone(done: LinksResetDone) {
    setProblem(null)
    if (done.kept) {
      setNote('Links kept. A full pass now syncs with the Bambuddy at its new address.')
      if (data) setData(statusAfterKeptReset(data, done), { supersede: true })
    } else {
      const deleted = RESET_ROWS.slice(0, LINK_ROWS).reduce((sum, row) => sum + done.deleted[row.key], 0)
      setNote(`${plural(deleted, 'link', 'links')} reset. A full pass now links the new Bambuddy.`)
      if (data) setData({ ...data, instance: null, instance_changed: false, errors: [] }, { supersede: true })
    }
    setWaiting({ requestedAt: new Date().toISOString() })
  }

  async function syncNow() {
    setPending(true)
    setNote(null)
    setProblem(null)
    try {
      const run = await runSync()
      setNote('Sync started. It runs in the background; this status updates when the pass is done.')
      setWaiting({ requestedAt: run.requested_at })
    } catch (cause) {
      setProblem(runProblem(cause))
    } finally {
      setPending(false)
    }
  }

  return (
    <FieldRow id="filament-sync-status" label="Sync status" help="Each item whose last write to Bambuddy failed stays listed until a later write works.">
      <div id="filament-sync-status" className="flex flex-col gap-2 text-sm">
        {data && (
          <div role="status" className="flex flex-col gap-1">
            <p>
              {data.last_pass_at ? (
                <>
                  Last pass <When at={data.last_pass_at} />: {countsLine(data.counts)}
                </>
              ) : (
                'No pass yet.'
              )}
            </p>
            {data.last_ok_at && data.last_ok_at !== data.last_pass_at && (
              <p className="text-muted-foreground">
                Last success <When at={data.last_ok_at} />.
              </p>
            )}
            {data.last_error && <p className="text-warn">The last pass failed: {syncErrorText(data.last_error)}</p>}
            {data.skipped && <p className="text-muted-foreground">{SKIPPED_TEXT[data.skipped]}</p>}
          </div>
        )}
        {data?.instance_changed && <ResetLinks mirror={mirror} onReset={resetDone} />}
        {data && data.errors.length > 0 && (
          <ul aria-label="Items not synced" className="flex flex-col gap-2">
            {data.errors.map((error) => {
              // The item's own name (a spool's `#N`); its id and "(deleted)" when the item is gone (R-6869-5).
              const name = `${KIND_LABELS[error.kind]} ${error.label ?? shortId(error.sb_id)}`
              return (
                <li key={`${error.kind}:${error.sb_id}`} className="flex flex-col gap-0.5">
                  <span className="font-medium">
                    {error.label == null ? (
                      <>
                        {name} <span className="font-normal text-muted-foreground">(deleted)</span>
                      </>
                    ) : (
                      <AppLink href={itemHref(error)} className={LINK}>
                        {name}
                      </AppLink>
                    )}
                  </span>
                  <span className="text-warn">{syncErrorText(error.error)}</span>
                  <span className="text-muted-foreground">
                    {plural(error.attempts, 'attempt', 'attempts')}, last <When at={error.at} />
                  </span>
                </li>
              )
            })}
          </ul>
        )}
        {status.error && <p className="text-warn">The sync status could not be read. Reload the page to try again.</p>}
        <div>
          <Button type="button" size="sm" variant="outline" disabled={pending || waiting !== null} onClick={() => void syncNow()}>
            Sync now
          </Button>
        </div>
        <p role="status" aria-live="polite" className="text-muted-foreground empty:hidden">
          {note}
        </p>
        {problem && (
          <p role="alert" className="text-warn">
            {problem}
          </p>
        )}
      </div>
    </FieldRow>
  )
}

/** #6905 — what started a pass, as a person says it. */
const TRIGGER_TEXT: Record<NonNullable<SyncPass['trigger']>, string> = {
  timer: 'On the timer',
  change: 'After a change',
  full: 'Full pass',
  more: 'Follow-up',
}

/** How long a pass took, from its worker's start to the server's record; null when the clocks disagree. */
function duration(pass: SyncPass): string | null {
  const ms = new Date(pass.ended_at).getTime() - new Date(pass.started_at).getTime()
  if (!Number.isFinite(ms) || ms < 0) return null
  if (ms < 1_000) return 'under a second'
  const seconds = Math.round(ms / 1_000)
  return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`
}

/** A pass's outcome: it failed, did nothing, or worked. */
function outcome(pass: SyncPass): 'failed' | 'idle' | 'ok' {
  if (pass.error) return 'failed'
  if (pass.skipped || pass.instance_changed) return 'idle'
  return 'ok'
}

const OUTCOME_RULE = { failed: 'bg-warn', idle: 'bg-line-strong', ok: 'bg-ok' } as const

/** #6905 — why a past pass did nothing. */
const PASS_SKIPPED_TEXT: Record<NonNullable<SyncPass['skipped']>, string> = {
  no_bambuddy: 'Skipped: no Bambuddy was set up.',
  nothing_pushes: 'Skipped: nothing was set to go to Bambuddy.',
}

function passLine(pass: SyncPass): string {
  if (pass.skipped) return PASS_SKIPPED_TEXT[pass.skipped]
  if (pass.instance_changed) return 'Stopped: the links belong to another Bambuddy.'
  if (pass.error && !pass.written && !pass.failed) return 'Nothing written.'
  const { written, failed, deferred, conflicts, unmapped_k } = pass
  return countsLine({ written, failed, deferred, conflicts, unmapped_k })
}

/**
 * #6905 — the sync's recent passes (`GET /filament/sync/passes`), newest first, failed ones
 * too, a page at a time. Each row's rule says how it went. `lastPassAt` is the status's
 * last pass: when it moves, the first page is read again.
 */
function SyncPassesRow({ lastPassAt, form }: { lastPassAt: string | null | undefined; form: BackendSettings }) {
  const [passes, setPasses] = useState<SyncPass[] | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [reading, setReading] = useState(true)
  const [failed, setFailed] = useState(false)
  // Bumped by each read of the first page, so an older page asked for before it is dropped:
  // appended to the new first page, it would skip the rows the new passes pushed down.
  const generation = useRef(0)

  useEffect(() => {
    let stopped = false
    const mine = ++generation.current
    setReading(true)
    listSyncPasses({ limit: PASS_PAGE })
      .then((page) => {
        if (stopped) return
        setPasses(page.passes)
        setCursor(page.next_cursor)
        setFailed(false)
      })
      .catch(() => {
        if (!stopped) setFailed(true)
      })
      .finally(() => {
        if (generation.current === mine) setReading(false)
      })
    return () => {
      stopped = true
    }
  }, [lastPassAt])

  async function older() {
    if (!cursor) return
    const mine = generation.current
    setLoading(true)
    try {
      const page = await listSyncPasses({ limit: PASS_PAGE, cursor })
      if (generation.current !== mine) return
      setPasses((now) => [...(now ?? []), ...page.passes])
      setCursor(page.next_cursor)
      setFailed(false)
    } catch {
      if (generation.current === mine) setFailed(true)
    } finally {
      setLoading(false)
    }
  }

  // The stored value while the field holds one save would refuse.
  const typed = toPatchValue('filament_sync_pass_ttl_days', form.value('filament_sync_pass_ttl_days'), form.settings)
  const stored = form.settings.filament_sync_pass_ttl_days
  const kept = typeof typed === 'number' ? typed : typed === null ? SYNC_PASS_TTL.default : (stored ?? SYNC_PASS_TTL.default)
  return (
    <FieldRow id="filament-sync-passes" label="Recent passes" help={`Every pass, failed ones too, kept for ${plural(kept, 'day', 'days')}.`}>
      <div className="flex flex-col gap-2 text-sm">
        {passes && passes.length === 0 && <p className="text-muted-foreground">No pass recorded yet.</p>}
        {passes && passes.length > 0 && (
          <ol id="filament-sync-passes" aria-label="Recent sync passes" className="flex flex-col gap-1.5">
            {passes.map((pass) => {
              const state = outcome(pass)
              const took = duration(pass)
              return (
                <li key={pass.id} data-outcome={state} className="flex gap-2.5">
                  <span aria-hidden className={`w-0.5 shrink-0 rounded-full ${OUTCOME_RULE[state]}`} />
                  <div className="flex min-w-0 flex-col">
                    <p>
                      <When at={pass.ended_at} />
                      <span className="text-muted-foreground">
                        {pass.trigger && `, ${TRIGGER_TEXT[pass.trigger].toLowerCase()}`}
                        {took && `, took ${took}`}
                      </span>
                    </p>
                    <p className={state === 'idle' ? 'text-muted-foreground' : undefined}>{passLine(pass)}</p>
                    {pass.error && <p className="text-warn">Failed: {syncErrorText(pass.error)}</p>}
                  </div>
                </li>
              )
            })}
          </ol>
        )}
        {failed && <p className="text-warn">The recent passes could not be read. Reload the page to try again.</p>}
        {cursor && (
          <div>
            <Button type="button" size="sm" variant="ghost" disabled={loading || reading} onClick={() => void older()}>
              {loading && <Spinner />}
              Show older passes
            </Button>
          </div>
        )}
      </div>
    </FieldRow>
  )
}

/** #6905 — how many days a pass's row is kept. A stored choice, saved with "Save Filament". */
function SyncPassTtlRow({ form }: { form: BackendSettings }) {
  return (
    <FieldRow
      id="setting-filament_sync_pass_ttl_days"
      {...docOf(form.settings, 'filament_sync_pass_ttl_days')}
      error={form.errors.filament_sync_pass_ttl_days}
    >
      <Input
        id="setting-filament_sync_pass_ttl_days"
        type="number"
        min={SYNC_PASS_TTL.min}
        max={SYNC_PASS_TTL.max}
        step={1}
        inputMode="numeric"
        placeholder={String(SYNC_PASS_TTL.default)}
        value={form.value('filament_sync_pass_ttl_days')}
        onChange={(event) => form.setField('filament_sync_pass_ttl_days', event.target.value)}
        className="sb-num max-w-32"
      />
    </FieldRow>
  )
}

/**
 * #7170 — the CUPS server the Labels page prints to. A stored choice with no env seed, so no
 * source badge and no reset; only a person's session saves it.
 */
function LabelServerRow({ form }: { form: BackendSettings }) {
  return (
    <FieldRow
      id="setting-filament_label_server_url"
      {...docOf(form.settings, 'filament_label_server_url')}
      error={form.errors.filament_label_server_url}
    >
      <Input
        id="setting-filament_label_server_url"
        type="text"
        value={form.value('filament_label_server_url')}
        onChange={(event) => form.setField('filament_label_server_url', event.target.value)}
        className="sb-num"
      />
    </FieldRow>
  )
}

/** #6905 — the passes one page of the history holds. */
const PASS_PAGE = 10

/**
 * #7176 — whether ScadBuddy's filament presets are copied to Bambuddy's local presets. A
 * stored choice, on by default, saved with "Save Filament".
 */
function PresetMirrorRow({ form }: { form: BackendSettings }) {
  return (
    <FieldRow
      id="setting-filament_preset_mirror"
      {...docOf(form.settings, 'filament_preset_mirror')}
      error={form.errors.filament_preset_mirror}
    >
      <SwitchField
        id="setting-filament_preset_mirror"
        label="Copy each preset to Bambuddy"
        userOnly={false}
        checked={form.value('filament_preset_mirror') === 'true'}
        onCheckedChange={(checked) => form.setField('filament_preset_mirror', checked ? 'true' : 'false')}
      />
    </FieldRow>
  )
}

/**
 * #6868 — whether a spool that reaches 0 g, or is archived, adds its type to the shopping
 * list. A stored choice (no environment value, so no badge), on by default, saved with
 * "Save Filament".
 */
function AutoShoppingRow({ form }: { form: BackendSettings }) {
  return (
    <FieldRow
      id="setting-filament_shopping_auto_add_empty"
      {...docOf(form.settings, 'filament_shopping_auto_add_empty')}
      error={form.errors.filament_shopping_auto_add_empty}
    >
      <SwitchField
        id="setting-filament_shopping_auto_add_empty"
        label="Add a spool's type when it runs out"
        userOnly={false}
        checked={form.value('filament_shopping_auto_add_empty') === 'true'}
        onCheckedChange={(checked) => form.setField('filament_shopping_auto_add_empty', checked ? 'true' : 'false')}
      />
    </FieldRow>
  )
}

/** #7156 — the setting's title (`setting_docs.py`), which the override mode's status names when it holds Bambuddy off. */
const AUTO_ADD_LABEL = 'Add spools for unknown RFID tags'

/**
 * #7156 — a stored choice, on by default, saved with "Save Filament". On leaves Bambuddy's own
 * auto-add of unknown RFID spools as it is; off holds it off, beside the RFID override mode below.
 */
function AutoAddRfidRow({ form }: { form: BackendSettings }) {
  return (
    <FieldRow
      id="setting-filament_auto_add_unknown_rfid"
      {...docOf(form.settings, 'filament_auto_add_unknown_rfid')}
      error={form.errors.filament_auto_add_unknown_rfid}
    >
      <SwitchField
        id="setting-filament_auto_add_unknown_rfid"
        label="Let Bambuddy add spools for unknown tags"
        userOnly={false}
        checked={form.value('filament_auto_add_unknown_rfid') === 'true'}
        onCheckedChange={(checked) => form.setField('filament_auto_add_unknown_rfid', checked ? 'true' : 'false')}
      />
    </FieldRow>
  )
}

/**
 * #6625 — the nozzles a 3DFP preset fetch exports for each printer model, one checkbox
 * each. `profile_fetch_nozzles` is held in the form comma-joined and saved with "Save
 * Filament"; none ticked is refused before the save.
 */
function FetchNozzlesRow({ form }: { form: BackendSettings }) {
  const raw = form.value('profile_fetch_nozzles')
  const ticked = raw === '' ? [] : raw.split(',')
  const toggle = (nozzle: string, checked: boolean) =>
    form.setField('profile_fetch_nozzles', nozzlesValue(checked ? [...ticked, nozzle] : ticked.filter((one) => one !== nozzle)))
  const doc = docOf(form.settings, 'profile_fetch_nozzles')
  return (
    <FieldRow
      id="setting-profile_fetch_nozzles"
      {...doc}
      badge={<FieldBadge form={form} name="profile_fetch_nozzles" />}
      error={form.errors.profile_fetch_nozzles}
    >
      <fieldset id="setting-profile_fetch_nozzles" aria-label={doc.label} className="flex flex-wrap gap-x-5 gap-y-2">
        {FETCH_NOZZLES.map((nozzle) => (
          <label key={nozzle} className="flex items-center gap-2 text-sm">
            <Checkbox aria-label={`${nozzle} mm`} checked={ticked.includes(nozzle)} onCheckedChange={(checked) => toggle(nozzle, checked === true)} />
            <span className="sb-num">{nozzle} mm</span>
          </label>
        ))}
      </fieldset>
    </FieldRow>
  )
}

/**
 * #6264 — the RFID override mode (Bambuddy stops adding a spool for an RFID tag it does not
 * know). It is an action with a status, not a draft: the switch writes the mode on the server
 * at once, which answers with where Bambuddy stands.
 */
function OverrideRow({ status }: { status: AsyncState<RfidStatus> }) {
  const [pending, setPending] = useState(false)
  const [report, setReport] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)

  async function change(call: () => Promise<RfidStatus | RfidReconcileReport>) {
    setPending(true)
    setProblem(null)
    setReport(null)
    try {
      const answer = await call()
      if ('mode' in answer) {
        status.setData(answer.mode)
        setReport(reportLine(answer))
      } else {
        status.setData(answer)
      }
    } catch (cause) {
      setProblem(cause instanceof ApiError ? cause.detail : 'The server could not be reached. Try again.')
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <FieldRow
        id="setting-filament-rfid-override"
        label="RFID override mode"
        help="On, Bambuddy stops adding a spool for an RFID tag it does not know. ScadBuddy's spools take their place."
      >
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <Switch
              id="setting-filament-rfid-override"
              checked={status.data?.override_mode ?? false}
              disabled={pending || status.data === undefined}
              onCheckedChange={(checked) => void change(() => setOverrideMode(checked))}
            />
            <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => void change(() => reconcileRfid())}>
              Apply now
            </Button>
          </div>
          {status.data && (
            <p role="status" className="text-sm text-muted-foreground">
              {statusLine(status.data)}
            </p>
          )}
          {status.error && <p className="text-sm text-warn">The mode could not be read. Reload the page to try again.</p>}
          {report && <p className="text-sm text-muted-foreground">{report}</p>}
          {problem && (
            <p role="alert" className="text-sm text-warn">
              {problem}
            </p>
          )}
        </div>
      </FieldRow>
    </>
  )
}

/**
 * #6262 — the configuration the Filament page leans on: how Bambuddy's spools reach
 * ScadBuddy's own, and the numbers a spool falls back on. The spools themselves, their
 * types and tags are not here; they are on the Filament page.
 */
export function Component() {
  const syncStatus = useAsync(() => getSyncStatus(), [])
  const rfidStatus = useAsync(() => getRfidStatus(), [])
  // A saved direction changes `skipped`, and a saved auto-add setting the RFID status: read both again.
  const form = useBackendSettings(id, (s) => fieldsOf(id, s), {
    onSaved: () => {
      syncStatus.refresh()
      rfidStatus.refresh()
    },
  })
  return (
    <SavedSection
      form={form}
      id={id}
      title={title}
      description={
        <>
          Spools, types and tags are in{' '}
          <AppLink href={filamentLinks.spools} className={LINK}>
            Filament
          </AppLink>
          .
        </>
      }
    >
      <RuntimeRows form={form} section="filament" />
      <LabelServerRow form={form} />
      <SyncRows form={form} />
      <SpoolsImportRow />
      <PresetsImportRow />
      <SyncStatusRow status={syncStatus} mirror={form.settings.filament_preset_mirror} />
      <SyncPassesRow lastPassAt={syncStatus.data?.last_pass_at} form={form} />
      <SyncPassTtlRow form={form} />
      <AutoAddRfidRow form={form} />
      <OverrideRow status={rfidStatus} />
      <FetchNozzlesRow form={form} />
      <PresetMirrorRow form={form} />
      <AutoShoppingRow form={form} />
      <FieldRow
        id="filament-currency"
        label="Currency"
        help="Costs are in the shop's currency, which an order copies when it is made."
      >
        <p id="filament-currency" className="text-sm">
          <span className="sb-num font-medium">{form.settings.orders_currency}</span>
          {' · '}
          <AppLink href="/settings/printing#orders" className={LINK}>
            Change it in Orders
          </AppLink>
        </p>
      </FieldRow>
    </SavedSection>
  )
}
