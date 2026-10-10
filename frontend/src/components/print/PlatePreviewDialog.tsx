import { useCallback, useState } from 'react'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { PlateScene } from './PlateScene'

type View = 'colors' | 'layers'

type Props = {
  open: boolean
  onClose: () => void
  /** The plate's preview mesh. */
  url: string
  /** What the dialog calls the plate: its name, or "Plate N". */
  label: string
  colors: Map<string, string>
}

/** The Layers slider's step, in mm: a common layer height. The cut is the model's, not a slice's. */
const STEP = 0.2

function background(): string {
  return getComputedStyle(document.documentElement).getPropertyValue('--sb-bg').trim() || '#0b0e13'
}

/**
 * #1723 — the plate in 3D, over the print dialog: Colors (each part in its chosen spool's
 * colour) by default, and Layers, the model cut at a height to show it building up. A
 * dialog of its own, so the print dialog underneath keeps every choice while it is open,
 * and on a phone it covers the screen rather than pushing the dialog's controls aside.
 */
export function PlatePreviewDialog({ open, onClose, url, label, colors }: Props) {
  const [view, setView] = useState<View>('colors')
  const [height, setHeight] = useState<number | null>(null)
  const [cut, setCut] = useState<number | null>(null)
  const onHeight = useCallback((next: number) => setHeight(next), [])

  const step = STEP
  const top = height === null ? null : Math.max(step, Math.ceil(height / step) * step)
  const cutAt = view === 'layers' ? (cut ?? top) : null

  return (
    <Dialog
      open={open}
      title={`${label} in 3D`}
      description="What each part prints in, from the spools chosen. The model, not the slicer's toolpaths."
      onClose={onClose}
      size="wide"
      footer={<Button onClick={onClose}>Close</Button>}
    >
      <div className="flex flex-col gap-3">
        <div role="radiogroup" aria-label="View" className="flex gap-1.5">
          {(['colors', 'layers'] as const).map((option) => (
            <button
              key={option}
              type="button"
              role="radio"
              aria-checked={view === option}
              onClick={() => setView(option)}
              className={`rounded-[6px] border px-3 py-1 text-[13px] ${
                view === option ? 'border-accent bg-accent/8 text-ink' : 'border-line text-muted'
              }`}
            >
              {option === 'colors' ? 'Colors' : 'Layers'}
            </button>
          ))}
        </div>
        <div className="h-[min(60dvh,560px)] min-h-[240px] overflow-hidden rounded-[6px] border border-line">
          {open && <PlateScene url={url} colors={colors} cutAt={cutAt} onHeight={onHeight} background={background()} />}
        </div>
        {view === 'layers' && (
          <label className="flex flex-col gap-1 text-[12px] text-muted">
            <span>
              Up to <span className="sb-num text-ink">{(cutAt ?? 0).toFixed(2)}</span>
              {top !== null && (
                <>
                  {' '}
                  of <span className="sb-num">{top.toFixed(2)}</span>
                </>
              )}{' '}
              mm
            </span>
            <input
              type="range"
              aria-label="Layer height"
              min={step}
              max={top ?? step}
              step={step}
              value={cutAt ?? step}
              disabled={top === null}
              onChange={(event) => setCut(Number(event.target.value))}
              className="accent-[var(--sb-accent)]"
            />
          </label>
        )}
      </div>
    </Dialog>
  )
}
