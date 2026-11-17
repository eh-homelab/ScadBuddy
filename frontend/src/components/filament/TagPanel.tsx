import { useState } from 'react'
import { ApiError } from '../../api/client'
import { bindTag, unbindTag, type SpoolDetail, type TagBind } from '../../api/filament'
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
 * #6262 — a spool's tag: the open one with "Take off", the last one it held with "Bind tag
 * again" (free) or "Tag is on spool #N — Move it here" (another spool holds it now;
 * Decision 7), and a form to bind any tag by its UID or tray UUID. A restore binds
 * nothing: the tag comes back only through these buttons, which post the last tag's own
 * keys. The server moves a tag that another spool holds whether or not `override` is set; the flag only records that a person did it on purpose. "Move it here" sends `override: true`, and the server writes `tag_unbound` and
 * `tag_bound` events on both spools.
 */
export function TagPanel({ spool, onChanged }: Props) {
  const [uid, setUid] = useState('')
  const [tray, setTray] = useState('')
  const [kind, setKind] = useState<TagBind['kind'] & string>('other')
  const [override, setOverride] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
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
    override: last?.held_by != null,
  })

  function bind() {
    if (!uid.trim() && !tray.trim()) {
      setError('Enter a tag UID or a tray UUID.')
      return
    }
    void run(() => bindTag(spool.id, { ...(uid.trim() ? { tag_uid: uid.trim() } : {}), ...(tray.trim() ? { tray_uuid: tray.trim() } : {}), kind, override }))
  }

  return (
    <div className="flex flex-col gap-3" data-testid="tag-panel">
      {spool.tag ? (
        <div className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-[13px]">
          <div className="min-w-0 flex-1">
            <p className="font-medium">{KINDS.find((one) => one.id === spool.tag!.kind)?.name ?? spool.tag.kind}</p>
            <p className="truncate font-mono text-[12px] text-muted-foreground">{[spool.tag.tag_uid, spool.tag.tray_uuid].filter(Boolean).join(' · ')}</p>
          </div>
          <Button type="button" size="xs" variant="outline" disabled={busy} onClick={() => void run(() => unbindTag(spool.tag!.id))}>
            Take off
          </Button>
        </div>
      ) : (
        <p className="text-[13px] text-muted-foreground">No tag is on this spool.</p>
      )}

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
          <Input id="tag-tray" className="font-mono" value={tray} onChange={(e) => setTray(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1">
          <Label className="text-[13px]">Kind</Label>
          <Select value={kind} onValueChange={(next) => setKind(next as typeof kind)}>
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
            <Checkbox checked={override} onCheckedChange={(next) => setOverride(next === true)} />
            Record that the tag was taken from another spool on purpose
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
