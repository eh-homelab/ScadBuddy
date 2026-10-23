/**
 * Wire types for the ScadBuddy API.
 *
 * Every one is an alias into `schema.d.ts`, which is generated from
 * `backend/openapi.json` by `pnpm gen:api`. Nothing here is hand-written against the
 * spec any more: the previous version was, and it disagreed with the running backend
 * in a dozen places (`api_key_set` vs `has_api_key`, `job_id` vs `id`, a `{x,y,z}`
 * bounding box that is really `{min,max,size}`, a thumbnail PUT that is multipart).
 * `schema.d.ts` is not committed (#492): typecheck, test and build regenerate it
 * first, so after a backend change `tsc` points at whatever broke.
 */
import type { Origin, Owner, SessionStatus } from '../agent/chat/protocol'
import type { components } from './schema'

type Schemas = components['schemas']

export type ParamValue = boolean | number | string

export type Param = Schemas['Parameter']
export type ParamType = Param['type']
export type ParamOption = Schemas['Option']
export type CustomizerSchema = Schemas['CustomizerSchema']
/** A revision's schema, with that revision's own `ui` declaration (#425). */
export type RevisionSchema = Schemas['RevisionSchema']

/** #204 — an SVG or PNG uploaded for a `// file` parameter; its `id` is the value. */
export type Asset = Schemas['AssetMeta']
/** #296 — how much the upload store holds, against its caps (0 is no limit). */
export type AssetUsage = Schemas['AssetUsage']
export type StoreUsage = Schemas['StoreUsage']

export type ModelSummary = Schemas['ModelRecord']
/** #274 — one image or video of a template, in its order; the first is the cover. */
export type MediaView = Schemas['MediaView']
export type SourceCheck = Schemas['SourceCheck']
/** #1290 — one `.scad` file of a model's directory, `model.scad` (`main`) first. */
export type SourceFile = Schemas['SourceFile']
export type Diagnostic = Schemas['Diagnostic']

export type PastedSource = Schemas['PastedSource']

/** #156 — the body of `POST /models/{id}/duplicate`, and what a duplicate records. */
export type DuplicateRequest = Schemas['DuplicateRequest']
export type Upstream = Schemas['Upstream']

/** #157 — where a duplicate stands against its upstream, and the merge it would make. */
export type UpstreamState = Schemas['UpstreamStatus']['state']
export type UpstreamStatus = Schemas['UpstreamStatus']
export type MergePreview = Schemas['MergePreview']
export type UpstreamMerge = Schemas['UpstreamMerge']

/** #153 — a model fetched from a URL rather than uploaded or pasted. */
export type UrlImport = Schemas['UrlImport']

/** #90 — one entry of a model's git history, and a patch between two of them. */
export type ModelVersion = Schemas['ModelVersion']
export type VersionFile = Schemas['VersionFile']
export type VersionDiff = Schemas['VersionDiff']

export type Job = Schemas['JobStatus']
/** #1899 — a print's own support structures, over the template's `print_settings`. */
export type SupportsChoice = Schemas['SupportsChoice']
/** #3628 — a print's own prime tower, over the template's `print_settings`. */
export type PrimeTowerChoice = Schemas['PrimeTowerChoice']
export type JobState = Job['status']
export type BoundingBox = Schemas['BoundingBox']
export type PartInfo = Schemas['PartInfo']
export type RenderAccepted = Schemas['RenderAccepted']
/** #2340 — a command still accepting, answered 202 for the client to follow (spec 2026-10-13). */
export type CommandAccepting = Schemas['CommandAccepting']

export type Output = Schemas['OutputDetail']
export type ArrangeRequest = Schemas['ArrangeRequest']
/** #3637 — one object of an arrange, maybe in other colours. */
export type ArrangeObject = Schemas['ArrangeObject']
/** #2146 — open a library file or an output as an editable mesh model. */
export type MeshModelCreate = Schemas['MeshModelCreate']
export type ManifestObject = Schemas['ManifestObject']
/** #902 — the re-render queued to give an output saved before Arrange its objects. */
export type BackfillState = Schemas['BackfillState']
/** #902 — Arrange's 409 naming every output that needs a re-render first. */
export type NeedsBackfillProblem = Schemas['NeedsBackfillProblem']
export type BomEntry = Schemas['BomEntry']
export type MigrateResult = Schemas['MigrateResult']
export type LibraryCopy = Schemas['LibraryCopy']
export type EditTarget = Schemas['EditTarget']

/** Named parameter sets per template: shipped with it (`template`) or saved (`mine`). */
export type ParamPreset = Schemas['ParamPreset']
export type ParamPresetCreate = Schemas['ParamPresetCreate']
export type ParamPresetUpdate = Schemas['ParamPresetUpdate']
export type ParamPresetDuplicate = Schemas['ParamPresetDuplicate']

