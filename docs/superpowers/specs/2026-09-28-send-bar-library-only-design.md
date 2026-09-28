# Send bar: library upload only (#312)

Approved 2026-09-28. Supersedes the send-bar queue path of 2026-09-24 and the "send bar still runs the configured pipeline" note of 2026-09-27.

## Decisions
- The send bar (ActionBar → SendDialog → POST /outputs/{id}/send) becomes **library upload only**. Its queue mode is removed entirely, and printing happens only through the Print dialog (spool-first flow).
- Remove the Settings print defaults: the pipeline selector, `pipeline_id`, and the raw `printer_preset` / `process_preset` / `filament_presets` / `bed_type` (verify the exact names) settings fields that the no-pipeline send used. Update the agent `SETTINGS_FIELDS` (frontend/src/agent/catalog.ts) and the plugin docs (plugins/scadbuddy/skills/print/SKILL.md, plus any other mention).
- Remove `pipeline_for`, `run_pipeline` (client), `set_model_pipeline`, the `"model_pipeline"` SettingsSection, and the unread `model_pipelines` field.

## Current map (from exploration; file:line refs are approximate)
- Frontend: ActionBar.tsx:175-196 opens SendDialog. SendDialog.tsx has the mode radio library/queue (14-25, 181-206), copies (44-48, 208-235), PrintOptionsDisclosure in queue mode (237-244), send() → api.sendOutput(id, {mode, options}) (89-105), result branches queue_item_id / pipeline_run_id / library (144-161), the edit-link message (166-174), and eligibilityIssues(cause.problem) (101; lib/problems.ts:27-33). The client is api.sendOutput (client.ts:416).
- Backend: api/outputs.py:337-361 send_output_to_bambuddy → send.py send_output (614-635). observer.started only when a print was queued.
  - upload_output / ensure_uploaded (249-341) with the target_for → _target_model_and_preset pipeline branch (130-170). Layout uses the pipeline's target_model_class / target_printer_id / printer_preset when a pipeline is set; otherwise settings.printer_id.
  - mode library → return after upload + attach_edit_link.
  - mode queue → _queue_send (498-611): pipeline_for, scope_printer(fetch_pipeline) (472-495), _resolve_options, then one of three branches: A = run_pipeline, B = pipeline_slice_request (405-433), C = _settings_slice_request (384-402); all go through slice → await_slice → enqueue. request_scope (436-445). DEFAULT_PLATE.
- run_for_output (pipelines.py) calls target_for with an explicit printer_id + nozzle_diameter and never touches the pipeline branch.
- client.pipelines() is used by send.py:153 and api/settings.py:220 (get_targets feeds the Settings dropdown). client.run_pipeline is only used by send.py:525.
- ELIGIBILITY_PROBLEM (bambuddy/errors.py:22, 101-107): a generic Bambuddy 409 mapper. Its practical trigger is run_pipeline. Check other 409 sources before removing it.
- Progress: progress.py normalizes route "pipeline" (180) and "slice_queue". PrintProgressPanel.tsx:46-56 special-cases route === 'pipeline'. Records hold pipeline_run_id from old sends. **Ruling:** drop the pipeline route from progress too. New sends never create one, and history comes from Bambuddy archives (#305).
- Settings: StoredSettings has no model_config, so pydantic's default extra="ignore" means removing fields still loads an old settings.json, and the keys are dropped on the next _write. Consumers to update in the same change: pipeline_for, api/settings.py SettingsView / SettingsPatch / _view / get_print_options (160-165 resolves printer_id from the pipeline) / get_targets, SettingsPage.tsx:346-368 (pipeline select, plus stale help text "and for Print").
- Print options: resolve_print_options has global → per-printer → per-model → per-request scopes. The picker uses them too (test_print_options_picker.py). Keep what the picker uses; remove only what is send-only.
- Tests: backend test_send.py (31) and test_send_options.py (17) cover the send pipeline and queue paths. Retarget the library-mode tests and delete the queue/pipeline ones, listing them in the commit body. Frontend: about 9 tests in CustomizePage.test.tsx open the send dialog.
- The events/agent/plugin references come from the recent main merge: core/events.py SettingsSection "model_pipeline" (~165), test_event_bus.py:370, agent/src/api/schema.d.ts (generated), frontend/src/agent/catalog.ts:33.

## Done when
- The send dialog has no queue mode, copies, print options, or pipeline/eligibility handling. It uploads, attaches the edit link, and links to the Bambuddy library file. The Print button stays the only way to print.
- No production code references pipelines: grep for `pipeline` in backend/scadbuddy and frontend/src (excluding comments that explain history, recordings, and the spool-first picker's own word "pipeline" in docs) turns up nothing live.
- The Settings page has no pipeline or raw-preset fields. An old settings.json still loads.
- Generated files are regenerated (openapi.json, frontend + agent schema.d.ts), and all gates pass (backend, frontend, e2e, agent, plugin lint).
- docs/user-guide.md, the spec (supersede note: the send bar no longer queues), and the plugin skill are updated. #312 is closed by the PR.
