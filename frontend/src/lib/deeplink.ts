import type { EditTarget, Output } from '../api/types'

/**
 * The "Edit in ScadBuddy" deep link, mirrored from `scadbuddy/library/deeplink.py`.
 * The backend stamps the absolute form into every 3MF and attaches it to the file
 * Bambuddy holds; the app only ever needs the path.
 */
export function editPath(outputId: string): string {
  return `/edit/${outputId}`
}

/**
 * What `/edit/{id}` hands the customizer through router state, so one Edit click is
 * one `GET /outputs/{id}/edit`. Absent when the customizer is opened directly — a
 * pasted `/m/{slug}?from={id}`, or a reload — so the customizer still fetches.
 */
export interface EditNavigationState {
  editTarget: EditTarget
}

/**
 * The same target `/outputs/{id}/edit` would answer with, built from an output the
 * page has already loaded. A history row holds every field of it, so clicking Edit
 * there need not ask the backend to resolve what the row is showing.
 */
export function editTargetFor(output: Output): EditTarget {
  return {
    output_id: output.id,
    slug: output.slug,
    name: output.name ?? null,
    params: output.params,
    model_version: output.model_version,
    source: 'record',
  }
}
