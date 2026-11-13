# Storefront: sell what you are looking at (2026-11-13)

The owner: "Improve the UX and make it easy. It's complicated." Today a product reaches the
public storefront only through the back office's Storefront page
(`frontend/src/pages/StorefrontPage.tsx`, `components/storefront/ListingEditor.tsx`): start a
blank listing, pick a template and a commit, decide "Locked at …" or "Customer may change" for
every parameter, tick filaments, tick media, save, then publish. Nothing starts from the work
itself, and the assistant has no tool for it.

This design makes the common case one click plus Publish. It builds on the storefront spec
`2026-11-02-storefront-design.md` §5.3 and changes no backend rule.

## 1. Facts it rests on

Read from the code on 2026-11-13 (`origin/main` f7a7f39d2):

- Publish checks every colour a listing offers, locks or leaves at its default against the
  colours of the listing's price-book filaments, case-insensitively, else 422
  `listing_colour_unknown` (`backend/scadbuddy/orders/storefront/listings.py` `checked_fixed`,
  `_check_colour`).
- A visitor who does not set an offered parameter gets the schema's default, checked against
  the listing's bound (`orders/storefront/inputs.py` `listing_values`). A listing has no
  per-listing default for an offered parameter. So an offered parameter starts at the
  template's default for the customer, never at the shop's current value, and an offered
  colour's default must be one of the listing's filaments.
- Saving a published listing unpublishes it (`ListingStore.update`; `api/storefront.py`
  `update_listing`).
- A render job records `slug`, `params`, `model_version` (the commit), `started_at` and
  `finished_at` (`render/job_models.py` `Job`); an output records `slug`, `name`,
  `model_version` and `job_id` (`library/outputs.py` `OutputMeta`, `api/outputs.py`
  `OutputDetail`). `model_version` can be a content hash where there is no repository.
- From the storefront go-live (coordinator, 2026-11-13): a hand-uploaded `.glb` on a revision is
  stored as `application/octet-stream`, so customers never see it; and a render longer than
  about 100 s gives a Cloudflare 504 for the storefront's estimate and preview (#6120).

## 2. "Sell on storefront"

One action, three places:

- The Customize page (`/m/:slug`, which is also the model's page): Generate's menu gets
  "Sell on storefront…", enabled once the values on screen have a render (as "Rendered
  image…" is). It opens `/storefront?sell=job:<job id>`.
- A saved output: the History page's row menu gets "Sell on storefront", opening
  `/storefront?sell=output:<output id>`.
- The Storefront page itself: "New listing" still starts on the first template, now with the
  same smart defaults (§3) at the template's defaults.

The Storefront page reads `sell`, builds the listing (§3) and opens it in the editor unsaved,
with the live preview beside it. Nothing is written until the person presses Publish (or Save
draft). The parameter is removed from the address once read, so a reload does not make a
second listing.

## 3. The filled-in listing

Built by one pure function, `frontend/src/components/storefront/sellDraft.ts` `sellDraft`, from
the template's schema at the commit, the values, the price book's filaments and the model's
media:

- Template and commit: the job's or output's `model_version` when the template's history has
  it, else the current version, with a note that says so.
- Title: the output's name, else the model's name.
- Pictures: every model media item that is an image (`kind` `image`, an `image/*` content
  type, not missing). Videos and anything else are left unticked.
- Parameters (the smart defaults; also used for a blank listing):
  - Free text (`string` with no options): customer may change, at most the schema's
    `max_length` (capped at 200). The shop's typed text is a sample and is not kept.
  - Colour (`// color`): customer may change when the current value is the template's
    default; locked at the current value when the person changed it, so the product sells
    as seen (§1: an offered colour would start at the default). One switch offers it.
  - Everything else (numbers, selects, switches: the geometry): locked at the current value.
  - Fonts and files are never offered (as today).
- Colours: each colour value (offered or locked, current or default) is matched to a
  price-book filament of the same colour. Every matched filament is a listing filament. A
  colour with no match is listed above the editor: "No filament is #1B6CA8 (Body colour)",
  with a picker of the filaments, nearest colour first, which locks the parameter at that
  filament's colour, and a link to Settings → Orders to add one. Publish stays possible, and
  the publish refusal names the parameter if the person goes on anyway.
- Render time: when the job (or the output's job) took more than 100 s, a warning: "This
  design took N s to render. The storefront's preview and price give up after about
  100 s (#6120)."

## 4. A simpler editor

- Plain words and three groups in the order a shop thinks:
  1. "Product": title, description, pictures.
  2. "What the customer chooses": the offered parameters, each with its bounds ("From", "To",
     "Step"; "Most characters"), and the colours on offer.
  3. "Set by you": the locked parameters, one line each with the value and a "Let the
     customer change it" switch.
  The template and version move to a "Design" line at the top ("Name Keychain, version
  a1b2c3d (current)"), with "Change" opening the two pickers.
- Each parameter row is a switch ("Customer can change") instead of two radio buttons.
- Buttons: "Publish" (saves and publishes) and "Save draft". While a published listing has
  unsaved changes the page says "Saving takes it off the store until you publish again."
- Publish is disabled only for a missing title or version, and the page says which.

## 5. Assistant tools

In `agent/src/tools/storefront.ts`, over the existing back-office routes:

- `storefront_listing_draft` (tier `write`): `{output_id}` or `{slug, values?}`, optional
  `title`. It reads what the page reads, builds the listing with the same rules as §3 (the
  agent's own copy of the function, with a vector test shared with the frontend), and creates
  it unpublished. It answers the listing id, the notes of §3 (unmatched colours, render
  time) and the page link `/storefront?listing=<id>`.
- `storefront_listing_publish` (tier `outward`): `{listing_id}`. It publishes, so it waits for
  the owner's approval in the UI like every outward tool.

So "put the navy owl on the store" is: find the output, draft, then publish after approval.

## 6. Out of scope

- Pipeline templates (publish refuses them, `listing_pipeline_unsupported`): #5701.
- A per-listing default for an offered parameter (needs a backend and storefront change).
- Adding the output's render as a model picture from the editor (Generate → Rendered image…
  already adds one to the model's media).
- Prices: the storefront prices each order from its quote, not the listing.
- Any change to the public storefront (`frontend/src/storefront/**`) or the backend.
