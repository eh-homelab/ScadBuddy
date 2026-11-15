# Storefront customize page: UX rework

Date: 2026-11-13. Status: accepted (the owner's request of 2026-11-13). Epic: storefront #4962 (follow-up).

## 1. Why

The owner walked the live shop (`/l/6089e09b…`, Star Name Keychain) and called the
customize page "horrible". A Playwright walk on 2026-11-13 confirmed each point:

| Seen | Cause |
| --- | --- |
| Each colour parameter shows 29 pills, four times over; names repeat ("Blue PLA" twice) | `ParamField.tsx` renders one pill per filament; `filamentName` is `colour_name material`, and two filaments can share it |
| Only PLA can be picked | the listing's `colours` hold only PLA filaments; a choice is a hex, so one hex cannot name two materials |
| Nothing shows until the customer presses Render | `CustomizePage.tsx` renders only on the button |
| Every price reads "about $1.00", grams "0.0 g" | `rough_estimate` (`orders/estimate.py`) prices by volume; `rounding: to_major` rounds up to whole dollars; a filament with no density gives 0 g (#6136, fixed by #6148) |
| Leaving the page loses the name, the colours and the preview | the state lives only in React |
| An unpicked colour whose template default is no shop filament is refused | `listing_values` falls back to `parameter.initial` (#6144) |
| On a phone, "Account" and "Your order" run together | header spacing |

## 2. Goals

1. **Materials.** The customer may choose any material the listing allows, per colour
   part. The shop may restrict a listing to **one material**: every part then uses the
   same material, and the customer picks the material once.
2. **A better colour picker.** No wall of pills. Swatches grouped by material, each
   filament told apart (name, material, and a short hex when two names match).
3. **Automatic render.** The page renders on load with the shop's defaults, and again
   after each change, debounced. No Render button.
4. **A real price.** The price comes from a slice of the render, with the listing's
   printer and process, not from the mesh volume. "If we can render, we can slice."
5. **Defaults.** Each offered parameter has a listing default (#6144). The page
   preselects it, and the first render and price use it.
6. **Nothing lost.** Leaving the page and coming back restores every choice, the
   quantity and the preview.

## 3. Non-goals

- No change to the back office's StorefrontPage or ListingEditor in this work; scadsy
  owns them (#6140). The new listing fields get their editor control there.
- No change to the customer model upload flow (#6147).
- No change to checkout or payment.

## 4. Anti-goals

- A price shown as exact. It stays "about", confirmed by the shop.
- A slice that runs in a pod with a database secret, or a new database client (#5295).
- A render storm: auto-render must not trip `render_limited` in normal use.
- Any support detail shown to the customer (owner rule, 2026-11-13: "Be sure not to
  expose support material selection. That's an internal detail."). No support material,
  support filament, support or interface setting appears in the material choice, the
  colour pickers or the estimate. The shop picks support internally.

## 5. Design

### 5.1 Listing fields (backend, `orders/storefront/wire.py`, `listings.py`, `inputs.py`)

- `NumberBound`, `ChoiceBound`, `TextBound` gain `default: ParamValue | None` (#6144).
  `listing_values` uses it before `parameter.initial`. Publish refuses an offered
  parameter whose effective default is outside its bound (422 `listing_default_invalid`),
  and an offered colour whose effective default is no listing filament's colour
  (422 `listing_colour_unknown`, as today for choices).
- `ListingInput` gains `one_material: bool = False`.
- The public bound carries `default`; `PublicListing` carries `one_material`.
- `RenderBody` and `AddListingItem` keep `colours: {slot: filament_id}`. They gain
  `picks: {param_name: filament_id}`: the filament the customer picked for each colour
  parameter. A pick must be a listing filament whose colour equals the parameter's
  value (422 `colour`). With `one_material`, every pick (and every default's filament)
  must share one material (422 `listing_mixed_material`). The CRUD side maps picks to
  slots after the render, by colour, so the client no longer guesses
  (`slotFilaments` goes).

- Support stays internal. A support filament (material matching
  `/support|pva|bvoh|hips/i`, or a price-book filament marked support) is never in a
  listing's public `colours`. Publish refuses to offer a parameter whose name or label
  matches `/support|interface/i` (422 `listing_support_offered`); it may only be fixed.

  Why picks and not slots: the slot order is known only after the render, and a hex
  alone cannot tell PLA from PETG.

### 5.2 Colour picker (frontend, `storefront/components/ColourPicker.tsx`)

- Closed, a colour parameter is one row: label, the picked swatch, its name and
  material. Tap opens a popover (sheet on a phone).
- Open: a material filter (chips, hidden with one material or with `one_material`),
  then a grid of round swatches, 40 px, each with its name under it. Search by name
  when there are more than 24.
- Names: `colour_name` (else the hex). Material shows as a small tag. Two filaments with
  the same name and material show the short hex after the name.
- With `one_material`, a single "Material" control sits above the parameters; each
  picker then lists only that material. Changing material re-picks each colour to the
  nearest hex in the new material (RGB distance), and says so.
- Keyboard: arrow keys move through the grid, Enter picks, Escape closes.

### 5.3 Automatic render (frontend, `CustomizePage.tsx`)

- On load, once the listing and any saved state are read, render the current values.
- After a change, wait 900 ms of quiet; render when every value is valid.
- A newer render supersedes an older one: the page follows only the latest. The old one
  runs on (the edge has no cancel).
- A refusal (`render_limited`, `render_queue_full`) shows its message and retries once
  after `Retry-After`.
- The viewer keeps the previous render, with its "rendering" overlay, while a new one
  runs; the old price is hidden until the new one is read.
- A price read that fails for a passing reason (a Cloudflare 504 while the shop works it
  out, #6120) is read again with backoff, never shown as an error at once.

### 5.4 Price from a slice

Backend detail is in §5.6 (filled from the slice path map). The page shows
"about $X for one" plus grams and print time from the slice. While the slice runs it
shows "Working out the price…". If the slice fails, it falls back to the volume
estimate and says "rough".

### 5.5 Keeping choices (frontend, `storefront/customizeState.ts`)

- `localStorage` key `scadbuddy.storefront.customize.<listingId>`, JSON
  `{v: 1, values, quantity}` (picks and material join it with #6152). The page sends the
  kept values again on return; the shop reuses the render it made of them (`reuse_finished`).
- Written on each change. Read on mount; `keepOffered` drops what the listing no longer
  allows.
- Cleared for that listing after "Add to order" succeeds.
- Every access is in try/catch; the page works without storage.

### 5.6 Slice path

Measured on origin/main c61361b84 (read of the code, 2026-11-13):

- Today a slice needs an output row or a library file (`orders/operations.py`
  `PreviewSlicer._start`, `bambuddy/subject.py` `PrintSubject`), a printer, nozzles, a
  process and a spool per slot, all typed by a person in the print dialog
  (`quote.py` `SliceQuoteRequest`). A visitor render is only a render job, pruned after
  24 h (`render/jobs.py`). Price-book filaments have no slicer preset.
- The Rust slicer worker holds no database secret (`docs/security/trust-boundaries.md`).
  The native `SliceWorkflow`'s prepare and record steps run on the `library` queue in the
  API process. The default backend is Bambuddy's sidecar.

Design:

1. **Shop slice profile.** One storefront setting: printer, nozzle, process and bed, plus
   a slicer filament preset per material. Publish refuses a listing whose materials have
   no preset (422 `listing_material_unsliceable`). Support settings come from the profile
   and are never shown (§4).
2. **New subject kind** `storefront_job`: the native slice path reads the render job's
   `model_3mf` blob directly. The CRUD process starts `SliceWorkflow`; the Rust worker
   slices from transfer URLs only. Never the Bambuddy path: public render output never
   enters the shop's Bambuddy library.
3. **Shared and cached.** One slice per (render job, material per slot, profile). Visitors
   who share a render share the slice. A table `storefront_job_slices` keeps grams per
   slot and seconds; the sliced 3MF is not kept.
4. **Capacity.** At most one storefront slice runs at once (setting, default 1), in a
   lower-priority task queue than back-office quotes. A slice starts only for the render
   on screen after the debounce, never for a superseded one.
5. **Price.** `price(...)` with machine time from the profile's printer rate, the
   filament lines from the slice, no `estimated` flag. `StoreEstimate` gains
   `source: "slice" | "volume"` and `print_seconds`. Until the slice is done, or if it
   fails, the volume estimate is shown, marked rough.
6. **Retention.** Slices are swept with their render rows.

This is the largest part; it ships last (PR D), after #6148.

### 5.7 Small fixes

- Header: a gap between "Account" and "Your order" at phone width.
- Labels and placeholders: #6142.

## 6. Delivery

| PR | Scope | Files |
| --- | --- | --- |
| A | Keep choices; automatic render with debounce; preview overlay; header gap | `CustomizePage.tsx`, new `customizeState.ts`, `storefront.css`, tests |
| B | Per-parameter `default` (#6144) and its preselect | `wire.py`, `listings.py`, `inputs.py`, `ParamField.tsx`, tests |
| C | `one_material`, `picks`, colour picker | `wire.py`, `inputs.py`, `listings.py`, `ColourPicker.tsx`, `ParamField.tsx`, tests |
| D | Price from a slice | per §5.6, after #6148 |

PR B and C rebase on #6147 (it edits `wire.py`). PR D rebases on #6148.
