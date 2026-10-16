import type { Settings } from '../api/types'
import { safeHttpUrl } from './safeUrl'

/**
 * #1293 — the Temporal UI page for one workflow, from the same "Temporal UI ↗" URL
 * Settings links and the namespace the deployment set (a bootstrap value). Null when
 * any of the three is unknown, so no link points at a guess.
 */
export function temporalWorkflowUrl(
  settings: Settings | undefined,
  workflowId: string | null | undefined,
): string | null {
  const ui = safeHttpUrl(settings?.temporal_ui_url)
  const namespace = settings?.bootstrap?.find((entry) => entry.name === 'temporal_namespace')?.value
  if (!ui || !namespace || !workflowId) return null
  return `${ui.replace(/\/+$/, '')}/namespaces/${encodeURIComponent(namespace)}/workflows/${encodeURIComponent(workflowId)}`
}
