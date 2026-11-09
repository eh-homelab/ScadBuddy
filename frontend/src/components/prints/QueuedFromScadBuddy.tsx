import type { ScadBuddyJob } from '../../api/types'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

/** What the badge's tooltip names: the template or library file the job prints. */
function source(job: ScadBuddyJob): string {
  if (job.library_file_id != null) return `library file ${job.library_file_id}`
  if (job.slug) return `an output of ${job.slug}`
  return 'a ScadBuddy output'
}

/**
 * #3741 — marks a queue item or printer job that ScadBuddy queued or started. The
 * backend sets `scadbuddy` only from a record of that job itself: the send a ScadBuddy
 * run or reprint recorded for its queue item, or a run's LAN start on that printer. A
 * linked archive alone is not enough, so a job queued from Bambuddy (its reprint of a
 * ScadBuddy print too) or started on the printer has none.
 */
export function QueuedFromScadBuddy({ job, className }: { job: ScadBuddyJob; className?: string }) {
  return (
    <Badge
      variant="outline"
      className={cn('shrink-0 border-primary/30 font-normal text-primary', className)}
      title={`ScadBuddy queued or started this job: ${source(job)}`}
    >
      Queued from ScadBuddy
    </Badge>
  )
}
