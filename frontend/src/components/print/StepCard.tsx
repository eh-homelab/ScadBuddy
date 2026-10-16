import type { ReactNode } from 'react'
import { Card, CardContent, CardHeader } from '@/components/ui/card'

/**
 * One of the print dialog's Advanced steps: a card under a heading (#3669). Not a group
 * named by it: the step's one field usually has the same name, and two controls called
 * "Print sequence" would be one too many.
 */
export function StepCard({ title, children, testId }: { title: ReactNode; children: ReactNode; testId?: string }) {
  return (
    <Card size="sm" data-testid={testId}>
      <CardHeader>
        <h3 className="text-sm leading-snug font-medium">{title}</h3>
      </CardHeader>
      <CardContent className="space-y-2">{children}</CardContent>
    </Card>
  )
}
