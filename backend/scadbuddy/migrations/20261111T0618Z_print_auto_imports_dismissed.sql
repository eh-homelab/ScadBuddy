-- #4699 phase 2: a person (or the assistant) dismisses a print from Unsorted imports
-- (`POST /print-imports/auto/{archive_id}/dismiss`). The row stays, so the watcher never
-- takes the archive again (`AutoImportStore.known` reads every row), and its state is
-- `dismissed`: it leaves the inbox. `dismissed_at` is when.
ALTER TABLE print_auto_imports DROP CONSTRAINT IF EXISTS print_auto_imports_state_check;
ALTER TABLE print_auto_imports ADD CONSTRAINT print_auto_imports_state_check
    CHECK (state IN ('linked', 'inbox', 'dismissed'));
ALTER TABLE print_auto_imports ADD COLUMN IF NOT EXISTS dismissed_at timestamptz;
