import type { RackAlgorithm, RackOption } from '../../api/types'

/** #836 — the four ways to rank the rack (spec 2026-10-01 §4). */
export const ALGORITHM_LABELS: Record<RackAlgorithm, string> = {
  least_used: 'Least used',
  oldest_first: 'Oldest first',
  newest_first: 'Newest first',
  bambuddy: 'Let Bambuddy pick',
}

export const flowLabel = (flow: RackOption['flow']) => (flow === 'high_flow' ? 'High Flow' : 'Standard')
