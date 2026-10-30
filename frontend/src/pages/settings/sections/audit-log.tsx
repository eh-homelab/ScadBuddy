import type { ComponentType } from 'react'
import { Link } from 'react-router'
import { AiActivityLog } from '../../../components/audits/AiActivityLog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../../../components/ui/tabs'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'audit-log'
export const area: AreaId = 'audits'
export const title = 'Audit log'
export const order = 10

type AuditLog = { value: string; label: string; Component: ComponentType }

/** The logs, one tab each. A second log is one more entry. */
export const AUDIT_LOGS: readonly [AuditLog, ...AuditLog[]] = [
  { value: 'ai-activity', label: 'AI activity', Component: AiActivityLog },
]

/**
 * A pointer to /activity for background jobs (spec §3.2), then the logs as tabs, even with
 * one (#781). AI activity is saved on its own (#258). The pointer always shows; the AI
 * activity log only when the assistant is available. The selected tab is local state.
 */
export function Component() {
  return (
    <Section id={id} title={title}>
      <p className="text-sm text-muted-foreground">
        Background jobs are on the{' '}
        <Link to="/activity" className="underline decoration-border underline-offset-2 hover:text-primary">
          Activity page
        </Link>
        .
      </p>
      <Tabs defaultValue={AUDIT_LOGS[0].value}>
        <TabsList aria-label="Audit logs">
          {AUDIT_LOGS.map((l) => (
            <TabsTrigger key={l.value} value={l.value}>
              {l.label}
            </TabsTrigger>
          ))}
        </TabsList>
        {AUDIT_LOGS.map((l) => (
          <TabsContent key={l.value} value={l.value}>
            <l.Component />
          </TabsContent>
        ))}
      </Tabs>
    </Section>
  )
}
