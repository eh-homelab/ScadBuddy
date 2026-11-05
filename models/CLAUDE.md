# Bundled models — notes for agents

These notes cover `models/`; paths are repo-relative; the root `CLAUDE.md` has the repo-wide rules.

## Commands
Template checks (the `models` job): each `models/<slug>/verify.sh` the PR touches, or all
of them when the Dockerfile, `ci.yml` or the selector/runner scripts change, and always on
push to main and the weekly schedule. Run the same locally, in the Dockerfile's `base`
stage (OpenSCAD plus the image's fonts):

```bash
docker build --target base -t scadbuddy-verify:ci .
docker build --target test -t scadbuddy:test .   # for templates with a pipeline/ (#427)
SCADBUDDY_OPENSCAD_IMAGE=scadbuddy-verify:ci SCADBUDDY_FONTS_IMAGE=scadbuddy-verify:ci \
  SCADBUDDY_PIPELINE_IMAGE=scadbuddy:test \
  bash -c '.github/scripts/select-models.sh all | .github/scripts/verify-models.sh'
```

Without `SCADBUDDY_PIPELINE_IMAGE` a template's pipeline check prints "skipped".


## Layout
- `models/` — bundled example models (`models/<name>/verify.sh`).

## See also

- Image and OpenSCAD facts: see the root `CLAUDE.md`. The `verify.sh` label rule is in the root lint paragraph.
