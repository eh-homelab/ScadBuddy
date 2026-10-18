-- #2672: a batch print, one stepped print dialog over several library files. Kept here so
-- a reload mid-batch resumes where it was left: the step in view, each file's choices
-- and what Queue all did with it.
CREATE TABLE print_batches (
    id          text PRIMARY KEY,
    status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'queued')),
    -- The step in view: a file's position, or the item count for the review.
    step        integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE print_batch_items (
    batch_id         text NOT NULL REFERENCES print_batches (id) ON DELETE CASCADE,
    position         integer NOT NULL,
    library_file_id  bigint NOT NULL,
    filename         text NOT NULL,
    sliced           boolean NOT NULL DEFAULT false,
    skipped          boolean NOT NULL DEFAULT false,
    removed          boolean NOT NULL DEFAULT false,
    -- The PrintRunRequest the step chose, as the run takes it; null until chosen.
    request          jsonb,
    -- What the review lists for it: printer, plate, spools, time and filament.
    summary          jsonb,
    -- What its step's check said the run would refuse.
    problems         jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- What Queue all did with it: the run it started, or why it did not.
    outcome          jsonb,
    PRIMARY KEY (batch_id, position)
);
