import type { ComponentProps } from 'react'
import { Link } from 'react-router'
import { cn } from '@/lib/utils'

/**
 * #6869 — a filament entity's name as a link to its page (the path from `lib/filamentLinks.ts`).
 * A target whose row is gone is its name and "(deleted)", never a link that 404s (R-6869-5).
 */
export function EntityLink({
  to,
  deleted = false,
  className,
  children,
  ...props
}: { to: string; deleted?: boolean } & Omit<ComponentProps<typeof Link>, 'to'>) {
  if (deleted)
    return (
      <span className={className}>
        {children} <span className="text-muted-foreground">(deleted)</span>
      </span>
    )
  return (
    <Link {...props} to={to} className={cn('underline-offset-4 hover:underline', className)}>
      {children}
    </Link>
  )
}
