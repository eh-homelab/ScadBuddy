import type { ButtonHTMLAttributes, ReactNode } from 'react'

type Variant = 'primary' | 'default' | 'ghost' | 'danger'
type Size = 'sm' | 'md'

const VARIANT: Record<Variant, string> = {
  primary:
    'bg-accent text-accent-ink border-accent hover:brightness-110 disabled:hover:brightness-100 font-medium',
  default: 'bg-surface-2 text-ink border-line hover:border-line-strong hover:bg-surface-3',
  ghost: 'bg-transparent text-muted border-transparent hover:text-ink hover:bg-surface-2',
  danger: 'bg-transparent text-warn border-line hover:bg-warn/10 hover:border-warn/50',
}

/** A ghost toggle that is on (#1488): without it a pressed ghost button looks unpressed. */
const GHOST_PRESSED = 'bg-surface-3 text-ink border-line hover:bg-surface-3'

const SIZE: Record<Size, string> = {
  sm: 'h-7 px-2.5 text-[13px]',
  md: 'h-9 px-3.5',
}

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  children: ReactNode
}

export function Button({
  variant = 'default',
  size = 'md',
  className = '',
  children,
  ...rest
}: Props) {
  const pressed = rest['aria-pressed'] === true || rest['aria-pressed'] === 'true'
  const look = variant === 'ghost' && pressed ? GHOST_PRESSED : VARIANT[variant]
  return (
    <button
      type="button"
      {...rest}
      className={`inline-flex shrink-0 items-center justify-center gap-2 rounded-[6px] border whitespace-nowrap transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${look} ${SIZE[size]} ${className}`}
    >
      {children}
    </button>
  )
}
