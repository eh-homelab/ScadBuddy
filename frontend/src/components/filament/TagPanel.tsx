import { SmartphoneNfcIcon } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../api/client'
import { bindTag, getTag, unbindTag, type SpoolDetail, type TagBind } from '../../api/filament'
import { filamentLinks } from '../../lib/filamentLinks'
import { useAsync } from '../../lib/useAsync'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { KINDS } from './tagKinds'

const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The server could not be reached. Try again.')

interface Props {
  spool: SpoolDetail
  /** A bind or an unbind landed: the page reads the spool again. */
  onChanged: () => void
}

/**
 * #6262, #6264 — a spool's tag: the open one with "Take off" (with an "Override" mark when
 * the open binding was made with `override`; the weight-lock line follows the spool's own
 * `weight_locked`, which unbinding never clears), the last one it held
 * with "Bind tag again" (free) or "Tag is on spool #N — Move it here" (another spool holds it
 * now; Decision 7), and a form to bind any tag by its UID or tray UUID.
 * A restore binds nothing: the tag comes back only through
 * these buttons, which post the last tag's own keys. The server moves a tag that another
 * spool holds whether or not `override` is set. "Move it here" sends `override: false`
 * (Decision 3). The override box is for a Bambu tag that belongs to a different filament
 * than this spool: it needs a tray UUID, and it sends `override: true`, which makes the
 * binding keep this spool's own filament (the AMS percentage never changes its grams left).
 * "Read or write with a phone" opens the scan page (#7273), which reads and writes tags through Web NFC.
 */
export function TagPanel({ spool, onChanged }: Props) {
  const [uid, setUid] = useState('')
  const [tray, setTray] = useState('')
  const [kind, setKind] = useState<TagBind['kind'] & string>('other')
  const [override, setOverride] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** Counts the changes made here, so the open tag's binding (its Override mark) is read again. */
  const [changes, setChanges] = useState(0)

  const openTag = spool.tag
  const binding = useAsync(() => (openTag ? getTag(openTag.id) : Promise.resolve(null)), [openTag?.id, changes])
  const overridden = binding.data?.binding?.override === true

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      setChanges((count) => count + 1)
      onChanged()
    } catch (cause) {
      setError(message(cause))
    } finally {
      setBusy(false)
    }
  }

  const last = spool.last_tag
  const lastKeys = (): TagBind => ({
    ...(last?.tag.tag_uid ? { tag_uid: last.tag.tag_uid } : {}),
    ...(last?.tag.tray_uuid ? { tray_uuid: last.tag.tray_uuid } : {}),
    kind: last?.tag.kind ?? 'other',
    override: false,
  })

  const hasTray = tray.trim() !== ''
  /** The server takes `override` only for a Bambu tag (`bambu_mifare`) with a tray UUID. */
  const canOverride = hasTray && !kind.startsWith('ntag')

  function bind() {
    if (!uid.trim() && !hasTray) {
      setError('Enter a tag UID or a tray UUID.')
      return
    }
    void run(() =>
      bindTag(spool.id, {
        ...(uid.trim() ? { tag_uid: uid.trim() } : {}),
        ...(hasTray ? { tray_uuid: tray.trim() } : {}),
        kind,
        override: override && canOverride && kind === 'bambu_mifare',
      }),
    )
  }

  return (
    <div className="flex flex-col gap-3" data-testid="tag-panel">
      {spool.tag ? (
        <div className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-[13px]">
          <div className="min-w-0 flex-1">
            <p className="flex flex-wrap items-center gap-2 font-medium">
              {KINDS.find((one) => one.id === spool.tag!.kind)?.name ?? spool.tag.kind}
              {overridden && <Badge variant="secondary">Override</Badge>}
            </p>
            <p className="truncate font-mono text-[12px] text-muted-foreground">{[spool.tag.tag_uid, spool.tag.tray_uuid].filter(Boolean).join(' · ')}</p>
          </div>
          <Button type="button" size="xs" variant="outline" disabled={busy} onClick={() => void run(() => unbindTag(spool.tag!.id))}>
            Take off
          </Button>
        </div>
      ) : (
        <p className="text-[13px] text-muted-foreground">No tag is on this spool.</p>
      )}

      {spool.weight_locked && <p className="text-[12px] text-muted-foreground">Weight locked: the AMS percentage never changes grams left</p>}

      {!spool.tag && last && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-dashed px-3 py-2 text-[13px]">
          <p className="min-w-0 flex-1 text-muted-foreground">
            Last tag <span className="font-mono">{last.tag.tag_uid ?? last.tag.tray_uuid}</span>
            {last.reason ? `, taken off: ${last.reason}` : ''}.
          </p>
          <Button type="button" size="xs" variant="outline" disabled={busy} onClick={() => void run(() => bindTag(spool.id, lastKeys()))}>
            {last.held_by == null ? 'Bind tag again' : `Tag is on spool #${last.held_by} — Move it here`}
          </Button>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button asChild size="sm" variant="ghost">
          <Link to={filamentLinks.scan}>
            <SmartphoneNfcIcon /> Read or write with a phone
          </Link>
        </Button>
      </div>

      <form
        className="grid grid-cols-1 gap-2 sm:grid-cols-2"
        aria-label="Bind a tag"
        onSubmit={(event) => {
          event.preventDefault()
          bind()
        }}
      >
        <div className="flex flex-col gap-1">
          <Label htmlFor="tag-uid" className="text-[13px]">
            Tag UID
          </Label>
          <Input id="tag-uid" className="font-mono" value={uid} onChange={(e) => setUid(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="tag-tray" className="text-[13px]">
            Tray UUID
          </Label>
          <Input
            id="tag-tray"
            className="font-mono"
            value={tray}
            onChange={(e) => {
              setTray(e.target.value)
              if (e.target.value.trim() === '') setOverride(false)
            }}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label className="text-[13px]">Kind</Label>
          <Select
            value={kind}
            onValueChange={(next) => {
              setKind(next as typeof kind)
              if (next !== 'bambu_mifare') setOverride(false)
            }}
          >
            <SelectTrigger aria-label="Tag kind" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {KINDS.map((one) => (
                <SelectItem key={one.id} value={one.id}>
                  {one.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex items-end gap-3">
          <Label className="flex items-center gap-2 pb-2 text-[13px] font-normal">
            <Checkbox
              checked={override && canOverride}
              disabled={!canOverride}
              onCheckedChange={(next) => {
                setOverride(next === true)
                if (next === true) setKind('bambu_mifare')
              }}
            />
            This Bambu tag belongs to a different filament (override)
          </Label>
        </div>
        <div className="sm:col-span-2">
          <Button type="submit" size="sm" variant="outline" disabled={busy}>
            Bind tag
          </Button>
        </div>
      </form>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </div>
  )
}
