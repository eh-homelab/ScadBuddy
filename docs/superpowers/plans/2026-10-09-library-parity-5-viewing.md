# Library parity 5: viewing library prints (#1753)

Part of #1749. Rows closed: F4 (analyzers for a library file), F8 (RenderSection for a
library print), H5 (`_files` names the library file). Builds on #1882
(`LibrarySource.fetch_3mf`) and #1927/#1932 (`render/objects3mf.py`).

Owner rule: a library-file print differs from an output's only in how the 3MF is
obtained. Viewing follows it: the preview and the geometry checks read the 3MF that
`fetch_3mf` returns, as an output's read its `model.3mf`.

## Tasks

1. **Bound the object reader (security, test-first).** `objects3mf` caps component
   depth but not fan-out: a few KB of components that each name the same object many
   times expand exponentially. Count every object visited and every triangle produced
   against `MAX_VISITS` / `MAX_TRIANGLES`; past either, `UnreadableObjectsError`.
2. **`read_plate_parts(payload, plate)`** in `objects3mf`: one `ColourPart` per colour of
   the plate's build items, placed as the file places them (plates from
   `model_settings.config`; a file with none is one plate). Same refusals as
   `read_objects`; `NoSuchPlateError` for a plate it lacks.
3. **`bambuddy/library_view.py`**: download through `LibrarySource.fetch_3mf`, read the
   plate's parts, write the preview GLB and the `GeometryAnalysis`; cached under
   `cache/library-views/` by the file's `file_hash` and plate, bounded by count.
4. **Routes**: `GET /print/library/{id}/preview.glb?plate=` (the GLB, 422 when the file
   cannot be read), `GET /print/library/{id}/file` (the file itself, proxied). Agent
   coverage entries for both (binary).
5. **Analyzers by subject**: `AnalysisTarget.library_file_id`; the context reads geometry
   from step 3, the slots off the file itself, and its narrowest scope is
   `print:library:<id>` (the `print` scope key accepts it). Plate fit stays skipped: a
   library file is printed where its author placed it. `AnalysisReport` gains
   `library_file_id`; `slug` is null for one.
6. **`_files`**: a library print lists `library_file` (the file) and `preview_glb`.
7. **Frontend**: `AnalyzerPanel`/`useAnalysis` take an `AnalysisTarget`; PrintPicker shows
   the panel for a library file; `RenderSection` renders a library print's GLB; msw mocks.
8. **Other findings**: `send.py` `_colours_for` and `bambu3mf` reads: verdicts, issue if
   real and off this path.
