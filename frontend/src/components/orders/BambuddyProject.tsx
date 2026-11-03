import { bambuddyBase } from '../../lib/bambuddyLinks'

/**
 * #4253 (spec 2026-10-18 §8.1) — the Bambuddy project mirroring an order or one of its
 * items: a link to its page in Bambuddy, or its number where no web URL is known.
 */
export function BambuddyProject({ projectId, label }: { projectId: number; label: string }) {
  const base = bambuddyBase()
  if (!base) {
    return (
      <span className="text-xs text-muted-foreground" data-testid="bambuddy-project">
        {label} #{projectId}
      </span>
    )
  }
  return (
    <a
      href={`${base}/projects/${projectId}`}
      target="_blank"
      rel="noreferrer"
      className="text-xs text-primary underline-offset-4 hover:underline"
      data-testid="bambuddy-project"
    >
      {label}
    </a>
  )
}
