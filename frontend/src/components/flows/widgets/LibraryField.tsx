import { useState } from 'react'
import type { LibrarySearchHit } from '../../../api/types'
import { LibraryFilePicker } from '../../orders/LibraryFilePicker'

/** A library file picked in the picker, then shown until it is changed. */
export function LibraryField({ label, onPick }: { label: string; onPick: (file: LibrarySearchHit | null) => void }) {
  const [picked, setPicked] = useState<LibrarySearchHit | null>(null)
  return (
    <LibraryFilePicker
      open
      label={label}
      picked={picked}
      disabled={false}
      onPick={(file) => {
        setPicked(file)
        onPick(file)
      }}
    />
  )
}
