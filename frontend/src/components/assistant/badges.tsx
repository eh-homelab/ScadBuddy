import type { Origin, Owner, Risk } from '../../agent/chat/protocol'

const RISK_STYLE: Record<Risk, string> = {
  read: 'border-line text-muted',
  write: 'border-accent/50 text-accent',
  outward: 'border-warn/60 text-warn',
}

const RISK_TITLE: Record<Risk, string> = {
  read: 'Reads only',
  write: 'Changes a model; undoable through its history',
  outward: 'Leaves ScadBuddy (send, print, delete); needs your approval',
}

function Pill({ className, title, children }: { className: string; title?: string; children: string }) {
  return (
    <span
      title={title}
      className={`inline-flex h-[18px] shrink-0 items-center rounded-full border px-1.5 text-[10.5px] leading-none font-medium uppercase tracking-wide ${className}`}
    >
      {children}
    </span>
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
    <Pill className="border-line text-muted" title={`Started from ${ORIGIN_LABEL[origin]}`}>
      {ORIGIN_LABEL[origin]}
    </Pill>
  )
}

export function DurableBadge() {
  return (
    <Pill className="border-accent/50 text-accent" title="Durable: survives restarts; approvals wait as long as needed">
      Durable
    </Pill>
  )
}

export function OwnerBadge({ owner }: { owner: Owner }) {
  return owner.kind === 'browser' ? (
    <Pill className="border-ok/50 text-ok">you</Pill>
  ) : (
    <Pill className="border-accent/50 text-accent" title={`Controlled by ${owner.label}`}>
      {owner.label}
    </Pill>
  )
}
