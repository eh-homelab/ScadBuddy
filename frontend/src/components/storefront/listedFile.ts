import { useEffect, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { LibraryFileEntry, OutputPlate, StorefrontFileSnapshot as FileSnapshot } from '../../api/types'
import { isSupportMaterial, type DraftSlot, type ListingDraft } from './listingDraft'

/** #6710 §9 — a file listing's library file, as the page reads it and the editor shows it. */

/** What the page read of the draft's library file. */
export interface ListedFile {
  /** `gone`: the library has no such file (404); `failed`: any other failed read. */
  state: 'loading' | 'ready' | 'gone' | 'failed'
  entry: LibraryFileEntry | null
  /** The file's plates; null until read, or when they could not be read. */
  plates: OutputPlate[] | null
  /** The file's parts; null until read, or when they could not be read. */
  slots: DraftSlot[] | null
  /**
   * Where `slots` came from: the publish snapshot (which reads which parts print), or the
   * file's declared slots before a publish (every one counted as used).
   */
  slotsFrom: 'publish' | 'file'
  /** Whether the read of `slots` failed (they stay null). */
  slotsFailed?: boolean
  /** Whether the read of `plates` failed (they stay null). */
  platesFailed?: boolean
}

/**
 * #6710 — the draft's library file as the editor shows it: the library's entry (`gone` on a
 * 404), its plates and its parts. A published listing's snapshot of that file is its pinned
 * version, so its plates and parts (which say which slots print) are the snapshot's; before
 * a publish the parts are the file's declared slots (`GET /print/library/{id}/filaments`),
 * every one counted as used, a support material's as support.
 */
export function useListedFile(fileId: number | null, snapshot: FileSnapshot | null): ListedFile | undefined {
  const [read, setRead] = useState<{ id: number; file: ListedFile } | null>(null)
  const pinned = snapshot !== null && snapshot.file_id === fileId ? snapshot : null
  const fromSnapshot = pinned !== null
  useEffect(() => {
    if (fileId === null) return
    let live = true
    const start: ListedFile = { state: 'loading', entry: null, plates: null, slots: null, slotsFrom: 'file' }
    setRead({ id: fileId, file: start })
    const set = (patch: Partial<ListedFile>) => {
      if (live) setRead((cur) => (cur?.id === fileId ? { id: fileId, file: { ...cur.file, ...patch } } : cur))
    }
    api
      .getLibraryFile(fileId)
      .then((entry) => set({ state: 'ready', entry }))
      .catch((caught: unknown) => set({ state: caught instanceof ApiError && caught.status === 404 ? 'gone' : 'failed' }))
    if (!fromSnapshot) {
      api
        .getLibraryPlates(fileId)
        .then((plates) => set({ plates }))
        .catch(() => set({ platesFailed: true }))
      api
        .getLibraryFilaments(fileId)
        .then((options) =>
          set({
            slots: (options.slots ?? []).map((slot) => ({
              slot: slot.slot_id - 1,
              colour: slot.colour || '#808080',
              material: slot.material ?? null,
              used: true,
              support: isSupportMaterial(slot.material),
            })),
          }),
        )
        .catch(() => set({ slotsFailed: true }))
    }
    return () => {
      live = false
    }
  }, [fileId, fromSnapshot])
  if (fileId === null) return undefined
  const file = read?.id === fileId ? read.file : { state: 'loading' as const, entry: null, plates: null, slots: null, slotsFrom: 'file' as const }
  if (!pinned) return file
  return { ...file, plates: pinned.plates.map((index) => ({ index, has_thumbnail: true })), slots: pinned.slots, slotsFrom: 'publish' }
}

/** The types a listing can sell (§9.2): never a sliced file or another type. */
export const LISTED_TYPES = ['3mf', 'stl'] as const

/** Plate `n`'s picture: the listing's pinned copy once it has one (§9.3), else the library's file. */
export function platePicture(draft: ListingDraft, listingId: string | null | undefined, plate: number): string | null {
  const file = draft.library_file
  if (!file) return null
  if (file.pinned && listingId) return api.storefrontListingPlateUrl(listingId, plate)
  return api.libraryPlateThumbnailUrl(file.id, plate)
}
