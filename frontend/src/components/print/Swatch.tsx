import type { ReactElement } from 'react'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { normalizeHex } from '../../lib/format'
import { isTranslucent, parseRgba } from '../../lib/rgba'
import { RgbaSwatch } from '../filament/RgbaSwatch'

const SIZE = { sm: 'size-4', md: 'size-5' } as const

/**
 * What a chip is, on hover. The chip itself stays hidden from assistive tech: whatever
 * it sits beside names the colour in words. `data-colour` is what it shows, for a test.
 */
function Named({ name, children }: { name: string; children: ReactElement }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent>{name}</TooltipContent>
    </Tooltip>
  )
}

/**
 * A colour chip, as the filament picker and the print dialog's panes show one: the spool
 * chip (`RgbaSwatch`), alpha and all (#6262). `#RRGGBB` and shorthand read as opaque; the
 * hover names an opaque colour `#RRGGBB` and a translucent one `#RRGGBBAA`.
 */
export function Swatch({ colour, size = 'md' }: { colour: string | null | undefined; size?: 'sm' | 'md' }) {
  const rgba = parseRgba(colour ?? '') ?? `${normalizeHex(colour ?? '#000000')}FF`
  return (
    <Named name={isTranslucent(rgba) ? rgba : rgba.slice(0, 7)}>
      <RgbaSwatch rgba={rgba} size={size} />
    </Named>
  )
}

/** A clear spool's chip: a hatched disc, never the black a missing colour reads as. */
export function ClearSwatch({ size = 'md' }: { size?: 'sm' | 'md' }) {
  return (
    <Named name="clear">
      <span
        aria-hidden="true"
        data-colour="clear"
        data-testid="clear-swatch"
        className={cn('inline-block shrink-0 rounded-full ring-1 ring-black/25 ring-inset', SIZE[size])}
        style={{ background: 'repeating-linear-gradient(45deg, transparent 0 3px, rgb(0 0 0 / 0.18) 3px 4px)' }}
      />
    </Named>
  )
}

/** A spool with no colour recorded, or one ScadBuddy can't read: an empty dashed chip,
 * never black and never "clear", which says something the spool did not. */
export function UnknownSwatch({ size = 'md' }: { size?: 'sm' | 'md' }) {
  return (
    <Named name="colour not recorded">
      <span
        aria-hidden="true"
        data-colour="colour not recorded"
        data-testid="unknown-swatch"
        className={cn('inline-block shrink-0 rounded-full border border-dashed border-black/35', SIZE[size])}
      />
    </Named>
  )
}
