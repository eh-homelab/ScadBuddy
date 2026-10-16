import type { ReactNode } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'

/** A page still loading (#3672): a few placeholder rows where it will be, named for a screen reader. */
export function PageLoading({ label, rows = 3 }: { label: string; rows?: number }) {
  return (
    <div role="status" aria-label={label} className="mx-auto flex w-full max-w-3xl flex-col gap-3 px-4 py-6">
      <Skeleton className="h-6 w-1/3" />
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-14 w-full" />
      ))}
      <span className="sr-only">{label}</span>
    </div>
  )
}

/** A page that could not be shown: what happened, and the ways on. */
export function PageProblem({
  title,
  message,
  alert = true,
  children,
}: {
  title: string
  message?: ReactNode
  /** False for a page that is no failure, only nothing to show here (an arranged output). */
  alert?: boolean
  children?: ReactNode
}) {
  return (
    <div className="px-4 py-16">
      <Card role={alert ? 'alert' : undefined} className="mx-auto max-w-lg">
        <CardHeader>
          <CardTitle>
            <h1>{title}</h1>
          </CardTitle>
          {message && <CardDescription>{message}</CardDescription>}
        </CardHeader>
        {children && <CardContent className="flex flex-wrap items-center gap-2">{children}</CardContent>}
      </Card>
    </div>
  )
}
