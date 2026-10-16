import { principalLabel } from '../../agent/chat/labels'
import type { Origin, Owner, Risk } from '../../agent/chat/protocol'
import type { HandoffOffer } from '../../agent/chat/state'
import { Badge } from '@/components/ui/badge'
import { Tip } from './Tip'

const RISK_STYLE: Record<Risk, string> = {
  read: 'text-muted-foreground',
  write: 'border-primary/50 text-primary',
  outward: 'border-destructive/50 text-destructive',
}

const RISK_TITLE: Record<Risk, string> = {
  read: 'Reads only',
  write: 'Changes a model; undoable through its history',
  outward: 'Leaves ScadBuddy (send, print, delete); needs your approval',
}

/** #3673 — a status pill: shadcn's outline badge, its meaning in a tooltip. */
function Pill({ className, title, children }: { className: string; title?: string; children: string }) {
  return (
    <Tip content={title}>
      <Badge variant="outline" className={`h-[18px] px-1.5 text-[10.5px] tracking-wide uppercase ${className}`}>
        {children}
      </Badge>
    </Tip>
  )
}

export function RiskBadge({ risk }: { risk: Risk }) {
  return (
    <Pill className={RISK_STYLE[risk]} title={RISK_TITLE[risk]}>
      {risk}
    </Pill>
  )
}

const ORIGIN_LABEL: Record<Origin, string> = {
  chat: 'chat',
  mcp: 'MCP',
  analyzer: 'analyzer',
  hook: 'plugin hook',
}

export function OriginBadge({ origin }: { origin: Origin }) {
  return (
    <Pill className="text-muted-foreground" title={`Started from ${ORIGIN_LABEL[origin]}`}>
      {ORIGIN_LABEL[origin]}
    </Pill>
  )
}

/** Plan 5d — a session that runs as a durable Temporal workflow (spec §6.1). */
export function DurableBadge() {
  return (
    <Pill className="border-primary/50 text-primary" title="Runs as a durable workflow: a turn survives a restart of the assistant">
      Durable
    </Pill>
  )
}

/**
 * #1284 — a session its owner has offered to another principal: nothing has moved
 * until that principal accepts (agent sessions/manager.ts `handoff`).
 */
export function OfferBadge({ offer }: { offer: HandoffOffer }) {
  return (
    <Pill
      className="border-destructive/50 text-destructive"
      title={`Offered until ${new Date(offer.until).toLocaleString()}; it moves only when accepted`}
    >
      {`Offered to ${offer.to.id ? principalLabel(offer.to.id) : offer.to.label}`}
    </Pill>
  )
}

export function OwnerBadge({ owner }: { owner: Owner }) {
  return owner.kind === 'browser' ? (
    <Pill className="border-ok/50 text-ok">you</Pill>
  ) : (
    <Pill className="border-primary/50 text-primary" title={`Controlled by ${owner.label}`}>
      {owner.label}
    </Pill>
  )
}