export type FontFamily = Schemas['FontFamily']
/** #2358 — the characters of a text a font has no glyph for (`GET /fonts/glyphs`). */
export type GlyphCoverage = Schemas['GlyphCoverage']
export type FontCatalogue = Schemas['FontCatalogueView']
export type CatalogueFont = Schemas['CatalogueEntry']
export type FontVariant = Schemas['FontVariant']
export type InstalledFamily = Schemas['InstalledFamily']

/** #93 — third-party OpenSCAD libraries: the curated catalogue, and each model's own pins. */
export type CatalogueLibrary = Schemas['CatalogueLibrary']
export type DependencyReport = Schemas['DependencyReport']
export type IncludeTarget = Schemas['IncludeTarget']
export type ModelLibrary = Schemas['ModelLibrary']
/** #217 — a `libraries` entry in model.json that is not a pin, and why. */
export type InvalidLibraryEntry = Schemas['InvalidLibraryEntry']
export type LibraryPinRequest = Schemas['LibraryPinRequest']
/** #169 — the upgrade flow: who pins a library, a candidate ref's check, and a re-pin. */
export type LibraryUser = Schemas['LibraryUser']
export type LibraryCheck = Schemas['LibraryCheck']
export type LibraryCheckRequest = Schemas['LibraryCheckRequest']
export type LibraryRepinRequest = Schemas['LibraryRepinRequest']
export type InstalledLibrary = Schemas['InstalledLibrary']
export type ModelPatch = Schemas['ModelPatch']

export type Settings = Schemas['SettingsView']
export type SettingsUpdate = Schemas['SettingsPatch']
export type ConnectionTest = Schemas['ConnectionTest']
/** #322 — one Bambuddy scope's result in the connection test. */
export type ScopeCheck = Schemas['ScopeCheck']
/** #322 — where an env-seeded setting's value comes from. */
export type SettingSource = NonNullable<Settings['sources']>[string]
export type BootstrapValue = Schemas['BootstrapValue']
export type RememberedChoices = Schemas['RememberedChoices']
export type BambuddyStatus = Schemas['BambuddyStatus']
export type BambuddyTargets = Schemas['BambuddyTargets']
export type BambuddyFolder = Schemas['Folder']
export type BambuddyPrinter = Schemas['Printer']
export type PresetRef = Schemas['PresetRef']
export type SidebarLink = Schemas['SidebarLink']

/** Task 9 R5 — a filament preset a nozzle size takes, for Advanced mode's per-slot override. */
export type FilamentPresetOption = Schemas['FilamentPresetOption']

export type PrintRunRequest = Schemas['PrintRunRequest']
/**
 * #3823 — a run request that also names the registry printer (`printer`, plan 2026-10-16
 * Task 8). Until the backend reads it, `printer_id` (the Bambuddy printer the row links)
 * is what it acts on, and it ignores `printer`: the request model takes no extra keys as
 * errors (`print_run.py` `PrintRunRequest`, pydantic's default `extra='ignore'`).
 */
export type RegistryRunRequest = PrintRunRequest & { printer?: string }
export type PrintRunResult = Schemas['PrintRunResult']
/** #755, #760 — what the run would refuse for the dialog's choices, before Print. */
export type PrintCheck = Schemas['PrintCheck']
export type PrintRun = Schemas['PrintRun']
/** #1053 — a Bambuddy write as an operation: `GET /operations/{id}` reads it. */
export type Operation = Schemas['Operation']

