-- #931: the kinds the remaining tools' extractors record (src/sessions/touched.ts
-- RESOURCE_TYPES): libraries by name (a model's pin, with its model_slug, or the
-- shared checkout, without), fonts, stored settings and remembered choices,
-- and Bambuddy's projects, library files and print archives.
ALTER TABLE ai_session_resources
  DROP CONSTRAINT ai_session_resources_resource_type_check,
  ADD CONSTRAINT ai_session_resources_resource_type_check CHECK (resource_type IN
    ('model', 'revision', 'preset', 'asset', 'render_job', 'output', 'print_run', 'print',
     'library', 'font', 'setting', 'project', 'bambuddy_file', 'print_archive',
     'unclassified'));
