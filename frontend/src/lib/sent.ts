import type { Output } from '../api/types'

/**
 * #2287 — whether any of the output reached Bambuddy: a library copy, a queue item, a
 * print. Shared so every page that says "Send" or "Send again" draws the line the same
 * way (#2545). A print over the LAN (#4644) never reached Bambuddy.
 */
export function wasSent(output: Output): boolean {
  return (
    (output.library_files ?? []).length > 0 ||
    output.queue_item_id != null ||
    (output.plates ?? []).length > 0 ||
    (output.print_route != null && output.print_route !== 'lan')
  )
}