// Flows (#1057, backend `api/flows.py`): a flow's runs and what they wait on.
export type FlowRun = Schemas['Run']
export type FlowRunView = Schemas['RunView']
export type FlowRunStatus = FlowRunView['status']
export type FlowPending = Schemas['PendingEntry']
/** #2482 — a parent's narrowing beside the call it narrows. */
export type FlowProposal = Schemas['ProposalView']
export type FlowStep = Schemas['FlowStep']
export type FlowDecided = Schemas['Decided']
/** #2350 — a registered flow version, with and without its script. */
export type FlowDefinition = Schemas['Definition']
export type FlowSummary = Schemas['DefinitionSummary']
/** #2503 — one version of a flow's git history. */
export type FlowVersion = Schemas['FlowVersion']
/** #2485 — whether a flow is archived or deleted, and by whom. */
export type FlowState = Schemas['FlowState']
/** #2487 — the editor's Save body, its check, and what it completes. */
export type FlowRegister = Schemas['DefinitionCreate']
export type FlowScriptCheck = Schemas['ScriptCheckResult']
/** A problem with a script or (#2483, `file` inputs.json) its inputs schema; `file` is flow.py when left out. */
export type FlowScriptProblem = Omit<Schemas['ScriptProblem'], 'file'> & Partial<Pick<Schemas['ScriptProblem'], 'file'>>
export type FlowHostInterface = Schemas['HostInterface']
export type FlowRunStarted = Schemas['RunAnswer']
/** #2483 — a run's options: its typed inputs (and its grant, #2481). */
export type FlowRunOptions = Schemas['RunCreate']
export type FlowResetPoints = Schemas['ResetPoints']
export type FlowResetPreview = Schemas['ResetPreview']
export type FlowReset = Schemas['Reset']
/** #2481 — a run's grant: what its outward calls may do without asking, and what it let through. */
export type FlowGrantSpec = Schemas['GrantSpec']
export type FlowGrantRule = Schemas['GrantRule']
export type FlowGrantArg = Schemas['ArgBound']
export type FlowGrantView = Schemas['GrantView']
export type FlowGrantUse = Schemas['GrantUseView']
/** #2484 — a flow trigger: a schedule or an event that starts runs of a pinned version. */
export type FlowTrigger = Schemas['TriggerView']
export type FlowTriggerCreate = Schemas['TriggerCreate']
export type FlowTriggerUpdate = Schemas['TriggerUpdate']
export type FlowTriggerSchedule = Schemas['ScheduleTiming']
export type FlowTriggerEvent = Schemas['EventTiming']
export type FlowTriggerFire = Schemas['Fire']
export type FlowStandingGrant = Schemas['StandingGrantView']
export type FlowRunTrigger = Schemas['RunTrigger']
/** A route's 202: the operation to follow, `repeated` when an earlier press started it. */
export type OperationAccepted = Schemas['OperationAccepted']

/** spec 2026-09-27 — the spool-first print dialog's own choices. */
export type ChoicesView = Schemas['ChoicesView']
export type PrintChoices = Schemas['PrintChoices']
/** #717 — filament profiles: cited process changes suggested for a spool. */
export type FilamentProfile = Schemas['FilamentProfile']
export type ProfilePreview = Schemas['ProfilePreview']
export type NozzleChoice = Schemas['NozzleChoice']
export type InstalledNozzle = Schemas['InstalledNozzle']
export type TierOption = Schemas['TierOption']
export type PrintOptions = Schemas['PrintOptions']
export type PrintOptionsView = Schemas['PrintOptionsView']
export type PrintOptionsState = Schemas['PrintOptionsState']
export type PrintOptionsUpdate = Schemas['PrintOptionsUpdate']
/** Where an override is remembered. `request` is not one — it is not remembered. */
export type OptionScope = PrintOptionsUpdate['scope']

/** #87 — the filament picker's wire types. */
export type FilamentOptions = Schemas['FilamentOptions']
export type SpoolOption = Schemas['SpoolOption']
export type SlotNeed = Schemas['SlotNeed']
export type SlotChoice = Schemas['SlotChoice']
export type FilamentPlan = Schemas['FilamentPlan']
export type FilamentWarning = Schemas['FilamentWarning']
export type LoadedAt = Schemas['LoadedAt']
/** #78 — a printer's mounted nozzle, and what the picker remembers per model. */
export type NozzleInfo = Schemas['NozzleInfo']
export type ModelPrintChoices = Schemas['ModelPrintChoices']
/** #83 — the plate remembered per printer, and an output's plates. */
export type PrinterBedType = Schemas['PrinterBedType']
/** #836 — the rack nozzle ScadBuddy would pick, its candidates, and what a run sent. */
export type RackPickView = Schemas['RackPickView']
export type RackOption = Schemas['RackOption']
export type NozzlePlan = Schemas['NozzlePlan']
export type PlannedSlot = Schemas['PlannedSlot']
export type UnknownTray = Schemas['UnknownTray']
export type PreviewStarted = Schemas['PreviewStarted']
export type SlicePreview = Schemas['SlicePreview']
/** #2671 — what a sliced library file was sliced for, read from the file itself. */
export type SlicedProfile = Schemas['SlicedProfile']
/** #2672 — a batch print, kept in Postgres so a reload resumes it. */
export type PrintBatch = Schemas['PrintBatch']
export type BatchItem = Schemas['BatchItem']
export type BatchChoice = Schemas['BatchChoice']
export type BatchSummary = Schemas['BatchSummary']
export type BatchOutcome = Schemas['BatchOutcome']
export type NewBatchFile = Schemas['NewBatchFile']
/** #2951 — Queue all stopped to answer in time: go on from `next_position`. */
export type BatchQueueProgress = Schemas['BatchQueueProgress']
export type TrayAssigned = Schemas['TrayAssigned']
export type RackSentPick = Schemas['RackSentPick']
export type RackAlgorithm = NonNullable<Schemas['ChoicesView']['rack_algorithm']>
export type PrinterRackAlgorithm = Schemas['PrinterRackAlgorithm']
/** #1298, #2170 — each hotend on a printer's rack: its use, what it has loaded, and its serial (the one route that carries one, spec 2026-10-01 §7 amendment). */
export type PrinterRackUsage = Schemas['PrinterRackUsage']
export type RackHotendUsage = Schemas['RackHotendUsage']
export type RackSpoolUse = Schemas['RackSpoolUse']
/** #907, #1862 — Bambu's print sequence, a per-print process override. */
export type PrintSequence = NonNullable<Schemas['PrintRunRequest']['print_sequence']>
export type OutputPlate = Schemas['OutputPlate']

