import { ChevronDownIcon } from 'lucide-react'
import { useState } from 'react'
import { ApiError } from '../../api/client'
import { archiveSpool, deleteSpool, getSpool, moveSpool, patchSpool, type Spool, type SpoolType } from '../../api/filament'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'

/** A place to move to, its path from the top ("Room / Shelf A"). */
export interface Place {
  id: string
  path: string
}

interface Props {
  selected: Spool[]
  places: Place[]
  types: SpoolType[]
  /** After a run: what it did, each failure by spool number; the tab reloads. */
  onDone: (report: string) => void
  onClear: () => void
}

/** The refusal's words without a closing full stop; the report adds its own. */
const why = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'the request failed').replace(/\.+$/, '')
const spools = (count: number) => (count === 1 ? '1 spool' : `${count} spools`)

/**
 * #6262 — what to do with the selected spools: archive, restore, delete, move or set
 * their type. One request per spool, in number order; the report names each spool that
 * failed and why. Delete reads each spool first and skips one that has printed (its
 * `in_use`), naming it: the backend would refuse it, and archiving is the way out.
 */
export function BulkBar({ selected, places, types, onDone, onClear }: Props) {
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const ordered = [...selected].sort((a, b) => a.number - b.number)

  const run = async (said: (count: string) => string, each: (spool: Spool) => Promise<'skip' | void>, skipped: (spool: Spool) => string = () => '') => {
    setBusy(true)
    let done = 0
    const notes: string[] = []
    for (const spool of ordered) {
      try {
        if ((await each(spool)) === 'skip') notes.push(skipped(spool))
        else done += 1
      } catch (cause) {
        notes.push(`#${spool.number} failed: ${why(cause)}.`)
      }
    }
    setBusy(false)
    onDone([`${said(spools(done))}.`, ...notes].join(' '))
  }

  const remove = () =>
    run(
      (n) => `Deleted ${n}`,
      async (spool) => {
        if ((await getSpool(spool.id)).in_use) return 'skip'
        await deleteSpool(spool.id)
      },
      (spool) => `Skipped #${spool.number}: it has printed; archive it instead.`,
    )

  return (
    <div role="toolbar" aria-label="Selected spools" className="flex flex-wrap items-center gap-1.5 rounded-md border bg-muted/40 px-2 py-1.5 text-[13px]">
      <span className="mr-1 font-medium tabular-nums">{spools(selected.length)} selected</span>
      <Button size="sm" variant="outline" disabled={busy} onClick={() => void run((n) => `Archived ${n}`, (s) => archiveSpool(s.id, true).then(() => {}))}>
        Archive
      </Button>
      <Button size="sm" variant="outline" disabled={busy} onClick={() => void run((n) => `Restored ${n}`, (s) => archiveSpool(s.id, false).then(() => {}))}>
        Restore
      </Button>
      <Button size="sm" variant="outline" disabled={busy} onClick={() => setConfirming(true)}>
        Delete
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button size="sm" variant="outline" disabled={busy}>
            Move to <ChevronDownIcon />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 overflow-y-auto">
          <DropdownMenuItem onSelect={() => void run((n) => `Took ${n} out of their place`, (s) => moveSpool(s.id, null).then(() => {}))}>No place</DropdownMenuItem>
          {places.map((place) => (
            <DropdownMenuItem key={place.id} onSelect={() => void run((n) => `Moved ${n} to ${place.path}`, (s) => moveSpool(s.id, place.id).then(() => {}))}>
              {place.path}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button size="sm" variant="outline" disabled={busy || !types.length}>
            Set type <ChevronDownIcon />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 overflow-y-auto">
          {types.map((type) => (
            <DropdownMenuItem key={type.id} onSelect={() => void run((n) => `Set ${type.code} on ${n}`, (s) => patchSpool(s.id, { type_id: type.id }).then(() => {}))}>
              {type.code}
              {type.colour_name && <span className="text-muted-foreground">{type.colour_name}</span>}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      <Button size="sm" variant="ghost" className="ml-auto" disabled={busy} onClick={onClear}>
        Clear selection
      </Button>

      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {spools(selected.length)}?</AlertDialogTitle>
            <AlertDialogDescription>
              Each spool goes with its ledger and tags. A spool that has printed is skipped: archive it instead.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction variant="destructive" onClick={() => void remove()}>
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
