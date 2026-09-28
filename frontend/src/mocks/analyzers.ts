import type {
  AnalysisReport,
  AnalysisRequest,
  AnalyzerDecision,
  AnalyzerDiagnostic,
  AnalyzerSource,
  Output,
  ScopeRef,
} from '../api/types'

/**
 * #284 — the mock print analyzers, shaped like `POST /analyzers/run`'s answer
 * (`backend/scadbuddy/analyzers/runner.py` `build_report`, advanced detail). The rules
 * and their sources are the backend's own (`backend/scadbuddy/analyzers/builtin.py`,
 * `sources.py`); the measurements are made up for the keychain.
 */

const ACCESSED = '2026-09-28'
const STUDIO = 'https://github.com/bambulab/BambuStudio/blob/f977235e6d736c4c0b650520ac5a5b72cbfe9244'

export const MESH_DEFECT_SEVERITY: AnalyzerSource = {
  url: 'https://wiki.bambulab.com/en/software/bambu-studio/release/release-note-2-7-1',
  title: 'Bambu Studio V2.7.1 Release Note | Bambu Lab Wiki',
  quote: 'Non‑manifold edges are reported as Error, and open edges as Info.',
  accessed: ACCESSED,
  supports: ['SB1001', 'SB1002'],
}

export const SUPPORT_THRESHOLD_ANGLE: AnalyzerSource = {
  url: `${STUDIO}/src/libslic3r/PrintConfig.cpp#L6005-L6013`,
  title: 'Bambu Studio PrintConfig.cpp: support_threshold_angle',
  quote: 'Support will be generated for overhangs whose slope angle is below the threshold.',
  accessed: ACCESSED,
  supports: ['SB1003', 'support_threshold_angle'],
}

export const PROCESS_COMMON_SUPPORT: AnalyzerSource = {
  url: `${STUDIO}/resources/profiles/BBL/process/fdm_process_common.json#L31`,
  title: 'Bambu Studio fdm_process_common.json (every BBL process preset inherits it)',
  quote: '"enable_support": "0",',
  accessed: ACCESSED,
  supports: ['SB1003', 'enable_support'],
}

export const ANALYZER_CRASH: AnalyzerSource = {
  url: 'https://github.com/eh-homelab/ScadBuddy/issues/284',
  title: '#284 Print analyzers & fixers',
  quote: 'A crashed analyzer is itself a visible diagnostic (`SB0001`), never a silent skip.',
  accessed: ACCESSED,
  supports: ['SB0001'],
}

/** `SB1003` as the backend reports it, with its one fix (an unverified target). */
export const overhangDiagnostic: AnalyzerDiagnostic = {
  id: 'SB1003',
  key: 'SB1003',
  title: 'Overhangs past the support threshold',
  severity: 'info',
  category: 'geometry',
  message:
    "38 mm² of overhang slopes 30° or less from horizontal, where Bambu Studio's default threshold generates support once supports are on.",
  why: "Bambu's process presets generate support below a 30° slope and leave supports off.",
  location: {
    kind: 'mesh',
    bbox: { min: [2, 1, 3], max: [20, 9, 4], size: [18, 8, 1] },
    edges: [],
    edges_truncated: false,
  },
  evidence: [
    { label: 'overhang area at 60° or more below horizontal', value: 38, unit: 'mm²', origin: 'geometry' },
  ],
  sources: [SUPPORT_THRESHOLD_ANGLE, PROCESS_COMMON_SUPPORT],
  fixes: [
    {
      id: 'enable-support',
      title: 'Turn on supports',
      description:
        'Slice with a process preset derived from the resolved one, with supports on. The threshold angle is left at the base preset\'s.',
      changes: [
        {
          target: 'derived_process_preset',
          setting: 'enable_support',
          base: null,
          base_known: false,
          base_note:
            'Bambuddy exposes no preset contents; every BBL system process preset inherits "enable_support": "0" from fdm_process_common.json.',
          proposed: '1',
          sources: [PROCESS_COMMON_SUPPORT],
          verified: false,
          to_verify:
            "Whether Bambuddy's /local-presets/ can create a process preset that inherits from a base preset plus a diff, and slice with it (AI spec §3.2).",
          outward: true,
        },
      ],
    },
  ],
  slots: [],
  status: 'open',
}