/** #313 — the Library page's listing, and one row of it. */
export type LibraryListing = Schemas['LibraryListing']
/** #2349 — one library file's name by id (`GET /print/library/names`). */
export type LibraryFileName = Schemas['LibraryFileName']
export type LibraryEntry = Schemas['LibraryEntry']
export type LibraryFolderView = Schemas['LibraryFolderView']
export type LibrarySearch = Schemas['LibrarySearch']
export type LibrarySearchHit = Schemas['LibrarySearchHit']
export type LibraryDeleteResult = Schemas['LibraryDeleteResult']
export type LibraryRestoreResult = Schemas['LibraryRestoreResult']
export type LibraryMoveResult = Schemas['LibraryMoveResult']
export type LibraryCopyResult = Schemas['LibraryCopyResult']
export type LibraryRenameResult = Schemas['LibraryRenameResult']
export type LibraryFolder = Schemas['LibraryFolder']
export type LibraryFolderCreate = Schemas['LibraryFolderCreate']
export type LibraryFolderDeleteResult = Schemas['LibraryFolderDeleteResult']
export type LibraryFolderUpdate = Schemas['LibraryFolderUpdate']
/** #1863 — the objects Arrange reads from a library file ScadBuddy did not make. */
export type LibraryFileObjects = Schemas['LibraryFileObjects']
export type LibraryFileObject = Schemas['LibraryFileObject']

/** #2351 — Bambuddy's spool inventory and loaded slots, read through `/farm/inventory`. */
export type InventoryView = Schemas['InventoryView']
export type InventorySpool = Schemas['InventorySpool']

/**
 * #89 — run tracking.
 *
 * `PrintRoute` and `PrintStage` are named aliases onto the generated members rather
 * than string literals of their own: the backend's vocabulary is the contract, and a
 * hand-kept copy would drift the moment it gains a state.
 */
export type PrintProgress = Schemas['PrintProgress']
/** #977 — what each printer is doing now, whoever started it. */
export type PrintersNow = Schemas['PrintersNow']
/** #2351 — Bambuddy's print queue and the farm's totals, read through `/farm`. */
export type QueueItem = Schemas['QueueItem']
export type ArchiveStats = Schemas['ArchiveStats']
export type PrinterNow = Schemas['PrinterNow']
/** #3823 — the printer registry: one row per physical printer, keyed by serial. */
export type PrinterRow = Schemas['PrinterRow']
export type PrinterCreate = Schemas['PrinterCreate']
export type PrinterPatch = Schemas['PrinterPatch']
export type PrintPath = PrinterRow['preferred_path']
export type DiscoveredPrinter = Schemas['DiscoveredPrinter']
/** The LAN link as typed: the access code goes once and never comes back. */
export type LanLink = Schemas['LanLink']
/** #308 — ScadBuddy's print history: one linked Bambuddy archive per print. */
export type PrintSummary = Schemas['PrintSummary']
export type PrintPage = Schemas['PrintPage']
/** Orders (#4249, spec 2026-10-18 §3): customers, orders and their line items. Money is
 * integer minor units (`*_minor`) in the order's `currency`. */
/** A body whose fields `K` the backend defaults, so a caller may leave them out (the
 * generated type lists a defaulted field as required, as `FlowScriptProblem` notes). */
