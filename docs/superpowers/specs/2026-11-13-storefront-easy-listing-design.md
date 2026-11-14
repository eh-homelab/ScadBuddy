# Storefront: sell what you are looking at (2026-11-13)

The owner: "Improve the UX and make it easy. It's complicated." Today a product reaches the
public storefront only through the back office's Storefront page
(`frontend/src/pages/StorefrontPage.tsx`, `components/storefront/ListingEditor.tsx`): start a
blank listing, pick a template and a commit, decide "Locked at …" or "Customer may change" for
every parameter, tick filaments, tick media, save, then publish. Nothing starts from the work
itself, and the assistant has no tool for it.

This design makes the common case one click plus Publish. It builds on the storefront spec
`2026-11-02-storefront-design.md` §5.3 and changes no backend rule.

## 1. What is hard today (a walk-through)

Walked on 2026-11-13 with Playwright (Chromium, 1440 × 1000) against `vite` with the msw
mocks (`VITE_MOCK_API=1`), as the owner would list the keychain: Storefront → New listing →
Publish. Nothing was saved on the production back office. What got in the way:

1. No way in from the work. The Customize page and a saved output have no storefront action;
   the owner starts blank and chooses the template and its version again.
2. A new listing offers every ranged number, switch and select: the keychain's customer form
   has seven fields (text size, letter depth, thickness, keyring hole, hole position …). To sell
   the product as designed, the owner switches each one off.
3. Colours fail late. A new listing locks each colour at the template's default and ticks no
   filament. The mocks publish it, but the real publish answers 422 `listing_colour_unknown`
   ("'body_color' is '#1B6CA8', which is not the colour of a listing filament") after every
   other choice is made. Nothing says so before.
4. "Customer may change" is greyed out on colours and the typeface with no reason given.
5. Technical words: "Template", a version shown as `c3d4e5f · Edit name-keychain sc…`, each
   variable name (`text_size`) beside its caption, "Locked at Liberation Sans:style=Bold",
   "Least"/"Most", "Longest text", "Filaments", "Media".
6. Every parameter is a box of two radio buttons and sub-fields: eleven boxes in one list,
   offered and locked mixed.
7. The template's picture is not ticked, so the customer sees no picture.
8. Saving a published listing takes it off the store, and the page does not say so.
9. Publish greys out without a title and does not say why.
10. Publish and Save are below the whole list of parameters.

Found again after the first build (§5): with the geometry locked, the "Set by you" list is
nine boxes long, and the actions were still at its bottom (10). Both are fixed in §5.

## 2. Facts it rests on

Read from the code on 2026-11-13 (`origin/main` f7a7f39d2):

- Publish checks every colour a listing offers, locks or leaves at its default against the
  colours of the listing's price-book filaments, case-insensitively, else 422
  `listing_colour_unknown` (`backend/scadbuddy/orders/storefront/listings.py` `checked_fixed`,
  `_check_colour`).
- A visitor who does not set an offered parameter gets the listing's `default`, else the
  template's (`effective_default`, #6144), checked against the listing's bound
  (`orders/storefront/inputs.py` `listing_values`). An offered colour's default must be one
  of the listing's filaments.
- Saving a published listing unpublishes it (`ListingStore.update`; `api/storefront.py`
  `update_listing`).
- A render job records `slug`, `params`, `model_version` (the commit), `started_at` and
  `finished_at` (`render/job_models.py` `Job`); an output records `slug`, `name`,
  `model_version` and `job_id` (`library/outputs.py` `OutputMeta`, `api/outputs.py`
  `OutputDetail`). `model_version` can be a content hash where there is no repository.
- From the storefront go-live (coordinator, 2026-11-13): a hand-uploaded `.glb` on a revision is
  stored as `application/octet-stream`, so customers never see it; and a render longer than
  about 100 s gives a Cloudflare 504 for the storefront's estimate and preview (#6120).


## 3. "Sell on storefront"

One action, three places:

