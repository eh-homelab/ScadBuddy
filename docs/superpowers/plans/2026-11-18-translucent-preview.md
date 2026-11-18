# Translucent and clear filaments in the 3D preview (#6349)

## Goal

Spec [§5.1](../specs/2026-11-14-filament-management-design.md) says the 3D preview draws
a translucent or clear colour see-through. The alpha must show in the viewer, not only in
swatches. A clear part must stay visible.

Where it stood: #6243 and #6289 already draw a part see-through from the GLB's
`scadbuddy_alpha` extra, in the editor's preview (`Preview.tsx`) and the print dialog's
(`PlateScene.tsx`). Two things were missing:

1. No floor. A clear part (alpha `00`-`20`) drew at 0 to 13 % and vanished.
2. The print dialog ignored the alpha of the spool a slot prints in. Its `colors` map
   (`resolvedColors`) dropped the alpha with `normalizeHex`.

## Decisions

- One helper owns the rule: `lib/rgba.ts` `opacityOfAlpha` and `previewOpacity`. Opacity is
  1 at alpha `FF`, a missing alpha, a six-digit hex or junk. Otherwise it is
  `max(alpha / 255, 0.35)`. Why: one place for the floor, next to the other `#RRGGBBAA`
  helpers, and the editor and the print dialog cannot drift apart.
- The floor is `MIN_PREVIEW_OPACITY = 0.35`. Why: spec §5.1's clear band is `00`-`20`
  (0 to 13 %); at 35 % the part reads as glass and stays visible over the plate and the
  parts behind it. The translucent preset (`80`, 50 %) is above it and unchanged.
- `designOpacityOf` (the GLB's alpha) uses the floor, so every path through the editor and
  the dialog gets it.
- `transparent: true` only below 1 (`seeThrough`, `applyPartOpacity`). Why: an opaque part
  keeps the depth-write and sorting cost of the opaque pass.
- A see-through part sets `depthWrite: false`, draws front faces only, and has
  `renderOrder` 1, so the opaque parts draw first and show through it. These were already
  the rules of #6243 and #6289; `applyPartOpacity` now applies them in both directions
  (back to solid at 1) because the print dialog changes a part's spool at run time.
- The print dialog takes opacity from the spool colour a slot prints in when that colour
  spells an alpha (`#RRGGBBAA`, below `FF`), else from the part's GLB alpha
  (`slotOpacity`). Why: a six-digit spool colour must not turn a translucent design part
  opaque. `resolvedColors` keeps the alpha of a translucent spool colour.
- The alpha reaches the print dialog from `SpoolOption.rgba` (`#RRGGBBAA`, the held SB
  spool's effective value or the tray's reported colour), which the backend already sent.
  `resolvedColors` reads `rgba ?? colour`. Why: `colour` stays `#RRGGBB` because the
  render splits by it and `colours_match` and the slot match read it; one field carries
  the alpha, so nothing else changes and no backend code moves.
- The preview's bounding-box outline (`Preview.tsx`) is a separate `lineSegments` and is
  unchanged: it stays visible around a clear part.
- Test hook: `Preview.tsx` sets `data-see-through` on its canvas, a comma list of each
  see-through material's opacity (empty when none). The e2e reads it, because WebGL
  pixels are not asserted anywhere in this repo (no pixel-diff baselines).
- The mock GLB writer (`mocks/glb.ts`) writes a `#RRGGBBAA` colour as a mesh of its own
  with `scadbuddy_alpha`, as `render/glb.py` does.

## Review Focus

- `previewOpacity` and the floor: the one rule, and its edge cases (`FF`, `FE`, `20`, `00`,
  six digits, junk).
- `PlateScene.tsx`: the recolour effect now sets opacity and `side` on every colour
  change. Check that an opaque part returns to `DoubleSide`, depth write on, order 0.
- `slotOpacity`: a six-digit spool colour keeps the part's own alpha.
- The `data-see-through` hook is the only production code added for the test.
- Screenshots: `2026-11-18-translucent-preview/translucent.png` (alpha `80`) and
  `clear.png` (alpha `00`, drawn at the floor), from `e2e/translucent-preview.spec.ts`.
