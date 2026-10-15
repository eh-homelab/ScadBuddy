import type { ButtonHTMLAttributes, ReactNode, Ref } from 'react'
import { Button as ShadcnButton } from '@/components/ui/button'
import { cn } from '@/lib/utils'

type Variant = 'primary' | 'default' | 'ghost' | 'danger'
type Size = 'sm' | 'md'

/** The legacy variants in shadcn's (#3653). */
const VARIANT = {
  primary: 'default',
  default: 'outline',
  ghost: 'ghost',
  danger: 'destructive',
} as const

const SIZE = { sm: 'sm', md: 'lg' } as const

/** A ghost toggle that is on (#1488): without it a pressed ghost button looks unpressed. */
const GHOST_PRESSED = 'bg-ui-muted text-foreground hover:bg-ui-muted dark:hover:bg-ui-muted'

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  children: ReactNode
  ref?: Ref<HTMLButtonElement>
}

/**
 * #3653 — the legacy button's props over shadcn's button (`@/components/ui/button`),
 * kept until every area has moved to that directly.
 */
export function Button({ variant = 'default', size = 'md', className, children, ...rest }: Props) {
  const pressed = rest['aria-pressed'] === true || rest['aria-pressed'] === 'true'
  return (
    <ShadcnButton
      type="button"
      {...rest}
      variant={VARIANT[variant]}
      size={SIZE[size]}
      className={cn(variant === 'ghost' && pressed && GHOST_PRESSED, className)}
    >
      {children}
    </ShadcnButton>
  )
}
