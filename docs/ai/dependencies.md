# Agent-managed dependencies: libraries, fonts and include resolution

Issue [#253](https://github.com/eh-homelab/ScadBuddy/issues/253), part of epic
[#249](https://github.com/eh-homelab/ScadBuddy/issues/249). The tools live in
[`agent/src/tools/libraries.ts`](../../agent/src/tools/libraries.ts); the backend
routes they call are in `backend/scadbuddy/api/{libraries,fonts}.py`. The spec's
registry rules apply (spec §5.1): one `defineTool` per tool, every backend operation
covered by a tool or by an entry in `agent/src/tools/coverage.ts`.

## Libraries

| Tool | Tier | Route | What it does |
|---|---|---|---|
| `list_libraries` | read | `GET /api/v1/libraries` | The curated catalogue (BOSL2, dotSCAD, NopSCADlib, Round-Anything, MCAD; `backend/scadbuddy/library/libraries.py` `CURATED`) |
| `pin_library` | write | `PUT /api/v1/models/{slug}/libraries/{name}` | Pin a catalogue library to a model at its ref or another tag or branch. Only the catalogue's own repository |
| `pin_library_from_url` | outward | same | Pin from any other https git URL, behind a human approval (spec §8.2) |
| `repin_library` / `repin_library_from_pinned_url` | write / outward | `PATCH /api/v1/models/{slug}/libraries/{name}` | Re-pin from the URL the pin records; outward when that URL is not the catalogue's |
| `unpin_library` | write | `DELETE /api/v1/models/{slug}/libraries/{name}` | Remove a library from a model |
| `list_installed_libraries` | read | `GET /api/v1/libraries/installed` | The checkouts on the volume and the models that pin each |
| `remove_library_checkout` | outward | `DELETE /api/v1/libraries/{name}` | Delete checkouts no model pins |
| `check_dependencies` | read | `POST /api/v1/models/{slug}/dependencies` | Include/use resolution and font literals, below (#253) |

A pin is a revision of the model's `model.json` (`libraries`), so every pin change is a
commit in the model's history (`backend/scadbuddy/library/libraries.py`, module
docstring). The upgrade flow's routes (`GET /api/v1/libraries/{name}/users`,
`POST /api/v1/models/{slug}/libraries/{name}/check`, #169) have no tool yet; their
`coverage.ts` entry says why.

## Include/use resolution

OpenSCAD only warns on a file it cannot find ("Can't open library", "Can't find include
file") and renders without it
([`src/core/lexer.l`](https://github.com/openscad/openscad/blob/master/src/core/lexer.l)).
`check_dependencies` answers the question before a render does. The backend
(`backend/scadbuddy/library/includes.py` `resolve_dependencies`) resolves each target
the way OpenSCAD does:

- Statements are found as OpenSCAD's lexer finds them: `include[ \t\r\n]*<`, then
  anything but a tab, line break or `>`. Comments and strings are skipped.
- Each target is resolved as `find_valid_path` resolves it
  ([`src/core/parsersettings.cc`](https://github.com/openscad/openscad/blob/master/src/core/parsersettings.cc)):
  beside the file that names it, then in each directory on `OPENSCADPATH` in order. A
  render's `OPENSCADPATH` is exactly the model's pinned checkouts
  (`backend/scadbuddy/render/runner.py` `run_openscad`, #93), so those are what is
  searched. OpenSCAD's built-in and user library directories are not.
- Statements in the model's own files are followed, relative to each file's directory.
  Files inside a library are not.
- An unresolved target carries a `reason`. When its first path component names a
  library the model does not pin (`use <BOSL2/std.scad>` names BOSL2), it also carries a
  `suggestion`: the curated library of that name (`source: "catalogue"`, pin it with
  `pin_library`), else a library another model pins from its own URL
  (`source: "installed"`, pin it with `pin_library_from_url`). `has_file` says whether a
  checkout on the volume actually has the target, when one is there to look in.
- Nothing is cloned. A pin whose checkout is missing from the volume is listed in
  `missing_checkouts`; the next render clones it again (`CheckoutFetcher`).
- `source` checks an unsaved edit against the saved model's directory and pins.

## Fonts

OpenSCAD's `FontCache::find_face_fontconfig` parses a font string with `FcNameParse`
and takes fontconfig's best match, so a family that is not installed renders in the
default font (DejaVu Sans in this image) with other geometry and no warning
([`src/FontCache.cc`](https://github.com/openscad/openscad/blob/master/src/FontCache.cc);
`CLAUDE.md`, "Verified OpenSCAD facts"). What the agent gets instead (#253):

- **A missing family is a hard error where a font value is set.** The render route and
  the preset routes (save, update, duplicate, and a template's own presets in
  `PATCH /api/v1/models/{slug}`) answer 422 when a `// font` value names a family
  fontconfig does not resolve, with `parameters` and `families` naming them
  (`backend/scadbuddy/api/params.py` `require_installed_fonts`). The template's own
  default and options, and `""` (the default font), are not judged. So `render_model`,
  `save_preset` and `update_preset` return that error. Families are read as
  `FcNameParse` reads them: up to the first unescaped `-` or `:`, split at commas, and
  compared ignoring case and blanks, as fontconfig compares them
  ([fontconfig user docs, "Font Names"](https://www.freedesktop.org/software/fontconfig/fontconfig-user.html);
  [`FcStrCmpIgnoreBlanksAndCase`](https://www.freedesktop.org/software/fontconfig/fontconfig-devel/fcstrcmpignoreblanksandcase.html)).
  A bare `-` in a family name is explained in the error: fontconfig reads it as the
  start of a point size.
- **An install is checked.** After downloading a family and rebuilding fontconfig's
  cache, `POST /api/v1/fonts/install` runs `fc-list :outline=true:scalable=true family`
  under the render's own environment (`backend/scadbuddy/core/fontconfig.py`
  `env_for`) and answers 500, naming the files, when the family does not resolve
  (`backend/scadbuddy/library/fonts.py` `FontService.install`). Outline, scalable faces
  are the ones OpenSCAD's `FontCache::init_pattern` asks for.
- **`fc-list` is what OpenSCAD sees in this image.** `openscad --info` in the backend
  image lists its font path as fontconfig's own directories plus `$HOME/.fonts`, with no
  bundled font directory (measured 2026-09-29 on a locally built `test` image; not
  re-measured on the current base-image pin). So a family `fc-list` resolves under the
  render's environment is one the render resolves.
- **Font literals in the source are reported, not refused.** `check_dependencies`
  lists every `font = "…"` string literal with the families that are missing. A font
  computed at run time is not seen.
- Without fontconfig on `PATH` (a development machine) nothing is refused and
  `fonts_checked` is false.

`list_fonts` (`GET /api/v1/fonts`) lists each family by its first name, unescaped
(`IBM 3270 Semi-Narrow`, not `IBM 3270 Semi\-Narrow`); `search_fonts` and
`install_font` cover the Google Fonts catalogue.
