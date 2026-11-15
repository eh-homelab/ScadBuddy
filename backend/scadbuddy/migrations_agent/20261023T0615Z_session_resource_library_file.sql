-- #4507: a file in ScadBuddy's own print library (`library_backend=native`), told
-- apart from Bambuddy's `bambuddy_file` (src/sessions/touched.ts RESOURCE_TYPES).
ALTER TABLE ai_session_resources
  DROP CONSTRAINT ai_session_resources_resource_type_check,
  ADD CONSTRAINT ai_session_resources_resource_type_check CHECK (resource_type IN
    ('model', 'revision', 'preset', 'asset', 'render_job', 'output', 'print_run', 'print',
     'library', 'font', 'setting', 'project', 'bambuddy_file', 'print_archive',
     'library_file', 'unclassified'));