/** `SB1002` on the keychain's second part. */
export const openEdgesDiagnostic: AnalyzerDiagnostic = {
  id: 'SB1002',
  key: 'SB1002:part-2',
  title: 'Open edges',
  severity: 'info',
  category: 'geometry',
  message: 'The Black part (part 2) has 3 open edges.',
  why: 'Measured on the closed per-colour solid ScadBuddy renders for this colour, never on the preview split.',
  location: {
    kind: 'mesh',
    part: 2,
    colour: 'Black',
    edges: [
      { part: 2, colour: 'Black', kind: 'open', faces: 1, start: [0, 0, 0], end: [1, 0, 0] },
      { part: 2, colour: 'Black', kind: 'open', faces: 1, start: [1, 0, 0], end: [1, 1, 0] },
      { part: 2, colour: 'Black', kind: 'open', faces: 1, start: [1, 1, 0], end: [0, 0, 0] },
    ],
    edges_truncated: false,
  },
  evidence: [{ label: 'open edges', value: 3, origin: 'geometry' }],
  sources: [MESH_DEFECT_SEVERITY],
  fixes: [],
  slots: [],
  status: 'open',
}

/** `SB0001`: an analyzer that raised, reported as a finding (`runner.py` `_crashed`). */
export function crashedDiagnostic(analyzer: string, title: string): AnalyzerDiagnostic {
  return {
    id: 'SB0001',
    key: `SB0001:${analyzer}`,
    title: 'An analyzer failed',
    severity: 'warning',
    category: 'analyzer',
    message: `${analyzer} (${title}) failed and did not run: ValueError.`,
    location: { kind: 'analyzer', edges: [], edges_truncated: false },
    evidence: [{ label: 'analyzer', value: analyzer, origin: 'runner' }],
    sources: [ANALYZER_CRASH],
    fixes: [],
    slots: [],
    status: 'open',
  }
}

/** A short stable digest, standing in for the backend's sha256 (`context.configuration_key`). */
export function digest(text: string, length = 16): string {
  let a = 0x811c9dc5
  let b = 0x01000193
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index)
    a = Math.imul(a ^ code, 0x01000193) >>> 0
    b = Math.imul(b + code, 0x85ebca6b) >>> 0
  }
  const hex = `${a.toString(16).padStart(8, '0')}${b.toString(16).padStart(8, '0')}`
  return hex.repeat(Math.ceil(length / hex.length)).slice(0, length)
}

/** Every scope a decision about this print can be stored at, broadest first (`context.scopes`). */
export function analysisScopes(output: Output, request: AnalysisRequest): ScopeRef[] {
  const printerId = request.printer_id ?? 1
  const values = output.params ?? {}
  const params = JSON.stringify(values, Object.keys(values).sort())
  return [
    { kind: 'global', key: '' },
    { kind: 'material', key: 'pla' },
    { kind: 'printer', key: 'model:h2c' },
    { kind: 'printer', key: `id:${printerId}` },
    { kind: 'template', key: output.slug },
    ...(output.model_version
      ? [{ kind: 'template_version' as const, key: `${output.slug}@${output.model_version}` }]
      : []),
    { kind: 'configuration', key: `${output.slug}#${digest(params)}` },
    { kind: 'print', key: output.id },
  ]
}

const SEVERITY_ORDER = { error: 0, warning: 1, info: 2, hidden: 3 } as const
const SCOPE_ORDER: ScopeRef['kind'][] = [
  'global',
  'material',
  'printer',
  'template',
  'template_version',
  'configuration',
  'print',
]

/**
 * The decision that decides `diagnostic` (`decisions.resolve`): an enforced one at the
 * broadest scope, else the narrowest, one about this instance beating one about every
 * instance at the same scope.
 */
