import { useRef, useState } from 'react'
import { api } from '../api/client'
import type { SettingsUpdate } from '../api/types'

export interface SettingSaver<K extends keyof SettingsUpdate> {
  /** Stores `value` under the key. The value goes to `PUT /settings` as it is given. */
  save(value: SettingsUpdate[K]): void
  /** True when the newest save failed. The page says so in its own words, for each setting. */
  failed: boolean
  /**
   * True once `save` was called. A stored value read after that is older than the person's
   * choice, so the page keeps its own. Read it when the stored value arrives: it is a getter.
   */
  savedBeforeRead: boolean
}

/**
 * R-7157-9, R-7216-16 — one save queue for one stored setting. Only one `PUT /settings` is
 * in flight at a time. A newer value replaces a value that still waits, so the newest value
 * is stored last. Only the newest save's failure shows.
 */
export function useSettingSaver<K extends keyof SettingsUpdate>(key: K): SettingSaver<K> {
  const [failed, setFailed] = useState(false)
  const queue = useRef<{ busy: boolean; next: { value: SettingsUpdate[K] } | null; seq: number; saved: boolean }>({ busy: false, next: null, seq: 0, saved: false })

  const save = (value: SettingsUpdate[K]) => {
    const q = queue.current
    q.saved = true
    q.seq += 1
    q.next = { value }
    setFailed(false)
    if (q.busy) return
    q.busy = true
    void (async () => {
      while (q.next) {
        const { value: body } = q.next
        q.next = null
        const seq = q.seq
        try {
          await api.putSettings({ [key]: body } as SettingsUpdate)
        } catch {
          // Only the newest save's failure is the page's to tell.
          if (seq === q.seq) setFailed(true)
        }
      }
      q.busy = false
    })()
  }

  return {
    save,
    failed,
    get savedBeforeRead() {
      return queue.current.saved
    },
  }
}
