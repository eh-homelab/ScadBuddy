# ruff: noqa: RUF001 -- the UI's own typographic apostrophes, which its tests match.
"""The name and help of every setting (#887): the one place they are written. ``GET
/settings`` answers them as ``docs`` (and the OpenAPI spec carries them as its default),
and the Settings page reads its labels and help from there.

Each entry is a field of :class:`scadbuddy.core.settings.Settings` (env-seeded or
bootstrap) or of :class:`scadbuddy.library.settings_store.SettingsPatch` (stored only).
``tests/test_setting_docs.py`` fails when a setting has no entry, an entry names no
setting, or a title or description is empty. The UI shows the title as the field's
label and the description under it, so a title must read well beside its control, and a
description says what the setting does, what happens when it is unset, and any security
note, in one to three short sentences.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, NamedTuple


class SettingDoc(NamedTuple):
    title: str
    description: str


_DECIMAL = "A decimal amount in the shop's currency, such as 1.50; 0 adds no line."
_TEMPORAL_QUEUE = (
    "The Temporal task queue {what} run on. The API and the worker that serves it must name"
    " the same queue."
)
_IMAGE_LAYOUT = "Unset, the image's own folder is used, or the repository's in a dev run."

SETTING_DOCS: Final[Mapping[str, SettingDoc]] = MappingProxyType(
    {
        # -- Connection --------------------------------------------------------------
        "bambuddy_url": SettingDoc(
            "Bambuddy URL",
            "Where ScadBuddy's server reaches Bambuddy. Every Bambuddy call is made from the"
            " server; the browser never calls Bambuddy. Unset, nothing can be sent or"
            " printed through Bambuddy.",
        ),
        "bambuddy_web_urls": SettingDoc(
            "Bambuddy web URLs",
            "Where browsers reach Bambuddy, when the URL above is one only ScadBuddy's server"
            " can. Comma-separated: links use the first, or whichever of them ScadBuddy is"
            " opened inside.",
        ),
        "bambuddy_api_key": SettingDoc(
            "API key",
            "Needs Read Status, Manage Library and Manage Queue; Manage Projects for"
            " projects, Manage Archives for print photos and Manage Inventory to record a"
            " tray's spool.",
        ),
        "bambuddy_render_api_key": SettingDoc(
            "Render worker API key",
            "A second key with Manage Library only. Render workers run template code and"
            " hold this key alone. Unset, standalone render workers get no Bambuddy key, and"
            " a render inside the API process uses the full key, so template code could"
            " print.",
        ),
        "public_url": SettingDoc(
            "ScadBuddy’s own URL",
            "What Bambuddy links to. ScadBuddy cannot infer it — it sits behind a proxy.",
        ),
        # -- Printing ----------------------------------------------------------------
        "default_printer": SettingDoc(
            "Printer",
            "The printer a print goes to when it names none. None set: a print must name its"
            " printer.",
        ),
        "printer_id": SettingDoc(
            "Default printer, by Bambuddy id",
            "An older client's way to set the default printer. A save keeps the registry"
            " printer that links this Bambuddy printer as the default printer.",
        ),
        "print_run_retention_seconds": SettingDoc(
            "Keep finished print runs for (days)",
            "Empty keeps every run, the start of print history. A number deletes runs that"
            " finished longer ago.",
        ),
        "operation_retention_seconds": SettingDoc(
            "Keep finished Bambuddy operations for (days)",
            "Sends, reprints, project filing and the like. Empty keeps every record; a number"
            " deletes those that finished longer ago. Keep it at least as long as Temporal's"
            " namespace retention, or a retried request whose record is gone is told it may"
            " have been done instead of its outcome.",
        ),
        "lan_require_plate_clear": SettingDoc(
            "Wait for a cleared plate between prints over the LAN",
            "On, a run of several plates or copies sent over the LAN waits after each print"
            " until someone says the plate is clear, on the Prints page, in the print dialog"
            " or through the assistant. Off, the next print starts once the printer has"
            " finished, as Bambuddy's queue does by default. A run keeps the setting it"
            " started with.",
        ),
        # -- Print watcher -------------------------------------------------------------
        "print_auto_import": SettingDoc(
            "Watcher",
            "Every 5 minutes, it reads the archives Bambuddy made since it was switched on."
            " It links a print to a model only when it is sure; every other print goes to"
            " Unsorted imports on the Prints page. Only a person can switch it.",
        ),
        "print_auto_import_sources": SettingDoc(
            "Take a print's 3MF from",
            "The watcher tries these in this order and takes the first that has the file."
            " With none, every print goes to Unsorted imports.",
        ),
        "print_auto_import_skip_printers": SettingDoc(
            "Leave these printers alone",
            "The watcher takes no print from a printer checked here.",
        ),
        "print_auto_import_skip_lan_printers": SettingDoc(
            "Leave these LAN-only printers alone",
            "A printer with no Bambuddy link has no archive. The watcher records its prints"
            " itself, and keeps the printer's file in ScadBuddy's own library. It records no"
            " print from a printer checked here.",
        ),
        # -- Projects & files ----------------------------------------------------------
        "library_folder_id": SettingDoc(
            "Inbox folder, for sends without a project",
            "Where a 3MF goes when it is sent without a project. A send to a project uses the"
            " project's own folder.",
        ),
        "last_project_id": SettingDoc(
            "Default project",
            "Where a print, Arrange or Generate is filed when what it holds is not all in one"
            " project already (then it opens on that project). A model opens on the project"
            " it was last filed in. None set: no project.",
        ),
        "destination_sort": SettingDoc(
            "Sort projects and folders by",
            "How the project and folder picker opens. It can be re-sorted there.",
        ),
        "store_backend": SettingDoc(
            "Blob store",
            "Where rendered pieces and other files are kept. This server's volume works with"
            " renders in the API only; the Bambuddy library and an S3 store work with any"
            " number of render workers. Takes effect when ScadBuddy and its render workers"
            " restart.",
        ),
        "library_backend": SettingDoc(
            "Print library",
            "Where print files are kept. Takes effect when the ScadBuddy API restarts; its"
            " workers need no restart. Switching moves no files: ScadBuddy’s own library"
            " starts empty, and Bambuddy’s files stay in Bambuddy.",
        ),
        "slicer_backend": SettingDoc(
            "Slicer",
            "Who slices the print dialog’s plates and the prints it queues. Takes effect at"
            " once. ScadBuddy’s slicer runs the Bambu Studio CLI in the scadbuddy-slicer"
            " worker; Bambuddy still queues and prints.",
        ),
        "store_s3_endpoint": SettingDoc(
            "Endpoint",
            "The S3 service’s URL, e.g. https://s3.us-east-1.amazonaws.com or a MinIO or"
            " Garage address.",
        ),
        "store_s3_region": SettingDoc(
            "Region",
            "The region requests are signed for; most self-hosted services take any.",
        ),
        "store_s3_bucket": SettingDoc(
            "Bucket",
            "An existing bucket. ScadBuddy does not create it.",
        ),
        "store_s3_prefix": SettingDoc(
            "Prefix",
            "Every object goes under this key prefix in the bucket.",
        ),
        "store_s3_access_key_id": SettingDoc(
            "Access key id",
            "The S3 access key's id. The S3 store needs it, with the secret below.",
        ),
        "store_s3_secret_access_key": SettingDoc(
            "Secret access key",
            "Sealed at rest, and never shown again once saved.",
        ),
        "store_s3_path_style": SettingDoc(
            "Path-style addressing",
            "The bucket in the path rather than the hostname. Most self-hosted services need"
            " it on.",
        ),
        "store_max_total_bytes": SettingDoc(
            "Blob store, total size",
            "A new file past this total is refused, unless the store already holds it. 0 is"
            " no limit. Applies on restart.",
        ),
        "store_max_count": SettingDoc(
            "Blob store, file count",
            "A new file past this count is refused, unless the store already holds it. 0 is"
            " no limit. Applies on restart.",
        ),
        "worker_cache_max_bytes": SettingDoc(
            "Render worker cache size",
            "How much of the pieces a worker fetched or rendered it keeps on its own disk."
            " The least recently used go first.",
        ),
        "read_max_triangles": SettingDoc(
            "Read at most this many triangles from a library file",
            "For Arrange, and a library file’s preview and print checks. 0 (the default) is"
            " no limit: any file is read, however large, so a huge one can use all of the"
            " server’s memory. Any other number refuses a file that expands to more, saying"
            " so. Every copy an object’s components place counts; one request may ask for"
            " its own.",
        ),
        "read_max_objects": SettingDoc(
            "Distinct objects read from a library file",
            "At most 2,000.",
        ),
        "read_max_visits": SettingDoc(
            "Objects visited reading a library file",
            "Components included: a small file whose components name one object many times"
            " expands to many visits. At most 200,000.",
        ),
        "read_max_paint_digits": SettingDoc(
            "Painting read from a library file (digits)",
            "Bambu Studio colour painting, counted once for every copy of a painted object."
            " At most 50,000,000.",
        ),
        # -- Uploads -------------------------------------------------------------------
        "media_upload_max_bytes": SettingDoc(
            "Largest media upload",
            "For template videos and print photos. The ingress in front of ScadBuddy has its"
            " own body limit; a value above it fails there instead.",
        ),
        "asset_max_total_bytes": SettingDoc(
            "File parameter uploads, total size",
            "The SVGs and PNGs attached to file parameters. 0 is no limit.",
        ),
        "asset_max_count": SettingDoc(
            "File parameter uploads, count",
            "How many SVGs and PNGs attached to file parameters are kept. 0 is no limit.",
        ),
        "asset_sweep_grace": SettingDoc(
            "Keep an unused upload for",
            "An upload no output, preset or render uses is removed after this long. At least"
            " an hour.",
        ),
        "asset_sweep_interval": SettingDoc(
            "Sweep unused uploads every",
            "How often the unused uploads are removed. 0 turns the sweep off.",
        ),
        "asset_fetch_domains": SettingDoc(
            "Fetch assets from",
            "The domains an SVG, PNG, STL, 3MF or OBJ may be fetched from by URL (the"
            " assistant's fetch_asset), a domain a line; each takes its subdomains, and every"
            ' redirect must stay on the list. Empty restores the defaults; "none" allows'
            " none.",
        ),
        "library_max_bytes": SettingDoc(
            "Largest OpenSCAD library checkout",
            "A library clone that grows past this size is stopped and refused. Every checkout"
            " shares the data volume.",
        ),
        # -- Rendering -----------------------------------------------------------------
        "render_timeout": SettingDoc(
            "Render timeout",
            "How long one openscad run may take before it is stopped.",
        ),
        "template_activity_max_timeout": SettingDoc(
            "Template activity limit",
            "The longest a template pipeline's own activity may run; a whole pipeline gets"
            " four times this.",
        ),
        "job_ttl": SettingDoc(
            "Keep finished renders for",
            "0 keeps them forever: the store then keeps every render’s files.",
        ),
        "render_concurrency": SettingDoc(
            "Renders at once",
            "How many render jobs each process runs at the same time. Each job runs one or"
            " more openscad processes. The others wait in the queue.",
        ),
        "solid_concurrency": SettingDoc(
            "Colours rendered at once, per job",
            "0 works it out from the CPUs of the container that renders: the render worker's"
            " when it runs on its own, else the API's.",
        ),
        "check_concurrency": SettingDoc(
            "Editor checks at once",
            "How many editor parse checks run at the same time. They do not go through the"
            " render queue, so they have their own limit.",
        ),
        "lsp_sessions": SettingDoc(
            "Editor language servers",
            "One per open editor. Past it, an editor goes without completion and hover.",
        ),
        "render_queue_max": SettingDoc(
            "Refuse renders past this many waiting",
            "0 accepts every render.",
        ),
        "preview_renders": SettingDoc(
            "Render a preview for models with no thumbnail",
            "On, a model with no thumbnail and no output is rendered at its default"
            " parameters in the background, and that render is its thumbnail.",
        ),
        "render_queue_depth_slo": SettingDoc(
            "Queue depth objective",
            "Exported for alerts; nothing is refused on reaching it.",
        ),
        "render_latency_slo": SettingDoc(
            "Render latency objective",
            "Exported for alerts; nothing is refused on reaching it.",
        ),
        "duplicate_staging_max_age": SettingDoc(
            "Treat an unfinished duplicate as abandoned after",
            "A model copy still unfinished after this long is taken as a crashed copy, and a"
            " sweep removes it. Shorter, a sweep could remove another replica's copy in"
            " flight.",
        ),
        "flow_approval_timeout_seconds": SettingDoc(
            "Deny a flow step waiting for approval after",
            "0 is never. A flow, or one of its runs, can set its own.",
        ),
        "orders_builtin_flow": SettingDoc(
            "Register the built-in order-fulfilment flow",
            "On, the API registers the built-in order-fulfilment flow at start when it has no"
            " version, or updates an unedited older built-in version.",
        ),
        # -- Fonts ---------------------------------------------------------------------
        "google_fonts_api_key": SettingDoc(
            "Google Fonts API key",
            "Optional. Without one the catalogue comes from fonts.google.com. A new key"
            " refetches the catalogue.",
        ),
        "fonts_catalogue_ttl": SettingDoc(
            "Refresh the font catalogue after",
            "How long the Google Fonts catalogue is kept before it is fetched again.",
        ),
        # -- Git remote ----------------------------------------------------------------
        "git_remote_url": SettingDoc(
            "Remote URL",
            "An http(s) git URL with no credentials in it. Models go to its `models` branch,"
            " flows to `flows`. Empty pushes nothing.",
        ),
        "git_remote_username": SettingDoc(
            "Username",
            "Who the token belongs to. Empty sends `git`, which GitHub and Gitea take with a"
            " token.",
        ),
        "git_remote_token": SettingDoc(
            "Token",
            "A token (or password) with write access to the remote, stored sealed under"
            " SCADBUDDY_SECRET_KEY_FILE and never shown again.",
        ),
        # -- Preview -------------------------------------------------------------------
        "display_unit": SettingDoc(
            "Show dimensions in",
            "For every model: the bounding box, plate sizes and fit warnings. Models,"
            " parameters and the files sent to Bambuddy stay in millimetres.",
        ),
        "default_plate": SettingDoc(
            "Default plate",
            "The plate the customizer draws and checks the model against until a printer is"
            " chosen in the print picker.",
        ),
        # -- Administration ------------------------------------------------------------
        "activity_follow": SettingDoc(
            "Notify me about activity from",
            "Principals whose finished activity rings the bell and shows a toast,"
            ' comma-separated, as stored: "browser" is this browser; others look like'
            ' "token:…" or an agent session\'s id. Empty is the default, "browser"; "none"'
            " follows nobody.",
        ),
        "activity_ttl_seconds": SettingDoc(
            "Keep activity for (seconds)",
            "86400 (a day) to 315360000 (ten years). Empty is the default, 30 days.",
        ),
        "activity_repair_window_seconds": SettingDoc(
            "Repair activity rows from the last (seconds)",
            "How far back the activity sweep looks for rows it missed. 600 to 604800. Empty"
            " is the default, one day.",
        ),
        "temporal_ui_url": SettingDoc(
            "Temporal UI URL",
            "The Temporal web UI, where render workflows can be inspected. Empty shows no link.",
        ),
        "spool_overlay_refresh_seconds": SettingDoc(
            "Refresh the spool providers' records every",
            "Bambuddy's spool records, copied on a schedule of their own so a print never"
            " waits on Bambuddy. 0 turns the refresh off.",
        ),
        # -- Diagnostics ---------------------------------------------------------------
        "log_level": SettingDoc(
            "Log level",
            "The least severe message the server logs: DEBUG, INFO, WARNING, ERROR or"
            " CRITICAL. DEBUG can be verbose.",
        ),
        "event_log_retention_seconds": SettingDoc(
            "Keep live-update events for",
            "For replay after a short disconnect. 0 is no limit.",
        ),
        "event_log_retention_rows": SettingDoc(
            "Keep at most this many live-update events",
            "Older events are pruned past this count. 0 is no limit.",
        ),
        "realtime_sockets": SettingDoc(
            "Live-update connections",
            "One per open tab.",
        ),
        # -- Orders --------------------------------------------------------------------
        "orders_currency": SettingDoc(
            "Orders currency",
            "The ISO 4217 code, such as USD or EUR, an order copies when it is made. A change"
            " applies to the next order; an existing order keeps its own.",
        ),
        "pricing_machine_rate_per_hour": SettingDoc(
            "Machine rate per hour",
            f"What an hour of print time costs, from the slice. {_DECIMAL}",
        ),
        "pricing_setup_fee": SettingDoc(
            "Setup fee per plate",
            f"Charged once for each plate an item prints. {_DECIMAL}",
        ),
        "pricing_labour_per_item": SettingDoc(
            "Labour per item",
            f"Charged once per unit. {_DECIMAL}",
        ),
        "pricing_markup_percent": SettingDoc(
            "Markup, percent",
            "A percentage of the other lines, such as 15 for 15 %. 0 adds no line.",
        ),
        "pricing_rounding": SettingDoc(
            "Round the unit price",
            "Every line is rounded to the minor unit once. Up to a whole unit then adds the"
            " difference as a line of its own.",
        ),
        "rough_estimate_factor": SettingDoc(
            "Rough estimate factor",
            "Multiplies a part's enclosed volume for walls, infill and purge in the estimate"
            " before a slice, such as 1.15. More than 0.",
        ),
        "pricing_labour_rate_per_hour": SettingDoc(
            "Labour rate per hour",
            "What an hour of your time costs a job. Each time entry keeps the rate in force"
            " when it was logged, so a change never rewrites past jobs. A decimal amount in"
            " the shop's currency, such as 25.",
        ),
        "pricing_usd_rate": SettingDoc(
            "One US dollar in the shop's currency",
            "Converts the assistant's spend, which is in US dollars, into a job's cost for"
            " orders in the shop's currency (#5330). 1 when the shop prices in US dollars."
            " More than 0.",
        ),
        "orders_thin_margin_percent": SettingDoc(
            "Thin margin below, percent",
            "An order or item whose margin is below this percent of what was charged is"
            " marked thin, in amber. Below 0 it is a loss, in red. Such as 15.",
        ),
        "orders_tax_percent": SettingDoc(
            "Tax, percent",
            "Added to an open order as one tax charge: this percent of its items, rounded"
            " half up, such as 8.25. At least 0 and below 100; 0 adds none. A tax charge"
            " typed on an order replaces it. A change applies to an order the next time its"
            " items or charges change.",
        ),
        "orders_tax_shipping": SettingDoc(
            "Tax shipping too",
            "On, the tax is also on the shipping charges. Fees and discounts are never taxed.",
        ),
        "orders_purchase_fee_flat_minor": SettingDoc(
            "Handling fee per bought item, minor units",
            "Added once for each thing the shop records as bought for a customer, in the"
            " minor unit of the orders currency, such as 150 for 1.50. An order in another"
            " currency takes only the percent below. 0 adds none.",
        ),
        "orders_purchase_fee_percent": SettingDoc(
            "Handling fee on bought items, percent",
            "Added as a percent of what the shop paid for the things it bought for a"
            " customer, such as 10 or 7.50. 0 to 100, at most two decimals. The flat fee and"
            ' this percent make one charge, "Handling for bought items", rounded half up'
            " once.",
        ),
        "orders_allow_unfulfil": SettingDoc(
            "Allow Unfulfil on a line item",
            "Off, a fulfilment is final: nobody can undo one, in the order page or through"
            " the agent. A change writes nothing to existing orders.",
        ),
        "orders_deposit_policy": SettingDoc(
            "Deposit policy",
            "The rules that decide the deposit an order asks for before work starts. None"
            " set: no deposit is asked.",
        ),
        "orders_customer_messaging": SettingDoc(
            "Customer messages",
            "The messaging plugin tool that sends order updates to customers. None set: the"
            " assistant only writes the message. The plugin's secrets stay in the agent.",
        ),
        # -- Storefront ----------------------------------------------------------------
        "storefront_turnstile_site_key": SettingDoc(
            "Storefront bot check: Turnstile site key",
            "Cloudflare Turnstile's public site key for the storefront's host. Set, the"
            " storefront asks for the check on design requests, submit, sign-in code"
            " requests and sign-ins, and refuses them while the secret below is not set."
            " Empty turns the check off.",
        ),
        "storefront_turnstile_secret": SettingDoc(
            "Storefront bot check: Turnstile secret key",
            "The widget's secret key, stored sealed under SCADBUDDY_SECRET_KEY_FILE and"
            " never shown again. Only this server sends it, to Cloudflare; the storefront"
            " and its page never see it.",
        ),
        "storefront_renders_per_minute": SettingDoc(
            "Storefront renders per visitor, per minute",
            "How many renders one storefront visitor may start in a minute. Past it, the"
            " storefront refuses the render. 1 to 60.",
        ),
        "storefront_renders_per_day": SettingDoc(
            "Storefront renders per visitor, per day",
            "How many renders one storefront visitor may start in a day. Past it, the"
            " storefront refuses the render. 1 to 10,000.",
        ),
        "storefront_render_concurrency": SettingDoc(
            "Storefront renders at once",
            "How many storefront renders may be open at the same time, for all visitors"
            " together. Past it, a new one is refused. 1 to 50.",
        ),
        "storefront_slices_concurrent": SettingDoc(
            "Slices at once",
            "How many storefront slices run at the same time, 1 to 4. The others wait. It"
            " takes effect once the storefront prices designs from a slice.",
        ),
        "storefront_draft_ttl_days": SettingDoc(
            "Remove idle storefront carts after (days)",
            "A storefront cart with no change for this many days is removed by the"
            " housekeeping sweep. 1 to 365.",
        ),
        "storefront_requests_enabled": SettingDoc(
            "Take design requests on the storefront",
            'On, visitors can "Ask for a design". Off, the storefront refuses a design request.',
        ),
        "storefront_materials": SettingDoc(
            "Storefront materials",
            "The materials a storefront print may be made of, in the shop's order of"
            " preference. A listing with no materials of its own takes these. At least one,"
            " at most 16.",
        ),
        "storefront_prepare_cap": SettingDoc(
            "Most colour combinations one prepare queues",
            'The most renders one "Prepare every colour combination" run of a listing'
            " queues. A listing with more is refused with its count. 1 to 4,096.",
        ),
        "storefront_shipping_options": SettingDoc(
            "Shipping options",
            "The ways an order reaches its customer, each with its price. None set: the"
            " storefront offers none.",
        ),
        "storefront_payment_instructions": SettingDoc(
            "Payment instructions",
            "How a customer pays by hand. It is shown as plain text, never as HTML.",
        ),
        "storefront_slice_profile": SettingDoc(
            "Shop slice profile",
            "The printer, and a spool per material, a storefront variant is sliced with."
            " None set: nothing is sliced, so no visitor sees a price.",
        ),
        # -- Storefront mail -----------------------------------------------------------
        "storefront_mail_host": SettingDoc(
            "SMTP host",
            "Such as smtp.example.com. Empty sends no mail: order links and sign-in codes"
            " wait, and order updates are left to a plugin. Only a person can change the"
            " mail connection.",
        ),
        "storefront_mail_port": SettingDoc(
            "SMTP port",
            "Usually 587 with STARTTLS.",
        ),
        "storefront_mail_user": SettingDoc(
            "SMTP user",
            "The login. Empty sends without one.",
        ),
        "storefront_mail_sender": SettingDoc(
            "Sender",
            "The From address of every mail, such as shop@example.com.",
        ),
        "storefront_mail_tls": SettingDoc(
            "TLS",
            "STARTTLS needs a server that offers it. A login is never sent without TLS.",
        ),
        # -- Deployment only (BOOTSTRAP_FIELDS) ----------------------------------------
        "openscad": SettingDoc(
            "OpenSCAD binary",
            "The openscad program the server runs for renders and checks.",
        ),
        "openscad_lsp": SettingDoc(
            "OpenSCAD language server binary",
            "The openscad-lsp program the editor's completion and hover run on.",
        ),
        "data_dir": SettingDoc(
            "Data folder",
            "The volume that holds the models, flows, library checkouts and caches.",
        ),
        "database_url": SettingDoc(
            "Database URL",
            "The Postgres database, as a libpq URL. Required: the settings and the render"
            " queue live there. It holds a credential, so Settings shows only the host and"
            " database.",
        ),
        "database_pool_size": SettingDoc(
            "Database connections",
            "How many Postgres connections this process keeps open. At least 1.",
        ),
        "secret_key_file": SettingDoc(
            "Secret key file",
            "A file with 32 random bytes in base64, which seals the stored API keys and"
            " tokens. Unset, most of them are stored in plain text, with a warning at start,"
            " and a secret that is only ever stored sealed (such as the Git remote token or"
            " the bot check secret) cannot be saved.",
        ),
        "secret_key_previous_file": SettingDoc(
            "Previous secret key file",
            "During a key rotation, the key before the current one. It opens virtual"
            " printers' access codes that were sealed before the rotation.",
        ),
        "allowed_origins": SettingDoc(
            "Allowed origins",
            "Comma-separated origins the UI is also served under, besides the public URL's."
            " A page from another origin cannot open live updates or save anything.",
        ),
        "trusted_proxies": SettingDoc(
            "Trusted proxies",
            "Comma-separated CIDRs whose X-Forwarded-For header is believed, and then only"
            " its last value. Empty, no forwarding header is believed.",
        ),
        "auth_mode": SettingDoc(
            "Sign-in mode",
            "off serves a request with no session or credential, observe serves and counts"
            " it, enforce refuses it. In enforce the API does not start without a usable"
            " login.",
        ),
        "auth_oidc_issuer": SettingDoc(
            "Sign-in issuer",
            "The OpenID Connect identity provider the browser is sent to sign in, and whose"
            " tokens are believed.",
        ),
        "auth_oidc_client_id": SettingDoc(
            "Sign-in client id",
            "ScadBuddy's client id at the sign-in issuer.",
        ),
        "auth_oidc_private_network": SettingDoc(
            "Sign-in issuer on a private network",
            "On, the issuer's discovery, keys and token endpoint may be on a private address."
            " Off, they are fetched only from public addresses.",
        ),
        "service_token_file": SettingDoc(
            "Service account token file",
            "On a worker: its projected Kubernetes ServiceAccount token, which it exchanges"
            " for a service credential. Set this or the bootstrap file, not both.",
        ),
        "service_bootstrap_file": SettingDoc(
            "Service bootstrap secret file",
            "On a worker outside Kubernetes: its bootstrap secret, which it exchanges for a"
            " service credential. Set this or the token file, not both.",
        ),
        "service_bootstrap_dir": SettingDoc(
            "Service bootstrap secrets folder",
            "On the API: one bootstrap secret file per caller; only their hashes are kept."
            " Unset, no bootstrap secret is accepted.",
        ),
        "service_account_namespace": SettingDoc(
            "Service account namespace",
            "On the API: the Kubernetes namespace whose ScadBuddy ServiceAccounts may"
            " exchange for a service credential. Unset, no Kubernetes token is accepted.",
        ),
        "crud_port": SettingDoc(
            "CRUD port",
            "The second port the API serves, for the workers' CRUD calls, the service-token"
            " exchange and /healthz only. 0 is any free port.",
        ),
        "seed_models_dir": SettingDoc(
            "Bundled models folder",
            f"The models the release ships with. {_IMAGE_LAYOUT}",
        ),
        "seed_libraries_dir": SettingDoc(
            "Bundled libraries folder",
            "The library checkouts the release ships with. Unset, the image's own folder is"
            " used; a dev run has none.",
        ),
        "frontend_dir": SettingDoc(
            "Web UI folder",
            f"The built web UI the server serves. {_IMAGE_LAYOUT}",
        ),
        "temporal_address": SettingDoc(
            "Temporal address",
            "The Temporal frontend's host:port. Required: every render runs on Temporal.",
        ),
        "temporal_namespace": SettingDoc(
            "Temporal namespace",
            "The Temporal namespace ScadBuddy's workflows run in.",
        ),
        "temporal_task_queue_render": SettingDoc(
            "Render task queue",
            _TEMPORAL_QUEUE.format(what="renders"),
        ),
        "temporal_task_queue_bambuddy": SettingDoc(
            "Print task queue",
            _TEMPORAL_QUEUE.format(what="print runs and Bambuddy writes"),
        ),
        "temporal_task_queue_library": SettingDoc(
            "Housekeeping task queue",
            _TEMPORAL_QUEUE.format(what="the housekeeping sweeps"),
        ),
        "temporal_task_queue_projects": SettingDoc(
            "Flow task queue",
            _TEMPORAL_QUEUE.format(what="flow runs"),
        ),
        "temporal_task_queue_agent_tools": SettingDoc(
            "Agent tools task queue",
            _TEMPORAL_QUEUE.format(what="the agent service's tools"),
        ),
        "temporal_task_queue_slicer": SettingDoc(
            "Slicer task queue",
            _TEMPORAL_QUEUE.format(what="the scadbuddy-slicer worker's slices"),
        ),
        "temporal_task_queue_customer_files": SettingDoc(
            "Customer files task queue",
            _TEMPORAL_QUEUE.format(what="the checks of a customer's uploaded files"),
        ),
        "temporal_sdk_metrics_port": SettingDoc(
            "Temporal SDK metrics port",
            "The loopback port where a worker's Temporal SDK exports its metrics, which the"
            " worker's /metrics adds. 0 turns it off.",
        ),
        "temporal_search_attributes": SettingDoc(
            "Write Temporal search attributes",
            "On, workflows set ScadBuddy's search attributes. Turn it on only once the"
            " namespace has them registered, or the workflows fail.",
        ),
        "temporal_worker_inprocess": SettingDoc(
            "Run the render worker in the API",
            "On, the API process serves the render queue itself, for one replica in dev and"
            " tests. Production runs the worker as its own Deployment.",
        ),
        "temporal_print_worker_inprocess": SettingDoc(
            "Run the print worker in the API",
            "On, the API process serves the print queue itself, for a deployment without the"
            " scadbuddy-print worker.",
        ),
        "temporal_projects_worker_inprocess": SettingDoc(
            "Run the flow worker in the API",
            "On, the API process serves the flow queue itself, for a one-process dev run.",
        ),
        "agent_internal_url": SettingDoc(
            "Agent sidecar URL",
            "Where the API reaches the agent sidecar in its own pod. The flow routes read the"
            " agent's tools from it, and refuse when it does not answer.",
        ),
        "api_internal_url": SettingDoc(
            "API internal URL",
            "Where the workers reach the API inside the cluster. The API also makes the"
            " native slicer's transfer URLs under it.",
        ),
        "slicer_profiles_dir": SettingDoc(
            "Slicer profiles folder",
            "Bambu Studio's BBL vendor profiles and version, which the native slicer's"
            " preset files are built from.",
        ),
        "download_cache_dir": SettingDoc(
            "Download cache folder",
            "Where whole Bambuddy downloads are kept, by checksum. Unset, the API keeps them"
            " on its data volume and a worker keeps none.",
        ),
        "download_cache_max_bytes": SettingDoc(
            "Download cache size",
            "How much of the Bambuddy downloads the cache keeps. The least recently used go first.",
        ),
        "storefront_public_url": SettingDoc(
            "Storefront URL",
            "The storefront edge's https origin. The links in the storefront's mails, and"
            " the pay page's link, use it.",
        ),
        "payments_fake_provider": SettingDoc(
            "Offer the fake payment provider",
            "On, a fake payment provider that needs no network is offered, for dev runs and"
            " tests. Never turn it on in production.",
        ),
        "revision": SettingDoc(
            "Build revision",
            "The commit this image was built from. /healthz reports it.",
        ),
        "version": SettingDoc(
            "Build version",
            "The image tag this process was built from. /healthz reports it.",
        ),
    }
)
