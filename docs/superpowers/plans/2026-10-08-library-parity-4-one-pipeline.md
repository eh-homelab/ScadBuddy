# Library parity 4: one slicing pipeline (#1752)

Part of #1749. Owner rule: a library-file print differs from an output's only in how
the 3MF to slice is obtained. Rows closed: B1-B4, B7, R3.

## Shape

- `PrintSource` keeps its data (subject, colours, options slug, print settings, plate
  ids, copy names) and gains `fetch_3mf`, the one per-kind step: an output reads its
  `model.3mf`; a library file downloads its bytes (capped), wrapping an STL in a
  ScadBuddy 3MF of one part. `lays_out` goes.
- Everything else is one pipeline (`_Pipeline`, mixed into both sources):
  `file_to_read`, `file_read_already`, `file_to_print`, `record`, `remember_project`,
  and `states_nozzles` (what `lays_out` answered, now judged from the fetched bytes).
- `send.py`'s copy functions work on a `Printable` (copies' key, own colours, inbox
  name, fetch, origin file) instead of an `OutputMeta`; the output wrappers stay, so
  Generate and the send bar are unchanged. Library copies are recorded in the uploads
  store under the subject's `run_subject` (`library:<id>`), as outputs' are by id.

## Laying out (riskiest step)

`_laid_out_for(payload, target)` judges the bytes:

1. ScadBuddy laid it out (`printer_settings_id` is the `ScadBuddy` placeholder and its
   plates read back): replated for the printer, nozzle stated, recolored (as today).
2. Bambu settings an author saved (`nozzles_statable`): the author's placement and
   plates are kept byte for byte; only the nozzle side, flows and filament colours are
   stated in `project_settings.config`.
3. Anything else (sliced, another slicer's, past the caps): printed as it is; unchanged
   bytes of a library file in the inbox print the file itself, with no copy.

The run's warnings and the rack's flow use case 3 alone as "not laid out".

## Steps (test first)

1. Tests: library 3MF by ScadBuddy is replated, stated and recolored, into a project
   folder, `record_sliced` and `remember_project` happen; an author's multi-plate file
   keeps every entry but the settings; an STL is wrapped; unchanged inbox file prints
   as it is; check warnings follow the bytes.
2. `bambu3mf`: `layout_of(payload)`, `state_nozzles(..., filament_colour=)`,
   `stl_3mf(payload)`.
3. `send.py`: `Printable`; generalize the copy functions.
4. `print_source.py`: `_Pipeline`, `fetch_3mf`, drop `lays_out`.
5. `print_run.py`, `print_activities.py`, `api/library_print.py`: pass the uploads
   store, use `states_nozzles`, remember a library project too.
6. Update the API tests that asserted the old as-is behaviour.
