# Template Gallery (Epic #273) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Templates carry an ordered set of images and videos. The catalogue can be searched and filtered, shown as cards (with an inline carousel) or as a list, and opens media in a lightbox. The template page shows the same gallery.

**Architecture:** Media files live beside the template in `data/models/<slug>/media/`, and their order and captions live in Postgres (`template_media`; a built-in's in its bundled `model.json` under `media`). A new router serves, uploads, reorders and deletes them behind a per-route body gate. One `MediaCarousel` and one `MediaLightbox` are shared by the catalogue (cards and list) and the template page. The catalogue's filter and view state lives in the URL.

**Tech Stack:** FastAPI + pydantic (backend, Python 3.12, uv), React 19 + Vite + Tailwind, msw, vitest, Playwright, `embla-carousel-react`, `yet-another-react-lightbox`.

**Spec:** the issue bodies are the spec: #273 (epic), #274, #275, #276, #277, #278, #279, #280. The decisions below settle the epic's open questions.

## Decisions (settle the epic's open questions)

1. **Media in git history.** Images (`png`, `jpg`, `jpeg`, `webp`) are committed with the template, like `thumbnail.png` today. Videos (`mp4`, `webm`) are *not*: the models-repo `.gitignore` (`library/history.py` `_gitignore_body`) gains `*/media/*.mp4` and `*/media/*.webm`, and the same patterns under `_builtin/`. **The list (order, captions, posters) is not in git: it is Postgres** (ruling: new state goes to Postgres, not files). A template of mine's list is the `template_media` table (backend `MIGRATIONS`, number 5, after #408 took 3 and the event log (#374) took 4). A restore therefore brings back an image's file but not its row: a file with no row is ignored (orphan sweep: #465), and a row whose file is gone is reported `missing: true` and the UI skips it. Writes put the file first, then the row, and remove the file if the insert fails. Without `SCADBUDDY_DATABASE_URL` (until #401 makes it required) only the legacy `thumbnail.png` is listed and media writes answer 503.
2. **Libraries.** Use `embla-carousel-react` for the carousel and `yet-another-react-lightbox` for the lightbox, with its Video, Zoom and Captions plugins. Record the added gzip size in the #275 PR. If it exceeds 40 kB gz, lazy-load the lightbox with `React.lazy`, since it is only needed on click.
3. **No autoplay.** A carousel shows a video's poster (or a play badge on a neutral tile) and never plays inline. Videos play only in the lightbox.
4. **Legacy thumbnail.** When a template has no media rows and `thumbnail.png` exists, the API synthesizes one image item `{id: "thumbnail", file: "thumbnail.png", kind: "image"}` with no migration. The first media write converts it: `thumbnail.png` moves to `media/<id>.png`, so there is still one cover and no duplicate. `GET /models/{slug}/thumbnail` keeps working and serves the cover: the first image, or the first video's poster, else the existing output plate-cover fallback (#179).
5. **Upload limit.** Add `media_upload_max_bytes: int = 1 GiB` to `core/settings.py`, env only (`SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES`). New state is not added to files (`settings.json`); a UI override waits for the Postgres settings table (#455/#322). `GET /settings` reports the value read-only as `media_upload_max_bytes`, for the client-side oversize check (#279).
6. **Built-ins.** Bundled models may ship `media/` plus `model.json` `media`; `sync_builtins` mirrors them like any other file. Built-in media is read-only (403 on writes, as for the source). `duplicate` copies `media/`, including video files that are not in git. A built-in's list is read from its bundled `model.json` `media` (shipped read-only content, not state); a duplicate copies it into `template_media` rows of its own.
7. **In-browser agent tools.** If `frontend/src/pages/agentTools*` exposes catalogue or navigation tools, the new URL params (`q`, `tag`, `origin`, `sort`, `view`) must be settable through the existing navigate tool. Do not add new agent tools in this epic.

## Global Constraints

- Every backend change passes: `uv run --frozen ruff check .`, `ruff format --check .`, `mypy` (strict), `pytest` in `backend/`.
- Every frontend change passes: `pnpm lint && pnpm typecheck && pnpm test && pnpm build` and `pnpm exec playwright test` in `frontend/`.
- API model or route changes regenerate, in this order: `uv run --frozen python -m scadbuddy.tools.export_openapi`, then `pnpm gen:api`, then `pnpm exec msw init public --save` (CLAUDE.md).
- The API key never reaches the browser; downloads go through `lib/embed.ts`; there is no Fullscreen API. Everything must work inside Bambuddy's sandboxed iframe.
- `localStorage` access is wrapped in try/catch; the page must work with it throwing.
- PR titles are conventional commits (`feat(catalogue): …`), and each body says `Fixes #N`. One PR per issue.
- Node 24, pnpm via corepack; add dependencies with `pnpm add` so the lockfile updates.

## Review Focus

- **A video whose file is gone** (removed by hand, or lost with the volume, since videos are not in git) → the item is `missing: true`, it is skipped in carousels, and the edit page shows it with a "file missing — remove" action. Test in #274 (API) and #279 (UI).
- **A 1 GiB upload with no Content-Length (chunked)** → refused by the per-route gate as soon as it crosses the limit, never buffered in memory. The global 32 MiB multipart cap still applies to every other route. Test in #274.
- **Rapid clicks on carousel arrows inside a card** → the slide changes; there is no navigation and no lightbox. Keyboard Enter on the focused *media* opens the lightbox; Enter on an arrow does not. Test in #275 (unit) and #277 (e2e).
- **A search with accents or mixed case** (`"Crème"` vs `"creme"`), and a tag with spaces or `&` in the URL, round-trip correctly through `?tag=`. Test in #276.
- **A template with zero media and no plate-cover fallback** → the placeholder is shown in the card, the list and the template page, with no empty carousel chrome. Test in #275.

---

## Shared contracts (every task reads this)

### Backend model (`backend/scadbuddy/library/media.py`, created in #274)

```python
MediaKind = Literal["image", "video"]

class MediaItem(BaseModel):
    id: str            # 12 hex chars; "thumbnail" only for the synthesized legacy item
    file: str          # file name inside media/ (or "thumbnail.png" for legacy)
    kind: MediaKind
    caption: str = ""
    poster: str | None = None   # file name of a poster image in media/, videos only

class MediaView(MediaItem):
    missing: bool = False       # entry present, file absent (videos after a restore)
    content_type: str
    size: int | None            # bytes; None when missing
```

`ModelMeta` gains `media: list[MediaItem] = []`, and `ModelRecord` gains `media: list[MediaView]` (in order; the first item is the cover).

### Routes (`backend/scadbuddy/api/media.py`, new router, mounted under `/api/v1`)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/models/{slug}/media/{id}` | – | the file; supports `Range` (206); `Cache-Control: private, max-age=31536000, immutable` (ids never change contents) |
| GET | `/models/{slug}/media/{id}/poster` | – | the poster image, 404 if none |
| POST | `/models/{slug}/media` | multipart `file` (+ optional `poster`, `caption`) | `ModelRecord` |
| PATCH | `/models/{slug}/media/{id}` | `{"caption": str}` | `ModelRecord` |
| PUT | `/models/{slug}/media/order` | `{"ids": [str, ...]}`, a permutation of the current ids | `ModelRecord` |
| DELETE | `/models/{slug}/media/{id}` | – | `ModelRecord` |

- Writes to `builtin:*` return 403, using the problem type the source routes already use.
- Types are checked by magic bytes: PNG, JPEG, WebP, MP4 (`ftyp` box at byte 4), WebM (EBML `1A 45 DF A3`). Anything else gets a 415.
- An oversized upload gets a 413 naming the limit in MB.

### Frontend (`frontend/src/api/types.ts`)

```ts
export type MediaView = Schemas['MediaView']
```

In `client.ts`:
- `mediaUrl(slug, item)` returns `/api/v1/models/{slug}/media/{id}`.
- `mediaPosterUrl(slug, item)` returns the poster URL.
- `uploadMedia(slug, file, {caption?, poster?}, onProgress?)` uses XHR for progress.
- `patchMedia`, `reorderMedia` and `deleteMedia` return `ModelSummary`.

### Components (`frontend/src/components/media/`, created in #275)

```ts
type Slide = { key: string; kind: 'image' | 'video'; src: string; poster?: string; alt: string; caption?: string }

function toSlides(slug: string, media: MediaView[]): Slide[]      // skips missing
function MediaCarousel(props: { slides: Slide[]; onOpen?: (index: number) => void;
  fallback?: React.ReactNode; className?: string; label: string }): JSX.Element
function MediaLightbox(props: { slides: Slide[]; index: number | null;
  onClose: () => void }): JSX.Element | null
```

- `MediaCarousel` with 0 slides renders `fallback`, and with 1 slide renders no controls.
- A slide change never calls `onOpen`. Only a click, or Enter/Space, on the media element does.
- `MediaLightbox` is closed when `index === null`.

### Catalogue URL state (`frontend/src/lib/catalogueQuery.ts`, created in #276)

```ts
type CatalogueQuery = { q: string; tags: string[]; origin: 'all' | 'builtin' | 'mine';
  sort: 'updated' | 'name'; view: 'cards' | 'list' }
function parseQuery(params: URLSearchParams): CatalogueQuery
function toParams(query: CatalogueQuery): URLSearchParams      // omits defaults
function filterModels(models: ModelSummary[], query: CatalogueQuery): ModelSummary[]
function tagCounts(models: ModelSummary[]): Array<{ tag: string; count: number }>
```

Matching folds case and accents (NFD, strip `\p{Diacritic}`). Several tags are ANDed. `view` is read by #278; #276 only parses it and carries it through.

---

## Waves and ownership

| Wave | Issue | Branch | Depends on |
|---|---|---|---|
| 1 | #274 media storage + API + upload-limit setting | `feat/274-template-media` | – |
| 1 | #276 search / tags / origin / sort | `feat/276-catalogue-filters` | – |
| 2 | #275 MediaCarousel + MediaLightbox | `feat/275-media-components` | #274 merged (for `MediaView`) |
| 3 | #277 card mode | `feat/277-card-carousel` | #275, #276 |
| 3 | #278 list mode + toggle | `feat/278-list-mode` | #275, #276 |
| 3 | #279 edit page media management | `feat/279-media-management` | #274 (+ #275 for preview) |
| 3 | #280 template page gallery | `feat/280-template-gallery` | #275 |

Each issue is one task, carried out with TDD by its implementer. The steps below are the per-task checklists. Implementers write the tests first, from each issue's "Done when" list and the Review Focus lines assigned to it.

### Task 1 (#274): media storage and API

**Files:**
- Create: `backend/scadbuddy/library/media.py` (the models above; `sniff_kind(head: bytes) -> tuple[MediaKind, str ext, str content_type] | None`; `MediaStore` helpers on the `Catalogue`: `media_dir(slug)`, `list_media(slug) -> list[MediaView]`, `add_media(slug, spooled_path, filename, caption, poster_path) -> ModelRecord`, `set_caption`, `reorder`, `remove_media`)
- Create: `backend/scadbuddy/api/media.py` (the router)
- Modify:
  - `backend/scadbuddy/library/catalogue.py`: `ModelMeta.media`, `ModelRecord.media`, `_record`, `thumbnail`/`thumbnail_source` cover logic, `duplicate` copies `media/`, `_clear_derived` if relevant;
  - `backend/scadbuddy/library/history.py`: `_gitignore_body` gains the video patterns;
  - `backend/scadbuddy/api/limits.py`: a per-route gate, a `MEDIA_UPLOAD_PATH` regex exempt from the global multipart cap and checked against `settings.media_upload_max_bytes` instead;
  - `backend/scadbuddy/core/settings.py`, `backend/scadbuddy/library/settings_store.py`: the setting;
  - the router include wherever `api/models.py` is included.
- Test: `backend/tests/api/test_media.py`, `backend/tests/test_media_store.py`
- Regenerate: `backend/openapi.json`, `frontend/src/api/schema.d.ts`. Extend the `frontend/src/mocks/fixtures.ts` + `handlers.ts` media routes, with one fixture template holding 3 images and 1 video, one holding legacy only, and one with none.
- Frontend: `types.ts` + `client.ts` additions.

Checklist:
- [ ] Tests first:
  - a legacy `thumbnail.png` is listed as one item;
  - upload PNG/JPEG/WebP/MP4/WebM succeeds, and a text file → 415;
  - `Range: bytes=0-99` → 206 with 100 bytes;
  - reorder with a non-permutation → 422;
  - delete removes the file and the entry;
  - a builtin write → 403;
  - duplicate copies media, including a video file;
  - the video is not in the git tree after commit (`git ls-files`);
  - an entry whose file is gone → `missing: true`;
  - a chunked body over the limit → 413 before full read;
  - another multipart route is still capped at 32 MiB;
  - the cover changes `GET /thumbnail` after a reorder;
  - `media_upload_max_bytes` comes from the environment and is reported read-only by `GET /settings`.
- [ ] Implement until green; run the full backend CI set.
- [ ] Regenerate the generated files; add mocks; add vitest for the client helpers.
- [ ] Commit, push, open the PR `feat(media): multiple images and videos per template` with `Fixes #274`.

### Task 2 (#276): catalogue search and filters

**Files:**
- Create: `frontend/src/lib/catalogueQuery.ts` + `catalogueQuery.test.ts`; `frontend/src/components/CatalogueFilters.tsx` + test
- Modify: `frontend/src/pages/CataloguePage.tsx` (read the query via `useSearchParams`; render filters, count, "Clear filters" and the no-results state; card tag chips become buttons that add the tag, placed outside the card `<Link>`, as `origin_url` already is)
- Test: `frontend/e2e/catalogue-filters.spec.ts`

Checklist:
- [ ] Unit tests first:
  - `parseQuery`/`toParams` round-trip, including a tag with a space and `&`;
  - defaults are omitted;
  - an accent- and case-folded match on name, description and tags;
  - multi-tag AND;
  - origin;
  - sort by name (locale compare) and by `updated_at` descending;
  - `tagCounts` is sorted by count descending, then by name.
- [ ] Component tests:
  - the debounced search updates the URL;
  - `/` focuses search (not while typing in an input);
  - a chip toggles a tag;
  - the no-results state is distinct from the no-models state.
- [ ] e2e: clicking a card's tag chip filters, and the URL plus the "N of M" count update.
- [ ] Commit, push, open the PR `feat(catalogue): search, tag, origin filters and sort` with `Fixes #276`.

### Task 3 (#275): shared carousel and lightbox
- [ ] `pnpm add embla-carousel-react yet-another-react-lightbox`, then record the bundle delta from `pnpm build` output in the PR body.
- [ ] Tests first (vitest + Testing Library):
  - 0 slides → fallback;
  - 1 slide → no controls;
  - next/prev/dots change the slide without calling `onOpen`;
  - a click on the media calls `onOpen(i)`;
  - ArrowLeft/Right when the region is focused;
  - `aria-roledescription="carousel"` and "2 of 5" labels;
  - a video slide shows the poster and a play badge with no `<video autoplay>`;
  - the lightbox opens at the index, Esc closes it, and focus returns to the trigger;
  - under `prefers-reduced-motion` embla's `duration` is 0.
- [ ] Implement in `frontend/src/components/media/{MediaCarousel,MediaLightbox,slides}.tsx`. Replace nothing yet.
- [ ] An e2e smoke test in embedded (iframe) mode, if the mocked e2e has an embedded fixture; otherwise run the unit tests only.
- [ ] Commit, push, open the PR `feat(media): shared MediaCarousel and MediaLightbox` with `Fixes #275`.

### Task 4 (#277): card mode
- [ ] Restructure `ModelCard`:
  - the title `<Link>` gets a stretched `::after` covering the card body, but not the media region or the action row;
  - the media region is a `MediaCarousel` with `fallback={<ModelThumbnail …/>}` and `onOpen` → the page-level `MediaLightbox`.
- [ ] e2e:
  - next changes the slide, the URL is unchanged and there is no lightbox;
  - a click on the image opens the lightbox;
  - a click on the title navigates;
  - rapid arrow clicks do not navigate.
- [ ] PR `feat(catalogue): inline media carousel on cards` with `Fixes #277`.

### Task 5 (#278): list mode and toggle
- [ ] A `view` toggle in the header (a segmented control), reading and writing `?view=`. Persist it to `localStorage['scadbuddy.catalogue.view']` inside try/catch, and restore it only when the URL has no `view`.
- [ ] A `ModelRow`: cover thumbnail (button, opens lightbox at 0) + count badge, name link, built-in badge, one-line description, tags (the #276 chip buttons), updated time, Duplicate. On narrow widths it keeps the thumbnail, name and tags.
- [ ] e2e: toggle to List and reload → still List; a thumbnail click opens the lightbox; a name click navigates.
- [ ] PR `feat(catalogue): list mode and view toggle` with `Fixes #278`.

### Task 6 (#279): edit page media management
- [ ] On `EditPage.tsx`, a `MediaManager` component:
  - drop zone plus file picker (`accept="image/png,image/jpeg,image/webp,video/mp4,video/webm"`);
  - per-file progress;
  - a refusal before upload when a file is over the limit read from `GET /settings`;
  - drag reorder with Move up/down buttons;
  - "Make cover";
  - caption edit on blur;
  - delete with confirm;
  - a missing item shown with "file missing — remove".
- [ ] Built-ins: media is shown read-only, with the Duplicate CTA.
- [ ] `UploadDialog` accepts several images and videos (the first becomes the cover), uploaded after create.
- [ ] Vitest against msw: add, reorder, make cover, caption, delete, missing, builtin read-only, and an oversize refusal.
- [ ] PR `feat(media): manage template media on the edit page` with `Fixes #279`.

### Task 7 (#280): template page gallery
- [ ] On `CustomizePage.tsx`, a "Gallery" tab beside the 3D preview (a tab on narrow widths, and a thumbnail strip under the preview on wide ones). It uses `MediaCarousel` + `MediaLightbox`. The live preview stays the default view, and there is no gallery chrome when a template has no media.
- [ ] Tests: a template with images and a video shows all of them; the preview is unaffected with no media.
- [ ] PR `feat(customize): media gallery beside the live preview` with `Fixes #280`.