type Defaulted<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>
export type Customer = Schemas['Customer']
export type CustomerContact = Schemas['Contact']
export type CustomerAddress = Schemas['Address']
export type CustomerCreate = Defaulted<Schemas['CustomerCreate'], 'company' | 'notes' | 'tags' | 'contacts' | 'addresses'>
export type CustomerPatch = Partial<Schemas['CustomerPatch']>
export type Order = Schemas['Order']
export type OrderStatus = Order['status']
export type OrderDetail = Schemas['OrderDetail']
export type OrderItem = Schemas['OrderItem']
export type OrderItemStatus = OrderItem['status']
/** An item with its quotes and fulfilment attempts, as `GET /orders/{id}` lists them. */
export type OrderItemDetail = Schemas['ItemDetail']
export type OrderTotals = Schemas['OrderTotals']
export type OrderPage = Schemas['OrderPage']
export type Quote = Schemas['Quote']
export type Fulfilment = Schemas['Fulfilment']
export type Shipment = Schemas['Shipment']
export type Payment = Schemas['Payment']
export type OrderCharge = Schemas['Charge']
export type OrderEvent = Schemas['OrderEvent']
export type AddressSnapshot = Schemas['AddressSnapshot']
export type OrderCreate = Defaulted<Schemas['OrderCreate'], 'notes' | 'due_at'>
export type OrderPatch = Partial<Schemas['OrderPatch']>
export type OrderItemCreate = Defaulted<Schemas['ItemCreate'], 'notes' | 'title' | 'slug' | 'commit' | 'inputs' | 'output_id'>
export type OrderItemPatch = Partial<Schemas['ItemPatch']>
export type ManualQuoteInput = Defaulted<Schemas['ManualQuoteInput'], 'label'>
export type FulfilmentInput = Defaulted<Schemas['FulfilmentInput'], 'notes' | 'failure_reason' | 'started_at' | 'finished_at'>
export type ShipmentInput = Partial<Schemas['ShipmentInput']>
/** `PATCH …/shipments/{shipment}`: only what is sent changes. */
export type ShipmentEdit = Schemas['ShipmentEdit']
export type PaymentCreate = Defaulted<Schemas['PaymentCreate'], 'notes' | 'method' | 'reference' | 'received_at'>
/** Orders phase 2 (#4250, spec 2026-10-18 §5): the price book, the slice quote and the
 * rough estimate. Rates are decimal strings; `minor_exponent` is the minor unit's decimal
 * places, the backend's (ISO 4217) and never `Intl`'s. */
export type QuoteLine = Schemas['QuoteLine']
export type Filament = Schemas['Filament']
export type FilamentInput = Defaulted<Schemas['FilamentInput'], 'subtype' | 'brand' | 'colour_name' | 'density_g_cm3'>
export type FilamentPatch = Partial<Schemas['FilamentPatch']>
export type PricingPolicy = Schemas['PricingPolicy']
/** `PUT /orders/price-book`: the whole policy, and the estimate's factor when sent. */
export type PolicyUpdate = Defaulted<Schemas['PolicyUpdate'], 'rough_estimate_factor'>
export type PriceBook = Schemas['PriceBook']
/** `POST …/items/{item}/quote`: the print dialog's choices, and a price-book filament per
 * colour slot (`colours[].slot`, from 0). */
export type SliceQuoteRequest = Schemas['SliceQuoteRequest']
export type OrderEstimate = Schemas['OrderEstimate']

/** The API's `ActivityItem`: the backend's `ActivityRow` as answered (advanced fields only with `?advanced=1`). */
export type Activity = Schemas['ActivityItem']
export type ActivityPage = Schemas['ActivityPage']
export type ActivityFacets = Schemas['ActivityFacets']
export type ActivityDetail = Schemas['ActivityDetail']
export type PrintDetail = Schemas['PrintDetail']
export type TimelapseFrames = Schemas['TimelapseFrames']
/** #311 — the print detail page's two writes. */
export type PrintAgain = Schemas['PrintAgain']
export type PrintAttachment = Schemas['PrintAttachment']
export type PrinterMedia = Schemas['PrinterMedia']
export type CopyProgress = Schemas['CopyProgress']
export type PrintRoute = PrintProgress['route']
export type PrintStage = PrintProgress['stage']

/**
 * #79 — projects. A project here is one of Bambuddy's plus the library folder that
 * belongs to it; ScadBuddy stores only which project a model's prints are filed under.
 */
export type ProjectView = Schemas['ProjectView']
export type ProjectChoices = Schemas['ProjectChoices']
/** #3651 — a library folder of no project, a destination of its own. */
export type FolderView = Schemas['FolderView']
/** #3651 — how the destination picker orders projects and folders. */
export type DestinationSort = Schemas['ProjectChoices']['sort']
/** #3651 — where some content is filed by default, and why. */
export type DefaultDestination = Schemas['DefaultDestination']
export type ProjectRequest = Schemas['ProjectRequest']
export type ProjectAttach = Schemas['ProjectAttach']
export type ProjectFile = Schemas['ProjectFile']
export type ModelProjects = Schemas['ModelProjects']
export type ModelProjectPut = Schemas['ModelProjectPut']
export type AttachResult = Schemas['AttachResult']

