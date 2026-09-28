import { DELETED_STATUS, statusLabel } from './status'

const TONE: Record<string, string> = {
  completed: 'border-ok/40 bg-ok/10 text-ok',
  failed: 'border-warn/40 bg-warn/10 text-warn',
  cancelled: 'border-line-strong text-muted',
  printing: 'border-accent/50 bg-accent/10 text-ink',
  [DELETED_STATUS]: 'border-dashed border-line-strong text-faint',
}

export function PrintStatus({ status }: { status: string }) {
  return (
    <span
      data-status={status}
      className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-px text-[11px] ${
        TONE[status] ?? 'border-line text-muted'
      }`}
    >
      {status === 'printing' && (
        <span aria-hidden className="size-1.5 animate-pulse rounded-full bg-accent motion-reduce:animate-none" />
      )}
      {statusLabel(status)}
    </span>
  )
}
