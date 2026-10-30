import type { ComponentType } from 'react'
import { AiActivityLog } from './AiActivityLog'

type AuditLog = { value: string; label: string; Component: ComponentType }

/** The logs of the Audits area, one tab each. A second log is one more entry. */
export const AUDIT_LOGS: readonly [AuditLog, ...AuditLog[]] = [
  { value: 'ai-activity', label: 'AI activity', Component: AiActivityLog },
]