/** #81 — the build volume the preview draws and checks the model against. */
export type Plate = Schemas['PlateView']
export type PlateCatalogue = Schemas['PlateCatalogue']
export type PlateFit = Schemas['PlateFit']

/**
 * #284 — the print analyzers (#461, `backend/scadbuddy/api/analyzers.py`): a run over
 * the request the print dialog would send, and the report it answers with.
 */
export type AnalysisRun = Schemas['AnalysisRun']
export type AnalysisTarget = Schemas['AnalysisTarget']
export type AnalysisRequest = Schemas['AnalysisRequest']
export type AnalysisReport = Schemas['AnalysisReport']
export type AnalyzerDiagnostic = Schemas['AnalyzerDiagnostic']
export type AnalyzerSeverity = AnalyzerDiagnostic['severity']
export type AnalyzerSource = Schemas['Source']
export type DiagnosticLocation = Schemas['DiagnosticLocation']
export type ScopeRef = Schemas['ScopeRef']
/** A decision about a finding at a scope; `DecisionCreate` records an ignore or a suppression. */
export type AnalyzerDecision = Schemas['Decision']
export type DecisionCreate = Schemas['DecisionCreate']
/** A fix a finding offers: a diff against the print's base, line by line. */
export type AnalyzerFix = Schemas['Fix']
export type SettingChange = Schemas['SettingChange']
export type FixRequest = Schemas['FixRequest']
export type FixPreview = Schemas['FixPreview']
export type FixApply = Schemas['FixApply']

export type SendRequest = Schemas['SendRequest']
export type SendResult = Schemas['SendResult']

/**
 * A view model, not a wire type: the API returns a flat `parameters` list plus the
 * group names in source order, and the panel wants them bucketed.
 */
export interface ParamGroup {
  name: string
  params: Param[]
}

/**
 * RFC 9457 problem details. The backend adds extensions alongside the standard
 * members — `required_scope` on a missing Bambuddy scope, `bambuddy_body` carrying
 * Bambuddy's own refusal verbatim — so unknown keys are kept, not dropped.
 */
export interface Problem {
  type?: string
  title: string
  status: number
  detail?: string
  instance?: string
  required_scope?: string
  bambuddy_status?: number
  bambuddy_body?: unknown
  [extension: string]: unknown
}

/**
 * The agent service's switch for the assistant's `http_request` tool (#827,
 * `GET/PUT /api/v1/ai/settings/http-request`). On by default.
 */
export interface HttpRequestSetting {
  enabled: boolean
}

/** #982 — an origin the user let one chat's headless browser open (`once`: one navigation, `session`: the chat). */
export interface ApprovedBrowserOrigin {
  session_id: string
  title: string
  origin: string
  scope: 'once' | 'session'
  approved_at: string
}

/**
 * #982 — the headless browser's lists (`GET/PUT /api/v1/ai/settings/browser-origins`,
 * agent `src/routes/browserOrigins.ts`): rules are an origin, a host, `*.host` or an
 * address range; deny wins over allow, and an origin on neither list asks the user.
 */
export interface BrowserOriginsSetting {
  allow: string[]
  deny: string[]
  defaults: { allow: string[]; deny: string[] }
  approved: ApprovedBrowserOrigin[]
}

/**
 * #1911 — the agent service's switch for the assistant's `get_printer_camera` tool
 * (`GET/PUT /api/v1/ai/settings/printer-camera`, agent `src/routes/printerCamera.ts`).
 * On by default; while off, every call to the tool is refused.
 */
export interface PrinterCameraSetting {
  enabled: boolean
}

/**
 * #1917 — the Claude model every assistant turn and the connection test use
 * (`GET/PUT /api/v1/ai/settings/model`, agent `src/routes/model.ts`): an alias such as
 * `opus` or a full model id; `null` is Claude Code's own default.
 */
export interface ModelSetting {
  model: string | null
}

/**
 * #1291 — the blank starter template (`GET /api/v1/ai/templates/blank`, agent
 * `src/routes/templates.ts`), the source the agent's create_from_template writes.
 */
export interface BlankTemplate {
  source: string
}

/**
 * #790 — what a new assistant session may spend in all (USD) and how many turns one
 * reply may take (`GET/PUT /api/v1/ai/settings/session-limits`, agent
 * `src/routes/sessionLimits.ts`). Applies to sessions started after a change.
 */
export interface SessionLimits {
  budget_usd: number
  max_turns: number
}