export function resolveDecision(
  diagnostic: AnalyzerDiagnostic,
  decisions: AnalyzerDecision[],
  scopes: ScopeRef[],
): AnalyzerDecision | undefined {
  const rank = (decision: AnalyzerDecision) => SCOPE_ORDER.indexOf(decision.scope.kind)
  const applicable = decisions.filter(
    (decision) =>
      decision.diagnostic_id === diagnostic.id &&
      (decision.instance == null || decision.instance === diagnostic.key) &&
      scopes.some((scope) => scope.kind === decision.scope.kind && scope.key === decision.scope.key),
  )
  const enforced = applicable.filter((decision) => decision.enforced)
  if (enforced.length > 0) return enforced.sort((a, b) => rank(a) - rank(b))[0]
  return applicable.sort(
    (a, b) => rank(b) - rank(a) || Number(b.instance != null) - Number(a.instance != null),
  )[0]
}

/** Each diagnostic with the status its decision gives it (`runner.apply_decisions`). */
function decide(
  diagnostics: AnalyzerDiagnostic[],
  decisions: AnalyzerDecision[],
  scopes: ScopeRef[],
): AnalyzerDiagnostic[] {
  return diagnostics.map((diagnostic) => {
    const decision = resolveDecision(diagnostic, decisions, scopes)
    if (!decision || decision.kind === 'accept') return diagnostic
    return {
      ...diagnostic,
      status: decision.kind === 'suppress' ? 'suppressed' : 'ignored',
      decision: { decision, stale: false },
    }
  })
}

/** The report for `output`: `diagnostics` decided, sorted and counted as `build_report` does. */
export function analysisReport(
  output: Output,
  request: AnalysisRequest,
  diagnostics: AnalyzerDiagnostic[] = [overhangDiagnostic, openEdgesDiagnostic],
  decisions: AnalyzerDecision[] = [],
): AnalysisReport {
  const scopes = analysisScopes(output, request)
  const sorted = decide(diagnostics, decisions, scopes).sort(
    (left, right) =>
      SEVERITY_ORDER[left.severity] - SEVERITY_ORDER[right.severity] ||
      left.key.localeCompare(right.key),
  )
  const open = sorted.filter((row) => row.status === 'open')
  const count = (severity: AnalyzerDiagnostic['severity']) =>
    open.filter((row) => row.severity === severity).length
  const errors = count('error')
  const warnings = count('warning')
  const suggestions = count('info')
  const parts = (
    [
      [errors, 'problem'],
      [warnings, 'warning'],
      [suggestions, 'suggestion'],
    ] as const
  )
    .filter(([n]) => n > 0)
    .map(([n, noun]) => `${n} ${noun}${n === 1 ? '' : 's'}`)
  const nozzles = request.choices?.nozzles ?? []
  return {
    output_id: output.id,
    slug: output.slug,
    detail: 'advanced',
    summary: {
      headline: parts.length > 0 ? parts.join(', ') : 'Nothing to report',
      errors,
      warnings,
      suggestions,
    },
    diagnostics: sorted,
    accepted_changes: [],
    skipped: [],
    inputs: (['output', 'geometry', 'plate', 'printer', 'choices', 'filaments', 'inventory'] as const).map(
      (name) => ({ name, available: name !== 'choices' || request.choices != null }),
    ),
    scopes,
    decisions_available: true,
    decisions_reason: null,
    base: {
      printer_id: request.printer_id ?? 1,
      printer_model: 'H2C',
      choices_origin: request.choices ? 'request' : null,
      nozzle_sizes: nozzles.map((nozzle) => nozzle.size),
      high_flow: nozzles.some((nozzle) => nozzle.flow === 'high_flow'),
      printer_preset_name: null,
      process_preset_name: request.choices?.process_name ?? null,
      bed_type: request.choices?.bed_type ?? null,
      plate_id: request.plate_id,
      plate_model: 'H2C',
      slots: (request.filament_plan?.slots ?? []).map((slot) => ({
        slot_id: slot.slot_id,
        spool_id: slot.spool_id,
      })),
      copies: request.copies ?? 1,
    },
  }
}