- The Customize page (`/m/:slug`, which is also the model's page): Generate's menu gets
  "Sell on storefront…", offered once the values on screen have a render (as "Rendered
  image…" is). It opens `/storefront?sell=job:<job id>`.
- A saved output: the History page's row menu gets "Sell on storefront", opening
  `/storefront?sell=output:<output id>`.
- The Storefront page itself: "New listing" still starts on the first template, now with the
  same smart defaults (§4) at the template's defaults.

The Storefront page reads `sell`, builds the listing (§4) and opens it in the editor unsaved,
with the live preview beside it. Nothing is written until the person presses Publish (or Save
draft). The parameter is removed from the address once read, so a reload does not make a
second listing. `?listing=<id>` opens a saved listing; since #6157 it redirects to the editor at `/storefront/listings/<id>`, the assistant's link (§6).

## 4. The filled-in listing

`frontend/src/components/storefront/sellSource.ts` `sellListing` reads the job or the output
(and the output's job, for its render time), the template's versions and the schema at the
commit. `listingDraft.ts` `defaultsFor` turns the schema, the values and the price book's
filaments into the parameters and filaments, and `sellMedia` picks the pictures:

- Template and commit: the job's or output's `model_version` when the template's history has
  it, else the current version, with a note that says so.
- Title: the output's name, else the model's name. A colour the customer can choose never goes
  in a listing's title (the owner refused "Owl Pencil Cup – Navy"): while the listing offers a
  colour, the names of its filaments' colours are taken out of the title, with the separators
  they leave (`titleColours`, `withoutWords`). A title typed with one gets a warning under it
  and "Take it out". Every listing filament's colour is a choice of each offered colour,
  except the colour of a support-only filament (below), which no customer can choose. So the names of
  the colours on offer go while any colour is offered; a colour may stay in the title of a
  listing that offers none, and a support-only filament's colour name always may.
- Pictures: every model media item that is an image (`kind` `image`, an `image/*` content
  type, not missing). Videos and anything else are left unticked.
- Parameters (the smart defaults; also used for a blank listing):
  - Free text (`string` with no options): customer may change, at most the schema's
    `max_length` (capped at 200). The shop's typed text is a sample and is not kept.
  - Colour (`// color`): customer may change, always (the owner, 2026-11-13: "why can't I
    specify colors"). Locking one is the person's explicit choice in the editor. It starts
    locked only when the price book has no filament to offer.
  - Everything else (numbers, selects, switches: the geometry): locked at the current value.
  - Fonts and files are never offered (as today).
  - Support settings are never offered, even when the template exposes them (the owner,
    2026-11-13: support material selection is internal). A parameter whose name, caption
    or group says "support" or "interface" is one (`isSupport`). It stays locked, the editor's
    switch says why, a save locks one an older listing offers, and a support colour neither
    picks the design's material nor becomes a colour on offer. Its filament still joins the
    listing when the price book has that colour exactly, with a density, because publish
    checks every locked colour against the listing's filaments (`listing_colour_unknown`).
    A listing filament is support-only when only a support setting uses its colour and its
    material is not the design's (that of a listing filament a customer colour's value names,
    or, when none is named, that of the listing filaments no support setting's colour names).
    Its colour is left out of every offered colour's choices, and the editor marks it "for
    supports only, not offered" (`isSupportOnly`). A filament of the design's material stays
    a choice whatever a support setting uses. A support filament with no density is not
    added (the estimate could not price it): it stays a colour with no filament
    (`colourProblems`).
- Colours on offer (`offeredFilaments`): every price-book filament with a density (one priced
  by weight) of the design's material, which is the material of the first filament whose
  colour one of the colour values is, compared case aside as publish compares them; every
  priced filament of any material when no value matches one. The filaments the values name
  come first.
- A customer who picks no colour gets the listing's default, else the template's (§2), and publish and the
  customer's render refuse a colour that no listing filament has. Such a colour, locked or
  an offered one's default (`colourProblems`), is named at the top of the editor ("Plate is
  #1B6CA8"), with a picker of the price book's filaments, nearest colour first, and a link to
  add one to the price book (Settings → Printing → Orders). Choosing a filament adds it and
  locks the colour at it (`fixColour`): the backend field for an offered colour's
  default exists (#6144), and only the editor control to set it is still to come; until it
  comes, locking is the fix. The note stays, live, until
  every colour has a filament. Publish stays possible, and the server's refusal names the
  parameter if the person goes on anyway.
- Render time: when the job (or the output's job) took more than 100 s, a note: "This design
  took N s to render. The store's preview and price give up after about 100 s" (#6120).

`test-fixtures/storefront/sell-draft-vectors.json` pins `defaultsFor`'s rules, and `listingInput`'s choices where a case has `input`; the frontend
test and the assistant's tool test (§6) both read it.

## 5. A simpler editor

- Plain words and groups in the order a shop thinks:
  1. A "Design" line at the top ("Name Keychain version c3d4e5f (current)"), with "Change"
     opening the design and version pickers.
  2. "Product": title, description, pictures.
  3. "Settings": every parameter, once, in the design's order (#6290). An offered one shows
     its bounds ("From", "To", "Step"; "Most characters"); a locked one shows its value. A
     switch never moves a row: the first build split them into "What the customer chooses"
     and a folded "Set by you", and turning a switch off moved the row out of sight.
  4. "Colours on offer": folded to the count and names of the ticked filaments, with
     "Show all N filaments" to open the list (open while none is ticked).
- No variable names beside the captions (the name stays in the row's tooltip).
- Each parameter row has one switch, "Customer can change", instead of two radio buttons. A
  switch that cannot be turned on says why ("A customer cannot choose a typeface.", "Tick a
  colour on offer below first.").
- "Publish" (saves and publishes) and "Save draft" sit at the top of the listing, above the
  editor. While a published listing has unsaved changes the page says "Saving a draft takes
  this listing off the store until you publish it again."
- Publish waits only for a version and a title, and the page says which is missing.

## 6. Assistant tools

In `agent/src/tools/storefront.ts`, over the existing back-office routes:

- `storefront_listing_draft` (tier `write`): `{output_id}` or `{slug, values?}`, optional
  `title`. It reads what the page reads, builds the listing with the same rules as §4 (the
  agent's own copy of `defaultsFor`, checked against the shared vectors), and creates it
  unpublished. It answers the listing id, the colours with no filament, the render-time note
  and the page link `/storefront/listings/<id>` (the editor, #6157).
- `storefront_listing_publish` (tier `outward`): `{listing_id}`. It publishes, so it waits for
  the owner's approval in the UI like every outward tool.

So "put the navy owl on the store" is: find the output, draft, then publish after approval.

## 7. Out of scope

- Pipeline templates (publish refuses them, `listing_pipeline_unsupported`): #5701.
- The editor control to set an offered parameter's default (the backend field is #6144).
- Adding the output's render as a model picture from the editor (Generate → Rendered image…
  already adds one to the model's media).
- Prices: the storefront prices each order from its quote, not the listing.
- Any change to the public storefront (`frontend/src/storefront/**`) or the backend. The
  walk found one there: the preview shows a switch's choices as "true" and "false"
  (`storefront/components/ListingView.tsx`); it is left to the storefront's own work.