/**
 * The session runtime settings (`GET/PUT /api/v1/ai/settings/session-runtime`, agent
 * `src/routes/sessionRuntime.ts`): how long a parked call is held, the warm process
 * cache's size (0: every turn is cold) and idle time, and the most a turn may run.
 */
export interface SessionRuntime {
  park_hold_seconds: number
  warm_max_per_replica: number
  warm_idle_seconds: number
  turn_max_seconds: number
}

/**
 * How many of the assistant's renders run at once (`GET/PUT /api/v1/ai/settings/render-limits`,
 * agent `src/routes/renderLimits.ts`). Past a concurrency limit a render waits for a slot,
 * up to the tool's own wait; a call is refused past 100 waiting per caller, or past
 * `per_window`, which counts waiting renders. Read live by the agent, so a change
 * applies to waiting renders too.
 */
export interface RenderLimits {
  /** Each caller's renders at once, unless an override names it. */
  per_principal: number
  /** Every caller's together; 0 is no limit. */
  global: number
  /** Renders one caller may start in 10 minutes; 0 is no limit. */
  per_window: number
  /** By principal id: `browser` (you and your chats), `token:<id>`, `oidc:<issuer>#<sub>`, `anonymous` (every anonymous MCP session together). */
  overrides: { principal: string; concurrent: number }[]
}

/**
 * Plan 5d — the mode a new assistant session gets when whoever starts it names none
 * (`GET/PUT /api/v1/ai/settings/session-mode`, agent `src/routes/sessionMode.ts`).
 * `durable_available` says whether a durable session could start now; when not, a
 * session that got durable from this default runs classic and says why
 * (`durable_unavailable_reason`). The PUT sends `mode` only.
 */
export interface SessionModeSetting {
  mode: 'classic' | 'durable'
  durable_available: boolean
  durable_unavailable_reason?: string
}

/**
 * Where a durable segment's subagents reach ScadBuddy's tools
 * (`GET/PUT /api/v1/ai/settings/durable-subagents`, agent `src/routes/durableSubagents.ts`).
 * `url` replaces agent-durable's own (null: the pod's loopback); `peers` are the addresses or
 * CIDR ranges besides loopback the agent answers on that route (empty: loopback only).
 */
export interface DurableSubagentsSetting {
  url: string | null
  peers: string[]
}

/**
 * The long edge, in pixels, the assistant panel scales an attached image to before
 * sending it (`GET/PUT /api/v1/ai/settings/images`, agent `src/routes/imageSettings.ts`).
 * `min` and `max` are the bounds the agent accepts; the PUT sends `long_edge` only.
 */
export interface ImageSettings {
  long_edge: number
  min: number
  max: number
}

/**
 * The longest automatic Hindsight recall query, in characters (`GET/PUT
 * /api/v1/ai/settings/memory`, agent `src/routes/memorySettings.ts`): the Claude Code
 * Hindsight plugin's `recallMaxQueryChars`. `0` sends the whole query; `default` and `max`
 * come from the agent, and the PUT sends `recall_max_query_chars` only.
 */
export interface MemorySettings {
  recall_max_query_chars: number
  default: number
  max: number
}

/**
 * #2329 — the caps on the assistant's workspace, in bytes (agent
 * `routes/workspaceSettings.ts`), with the bounds the form may save.
 */
export interface WorkspaceSettings {
  file_max_bytes: number
  workspace_max_bytes: number
  min: number
  file_max: number
  workspace_max: number
}

/** #2329 — one file in a session's workspace (agent `routes/sessionFiles.ts`). */
export interface WorkspaceFile {
  path: string
  media_type: string
  size: number
  sha256: string
  /** Where it came from: an upload (`kind: 'upload'`), or the tool and its ids, or a URL. */
  source: Record<string, string | number | boolean | null> | null
  created_at: string
  updated_at: string
}

/**
 * A session's background task (the agent's `routes/backgroundTasks.ts`): what its
 * CronCreate or ScheduleWakeup left running, which sends `prompt` to the session when due.
 */
export interface BackgroundTask {
  id: string
  kind: 'cron' | 'wakeup' | 'monitor' | 'workflow'
  description: string
  /** The schedule as words: "Every 5 minutes", "once, 1200 s after it was set". */
  schedule: string
  prompt: string
  state: 'active' | 'done' | 'cancelled' | 'failed'
  next_at: string | null
  fired: number
  last_fired_at: string | null
  expires_at: string | null
  detail: string | null
  created_at: string
}

export interface WorkspaceFileList {
  files: WorkspaceFile[]
  usage: { files: number; bytes: number }
  limits: { file_max_bytes: number; workspace_max_bytes: number }
}

