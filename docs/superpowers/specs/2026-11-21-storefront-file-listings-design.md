# Storefront: sell a library file as a listing (#6710)

Date: 2026-11-21. Status: draft. Nothing is built before the owner approves it. The owner
answered the questions of §14 on 2026-11-21, and this text follows those answers. The
pricing owner (scadify) set four constraints on
[a comment on #6710](https://github.com/eh-homelab/ScadBuddy/issues/6710#issuecomment-7033218958); §7 applies each of them, and §15 lists them with where each one is met.

Builds on:

- the storefront spec, [2026-11-02](2026-11-02-storefront-design.md) §5.3 (listings) and
  §5.4 (customize, estimate and the draft);
- the slice price, [plan 2026-11-14](../plans/2026-11-14-storefront-slice-price.md)
  (one pricing path, the price key, the shop slice profile);
- pipeline listings, [2026-11-17](2026-11-17-storefront-pipeline-listings-design.md),
  whose shape this spec follows, and which changes `ListingInput` at the same time (§13);
- "Sell on storefront", [2026-11-13 easy listing](2026-11-13-storefront-easy-listing-design.md).

## 1. Why

The owner asked to pick a **file** as a listing's source in the editor at
`/storefront/listings/new`, not only a template. Today a listing names one commit of a
template (`slug` and `commit`), and the customer changes its parameters. The shop has
3MF and STL files in its library that have no parameters: a model from MakerWorld, a
print it made before, a file a customer sent once. There is no way to sell one of them.

A file listing has no parameters. The customer picks a colour for each part of the file
(each filament slot it prints in) from the colours on offer, and a quantity. The price
comes from a slice of the file, through the one path the back office quotes with. The
order prints that file, with the customer's colours on its slots. Nothing renders. Publish
keeps a copy of the file, and that copy is the version the listing sells: a later change
of the library file does not change the listing (§5.3).

## 2. Facts it rests on

Each fact says where it was measured. "Read" means a read of the code on `origin/main`
7a3d93566, on 2026-11-21. Line numbers are on that commit.

1. **A listing is a template commit.** `storefront_listings.slug` is `NOT NULL`, and
   `commit` is `NOT NULL CHECK (commit ~ '^[0-9a-f]{40}$')`
   (`backend/scadbuddy/migrations/20261108T0530Z_storefront_listings.sql:8-26`).
   `ListingInput` requires `slug` and `commit` (`orders/storefront/wire.py:179-208`), and
   `ListingSnapshot` requires a `schema`, a `template_name`, `libraries` and a
   `snapshot_key` (`wire.py:147-164`). Read.
2. **Publish holds what it copies.** `_publish` puts the template snapshot and each
   media file in the content store and holds each key in `blob_refs` under
   (`storefront_listing`, listing id) (`orders/storefront/listings.py:108-109`,
   `:949-985`, `:1037-1065`). An unpublish, an edit and a delete drop the holds in the
   row's own transaction (module notes, `listings.py:1-17`). Read.
3. **The back office already quotes and prints a library file.** An order item of kind
   `library_file` names a file by id, its name and its library (`orders/models.py:482-491`,
   migration `20261107T0732Z_order_items_library_file.sql`). Its quote is
   `quote.py` `_library_quote` (`quote.py:1058-1103`): it slices with
   `Slicer.start_library(file.id, request, subject)` (`quote.py:1094`) and prices through
   the same `_priced` and `slice_and_price` (`quote.py:872-916`, `:1106-1194`). The item
   gets the slots its plates use, coloured as the file names them, only when it has none
   yet (`library_colours`, `quote.py:659-673`; the guard `if current is None or
   current.colours`, `quote.py:1080`). Its print checks that the file is still in the
   library that serves (`orders/printing.py:575-600` `_library_stands`). Read.
4. **A library file's slice takes no storefront preset today.** `Slicer.start_library`
   has no `slot_presets` (`quote.py:586-591`). `PreviewSlicer._start` passes
   `slot_presets` to `start_quote_preview` for an output (`orders/operations.py:299-315`)
   but not to `start_library_quote_preview` for a file (`operations.py:264-279`). That
   function forwards its other keywords to `start_preview_plates`
   (`orders/bambuddy.py:292-321`), which takes `slot_presets` (`bambuddy/preview.py:331`).
   Read.
5. **A slot with no spool is sliced in the file's own colour.** `resolver.resolve` sets
   `colours[idx] = (option.colour if option else slot.colour) or "#FFFFFF"`
   (`bambuddy/resolver.py:354`): a storefront slot has no spool, so it takes the colour
   the file names. A template variant is rendered in the visitor's colours first, so its
   3MF carries them. A library file is not rendered, so its 3MF carries the colours its
   author chose. Read.
6. **A library file's own settings.** `LibrarySource.print_settings` is `{}`
   (`bambuddy/print_source.py:1146-1149`): a slice of a library file applies no
   template settings, only the request's choices over what the file and the process
   preset give. Its remembered choices are kept under `library:<file id>`
   (`bambuddy/options.py:35-49` `options_scope`, `library_options_scope`), in the same
   `model_print_choices` map as a model's (`library/settings_store.py:526`). Read.
7. **What the print dialog opens on.** `opening_choices(remembered, status, bed_type)`
   gives the nozzles, tier, process and plate (`bambuddy/choices.py:189-210`). The
   dialog then seeds the print sequence, the process overrides and the settings per part
   from the same remembered entry (`frontend/src/lib/usePrintChoices.ts:162-190`
   `seedDialog`: `print_sequence`, `process_overrides`, `part_settings`). The storefront's
   `slice_request(profile, choices)` sends none of these three
   (`orders/storefront/slice_profile.py:101-114`), and `plan_variant` reads the remembered
   entry by `reference.slug` only (`orders/storefront/slice_price.py:627-629`). Read.
8. **The price key.** `price_key` hashes `KEY_VERSION` (3), the model's sha256, each
   slot's filament with its colour and preset, the profile, the choices, the currency,
   the policy and the machine rate (`slice_price.py:133`, `:391-436`). Its `model` is
   "the render's 3MF content" (docstring, `:402-407`). Read.
9. **A variant is a render.** `SliceVariant.reference` is a `Reference`
   (`slice_price.py:468-489`). `price_variant` renders it with `output_for_reference`,
   checks the 3MF's sha256 against the key's (`GEOMETRY_DIFFERS`), and slices the saved
   output with `run.slicer.start` (`slice_price.py:516-581`). The estimate reads a done
   render job (`renders.py:601-627` `render_estimate`, `:630-646` `variant_of`), and a
   cart line's add finds the visitor's done render (`line_prices.py:131-165`
   `variant_at_add`). Read.
10. **A library file's hash.** `LibraryFile.file_hash` is the file's SHA-256 when
    Bambuddy states one (`bambuddy/models.py:886-889`); some files state none (#2160,
    `api/library_print.py:1043-1046`). The native library states its row's `sha256`
    (`print_library/native.py:197-204`), and a native row's bytes never change: an edit
    renames or moves it only (`print_library/store.py:299-339` `update_file`). The
    Bambuddy-to-native copy already checks a download against the stated hash
    (`print_library/copy.py:383-398`). Read.
11. **A native file's blob is not safe to hold.** The native library's sweep deletes a
    `library` blob that no row names, and reads no `blob_refs` (`print_library/sweep.py:72-95`).
    A purged trashed file loses its blob after `LIBRARY_TRASH_TTL`, 30 days. Read.
12. **Printable types.** The dialog prints `3mf`, `stl` and `gcode.3mf`
    (`print_source.py:907-911`). A sliced file is queued as it is and has nothing to
    slice (`orders/bambuddy.py:302-306`). An STL has one slot of unknown colour
    (`print_source.py:1092-1097`) and is wrapped into a 3MF to print
    (`print_source.py:1217-1245`). The library search lists unsliced 3MFs only, unless
    `all` is set (`bambuddy/library_listing.py:17-18`, `:229`). Read.
13. **The back office's library file picker.** The order's Add item dialog has a
    library file picker over `GET /print/library/search`
    (`frontend/src/components/orders/LibraryFilePicker.tsx:14-28`). Read.
14. **A library file's preview.** The API reads plate `n` of a library file as a GLB,
    its parts in their colours (`bambuddy/library_view.py:379-391` `library_preview`,
    route `api/library_print.py:866-905`). `write_glb` writes each part's colour as a
    mesh extra (`render/glb.py:81-105`). Read.
15. **The public page fetches from its own origin.** The storefront's policy is
    `connect-src 'self'`; images and media may come from any https origin
    (`storefront/static.py:35-45`). A render's GLB reaches the page through the edge's
    `GET /renders/{job_id}/preview.glb` relay (`storefront/routes.py:526-542`). Read.
16. **The editor's design line.** A new listing keeps the Design and Version pickers open;
    a saved one folds them to one line with "Change"
    (`frontend/src/components/storefront/ListingEditor.tsx:27`, `:498-564`; #6711, merged
    2026-11-20). Read.
17. **One material per print.** `resolve_choice` picks the one material a print is made
    of, and a slot of `slots` (slot to filament id) fixes it to that filament's
    (`orders/storefront/inputs.py:390-420`). `check_one_material` refuses mixed slots
    (`inputs.py:544-556`). Read.
18. **Which slots a file's parts print in.** `part_settings_slots(payload)` gives the
    slots a printed part of a 3MF is made in, an empty set for a file with no
    `model_settings.config`, and `None` for a file it cannot write into
    (`render/bambu3mf.py:2620-2640`). Read.

Facts 19 to 25 were read on `origin/main` a7dfea096, on 2026-11-21, after the owner's
answers. Line numbers are on that commit.

19. **A library file's slice reads its bytes through one seam.** `start_library_quote_preview`
    builds a `LibrarySource` with `files=reads_for(file_id, client, native_reads)`
    (`orders/bambuddy.py:292-321`, `:314`). `LibrarySource` reads the file's row, its
    plates, its bytes and its slots only through that `LibraryReads`
    (`print_source.py:1016-1101`, `reads_of`), which has four calls: `library_file`,
    `library_plates`, `download_library_file` and `filament_requirements`
    (`print_library/reads.py:33-39`). With `read_own=True` the slots come from the
    downloaded bytes (`print_source.py:1104-1129`). The slicer then reads a laid-out copy
    that the pipeline uploads (`bambuddy/preview.py:315-368`, `_start_native` at
    `:371-426`), never the source file's id, except in one case (fact 20). Read.
20. **A Bambuddy file can be sliced in place.** `origin_id` is the file's id for a
    Bambuddy file and `None` for a native one (`print_source.py:1161-1164`). On Bambuddy's
    path, a source with an `origin_id` and nothing to lay out is sliced as that library
    file itself (`print_source.py:629-638`), and the dialog's slot read takes it too
    (`bambuddy/send.py:1600-1601`). Read.
21. **A library file's copies are recorded under its run subject.** A laid-out or raw
    copy is recorded under `library:<file id>` (`print_source.py:452-467`,
    `send.py:1512-1550`), and reused for the same folder and target on the rule that the
    bytes under a key do not change (`send.py:1615-1631`). Upload keys must be run
    subjects: `slice_owners` answers them and `linking.py:491` parses each with
    `PrintSubject.from_run_subject`. Read.
22. **The content store streams a held blob.** `ContentStore.get(ref)` streams a blob, and
    `stat(key)` gives its ref (`store/content.py:319`, `:432`). Read.
23. **An order item's print reads the live file.** A `library_file` item prints through
    `start_library_run` (called from `item_print_run`, `orders/operations.py:916`), whose run source is
    `SourceSpec(kind="library", file_id=…)` (`api/library_print.py:1407-1450`, `:1445`): the
    print worker reads the file from the library that serves at print time. Read.
24. **Remembered choices outlive the file.** `model_print_choices` loses a scope's row only
    when the choices are set empty or every choice is forgotten
    (`library/settings_store.py:1358-1372`, `:1540-1545`). A library file's delete leaves
    `library:<id>` in place. Read.
25. **How a template listing holds its pictures.** `media_ids` name images of the
    template's media (`catalogue.media_item`, `library/catalogue.py:1719-1726`).
    `PUT /storefront/listings/{id}/media` sets them and publishes a published listing
    again (`api/storefront.py:146-163`, `listings.py` `change_media`). Publish copies
    each one into the content store as `listing-media-<sha256>`, holds it under
    (`storefront_listing`, listing id) and lists it in `Listing.media` as a
    `ListingMedia` (`listings.py:949-985` `_copy_media`). The editor's
    `ListingPictures.tsx` picks, orders and removes them, and uploads a new one into the
    template's media (`api.uploadMedia(slug, file)`, `ListingPictures.tsx:83-107`). Read.

Not measured: whether Bambuddy ever changes a file's bytes under the same id. This design
does not rely on either answer: the price and the print use the held copy (decisions 3
and 8). The one place the answer matters is fact 21's copy reuse (§7.3.1).

## 3. Decisions in short

1. A listing gets a **source**: `template` (today) or `library_file`. A file listing names
   a library file and has no `slug`, `commit`, `params`, `fixed` or template media.
2. **Publish pins the file by its content sha256** and copies its bytes into the content
   store as `listing-file-<sha256>`, held in `blob_refs` as the snapshot is. It always
   copies, also for a native file, because the native sweep ignores `blob_refs` (fact 11).
   **The held copy is the listing's version of the file.** Library files have no version
   history, so the price never reads the live file's bytes after publish, and a print
   reads them only when they are still the pinned ones (§10.2).
3. **The price slices the held copy**, through `start_library` and the back office's own
   `start_library_quote_preview`, with the held copy in place of the library's reads
   (§7.3.1). So a file that changed or went after publish changes nothing: the listing
   stays live on the version it was published with, and its price stays firm. There is
   no file-state refusal and no "priced by hand" for this case.
4. **The slice request is built once, for both sources**, from the remembered choices of
   the subject's scope (`library:<id>` for a file, the slug for a template): the dialog's
   opening choices, plus the remembered print sequence, process overrides and settings
   per part, as the dialog seeds them (fact 7). A file listing reads `library:<id>` while
   that scope holds an entry, also after the file is deleted (fact 24). When it holds
   none, the listing reads the copy of that entry that publish took (`FileSnapshot.remembered`,
   §4.2). With neither, the file's own 3MF settings apply, as in the back office (fact 6).
5. **A spool-less slot can take a given colour.** `resolve` gains `slot_colours`, so a
   file's slot is sliced in the visitor's colour, not the author's (fact 5). A template
   variant passes none, and its request does not change.
6. **`KEY_VERSION` goes to 4.** The key's `model` now means "the sha256 of the file the
   slice reads", a render's 3MF or a library file, and the key gains the subject kind and
   the three setup fields. Every stored price is then read under a new key, so each
   variant is sliced once more on its next read.
7. **Every slot is offered.** The customer picks a colour for each slot the file's parts
   print in. A slot the file names in a support material is the one exception: the shop
   locks it to a support filament, as for a template (#6152). A print is of one material,
   as every listing's (fact 17).
8. **The order item is a `library_file` item** with the customer's colours on its slots
   and the pinned sha256 and copy key. **Its print prints the held copy.** When the
   library file is still the pinned one, the print reads it as today. When it changed or
   went, the print itself first puts the held copy into the library that serves and
   prints that file (§10.2). There is no "Restore the listed file" action: the print
   does it, so no step is left to the shop.
9. **The customer's preview is the file itself**, read once at publish into a held GLB
   per plate, and recoloured on the page by slot. No render job, no render limits for
   the preview.
10. **The editor's Design block becomes Source**, with two choices: a design (Design and
    Version, as today) or a library file (a picker over the library, 3MF and STL only).
11. **A file listing has pictures as a template listing has**: `media_ids` in the draft,
    `ListingMedia` in `Listing.media` once published, each held as `listing-media-<sha256>`
    under the listing, set through the same `PUT /storefront/listings/{id}/media` and
    edited in the same `ListingPictures` (fact 25). The pictures to pick from are the
    file's own plate pictures and the pictures the shop uploads. A file has no template
    media, so an upload is put straight at the key publish would give it (§5.4).

## 4. The listing (wire and migration)

### 4.1 The input

`ListingInput` (`wire.py:179-208`) gains a source and loosens the template's fields:

```python
#: What the listing sells (#6710): a commit of a template (``slug`` and ``commit``), or
#: a library file (``library_file``). Default ``template``, so a body built before #6710
#: (the agent's, an old editor's) still saves.
source: Literal["template", "library_file"] = "template"
slug: str | None = Field(default=None, min_length=1, max_length=200)
commit: str | None = Field(default=None, pattern=LISTING_COMMIT)
#: The file a ``library_file`` listing sells: an id of the library that serves.
library_file: ListedFileInput | None = None
```

```python
class ListedFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=1)
    library: ItemLibrary  # "bambuddy" | "native", as `LibraryFileRef.library`
    #: True asks the next publish to read the library's file now and pin that version
    #: ("Use the library's version", §9.4); False withdraws the request ("Keep the
    #: published version"). Left out, the stored request stays.
    repin: bool | None = None
    #: What `Listing.library_file` answers, accepted so a client can send back the
    #: object it read. It is ignored: no body sets the pin. A value that is not a
    #: lowercase hex sha256 is refused (422), so a malformed body does not save
    #: silently. `Listing.library_file` always answers it lowercase, so a read-back
    #: object passes; a client that upper-cases hashes gets the 422.
    pinned: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
```

The pin is the server's: no body can set it. The `library_file` column stores
`{id, library, pinned, repin}`. `pinned` is the sha256 of the held copy the listing sells,
or null before the first publish; `repin` is the stored request. A save writes `repin`
only when the body names it (`model_fields_set`), so a body that leaves it out (the
agent's, an older page's) neither makes nor withdraws a request. A save keeps the stored
`pinned` unless it changes `library_file.id` or `library`; that save stores `pinned` null
and `repin` false, whatever `repin` the body names (with no pin, the next publish reads
the library anyway), and drops the `storefront_listing_kept` hold on the old
`listing-file-*` key in its transaction, because no pin names that copy any more (an
order item's own hold keeps what an order needs, §10.1). A save never clears `pinned`
for `repin`: it only stores the request. Publish
writes `pinned` and `repin: false` in the same transaction as the snapshot, and only when
it succeeds. So a body that says nothing of the pin (the agent's, a script's, an older page's) never moves the listing to
the live file, and a body cannot name another file's held copy. `Listing.library_file` answers the stored
object, `pinned` and `repin` included, so the editor can read the file's state against
it (§9.4). A client may send that object back as it read it: the input takes `pinned`
and ignores it.

A `model_validator(mode="after")` keeps the two shapes apart. It answers 422 with code
`listing_source_invalid` and the field's `name` when:

- `source` is `template` and `slug` or `commit` is missing, or `library_file` is set;
- `source` is `library_file` and `library_file` is missing, or `slug`, `commit`,
  `params` or `fixed` is not empty;
- `source` is `library_file` and an id in `media_ids` is not a file listing's picture id:
  `plate:<n>` (plate `n`'s own picture, `n` from 1) or `upload:<sha256>` (a picture the
  shop uploaded to this listing, §5.4).

`Listing`, `ListingRow` and the agent's types read `slug` and `commit` as nullable.
`ListingRow` gains `source` and `filename` (the file's name for a file listing, else
null), so the list can show a file listing. `ListingQuery.q` also matches the filename.
A `slug` filter matches no file listing.

### 4.2 The snapshot

The `snapshot` column holds one of two shapes, told apart by `kind`:

```python
Snapshot = Annotated[ListingSnapshot | FileSnapshot, Field(discriminator="kind")]
```

`ListingSnapshot` gains `kind: Literal["template"] = "template"`, so every stored row reads
as it is. `FileSnapshot` is what publish read from the file:

```python
class FileSlot(BaseModel):
    #: From 0, as `ColourSlot.slot` and `RenderBody.colours` key a slot (slot id - 1).
    slot: int = Field(ge=0)
    #: The file's own colour, `#RRGGBB` (`quote.py` `file_colour`); grey when it names none.
    colour: str
    #: The file's own filament type (`FilamentRequirement.type`), or None.
    material: str | None
    #: Whether a printed part uses it (fact 18). An unused slot is never offered.
    used: bool
    #: A support or interface slot (`is_support` of `material`): the shop locks it.
    support: bool

class FileSnapshot(BaseModel):
    kind: Literal["library_file"] = "library_file"
    file_id: int
    library: ItemLibrary
    filename: str
    file_type: Literal["3mf", "stl"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int
    #: The held copy, `listing-file-<sha256>`.
    copy_key: str
    slots: list[FileSlot]
    plates: list[int]
    #: One held GLB per plate (`listing-preview-<sha256 of the GLB>`), in plate order.
    #: The pictures the draft's `media_ids` pick go in `Listing.media`, as a template's (§5.2).
    preview_keys: list[str]
    #: Each support slot's filament, as the shop locked it (§9.3), by slot from 0.
    support: dict[int, str] = Field(default_factory=dict)
    #: The remembered choices of `library:<file_id>` when publish ran, or None when the
    #: scope held none. `plan_variant` reads it only when that scope holds none (§7.1).
    remembered: ModelPrintChoices | None = None
```

`Listing.snapshot` becomes `Snapshot | None`. Every reader of `snapshot.schema_`,
`.fixed`, `.libraries` or `.snapshot_key` narrows on `kind` first; mypy finds each one.

### 4.3 Storage

One new migration, `$(date -u +%Y%m%dT%H%MZ)_storefront_file_listings.sql`:

```sql
ALTER TABLE storefront_listings
  ADD COLUMN source text NOT NULL DEFAULT 'template'
    CHECK (source IN ('template', 'library_file')),
  ADD COLUMN library_file jsonb,
  ALTER COLUMN slug DROP NOT NULL,
  ALTER COLUMN commit DROP NOT NULL;

ALTER TABLE storefront_listings ADD CONSTRAINT storefront_listings_source CHECK (
  CASE source
    WHEN 'template' THEN slug IS NOT NULL AND commit IS NOT NULL AND library_file IS NULL
    ELSE slug IS NULL AND commit IS NULL AND library_file IS NOT NULL
  END
);

ALTER TABLE order_items
  ADD COLUMN library_sha256 text CHECK (library_sha256 ~ '^[0-9a-f]{64}$'),
  ADD COLUMN library_copy_key text;
```

The commit's pattern check stays on the column; Postgres passes a CHECK on NULL. The
order item's two columns are null for every item made before, and for a `library_file`
item the back office adds by hand. State is in Postgres, bytes in the content store: no
`bytea`.

## 5. Publish

### 5.1 What publish checks

`publish` (`listings.py:1009-1034`) keeps its outer checks (S3 only, one publish of a
listing at a time). `_publish` then branches on `source`. For a file listing it checks,
in this order; the first fault wins:

| Order | Fault | Status and code | `name` |
| --- | --- | --- | --- |
| 1 | The file is not in the library that serves (`ServedLibrary.file` is None) | 422 `listing_file_unknown` | `library_file` |
| 2 | Its type is not `3mf` or `stl` (a sliced `gcode.3mf` included) | 422 `listing_file_unprintable`, `reason` `sliced` or `type` | `library_file` |
| 3 | It is larger than `MAX_DOWNLOAD_BYTES` (`print_source.py:919`) | 422 `listing_file_too_large` | `library_file` |
| 4 | Its download's sha256 is not its stated `file_hash` | 409 `listing_file_changed` | `library_file` |
| 5 | No offered slot is left (every slot unused or support) | 422 `listing_file_no_parts` | `library_file` |
| 6 | `materials` and `colours` as today (`check_materials`, `listings.py:828-847`) | as today | as today |
| 7 | No allowed material offers a colour for every offered slot | 422 `listing_file_slots_unprintable` | `colours` |
| 8 | A support slot has no support filament in `colours` | 422 `listing_file_support_unset`, `slots` | `colours` |
| 9 | A material with no filament type in the shop slice profile (`check_sliceable`, `listings.py:988-1006`) | as today | as today |

Rows 6 and 9 run unchanged. Row 7 is `printable_materials` over the offered slots, as
`check_locked_colours` is for a template's colour parameters (`listings.py:850-887`).
Each code carries `name`, so the editor shows it under the field it names.

Rows 1 to 4 read the library. They run only when the stored `pinned` is null or the
stored `repin` is true (§4.1): a first publish, a new file picked, or "Use the library's
version" (§9.4). When such a publish pins a sha256, it drops every `storefront_listing_kept`
hold on a `listing-file-*` key but the new one. When a `repin` publish is refused (the
file is gone, or any row of the table), the row keeps its old `pinned` and its held copy; the listing is
offline as after any edit, and the shop puts it back on its held version with "Keep the
published version" (§9.4), a save with `repin: false`, then a publish. Otherwise, with `pinned`
set, publish reads the held copy `listing-file-<pinned>` in place of the library, and
rows 1 to 4 become one check: that copy is held under the listing (§5.2 step 1), else
422 `listing_file_unknown`. So a publish again of the same version (after an edit of the
title, the colours or the pictures, and `change_media`'s publish) keeps that version,
and a file gone from the library does not stop it. An edit sets `published = false` and
`snapshot = NULL` (`listings.py:276-300`), so the pin cannot live in the snapshot: it
lives in the `library_file` column.

One more check runs after row 5: an id in `media_ids` that names no plate picture of the
file and no picture held for the listing is 422 `listing_media_unknown`, with the id as
`name`, as for a template (`_check_pictures`, `listings.py:1068-1094`).

### 5.2 What publish stores

1. **The copy.** The file is downloaded once (`LibrarySource.download`, as a print reads
   it), hashed as it streams, and put in the content store with kind `media` and key
   `listing-file-<sha256>`, as `_copy_media` puts media (`listings.py:949-985`). The key
   is held under (`storefront_listing_kept`, listing id), a holder kind of its own: an
   edit or an unpublish drops the (`storefront_listing`, listing id) holds
   (`listings.py:1-17`, `release_unheld` at `:549-563`), and the pinned version must
   outlive both. The `storefront_listing_kept` hold on `listing-file-<sha256>` is dropped
   only by the listing's delete, by a save that changes `library_file.id` or `library`
   (§4.1), and by a publish that pins another sha256 (each in the row's transaction, as
   the other drops are). The kept holds on uploaded pictures
   (`listing-media-*`, §5.4) follow their own rule there; a publish drops none of them.
   With `pinned` set, this step reads nothing: the copy is held already. Two
   listings of one file share one blob and hold it twice.
2. **The slots.** Read from the held bytes with the dialog's own reader (`LibrarySource.load`
   with `read_own=True` over the held copy's reads, §7.3.1, as `start_library_quote_preview`
   reads a file, `orders/bambuddy.py:308-321`): each declared slot, its colour and its type. `used` is
   from `part_settings_slots` of the 3MF (fact 18). When that answers `None` or an empty
   set, every declared slot counts as used. An STL has one slot, used.
3. **The previews.** One GLB per plate, from `library_preview` of the held bytes
   (fact 14), with each part's slot written as a mesh extra beside its colour (§8.2). Each
   is put as `listing-preview-<sha256 of the GLB>` and held. A file it cannot read a mesh
   from (`NotViewableError`) publishes with no preview; the page then shows its pictures
   only.
4. **The pictures**, in `media_ids` order, as a template's (fact 25). A `plate:<n>` id is
   plate `n`'s picture in the held 3MF (`Metadata/plate_<n>.png`, as `quote.py`
   `plate_image` reads a sliced file's), put as `listing-media-<sha256>` and held under
   (`storefront_listing`, listing id), as `_copy_media` does. An `upload:<sha256>` id is
   a picture already at `listing-media-<sha256>` (§5.4): publish adds the
   (`storefront_listing`, listing id) hold on it and copies nothing. Each becomes a
   `ListingMedia` of kind `image` with its id as `media_id`, so `ListingPictures` marks it
   Live as it marks a template's. An STL has no plate picture.
5. **The snapshot.** `FileSnapshot` as in §4.2, with `remembered` set to
   `model_print_choices.get(library_options_scope(file_id))` as publish reads it.
   `mark_published` writes it with the media, as today, and writes
   the `library_file` column's `pinned`.

A failure after the first hold drops every (`storefront_listing`, listing id) hold the
row does not name (`release_unheld`), as today. `release_unheld` also keeps a file
snapshot's `preview_keys` beside its `media` (today it keeps a template snapshot's
`snapshot_key` and the media, `listings.py:560-562`). A new (`storefront_listing_kept`)
hold that publish took and the row does not name in the `library_file` column's `pinned` is dropped
with them.

All of this runs in the CRUD service (today's API process), which already downloads and
reads library files for the back office (facts 3 and 14). No new database client, grant
or table access is added, and no untrusted code runs (#5295): publish reads the 3MF as
data, with the readers the back office uses.

### 5.3 What a change of the file does

A published listing never moves (decided by the owner, 2026-11-21). The `library_file` column's `pinned`
names the sha256, and the held copy keeps the bytes: the held copy is the listing's
version of the file. A file renamed or moved in the library keeps its id, so nothing
changes. A file deleted, or one whose bytes are no longer the pinned ones, changes
nothing either: the listing stays live on its pinned version, its price is the held
copy's (§7.3), and its order items print the held copy (§10.2). The editor reads the
file's state on open and says that the listing sells the published version (§9.4).
Publish never follows the file by itself; the shop moves the listing to the library's
version with "Use the library's version" (§9.4).

### 5.4 An uploaded picture

A file has no template media to upload into, so `ListingPictures`' upload goes to the
listing. `POST /storefront/listings/{id}/pictures` (multipart, one image) checks the type
and the size as the template's media upload does (`mediaProblem` in the editor, the
server's own check behind `api.uploadMedia`), puts the image in the content store with
kind `media` at `listing-media-<sha256>` (the key publish gives a template picture), and
holds it under (`storefront_listing_kept`, listing id). It answers the id
`upload:<sha256>`, which the editor adds to `media_ids` as it adds a template upload's
id (`onUploaded`). The listing must be saved first (it needs an id); the editor saves a
new draft before the first upload. A save, and `change_media` (`PUT …/media`), drop every
`storefront_listing_kept` hold on a `listing-media-*` key that the new `media_ids` does
not name, also one whose id never reached a save, in the same transaction as the row's
write. A hold taken or refreshed in the last hour is spared (an upload of a picture the
listing holds already sets that hold's `created_at` to now: the upload takes it with
`BlobRefs.hold` (`store/refs.py`), whose `ON CONFLICT … DO UPDATE SET created_at = now()`
refreshes it, never with `add`, whose `ON CONFLICT DO NOTHING` would count the hour from
the first upload), so an upload is not lost to a save from
another tab before the editor names it; the next save after that hour drops it. This is
the one new route: after the upload, the picture takes the template's path (§5.2 step 4).

## 6. The customer page

### 6.1 The public listing

`PublicListing` (`wire.py:396-420`) gains:

```python
source: Literal["template", "library_file"] = "template"
#: A file listing's parts: one per offered slot, in slot order. Empty for a template.
slots: list[PublicSlot] = Field(default_factory=list)
#: A file listing's 3D previews, one per plate, as edge paths (never presigned: the page
#: fetches only from its own origin, fact 15).
previews: list[str] = Field(default_factory=list)
```

```python
class PublicSlot(BaseModel):
    slot: int
    #: "Part 1", "Part 2", … in slot order. The file's colour names nothing a customer reads.
    label: str
    #: The listing filament it starts at (§6.2).
    default: str
```

`params` is empty for a file listing. A support slot is never in `slots` (spec 2026-11-02
§4, support stays internal); its locked filament stays in the snapshot.

### 6.2 The colours

Each offered slot starts at the closest listing colour to the file's own colour, in the
first allowed material that offers a colour for every offered slot (`closest_colour.py`,
as `resolve_choice` moves an untouched colour, `inputs.py:390-420`). So a listing the
customer does not touch looks as the author made it, as near as the shop's filaments
allow. The customer may then pick any listing colour for any slot. The first slot picked
fixes the material, as a colour parameter does today (`resolve_choice` with `slots`).

### 6.3 The page

`CustomizePage` branches on `source`. For a file listing:

- The preview is the held GLB of plate 1 (a plate chooser when there are more), fetched
  through the edge, `GET /api/storefront/listings/{id}/preview/{plate}.glb`. The CRUD
  route streams the held blob. Each mesh takes the colour of the filament picked for its
  slot (§8.2). Nothing renders, so a colour change shows at once.
- One `ColourPicker` per part, labelled "Part 1", "Part 2", …, with the colours on offer.
- The price reads `POST /api/storefront/listings/{id}/price` with the slot colours
  (§7.5), and shows as today (`Estimate.tsx`: "$X for one", "Working out the price…",
  "We price this design by hand").
- "Add to order" sends `AddListingItem` with `values` empty and `colours` set.

The title has no colour, and the customer gets every slot (repo rule). The page never
shows the filename, the file's own colours or a material.

As built (PR 6): the edge's prefix is `/api/store/v1`, so a preview path is
`/api/store/v1/listings/{id}/preview/{plate}.glb` (`storefront/wire.py`
`listing_preview_path`) and the price is `POST /api/store/v1/listings/{id}/price`.
`PublicSlot.default` is null when the listing offers no colour (it is then unavailable). A
material is printable for a file listing when the listing offers a customer filament of it
(each offered slot takes any colour of the print's one material, `file_choice`), so the
first such material of the effective list gives the defaults (`listings.py`
`_offered_file`). `previews` names only the plates publish held a GLB of (none for a file
with no readable mesh). The CRUD route is `GET …/storefront/listings/{id}/preview/{plate}.glb`
(`file_listings.py` `held_preview`, 404 `listing_not_found` or `preview_not_found`),
streamed from the content store; the edge relays it as a stream. A render, a variant and
the prepare run still take a template listing only (`published_listing`'s `source`); the
public list and listing take either. On the page (`frontend/src/storefront/pages/FileCustomize.tsx`),
`CustomizePage` hands a file listing to `FileCustomize` before its render effect ever sees
it. The parts are colour parameters named `colours.<slot>` (`fileParts.ts` `partsView`), so
`ListingView`, `colourPlan` and the clash text serve them unchanged; a pick of a colour that
only another material offers moves each untouched part to the closest colour of that
material, as for a template. The price read is debounced as a render is and follows the
price key only (the listing's version and the colours), so a change that keeps the key reads
nothing again. It keeps the last price on screen marked as updating until the new colours
are priced; a `last` answer (§7.5) shows at once, marked as updating, and the page reads
again after its `retry_after`. With no price known, "Working out the price…" shows from
the change on, through the debounce. It stops at a `render_limited` or `rate_limited`
refusal with its text, and keeps the last price on screen, not marked as updating, since
no update will come. So does a read past the time limit, or one that fails after a `last`
answer. That price is never shown as firm: the page says "This was our last price. We
confirm the price before you pay." until the colours change or a new read is due. With no
price on screen, the time limit shows "We price this design by hand". A plate chooser ("Plate 1", …)
sits over the viewer when the file has more than one plate. The §11.1 "Public" tests are
`tests/orders/storefront/test_public_files.py` and `tests/api/test_storefront_public_files.py`
(the native-library client's fixtures would clash with `test_storefront_public.py`'s).

## 7. Pricing

The pricing owner's constraints are the rule here. §15 maps each one to its section.

### 7.1 The subject

`SliceVariant` (`slice_price.py:468-489`) names what it slices, one of two:

```python
#: A template variant's render (today).
reference: Reference | None = None
#: A file listing's file (#6710): the listing's held copy of a library file, which the
#: slice reads (§7.3.1).
library_file: PricedFile | None = None
```

```python
class PricedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=1)
    library: ItemLibrary
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The held copy the slice reads, `listing-file-<sha256>` (`FileSnapshot.copy_key`).
    copy_key: str
    file_type: Literal["3mf", "stl"]
    size: int
    plates: list[int]
    #: The slots the slice must use (`FileSlot.used`), from 0.
    used: list[int]
    #: `FileSnapshot.remembered`, for a scope that holds none (decision 4).
    remembered: ModelPrintChoices | None = None
```

A validator requires exactly one. An operation started before this change has a
`reference` and no `library_file`, so it still reads.

`plan_variant` (`slice_price.py:602-662`) takes the subject in place of `reference`,
and reads the remembered entry by the subject's scope:

```python
if reference is not None:
    remembered = settings.model_print_choices.get(reference.slug)
else:
    remembered = settings.model_print_choices.get(library_options_scope(file.id), file.remembered)
choices = opening_choices(remembered, printer.status, printer.bed_type(settings))
```

A file listing reads `library:<id>` while that scope holds an entry. A library file's
delete leaves the entry (fact 24), so the shop's last choices for the file still apply
after it is gone. Only a scope with no entry (never set, or forgotten in the dialog)
falls back to the copy publish took; with neither, the file's own settings apply (§7.2).
So forgetting a file's choices returns its listing to the setup it was published with,
not to the file's own; "Use the library's version" and publish take a new copy (§9.4).

So the plan's inputs are the same for both sources: the same `opening_choices`, the same
`PrinterOpening` from `printer_opening` (through `live_opening`, `slice_price.py:923-942`)
and the same presets from `read_type_presets` (`slice_price.py:742-768`). No second
builder of choices, presets or requests exists.

### 7.2 The request

`slice_request(profile, choices)` (`slice_profile.py:101-114`) becomes
`slice_request(profile, choices, remembered)`. It sets what the dialog seeds from the
remembered entry (fact 7): `print_sequence`, `process_overrides` and `part_settings`. Any
Bambu Studio key goes through the one generic `process_overrides` (#2998); there is no
allowlist and no field per key. A sliced file's dialog seeds none of the three
(`usePrintChoices.ts`, `next.sliced`); publish refuses a sliced file (§5.1 row 2), so that
case never reaches here.

With nothing remembered, the request sets no overrides, and the slice applies what the
file and the process preset give, as a back-office slice of the file does (fact 6). The
same `start_library_quote_preview` runs in both, so the file's own settings reach the
slicer in the same way.

This applies to a template variant too. Today a template variant's slice drops the
remembered print sequence, overrides and settings per part, which the dialog sends
(fact 7). That is a parity gap of its own, and one builder closes it for both sources.
Decided (owner, 2026-11-21): template listings take the three fields too, as proposed.

### 7.3 The slice

`price_variant` (`slice_price.py:516-581`) branches on the subject. For a file:

1. Each slot's filament is read, as today (`FILAMENT_GONE`).
2. The held copy is read: `content.stat(file.copy_key)` (fact 22). None (only after a
   fault: a hold keeps it, §5.2 step 1): `FILE_GONE`, "the listing's copy of its file is
   missing". The library is not read: the live file's state does not change the price.
   This takes the place of `GEOMETRY_DIFFERS`, which compares a render's 3MF to the key's;
   the held copy's key is its sha256, so its bytes are the pinned ones.
3. `slice_and_price` with
   `start = lambda: run.slicer.start_library(file.id, variant.slice, subject,
   slot_presets=variant.slot_presets, slot_colours=variant.slot_colours,
   held=HeldFile(file, content))`.
4. `priced_for` runs `sliced_slots(plates, file.used)` and gives the filament of each
   used slot. A used slot at 0 g fails the price, as for a template.

`Slicer.start_library` (`quote.py:586-591`) gains the two keywords and `held`,
`PreviewSlicer` passes them to `start_library_quote_preview` (fact 4,
`orders/operations.py:264-279`), and that function passes the two keywords on to
`start_preview_plates` and uses `held` as §7.3.1 says. A back-office quote passes none
of the three, so its request and its input do not change.

#### 7.3.1 How the held copy reaches the slicer

The slice reads its file through one seam: the `LibraryReads` that
`start_library_quote_preview` gives `LibrarySource.load` (fact 19). The change is to give
it the held copy there, and to keep its copies apart from the live file's (step 4).
Nothing else in the path moves:

1. **`HeldFile`** (new, `orders/storefront/held_file.py`) is a `LibraryReads`
   (`print_library/reads.py:33-39`) over one held copy, built from the `PricedFile` and the content store:
   - `library_file(id)` answers a `LibraryFile` with the listed id, filename, type and
     size, and `file_hash` = the pinned sha256;
   - `library_plates(id)` answers the pinned `plates`;
   - `download_library_file(id)` streams `ContentStore.get` of `copy_key`
     (`store/content.py:319`);
   - `filament_requirements(id)` answers the snapshot's slots by slot id. It is reached
     only where the bytes give no slots (an STL, `print_source.py:989-998`).
2. **`start_library_quote_preview`** (`orders/bambuddy.py:292-321`) gains
   `held: HeldFile | None = None`. With it, `files=held` in place of
   `reads_for(file_id, client, native_reads)` (`:314`), and `LibrarySource.load` gets
   `held=True`. `native=native_reads if lan else None` stays: a LAN slice still uploads
   its laid-out copy into the native library, as a back-office slice does.
3. **`LibrarySource`** gains `held: bool = False`. `origin_id` answers `None` when it is
   set (`print_source.py:1161-1164`), so Bambuddy's path never slices the live Bambuddy
   file in place (fact 20, `print_source.py:629-638`) and the slot read never takes it
   (`send.py:1600-1601`). A held Bambuddy file then takes the native file's branch: its
   bytes are laid out and uploaded as a copy (`print_source.py:624-627`, or the upload
   below it).

4. **The copies name their version.** The copies stay recorded under the run subject
   `library:<file id>`, because upload keys must be run subjects (fact 21). A recorded copy
   is reused for the same folder and the same `target_key`, and no content is compared
   (`send.py:1360-1385`, `:1512-1549`, `:1605-1631`). The held path would put two byte
   versions under one key: a held slice could take a laid-out copy of a changed live
   file, and a back-office slice a copy of the held bytes. So `Printable` gains
   `version: str | None` (the pinned sha256 for a held source, None for any other), and
   every copy a held source records carries it in its `target_key`, as the prefix
   `v=<sha256>|` before the target key (`send.py:995`, `_hinted(target.key, …)`) and
   before `RAW_TARGET` (`send.py:1485`, `:1535`, `:1543`). A live copy's key has no
   prefix. Each match compares whole keys or prefixes built the same way, so a held copy
   is never taken for a live one, nor the reverse. The slot read's "any copy at all"
   (`send.py:1590-1599`) takes only copies of its own version: a held source those with
   its prefix, any other source those with none.

So the slicer reads a copy of the held bytes, laid out by the dialog's own pipeline, on
either slicer. `plan_variant`, `slice_and_price`, `opening_choices`, `printer_opening`,
`read_type_presets` and the request are the ones a back-office quote uses; only the bytes'
source differs, and those bytes are the bytes a back-office quote of the unchanged file
reads. The key stays on the content sha256 (§7.4).

PR 2 tests that a held Bambuddy file is never sliced in place, that a held slice never
reuses a copy recorded under `library:<id>` without its version prefix (a back-office copy
of other bytes), and that a back-office slice never reuses a held copy.

**Slot numbers.** The listing counts slots from 0 (`FileSlot.slot`, `PricedFile.used`,
`RenderBody.colours`), as `ColourSlot.slot` and a sliced plate's `PlateFilament.slot` do.
`resolve`'s `slot_presets` and `slot_colours`, and `part_settings_slots`, use slot ids,
from 1 (`resolver.py:347-352`). Two places convert. `plan_variant` builds
`slot_presets` and `slot_colours` keyed `slot + 1`, as `slot_presets` does today
(`slice_profile.py:92-98`). Publish stores `used` as each `part_settings_slots` id
less 1, so `used` compares with `PlateFilament.slot` (`quote.py:651`). PR 2 tests that slot 0's colour reaches slot id 1.

`slot_colours` is new (decision 5). `resolve` takes
`slot_colours: Mapping[int, str] | None`, by slot id, and a slot with no spool takes
`slot_colours.get(slot_id) or slot.colour`. `SliceVariant` gains
`slot_colours: dict[int, str]`, each slot's filament colour (`Filament.colour`). A
template variant leaves it empty: its 3MF holds the visitor's colours already.

No second slicer request exists: the file is sliced once per variant key, by the
operation that claimed it (`SlicePrices.claim`, `slice_price.py:231-267`), under
`storefront_slices_concurrent`, as today.

### 7.4 The key

`KEY_VERSION` goes from 3 to 4 (`slice_price.py:133`), because the key's meaning changes.
The body gains two fields:

```python
"subject": {"kind": "template"} | {"kind": "library_file", "library": file.library},
"setup": request.model_dump(mode="json",
                            include={"print_sequence", "process_overrides", "part_settings"}),
```

and `model` is the sha256 of the file the slice reads: a render's 3MF (today), or the
library file's content (the pinned `FileSnapshot.sha256`). It is never a job id or a file
id. Two listings of the same file at the same colours and the same setup share one
price. A change of the live library file does not move the key: `model` is the pinned
sha256, and the setup comes from `library:<id>` or the publish copy (§7.1). The slot colours are in the key already (each filament's
`colour`, `slice_price.py:420-427`).

After the bump, every stored row is read under a new key, so each variant is sliced again
on its next read. Old rows stay until the 90-day sweep (R13 of the slice-price plan). A
pinned key test (§11.1) keeps a template key from moving again by accident.

### 7.5 The read

A file listing has no render, so its price is read by the listing and the colours:

- CRUD route `POST /storefront/listings/{id}/price` (storefront service credential, as
  every edge route's), body `ListingPriceBody {listing_updated_at, colours: {slot:
  filament id}}`, `extra="forbid"`. It checks the colours with `resolve_choice` and
  `check_one_material` (fact 17), builds the variant, and answers `slice_estimate`'s
  `StoreEstimate` (`slice_price.py:796-828`).
- Edge route `POST /api/storefront/listings/{id}/price`, relayed.
- A read that **claims a new slice** counts as one render against the visitor's render
  limits (`storefront_renders_per_minute`, `…_per_day`, spec 2026-11-02 §5.4). A read that
  finds a price, or joins a running slice, costs nothing, as a reused render does. So a
  visitor cannot keep the slicer busy by changing colours.
- 409 `listing_changed` when `listing_updated_at` is not the listing's, as for a render.

As built (PR 6): "always show a price". While the visitor's colours have no price yet
(`slice_estimate` answers `pending`), the route answers `last`: the listing's last firm
price of the file it sells now, of any colours (`SlicePrices.listing_last`, the newest
`done` row of the pinned sha256, index `storefront_slice_prices_listing_last`; the
listing's own row first, else another listing's of the same file, since they share a price,
§7.4), with the pending `retry_after`. The slice runs
behind it as before, and a read again answers the colours' own price. A listing with no
such row answers `pending`. `last` is never a cart line's price (`line_prices.py` reads
`firm_price` only), as the template instant read's `last` (#6921) is not.

### 7.6 The cart and the checkout

- `add_listing_item` (`drafts.py:401-471`) branches on `source`. For a file listing it
  builds the variant as §7.5 does, reads `price_of`, and adds a `NewLibraryItem` (§10.1).
  `LinePrices.record` writes the same row as today: the listing, the pinned sha256 and
  each slot's filament (`line_prices.py:47-73`).
- `price_of` (`line_prices.py:168-193`) takes the item's subject. For a `library_file`
  item it checks that the listing is still published, still a file listing of the same
  file id, and still pinned at the line's sha256. Else the line is priced by hand.
- `reprice` (`line_prices.py:212-245`) reads a `library_file` line as it reads a model
  line (today it skips an item with no `reference`).

## 8. The previews

### 8.1 Why a held GLB

A template's customer sees a render in the colours picked. A file needs no render: its
geometry is fixed. A held GLB per plate shows it at no cost per visitor, and the page
recolours it.

### 8.2 The slot extra

`write_glb` (`render/glb.py:81-105`) writes `mesh.metadata[SLOT_EXTRA] = part.slot` beside
`COLOUR_EXTRA` when the part knows its slot (a `ColourPart` read from a 3MF does). The
viewer sets each mesh's colour from a `slotColours` prop when it is given, by that extra;
a mesh with no slot keeps its own colour. A render's GLB is drawn as today: the page
passes no `slotColours` for it.

## 9. The editor

### 9.1 The Source block

The editor's Design line (`ListingEditor.tsx:498-564`) becomes the **Source** block. It
stays at the top, because the source decides everything below it.

```
┌ Source ───────────────────────────────────────────────────────────┐
│ ( ) A design      (•) A library file                              │
│                                                                   │
│ [ Search the library…                                     ]       │
│ Keychain base.3mf · Prints/Keychains · 3MF · 2 parts  [Change]    │
└───────────────────────────────────────────────────────────────────┘
```

- **A new listing** (`isNew`) shows the two choices and keeps the chosen source's pickers
  open, as #6711 keeps the Design and Version pickers open (fact 16). "A design" shows the
  Design and Version selects as today. "A library file" shows the picker.
- **A saved listing** folds the block to one line, with "Change": "Design Dollhouse Kit,
  version 3f9a2c1" or "Library file Keychain base.3mf". "Change" opens both choices.
- Switching the source clears what the other source set (`params`, `fixed` and `commit`,
  or `library_file`) and `media_ids`, whose ids name the other source's pictures, keeps the title, the description and the colours on
  offer, and marks the draft unsaved.

### 9.2 The file picker

The picker is `LibraryFilePicker` (fact 13), with a `types` filter:

- `GET /print/library/search` gains `types: list[Literal["3mf", "stl"]]`. With `types`,
  the search lists only those types; `all` is ignored. The editor sends `types=3mf&types=stl`,
  so a sliced file or another type is never listed.
- Each hit shows its name, its folder path and its type. A picked file shows its parts,
  one swatch per part (as "Parts" below reads them) in the file's own colour, and its
  first plate's picture.

### 9.3 Below the Source block

- **Product**: title, description, as today. The title starts as the file's name without
  its ending and without colour words (`withoutWords`, `listingDraft.ts:313`), and the
  "Leave … out of the title" warning (`titleColours`, `listingDraft.ts:303`) runs over the
  file's colours too.
- **Pictures**: the same picture list as a template listing's (`ListingPictures`'
  `PictureList`, in `FilePictures`; fact 25, decision 11). No route reads an upload back
  before publish, so the editor shows an uploaded picture's image only in the tab that
  uploaded it; elsewhere it is listed as "Uploaded picture".
  The pictures to add are the file's plate pictures (`plate:<n>`) and the shop's uploads
  (`upload:<sha256>`, §5.4). A new file listing starts with every plate picture in
  `media_ids`. "Upload a picture" saves a new draft first. Live marks, order, remove and
  the republish of a published listing work as for a template (`PUT …/media`). Before
  the first publish, the editor shows a plate picture from the library file's plate
  image, as the picker does. Once the listing has a pin, it shows it from the held copy:
  `GET /storefront/listings/{id}/plates/{n}.png` streams `Metadata/plate_<n>.png` of
  `listing-file-<pinned>`, so a file gone from the library still shows its pictures, and
  they are the ones publish copies (§5.2 step 4).
- **Parts** takes the place of Settings: one row per slot. "Part 1 — the customer picks
  its colour" with the file's swatch, or, for a support slot, the support filament
  publish locks it to, read-only ("Support: locked to PVA Natural"): the first of the
  listing's support filaments of the slot's material, else its first support filament
  (§5.1 row 8, `FileSnapshot.support`). No body field sets it; the shop sets it by
  ticking support filaments under "Colours on offer", and with none ticked the row says
  so, as publish refuses it (`listing_file_support_unset`). Which slots print (`used`,
  fact 18) is known only once publish has read the file: before the first publish the
  editor lists every slot the file declares (`GET /print/library/{id}/filaments`
  `slots`), marks a slot support by its material (`SUPPORT_MATERIAL`), and says that
  publish leaves out a slot no part prints in. From the first publish on, the rows are
  `FileSnapshot.slots`, and an unused slot is not shown.
- **Materials allowed** and **Colours on offer** as today, with `offersColour` true.

### 9.4 The file's state

On open, the editor of a file listing with a `pinned` version reads the file
(`GET /print/library/{id}`, whose `file_hash` states the file's sha256 in lowercase hex,
#7073; a file whose entry states none is "a file with no stated hash" below). The listing
keeps selling its pinned version whatever this read finds (§5.3), so the Source block only informs:

- A file whose `file_hash` is not the pinned sha256: "This file changed in the library
  since you published. Customers still get the version you published." with the button
  "Use the library's version".
- A file that is gone: "This file is no longer in the library. Customers still get the
  version you published." with no button.
- A file with no stated hash, or the pinned one: nothing.

"Use the library's version" sends `repin: true` with the next save and marks the draft
unsaved; the next publish reads the library and pins its version (§5.1). While the stored
`repin` is true, the Source block says "The next publish takes the library's version."
with "Keep the published version", which saves with `repin: false`. Picking another file
in the picker clears the pin by its changed id. On a listing with a pin, the editor asks
before it saves that change: "Customers lose the version you published. The next publish
sells the file you picked." with "Save" and "Cancel"; a switch of such a listing to "A design"
asks the same, naming the design. The save drops the kept copy (§4.1),
so picking the old file again after it reads the library's version, not the published one.
Picking the stored file again before a save brings back its pin, its request, its pictures and
(unless the person ticked others) its colours.

### 9.5 Sell from the library

The library page's file menu gains "Sell on storefront…" for a 3MF or an STL. It opens
`/storefront/listings/new?sell=library:<id>` with the file as the source, as Generate's
menu opens it for a render (easy listing spec §3).

### 9.6 Refusals

`listingRefusal` (`listingDraft.ts:111`) gains kind `file` for the `listing_file_*` codes
and `listing_source_invalid`, each with `name`; `listing_media_unknown` stays a picture
refusal, as for a template. Each is shown in the Source block or under
Colours on offer as `name` says. A page built before maps an unknown code to its general
error, as today.

## 10. Orders

### 10.1 The item

A cart line of a file listing is a `library_file` item. `NewLibraryItem`
(`lifecycle.py:426-428`) gains:

```python
#: The slots a storefront line's customer chose (#6710), from 0: each with its colour and
#: its price-book filament. Empty for a back-office add, whose slots come from its first
#: quote (`library_colours`).
colours: tuple[ColourSlot, ...] = ()
#: The listing's pinned content and its held copy.
sha256: str | None = None
copy_key: str | None = None
```

`LibraryFileRef` (`orders/models.py:482-491`) gains `sha256: str | None = None` and
`copy_key: str | None = None`, read from the two new columns (§4.3). A support slot's
filament is the shop's lock from the snapshot. The first quote's `recolour` leaves the
colours as they are, because the item has them (fact 3).

At submit, a firm line records its `storefront` quote as any listing line does (slice
plan R5, R10). The item is a `library_file` item, which takes no revisions
(`lifecycle.py:537`).

The order holds the copy: the item adds a `blob_refs` hold on `copy_key` (holder kind
`order_item`, its id) in the add's transaction. So a later unpublish or delete of the
listing does not lose the bytes the order prints (§10.2).

### 10.2 The print

The item prints the held copy (decision 8). Today a `library_file` item's print reads the
live file at print time (fact 23). So the print first makes sure the library holds the
pinned bytes, and only then starts the run as today.

1. **The check.** `_library_stands` (`printing.py:575-600`) keeps today's rule for an
   item with no `sha256` (a back-office add). For an item with a `sha256`, it reads the
   file (`ServedLibrary.file`). The file stands when it is there and its sha256 is the
   item's: its `file_hash`, or, for a Bambuddy file that states none, the sha256 of a
   download. Else, when the held copy is there (`content.stat(copy_key)`), the check
   answers as today with `"restore": true` added. Only a missing held copy is
   `library_file_gone`. There is no `library_file_changed` refusal.
2. **The restore.** `item_print_run` (`orders/operations.py:871-984`), on a check with
   `restore`, puts the held copy into the library that serves before
   `start_library_run`. It looks first in the folder "Storefront" (made when missing) for
   a file whose sha256 is the item's, so a retry of the same claim uploads nothing. Else
   it uploads the copy, streamed from the content store, with the upload the library
   page uses (`POST /print/library/files`, `api/library_print.py:679-695` `post_library_file`): on the native
   library `NativeLibrary.upload` (`print_library/native.py:257`), on Bambuddy's
   `upload_raw` (`bambuddy/send.py:1488`). Then it points the item at the new file id, in
   one transaction with an order event that names both ids.
3. **The remembered choices.** It copies `library:<old id>` to `library:<new id>` when the
   new scope holds none, so the dialog opens on the file as before. The run itself takes
   the accepted quote's setup (`quoted_setup(quote)`, `printing.py:598`), so the print
   does not depend on this copy.
4. **The run's key.** Today `item_print_run` reads `file_id =
   checked.get("library_file_id")` and sets `subject = PrintSubject.library(file_id)`
   (`orders/operations.py:886-892`), and `existing` and `start` key the claimed run by
   it (`:914-933`). The restore runs
   before that line, and `subject`, `existing` and `start` take the item's new id, not
   the check's. No run of the claim can exist under the old id: the restore ends before
   any start. This holds because `OperationWorkflow` runs the check once and every retry
   of the run reuses that same `checked` (its `restore` flag too); a change that re-runs
   the check on a retry must keep it. A retry of the same claim restores nothing (it finds the file in
   "Storefront" by its sha256, or the item already moved), so it keys `claimed_run` by
   `library:<new id>` and finds the run its first try started.
5. **The run** starts on the new id, as today. The next print of the item finds that
   file with the pinned sha256, and stands without a restore.

As built (PR 4): the restore is `orders/held_print.py` `restore_held`, which the run calls
through `restored_file` (`orders/operations.py`) before it computes `subject`. The item's
move is `Lifecycle.move_library_file`, which also sets the item's library to the one the new
id is in and its filename to the name the library gave the file, and writes the order event `item.library_file_restored` (`item_id`,
`from_library_file_id`, `library_file_id`, `sha256`). The copy of the remembered choices is
`SettingsStore.copy_library_choices`, one `INSERT … ON CONFLICT DO NOTHING`. A held copy lost
between the check and the run is `library_file_gone` from the run. The folder's lookup, the
find and the upload run under one advisory lock (`OrderStore.restore_lock`), so two prints
racing to restore make one "Storefront" folder and one copy; a wait for it past
`PROJECT_LOCK_TIMEOUT_MS` (a slow upload to Bambuddy) is a 503 the operation tries again, as
a failed project ensure is. The find skips a run's per-print copy (`per_print`), which its
sweep trashes. A retry of the same claim finds the item moved and keeps that file while it
stands (`Shelf.stands`: live, with the pinned sha256, or a Bambuddy row that states no hash,
whose bytes the next print's check reads); when it is gone since, the held copy is
put back again, as any missing file is, and only a missing held copy is `library_file_gone`. The Bambuddy side of the restore
(`BambuddyShelf`, `served_shelf`) is in `orders/bambuddy.py`, the one file of `orders/` that
reaches the Bambuddy client. The check reads a Bambuddy file that states no hash only when its
stated size is the held copy's; another size is other bytes. The restore runs before the
print claims the item, so a claim refused after it (on hold, claimed by another print) leaves
the item on the restored file; that is intended, and the next print restores nothing.

The item's accepted quote stays: its bytes are the held copy's, which it priced
(§7.3). The listing is not changed: it keeps its pin and its held copy.

All of this runs in the CRUD service, which holds the content store and already writes
the library for the back office. No new database client or grant is added (#5295).

The print dialog opens on the item's file with its colours; the shop picks the spools.
Nothing here changes how a print maps spools to slots: the item's colours say which
colour each slot needs, and "Wait for the right colour" (#6218) is on by default.

## 11. Test plan

### 11.1 Backend

- **Wire.** Each `listing_source_invalid` case; a body with no `source` saves as a
  template listing; a stored template row reads with `kind` `template`.
- **Migration.** A file row with a slug, and a template row with no commit, are both
  refused by `storefront_listings_source`.
- **Publish** (`tests/orders/storefront/test_listings.py`,
  `tests/api/test_storefront_listings.py`): each row of §5.1 with its code and `name`, in
  order; the copy is held under `listing-file-<sha256>`; the slots of a two-colour 3MF
  fixture with `used` from `part_settings_slots`; an STL gives one used slot; a file with
  no readable mesh publishes with no preview; a failed publish drops its holds. The pin:
  a first publish writes `pinned` in the `library_file` column and holds the copy as
  `storefront_listing_kept`; an edit, an unpublish and a publish again keep that hold;
  a save that omits `repin`, and an agent body with no `pinned` at all, keep the stored
  `pinned` and the stored `repin`; a body that sends back `pinned` (another sha256
  included) saves and leaves the stored pin as it was; a save that changes
  `library_file.id` drops the old `listing-file-*` kept hold; a publish again with
  `pinned` set reads no library (a fake that fails every library read) and passes with
  the file deleted; after `repin: true`, or a changed `library_file.id`, the next publish
  pins the library's sha256 and drops every other `listing-file-*` kept hold but no
  picture's; a `repin` publish refused for a gone file keeps the old `pinned` and its
  hold, and a save with `repin: false` then publishes on the old version; the plate picture route streams the held copy's picture with the
  file deleted; a delete drops every kept hold. The snapshot's
  `remembered` is the `library:<id>` entry at publish, or None.
- **Pictures** (`test_listings.py`, `tests/api/test_storefront_listings.py`): `media_ids`
  of `plate:1` and `upload:<sha256>` publish as two `ListingMedia` held under the
  listing, in that order, with their ids as `media_id`; an unknown id is
  `listing_media_unknown` with `name`; an upload holds `listing-media-<sha256>` as
  `storefront_listing_kept` and answers `upload:<sha256>`; a save, and a `PUT …/media`,
  that does not name the id drop that hold, also for an id never saved, except within an
  hour of the upload; the same picture uploaded again after that hour refreshes the
  hold, so a save from another tab right after it spares it; `PUT …/media` republishes a
  published file listing.
- **Resolver** (`tests/bambuddy/test_resolver.py`): a spool-less slot with a
  `slot_colours` entry takes it; one without takes the file's; a slot with a spool takes
  the spool's, whatever `slot_colours` says.
- **The request** (`tests/orders/storefront/test_slice_profile.py`): `slice_request` sets
  the remembered `print_sequence`, `process_overrides` and `part_settings`; none
  remembered sets none.
- **The key** (`tests/orders/storefront/test_slice_price.py`): `KEY_VERSION` is 4; a pinned
  hex value for a template key and for a file key; two subjects with one sha256 and one
  setup give two keys only when their kind differs; a change of any remembered override
  gives another key.
- **The run** (`test_slice_price.py`, with the quote fakes): a file variant calls
  `start_library` once with `slot_presets`, `slot_colours` and `held`, and reads no
  library file; a library whose file is gone or holds other bytes gives the same row as
  one with the pinned file; a held copy missing from the content store is `FILE_GONE`
  and never slices; with no `library:<id>` entry the request carries the snapshot's
  `remembered` setup, and with one it carries that entry's; an unused slot at 0 g still
  prices; slot 0's colour
  reaches `resolve` as slot id 1, and a `part_settings_slots` id 1 is stored in `used` as 0.
- **The held reads** (`tests/orders/storefront/test_held_file.py`,
  `tests/orders/test_bambuddy.py`): `HeldFile` answers the pinned row, plates and bytes
  and the snapshot's slots; `start_library_quote_preview` with `held` reads its bytes,
  not the library's; a held Bambuddy-range file is never sliced in place (`origin_id` is
  None, so the fake Bambuddy sees an upload and no slice of the file id); a held slice
  never reuses a copy recorded under `library:<id>` with no `v=<sha256>|` prefix, and a
  back-office slice and slot read never reuse a held copy; without `held` the request,
  the input and the recorded keys are as today.
- **Parity, the owner's constraint 4** (`tests/orders/storefront/test_slice_parity.py`), a
  new test beside `test_the_storefront_slices_and_prices_as_the_back_office_quote`:
  - The back office: the dialog's `choices_for` over a `LibrarySource` of file 42, with
    `remembered = SETTINGS.model_print_choices["library:42"]` holding a process override
    (`{"enable_support": "1"}`), a print sequence and a setting per part; a
    `SliceQuoteRequest` with those choices, the dialog's three seeded fields and an SB
    spool per slot; `quote_run` on a `library_file` item of file 42 (`_library_quote`).
  - The storefront: `printer_opening`, then `plan_variant` with the file as its subject,
    then `price_variant`, with the held copy in a fake content store holding the same
    bytes as file 42.
  - Asserts: `start_library` is called for file 42 on both sides, the storefront's with
    `held`; the bytes each slice reads have the same sha256; the two requests are
    equal but `filament_plan` and `filaments`; `resolve` gives the same presets and the
    same colours per slot (`["GFSA00_22", "GFSG02_25"]`, `[RED, GREEN]`); the row's
    `total_minor`, `unit_price_minor`, `unit_cost_minor` and `lines` equal the quote's.
  - A back-office quote on a LAN printer needs loaded trays (negative tray ids; measured
    on prod by the pricing session, 2026-11-21). So a case with colours that no tray
    holds gives the back office `filament_overrides` plus the nearest tray, as the
    storefront price does.
  - The existing template test keeps passing, with the back office's request now carrying
    the same three seeded fields.
- **The read** (`tests/api/test_storefront_slice.py`): a first read is `pending` and takes
  one render of the visitor's limits; a second read of the same colours takes none; over
  the limit is 429; colours of two materials are `listing_colours_clash`.
- **The cart** (`test_drafts.py`, `test_submit.py`): an add makes a `library_file` item with
  the colours, the sha256 and a hold on the copy; `reprice` answers `by_hand` after the
  listing is republished on another file; a firm line records a `storefront` quote.
- **Orders** (`tests/orders/`): `_library_stands` of a storefront item with the pinned
  file stands as today; with the file gone, or with other bytes, it answers `restore`
  and never `library_file_changed`; with the held copy missing it is `library_file_gone`;
  a back-office item is unchanged. `item_print_run` with `restore` uploads the held copy
  into "Storefront", points the item at the new id with an order event, copies the
  remembered choices to an empty new scope only, and starts the run on the new id; a
  retry of the same claim finds that file by its sha256 and uploads nothing; the next
  print of the item stands without a restore.
- **Public** (`test_storefront_public.py`): a file listing answers `source`, its offered
  slots with labels and defaults, no support slot, and the edge preview paths; the preview
  route streams the held GLB.

### 11.2 Frontend

- `ListingEditor.test.tsx`: a new listing shows both source choices with the pickers open;
  "A library file" lists only 3MF and STL hits; a picked file shows its parts; a saved file
  listing folds to "Library file …" with "Change"; switching source clears the other's
  fields; each `listing_file_*` refusal shows under its field; the "changed in the library"
  and "no longer in the library" notes say that customers still get the published
  version, and "Use the library's version" sends `repin: true`; a pinned listing's plate pictures load
  from the listing's plate route; Pictures lists the
  file's plate pictures, a new file listing starts with all of them, an upload saves a
  new draft first and adds `upload:<sha256>`.
- `listingDraft.test.ts`: `listingInput` and `savedKey` of a file draft; the title default
  drops the ending and colour words.
- `CustomizePage.test.tsx`: a file listing shows one picker per part, recolours the
  preview with no render request, reads the price route, and adds with `colours`.
- `Preview` recolours by `slotColours` and leaves a mesh with no slot as it is.
- A Playwright run on the msw mocks: Sell on storefront from the library page, publish,
  then the public page.

### 11.3 Agent

- `storefront_listing_draft` with `library_file_id` drafts a file listing; the shared
  vectors in `test-fixtures/storefront/sell-draft-vectors.json` gain a file case, which
  `listingDraft.test.ts` and the agent's tool test both read.
  As built (PR 7): the file case has a `file` key (the library's answers for the file: its
  name, `…/filaments` `slots`, `…/plates`), `expected`, the body a new listing of it saves
  but `materials`, and `titles`, the colour names in each title that a customer can pick (the
  editor's `titleColours` of the new draft, the agent's `fileTitleColours`). The editor's `declaredSlots` and `platePictures` (`listingDraft.ts`) and
  the agent's copies make the parts and the pictures. `materials` differs on purpose: the page
  leaves a new listing on the shop's default, and the agent names the offered filaments'
  materials (`draftMaterials`, as for a design, #6302 review), so publish's materials check
  (§5.1 row 6) passes for a file of a material the default leaves out. The agent refuses a file
  whose type is not 3MF or STL before it posts (§5.1 row 2), and `lock` and `values` with
  `library_file_id`.

### 11.4 Before go-live (on prod, by hand)

- List one two-colour 3MF from the library, read its price as a visitor, and compare it to
  a back-office quote of the same file at the same colours on the profile's printer. Record
  both totals and the operation ids in the go-live PR.
- Delete the file from the library. Check that the visitor's price is the same firm
  price, order it, and print the item: the print puts the held copy into "Storefront"
  and prints it. Record the new file id and the run id in the go-live PR.

## 12. PR split

Each PR is a reviewable step. A file listing cannot be published, or is not shown to a
customer, until the step that makes it safe.

1. **`feat(print): a spool-less slot takes a given colour`** (backend). `slot_colours` in
   `resolve`, `start_preview_plates` and `start_library_quote_preview`;
   `Slicer.start_library` and `PreviewSlicer` take `slot_presets` and `slot_colours`. No
   storefront change; every existing caller passes neither.
2. **`feat(storefront): price a library file through the one path`** (backend). Starts
   only after scadify's #6921 merges, and keeps `storefront_slice_prices.variant_key` on
   done rows, which #6921 adds. `HeldFile`, `held` on `Slicer.start_library`,
   `PreviewSlicer` and `start_library_quote_preview`, `LibrarySource.held` with its
   `origin_id`, and `Printable.version` in the recorded copies' `target_key` (§7.3.1); the subject on `SliceVariant` and `plan_variant`, with the
   publish copy of the remembered choices; `slice_request` with the remembered setup;
   `KEY_VERSION` 4 with `subject` and `setup`; `price_variant` for a file (the held copy,
   `FILE_GONE` for a missing one); the pinned key tests; the held reads tests; the
   parity test of §11.1. No listing names a file yet, so only tests reach the file
   branch. The bump re-slices each template variant once. After 1.
3. **`feat(storefront): list a library file`** (backend). The migration; the wire, the pin
   and the snapshot; publish's checks, the copy and its `storefront_listing_kept` hold,
   the slots, the previews and the pictures (`plate:<n>`, `upload:<sha256>`, the upload
   route of §5.4); the price read and its edge route; the cart, `price_of`, `reprice` and
   submit as a `library_file` item with the sha256, the copy and its hold; the library
   search's `types`; the GLB slot extra. Until PR 6, `public_card` and `public_listing`
   leave a file listing out, because the public page cannot show one yet. Until PR 4, the
   print check of a storefront item is today's (`printing.py:588-589` checks only that
   the file exists): a gone file is `library_file_gone`, and a changed file prints the
   live bytes. No order can reach that case before PR 6, which comes after PR 4, because
   no file listing is public before PR 6. The plate picture route of §9.3. After 2.
4. **`feat(orders): print a storefront item from its held copy`** (backend). §10.2: the
   check's `restore`, the upload into "Storefront", the item moved to the new id, the
   remembered choices. No button: the print does it. After 3.
5. **`feat(storefront): choose a library file as a listing's source`** (frontend). The
   Source block, the file picker with `types`, the Parts section, Pictures over plate
   pictures and uploads, the refusals, the file state notes with "Use the library's
   version", the ask before a pinned listing saves another file (§9.4), "Sell on
   storefront…" on the library page, the mocks. After 3.
6. **`feat(storefront): a customer page for a file listing`** (frontend, and the backend
   switch). The file branch of `CustomizePage`, the slot recolour in `Preview`, the price
   route client; this PR lets `public_card` and `public_listing` show file listings. After 3,
   4 and 5.
7. **`feat(agent): draft a file listing`** (agent, shared vectors). After 3.

Not in this split: #6922 (warm the slice price on publish and Prepare colours) warms
every published listing through publish, so it covers file listings once PR 3 lands. A
file listing needs no change of its own there.

Then the prod checks of §11.4, as a comment on #6710.

## 13. With the other storefront work

- **Pipeline listings (#5701).** Its PR 2 adds `inputs` and `inputs_v` to `ListingInput` and
  `inputs` to `ListingSnapshot` (pipeline spec §4.2). A file listing sets neither: the
  validator of §4.1 refuses `inputs` on a `library_file` source. Whichever lands second
  rebases its wire change on the other's. Neither changes the other's price: a pipeline
  variant is a `reference` subject.
- **The slice price.** PR 2 changes `price_key` and `slice_request`. scadify owns that code
  and reviews PR 2. PR 2 starts after scadify's #6921 merges and keeps the
  `variant_key` that #6921 writes on done rows. #6922 (warm the price on publish) covers
  file listings through publish. No other pricing change is in this design.
- **Easy listing (#6131).** "Sell on storefront…" from the library (§9.5) follows the same
  entry as Sell from a render. #6131's smart defaults do not apply: a file has no
  parameters to lock.

## 14. Decided (owner, 2026-11-21)

1. Decided (owner, 2026-11-21): a template variant's slice also takes the remembered
   print sequence, process overrides and settings per part, as proposed (§7.2). This
   closes a parity gap with the dialog, changes template prices where a model has such
   choices remembered, and re-slices every variant once (`KEY_VERSION` 4).
2. Decided (owner, 2026-11-21): a file listing has uploaded pictures in this version, as
   a template listing has, through the same `media_ids`, `ListingMedia` and
   `ListingPictures` (decision 11, §5.2 step 4, §5.4, §9.3).
3. Decided (owner, 2026-11-21): a file that changed or went after publish leaves the
   listing live on its pinned version. The owner asked whether ScadBuddy has file
   versioning. Templates have git history; library files (3MF, STL) have none. The
   listing's held copy (`listing-file-<sha256>`) is the version: the price slices it
   (§7.3.1), and the order item prints it (§10.2). The listing never follows the file by
   itself (§5.3, §9.4).

## 15. The pricing owner's constraints

| Constraint | Where it is met |
| --- | --- |
| 1. One path: `plan_variant` → `slice_and_price`, the same `opening_choices`, `printer_opening` and `read_type_presets`; no second slicer request | §7.1 (one `plan_variant` for both subjects), §7.3 (one `slice_and_price`, one claim per key, `start_library` as the back office's quote uses), §7.3.1 (only the bytes' source changes, and the copies name their version: the held copy through the same `start_library_quote_preview`) |
| 2. Support and process settings from the library item: its remembered choices for the file, else its own 3MF settings, as the back-office dialog opens on it | §7.1 (`library:<id>` scope, then the copy publish took when the scope holds none), §7.2 (the dialog's seeded fields; none remembered leaves the file's own) |
| 3. Key the price on the file's content sha256; bump `KEY_VERSION` if the key's meaning changes | §7.4 (`model` is the pinned sha256; `KEY_VERSION` 4) |
| 4. Extend `test_slice_parity.py`: same slicer request and price as a back-office quote of that file | §11.1 "Parity" |

## 16. Out of scope

- A sliced file (`gcode.3mf`) as a listing: it is fixed for one printer and one set of
  colours (§5.1 row 2).
- A customer who changes the quantity per plate, or picks plates.
- A batch that pre-prices every colour combination of a file listing (#6237).
- A listing that follows its library file by itself when the file changes (§14 item 3);
  the shop moves it with "Use the library's version" (§9.4).
- Template media on a file listing.
