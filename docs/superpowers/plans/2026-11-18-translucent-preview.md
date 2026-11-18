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
  spells an alpha (`#RRGGBBAA`; any spelled alpha wins, `FF` included, so an opaque spool prints a translucent design part solid), else from the part's GLB alpha
  (`slotOpacity`). Why: a six-digit spool colour must not turn a translucent design part
  opaque. `resolvedColors` keeps any spelled alpha of a spool colour.
- The alpha reaches the print dialog from `SpoolOption.rgba` (`#RRGGBBAA`, the held SB
  spool's effective value or the tray's reported colour), which the backend already sent.
  `resolvedColors` reads `rgba ?? colour`. Why: `colour` stays `#RRGGBB` because the
  render splits by it and `colours_match` and the slot match read it; one field carries
  the alpha, so nothing else changes.
- The Bambuddy path's untagged-tray choices carry `rgba` too: `UnknownTray.rgba` is
  `tray_rgba(tray_color)`, and `tray_option` and `tray_questions` pass it on. Why: without
  it `resolvedColors` fell back to the six-digit `colour` and a translucent tray drew solid
  in the print dialog. `colour` stays six digits.
- A colour tried in the preview pane (`PreviewPane` `tried`, six digits) replaces a
  translucent spool's `#RRGGBBAA`, so the part takes the design's alpha, opaque for most.
  Why: a tried colour is a deliberate pick of one colour, preview only, and carries no
  alpha of its own; the spool's alpha belongs to the spool.
- `applyPartOpacity` sets `needsUpdate` only when `transparent` or `side` changed, since
  an opacity change alone needs no shader recompile. The recolour effect is the function
  `recolourParts` (`lib/partOpacity.ts`), so a unit test drives it.
- Light viewer: `clear-light.png` shows the clear part on the white ground. It stays
  visible: the floor (35 %) and the bounding-box outline both read on white.
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
- `recolourParts` (called by `PlateScene.tsx`'s recolour effect) now sets opacity and `side` on every colour
  change. Check that an opaque part returns to `DoubleSide`, depth write on, order 0.
- `slotOpacity`: a six-digit spool colour keeps the part's own alpha.
- The `data-see-through` hook is the only production code added for the test, and it renders only in the mocked build (`VITE_MOCK_API=1`).
- Screenshots: `2026-11-18-translucent-preview/translucent.png` (alpha `80`) and
  `clear.png` and `clear-light.png` (alpha `00`, drawn at the floor, dark and light viewer), from `e2e/translucent-preview.spec.ts`. The spec writes them only when `SCADBUDDY_E2E_SHOTS=1` is set.
