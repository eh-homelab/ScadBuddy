-- #316, #455: an output's uploads to Bambuddy's file library and the slices Bambuddy
-- made of them (`bambuddy.uploads.BambuddyUploadStore`). Outputs themselves are
-- still files, so nothing here references one: deleting an output deletes its rows.
CREATE TABLE output_bambuddy_uploads (
    output_id       text NOT NULL,
    library_file_id bigint NOT NULL,
    folder_id       bigint,
    target_key      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (output_id, library_file_id)
);
CREATE INDEX output_bambuddy_uploads_place
    ON output_bambuddy_uploads (output_id, folder_id, target_key);
CREATE TABLE output_bambuddy_slices (
    output_id              text NOT NULL,
    source_library_file_id bigint NOT NULL,
    sliced_library_file_id bigint NOT NULL,
    preset_key             text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (output_id, sliced_library_file_id),
    FOREIGN KEY (output_id, source_library_file_id)
        REFERENCES output_bambuddy_uploads (output_id, library_file_id) ON DELETE CASCADE
);
CREATE INDEX output_bambuddy_slices_source
    ON output_bambuddy_slices (output_id, source_library_file_id);
