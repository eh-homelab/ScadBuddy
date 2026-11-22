# Customizer, parameters, colours and fonts

## What it does

A template's OpenSCAD customizer variables become a form: tabs from `/* [Group] */`,
sliders, dropdowns, toggles, text, colour and font pickers, and file inputs. Presets save
a named set of values per template; a template can ship its own read-only presets. User
guide: [Customizing](../user-guide.md#customizing), [Fonts](../user-guide.md#fonts),
[File parameters](../user-guide.md#file-parameters),
[Multi-colour output](../user-guide.md#multi-colour-output).

## How it works

`openscad -o model.param` exports the schema; ScadBuddy adds the `// color`, `// font`
and `// file` annotations OpenSCAD does not type by scanning the source (see the root
[`CLAUDE.md` "Verified OpenSCAD facts"](../../CLAUDE.md#verified-openscad-facts-do-not-re-derive-re-measure-if-the-base-image-moves)).
Each `// color` parameter is one colour part, and later one closed solid and one extruder
in the 3MF ([preview and 3MF](preview-and-3mf.md)). A font name must exist in the image or
be a Google Fonts family, which is installed on demand.

## Code

- Page: `frontend/src/pages/CustomizePage.tsx`; form `frontend/src/components/ParameterPanel.tsx:52`;
  widgets `frontend/src/components/widgets/ParamWidget.tsx:32`.
- Schema: `backend/scadbuddy/render/schema.py` (types at `:16`, the annotation scan at
  `:46`), cached by `backend/scadbuddy/render/runner.py:641`.
- Routes: `backend/scadbuddy/api/params.py`, `backend/scadbuddy/api/presets.py`,
  `backend/scadbuddy/api/fonts.py`, `backend/scadbuddy/api/assets.py:93` (`// file` uploads).
- Fonts mirrored to render workers: `backend/scadbuddy/store/fonts.py` (`font-<dir>`
  keys, `:27`).

## Data

- Table `saved_presets` (`backend/scadbuddy/migrations/20260928T0729Z_saved_presets.sql`).
- Table `assets` and blob keys `asset-<id>` for file parameters.
- Remembered print choices per model: `model_print_choices`.

## Tests

- Backend: `backend/tests/test_schema.py`, `backend/tests/test_runner.py`.
- e2e: `frontend/e2e/customize.spec.ts`, `presets.spec.ts`, `action-bar.spec.ts`.

## Limits and known issues

- A missing font family falls back silently to DejaVu (root `CLAUDE.md`); fonts named only
  inside a library checkout are not mirrored to render workers on the bambuddy store.
