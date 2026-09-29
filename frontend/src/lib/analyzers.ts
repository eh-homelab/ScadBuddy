import type {
  AnalyzerDiagnostic,
  AnalyzerSeverity,
  DiagnosticLocation,
  ScopeRef,
} from '../api/types'
import { formatBbox } from './format'

/**
 * #284 — reading the print analyzers' report (#461) for the print dialog.
 *
 * No severity blocks Print. `backend/scadbuddy/analyzers/model.py:25-28`: "``error`` is
 * shown as a problem, but like every analyzer finding it is advisory: nothing here
 * disables printing", after the print-flow spec's own rule
 * (`docs/superpowers/specs/2026-09-24-print-flow-design.md:159`, "All three are advisory
 * and none disables Run"). #284 calls `error` blocking; #461 followed the spec.
 */

export const SEVERITY_LABEL: Record<AnalyzerSeverity, string> = {
  error: 'Problem',
  warning: 'Warning',
  info: 'Suggestion',
  hidden: 'Hidden',
}

/** Where a finding is, in the words the dialog uses; the backend's own vocabulary is `kind`. */
export function describeLocation(location: DiagnosticLocation): string {
  switch (location.kind) {
    case 'mesh': {
      const parts: string[] = []
      if (location.part !== null && location.part !== undefined) {
        parts.push(`Part ${location.part}${location.colour ? ` (${location.colour})` : ''}`)
      } else if (location.colour) {
        parts.push(`The ${location.colour} part`)
      }
      if (location.bbox) parts.push(`a ${formatBbox(location.bbox)} region`)
      const edges = location.edges?.length ?? 0
      if (edges > 0) {
        parts.push(
          `${edges}${location.edges_truncated ? '+' : ''} located edge${edges === 1 && !location.edges_truncated ? '' : 's'}`,
        )
      }
      return parts.length > 0 ? parts.join(' · ') : 'The model'
    }
    case 'plate':
      return 'The plate'
    case 'filament_slot':
      return location.slot_id !== null && location.slot_id !== undefined
        ? `Filament slot ${location.slot_id}`
        : 'A filament slot'
    case 'choices':
      return location.setting ? `Print choice ${location.setting}` : 'The print choices'
    case 'profile_setting':
      return location.setting ? `Setting ${location.setting}` : 'A profile setting'
    case 'analyzer':
      return 'The analyzer itself'
  }
}

/**
 * What a decision at `scope` covers. Keys are the backend's (`ScopeRef`,
 * `backend/scadbuddy/analyzers/model.py:210-216`): `pla` or `pla/silk`, `model:<model>`
 * or `id:<printer id>`, the slug, `<slug>@<version>`, `<slug>#<hash>`, the output id.
 */
export function scopeLabel(scope: ScopeRef): string {
  switch (scope.kind) {
    case 'global':
      return 'Every print'
    case 'material':
      return `Every ${scope.key} print`
    case 'printer':
      return scope.key.startsWith('id:')
        ? `This printer (#${scope.key.slice(3)})`
        : `Every ${scope.key.replace(/^model:/, '')} printer`
    case 'template':
      return 'This template'
    case 'template_version':
      return 'This template version'
    case 'configuration':
      return 'These parameters'
    case 'print':
      return 'This print'
  }
}

/**
 * The scopes a decision about `diagnostic` can take effect at. The backend resolves a
 * finding at `context.scopes_for(slots)`: a material scope applies only when one of the
 * finding's slots is that material, and a finding with no slots is about every slot, so
 * each of the report's materials. The report does not say which slot holds which
 * material, so a finding about some slots is offered no material scope rather than one
 * that would be stored and never apply (`backend/scadbuddy/analyzers/context.py`).
 */
export function scopesForFinding(scopes: ScopeRef[], diagnostic: AnalyzerDiagnostic): ScopeRef[] {
  if (!diagnostic.slots?.length) return scopes
  return scopes.filter((scope) => scope.kind !== 'material')
}

export type ScopeKind = ScopeRef['kind']

/**
 * Each kind's rank, broadest first, as the backend orders them (`SCOPE_ORDER`,
 * `backend/scadbuddy/analyzers/model.py:41-49`). The one copy the dialog and the msw
 * mock derive from: a kind the backend adds fails to typecheck here until it is placed.
 */
export const SCOPE_RANK: Record<ScopeKind, number> = {
  global: 0,
  material: 1,
  printer: 2,
  template: 3,
  template_version: 4,
  configuration: 5,
  print: 6,
}

/** The kinds broadest first. */
export const SCOPE_ORDER: readonly ScopeKind[] = (Object.keys(SCOPE_RANK) as ScopeKind[]).sort(
  (left, right) => SCOPE_RANK[left] - SCOPE_RANK[right],
)

/** Scopes broader than a template: a decision there reaches other models' prints. */
export function widerThanTemplate(scope: ScopeRef): boolean {
  return SCOPE_RANK[scope.kind] < SCOPE_RANK.template
}

export function sameScope(left: ScopeRef, right: ScopeRef): boolean {
  return left.kind === right.kind && left.key === right.key
}

/**
 * The scopes of `offered` (broadest first, as the report lists them) at which a new
 * decision beats one already made at `scope`. The backend takes the narrowest decision,
 * ranked by the report's position (`decisions.resolve`,
 * `backend/scadbuddy/analyzers/decisions.py:240`), so only `scope` itself and the
 * narrower ones qualify; a scope the report does not list is placed by its kind.
 */
export function atOrNarrower(offered: ScopeRef[], scope: ScopeRef): ScopeRef[] {
  const at = offered.findIndex((row) => sameScope(row, scope))
  return offered.filter((row, index) =>
    at >= 0 ? index >= at : SCOPE_RANK[row.kind] > SCOPE_RANK[scope.kind],
  )
}

/**
 * The findings the dialog lists, and the ones it sets aside under a disclosure: `hidden`
 * is advanced detail only, and a suppressed or ignored one was decided on
 * (`_simple`, `backend/scadbuddy/analyzers/runner.py:233`). A crashed analyzer
 * (`SB0001`) is a `warning`, so it is always listed unless someone suppressed it.
 */
export function partition(diagnostics: AnalyzerDiagnostic[]): {
  shown: AnalyzerDiagnostic[]
  setAside: AnalyzerDiagnostic[]
} {
  const shown: AnalyzerDiagnostic[] = []
  const setAside: AnalyzerDiagnostic[] = []
  for (const diagnostic of diagnostics) {
    const decided = diagnostic.status === 'suppressed' || diagnostic.status === 'ignored'
    ;(decided || diagnostic.severity === 'hidden' ? setAside : shown).push(diagnostic)
  }
  return { shown, setAside }
}
