import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'
import { cssRgba, parseRgba } from '../../lib/rgba'

/** The chip's width in px, its grams ring's stroke, and its checker's tile. */
const SIZE = {
  sm: { px: 16, ring: 2, checker: 'bg-size-[6px_6px]' },
  md: { px: 20, ring: 2, checker: 'bg-size-[6px_6px]' },
  lg: { px: 44, ring: 3, checker: 'bg-size-[8px_8px]' },
} as const

/**
 * The checkerboard a transparent colour is drawn over, as image editors draw one: a
 * clear spool shows the squares through it, a translucent one half hides them, an
 * opaque one hides them. Fixed greys, not theme colours, so one `#RRGGBBAA` looks the
 * same in both themes.
 */
const CHECKER = 'bg-[conic-gradient(#c9c9c9_0_25%,#f2f2f2_0_50%,#c9c9c9_0_75%,#f2f2f2_0)]'

export interface RgbaSwatchProps extends Omit<ComponentProps<'span'>, 'children'> {
  /** `#RRGGBBAA`, or `#RRGGBB` read as opaque. */
  rgba: string
  size?: keyof typeof SIZE
  gramsLeft?: number | null
  labelWeight?: number | null
}

/**
 * #6262 — a spool chip: its `#RRGGBBAA` colour as a disc over a checkerboard, so a
 * translucent or clear spool never looks like an opaque one. With both the grams left and
 * the label weight known, a thin ring around the disc shows what is left as an arc from
 * twelve o'clock. Hidden from assistive tech: whatever sits beside it names the spool,
 * so a chip shown alone needs a visible or `sr-only` name next to it.
 * `data-colour` is the `#RRGGBBAA`, for a test.
 */
export function RgbaSwatch({ rgba, size = 'md', gramsLeft, labelWeight, className, style, ...props }: RgbaSwatchProps) {
  const colour = parseRgba(rgba) ?? '#000000FF'
  const { px, ring, checker } = SIZE[size]
  const fraction =
    gramsLeft != null && labelWeight != null && labelWeight > 0 ? Math.min(1, Math.max(0, gramsLeft / labelWeight)) : null
  // The disc sits inside the ring, a pixel clear of it.
  const inset = fraction === null ? 0 : ring + 1
  const radius = (px - ring) / 2
  return (
    <span
      aria-hidden="true"
      data-testid="rgba-swatch"
      data-colour={colour}
      className={cn('relative inline-block shrink-0 rounded-full', className)}
      style={{ width: px, height: px, ...style }}
      {...props}
    >
      {fraction !== null && (
        <svg
          data-layer="grams"
          data-fraction={String(Number(fraction.toFixed(3)))}
          width={px}
          height={px}
          viewBox={`0 0 ${px} ${px}`}
          className="absolute inset-0 -rotate-90"
        >
          <circle cx={px / 2} cy={px / 2} r={radius} fill="none" strokeWidth={ring} className="stroke-foreground/15" />
          {fraction > 0 && (
            <circle
              cx={px / 2}
              cy={px / 2}
              r={radius}
              fill="none"
              strokeWidth={ring}
              pathLength={100}
              strokeDasharray={`${fraction * 100} 100`}
              className="stroke-foreground/70"
            />
          )}
        </svg>
      )}
      <span data-layer="checker" className={cn('absolute rounded-full', CHECKER, checker)} style={{ inset }} />
      <span
        data-layer="colour"
        className="absolute rounded-full ring-1 ring-black/25 ring-inset"
        style={{ inset, backgroundColor: cssRgba(colour) }}
      />
    </span>
  )
}
