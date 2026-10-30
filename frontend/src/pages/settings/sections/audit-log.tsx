import { Link } from 'react-router'
import { AiActivityLog } from '../../../components/audits/AiActivityLog'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'audit-log'
export const area: AreaId = 'audits'
export const title = 'Audit log'
export const order = 10

/**
 * AI activity, saved on its own (#258), and a pointer to /activity for background jobs
 * (spec §3.2). The pointer always shows; the audit card only when the assistant is available.
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
      <AiActivityLog />
    </Section>
  )
}