/**
 * #1941 — an image the composer uploaded when it was attached (agent `POST
 * /api/v1/ai/attachments`, `src/routes/attachments.ts`): the id a message sends, and the
 * names the image and its preview take in the session (#2479: pointers, never bytes).
 * Unsent, it expires after an hour.
 */
export interface AttachmentView {
  id: string
  name: string
  mediaType: 'image/png' | 'image/jpeg' | 'image/gif' | 'image/webp'
  preview: { name: string; mediaType: 'image/png' | 'image/jpeg' | 'image/webp' }
}

/** One assistant session as the agent's HTTP routes answer it (agent `routes/sessions.ts` `SessionView`). */
/** #795, #1885 — one edit `PATCH /api/v1/ai/sessions/:id` takes. */
export type AiSessionEdit = { title: string } | { done: true } | { archived: boolean }

export interface AiSessionView {
  id: string
  title: string
  origin: Origin
  status: SessionStatus
  updated_at: string
  parent_id: string | null
  turns: number
  cost_usd: number
  budget_usd: number
  running: boolean
  /** Plan 5d — absent from an older agent (classic). */
  mode?: 'classic' | 'durable'
  /** #1885 — archived by its owner: out of the panel's list, and read-only. Absent from an older agent. */
  archived?: boolean
  archived_at?: string | null
  /** #1284 — a live handoff offer: to whom, until when. */
  offer?: { to: Pick<Owner, 'kind' | 'label'> & { id?: string }; until: string } | null
}

/** #2482 — a run's step, as a queue entry names it. */
export interface FlowQueueStep {
  seq: number
  fn: string
}

/** #2482 — one hop action on an entry (`hops`), as the run page lists it. */
export interface FlowQueueHop {
  hop: string
  action: string
  actor: string
  at: string
  reason?: string | null
}

interface FlowQueueCommon {
  id: string
  hop: 'parent' | 'person' | null
  hop_until: string | null
  reason: string | null
  hops: FlowQueueHop[]
  step: FlowQueueStep | null
}

/**
 * #2482 — one entry of a flow run's one queue (agent `flows/queue.ts` QueueEntry,
 * `GET /api/v1/ai/flow-runs/:id/queue`): the run's own call, a child session's call
 * (answered by `POST /api/v1/ai/pending-input/{id}`), or its requested grant. A child's
 * `reasoning` is its model's text before the call: untrusted.
 */
export type FlowQueueEntry =
  | (FlowQueueCommon & {
      source: 'flow'
      kind: 'approval' | 'answer'
      call_id: string
      fn: string
      prompt: string | null
      since: string | null
      proposed: boolean
    })
  | (FlowQueueCommon & {
      source: 'child'
      kind: 'approval' | 'answer'
      session_id: string
      tool: string
      summary: string
      input_hash: string | null
      created_at: string
      expires_at: string
      /** Past its timer, not yet resolved by its worker: no one can decide it now. */
      expiring: boolean
      reasoning: string
    })
  | { source: 'grant'; id: string; status: 'requested'; created_at: string }

/** #2482 — a flow run's one queue (agent `flows/queue.ts` RunQueue). */
export interface FlowRunQueue {
  run_id: string
  name: string
  status: string
  starter_session: string | null
  entries: FlowQueueEntry[]
}

/**
 * #931 — one resource a session's tool call created, changed or deleted (agent
 * `sessions/touched.ts` TouchedRecord, `GET /api/v1/ai/sessions/:id/resources`).
 * `model` is the model it belongs to; `before`/`after` are what it was and became
 * where that has an id (a revision's parent and new commit). `print` is a Bambuddy
 * queue item id; `print_run` is ScadBuddy's own run.
 */
export interface SessionResource {
  type:
    | 'model'
    | 'revision'
    | 'preset'
    | 'asset'
    | 'render_job'
    | 'output'
    | 'print_run'
    | 'print'
    | 'library'
    | 'font'
    | 'setting'
    | 'project'
    | 'bambuddy_file'
    | 'print_archive'
    | 'library_file'
    | 'unclassified'
  /** Null only for `unclassified`. */
  id: string | null
  action: 'created' | 'modified' | 'deleted'
  model: string | null
  before: string | null
  after: string | null
  /** The tool whose call touched it. */
  tool: string
  at: string
}

/**
 * #931 — a resource to find the sessions of (agent `sessions/touched.ts` ResourceRef,
 * `GET /api/v1/ai/resources/:type/:id/sessions`). A `model` matches anything of that
 * model; any other kind matches its own id.
 */
export interface ResourceRef {
  type: Exclude<SessionResource['type'], 'unclassified'>
  id: string
}
