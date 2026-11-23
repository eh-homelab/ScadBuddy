"""A local replica of the parts of 3dfilamentprofiles.com that `3dfp.mjs` and
`scadbuddy.profile_fetch.site` drive (#6625 Task 5, plan Decision 17). It serves the pages
beside this file on 127.0.0.1 and records what the browser did, so the tests never reach
the real site."""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).parent
EXPORT_FILE = HERE / "export-h2c-0.4.json"
#: The name the replica's "Download" gives the exported file.
EXPORT_NAME = "Inland PLA+ @BBL H2C 0.4 nozzle.json"
SLICERS = ["Bambu Studio", "OrcaSlicer"]
PRINTERS = ["H2C", "X1C"]
#: No ``0.2 mm``: `test_an_option_the_dialog_lacks_is_reported`.
NOZZLES = ["0.4 mm", "0.6 mm"]
#: #7025: three of the seven rows the live search for "SUNLU Silk Bronze" listed on
#: 2026-11-22.
FILAMENTS: list[dict[str, Any]] = [
    {
        "id": 8916,
        "brand": "SUNLU",
        "material": "PLA",
        "type": "Silk",
        "name": "Bronze",
        "hexes": ["#578A61"],
    },
    {
        "id": 38537,
        "brand": "SUNLU",
        "material": "PLA+/Pro",
        "type": "Silk",
        "name": "BRONZE (250g)",
        "hexes": ["#779E7C"],
    },
    {
        "id": 1708,
        "brand": "SUNLU",
        "material": "PLA+/Pro",
        "type": "Silk",
        "name": "Bronze",
        "hexes": ["#689471"],
    },
]
#: The results table's columns, as measured.
COLUMNS = ["Brand", "Material", "Type", "Color", "RGB", "TD", "Retailer", "Actions"]
#: The add form's lists: brands, materials, types.
ADD_LISTS = {
    "brand": ["SUNLU", "eSUN", "Inland"],
    "material": ["PLA", "PLA+/Pro", "PETG"],
    "material_type": ["Basic", "Silk", "Silk Dual", "Kevlar"],
}


@dataclass
class Replica:
    """The site's state, and what the tests read back. ``checkpoint`` serves Vercel's
    check (429) on every page: ``never`` never clears it, ``clears`` clears it once its
    script has run. ``login_form`` ``disabled`` makes the form's fields uneditable."""

    email: str
    password: str
    known_ids: set[int] = field(default_factory=lambda: {30173})
    checkpoint: Literal["off", "never", "clears"] = "off"
    login_form: Literal["ok", "disabled"] = "ok"
    #: Set, the form signs in as the real site does (#6941): its script posts the login to
    #: ``/api/login``, which sets the session cookie after this many seconds, then the page
    #: goes to ``/my/dashboard``. Unset, the form posts itself to ``/login``.
    scripted_login_delay_s: float | None = None
    #: The three option lists of the export dialog: slicer, printer, nozzle.
    options: list[list[str]] = field(default_factory=lambda: [SLICERS, PRINTERS, NOZZLES])
    export_name: str = EXPORT_NAME
    #: Characters of white space the export's text gets at its end (a file too large).
    export_pad: int = 0
    #: How long an opened combobox waits before it shows its options.
    option_delay_ms: int = 0
    #: ``unhooked`` starts the download with a synthetic click event, which the driver's
    #: blob hook (it patches ``HTMLAnchorElement.prototype.click``) does not see. The page's
    #: own script can also bypass the hook and push to ``window.__dl`` itself (#6726):
    #: ``pushed_entry`` pushes ``{name, text}`` with no ``tooLarge`` flag, ``pushed_text``
    #: pushes the bare text, an entry that is not an object, ``pushed_long_name`` an entry
    #: whose name is over 1024 characters, ``pushed_number_text`` one whose text is a number.
    download: Literal[
        "hooked",
        "unhooked",
        "pushed_entry",
        "pushed_text",
        "pushed_long_name",
        "pushed_number_text",
    ] = "hooked"
    sessions: set[str] = field(default_factory=set)
    in_spools: set[int] = field(default_factory=set)
    logins: int = 0
    adds: list[int] = field(default_factory=list)
    exports: list[dict[str, str]] = field(default_factory=list)
    header_user_agents: list[str] = field(default_factory=list)
    page_user_agents: list[str] = field(default_factory=list)
    posted_bodies: list[str] = field(default_factory=list)
    #: #7025: what ``/filaments?q=`` lists, and its table's columns.
    filaments: list[dict[str, Any]] = field(default_factory=lambda: list(FILAMENTS))
    columns: list[str] = field(default_factory=lambda: list(COLUMNS))
    searches: list[str] = field(default_factory=list)
    #: The add form's lists; ``brand_select`` False leaves the brand out of its native
    #: select, so only typing into the combobox finds it.
    add_lists: dict[str, list[str]] = field(default_factory=lambda: dict(ADD_LISTS))
    brand_select: bool = True
    #: The ``brand|material|type`` keys that have site defaults.
    defaults: set[str] = field(default_factory=lambda: {"SUNLU|PLA+/Pro|Silk"})
    #: The add form's duplicate list, as `filaments` rows.
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    #: The add form's identifier fields beyond ``#sku-0``: ``(id, label)``.
    extra_fields: list[tuple[str, str]] = field(
        default_factory=lambda: [("gtin", "GTIN / UPC"), ("asin", "ASIN")]
    )
    #: False: "Save Filament" saves but stays on the form.
    save_lands: bool = True
    created: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 40000

    def expire_sessions(self) -> None:
        """Every session ends: the next page redirects to ``/login``."""
        self.sessions.clear()


def _page(name: str, **values: str) -> bytes:
    text = (HERE / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace(f"__{key}__", value)
    return text.encode()


def _handler(site: Replica) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, code: int, body: bytes, kind: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, to: str, cookie: str | None = None) -> None:
            self.send_response(303 if self.command == "POST" else 302)
            self.send_header("Location", to)
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _cookies(self) -> dict[str, str]:
            pairs = (
                part.strip().split("=", 1) for part in self.headers.get("Cookie", "").split(";")
            )
            return {pair[0]: pair[1] for pair in pairs if len(pair) == 2}

        def _signed_in(self) -> bool:
            return self._cookies().get("session") in site.sessions

        def _body(self) -> str:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
            site.posted_bodies.append(body)
            return body

        def _id(self, path: str, prefix: str) -> int | None:
            rest = path.removeprefix(prefix)
            return int(rest) if rest.isdigit() else None

        def do_GET(self) -> None:
            site.header_user_agents.append(self.headers.get("User-Agent", ""))
            url = urlsplit(self.path)
            path = url.path
            if path.startswith("/api/export/"):
                self._export(path, parse_qs(url.query))
                return
            if site.checkpoint != "off" and not (
                site.checkpoint == "clears" and "_vcrcs" in self._cookies()
            ):
                clears = "true" if site.checkpoint == "clears" else "false"
                self._send(429, _page("checkpoint.html", CLEARS=clears))
                return
            if path == "/login":
                error = "Invalid email or password" if "error" in url.query else ""
                disabled = "disabled" if site.login_form == "disabled" else ""
                scripted = "true" if site.scripted_login_delay_s is not None else "false"
                self._send(
                    200, _page("login.html", ERROR=error, DISABLED=disabled, SCRIPTED=scripted)
                )
                return
            if not self._signed_in():
                self._redirect("/login")
                return
            if path == "/my/spools":
                self._send(200, b"<!doctype html><title>My Spools</title><h1>My Spools</h1>")
                return
            if path == "/filaments":
                query = parse_qs(url.query).get("q", [""])[0]
                site.searches.append(query)
                words = query.casefold().split()
                rows = [
                    row
                    for row in site.filaments
                    if all(
                        word in f"{row['brand']} {row['type']} {row['name']}".casefold()
                        for word in words
                    )
                ]
                self._send(
                    200,
                    _page("search.html", ROWS=json.dumps(rows), COLUMNS=json.dumps(site.columns)),
                )
                return
            if path == "/filament/add":
                self._send(
                    200,
                    _page(
                        "add.html",
                        LISTS=json.dumps(site.add_lists),
                        DEFAULTS=json.dumps(sorted(site.defaults)),
                        DUPLICATES=json.dumps(site.duplicates),
                        EXTRA=json.dumps(site.extra_fields),
                        BRAND_SELECT=json.dumps(site.brand_select),
                        LANDS=json.dumps(site.save_lands),
                    ),
                )
                return
            if path == "/my/dashboard":
                self._send(200, b"<!doctype html><title>Dashboard</title><h1>Dashboard</h1>")
                return
            for prefix, page in (
                ("/filament/details/", "filament.html"),
                ("/my/spools/details/", "my-spool.html"),
            ):
                if path.startswith(prefix):
                    tdfp_id = self._id(path, prefix)
                    if tdfp_id is None or tdfp_id not in site.known_ids:
                        self._send(404, b"<!doctype html><title>404</title><h1>Not found</h1>")
                        return
                    if page == "my-spool.html" and tdfp_id not in site.in_spools:
                        # No record of it in My Spools: no export button.
                        self._send(200, b"<!doctype html><title>My spool</title><h1>No spools</h1>")
                        return
                    self._send(
                        200,
                        _page(
                            page,
                            ID=str(tdfp_id),
                            OPTIONS=json.dumps(site.options),
                            DOWNLOAD=json.dumps(site.download),
                            OPTION_DELAY_MS=str(site.option_delay_ms),
                        ),
                    )
                    return
            self._send(404, b"<!doctype html><title>404</title>")

        def do_POST(self) -> None:
            site.header_user_agents.append(self.headers.get("User-Agent", ""))
            path = urlsplit(self.path).path
            body = self._body()
            if path == "/api/ua":
                site.page_user_agents.append(body)
                self._send(204, b"")
                return
            if path == "/login":
                form = parse_qs(body)
                if form.get("email") == [site.email] and form.get("password") == [site.password]:
                    site.logins += 1
                    token = secrets.token_hex(8)
                    site.sessions.add(token)
                    self._redirect("/my/spools", cookie=f"session={token}; Path=/")
                else:
                    self._redirect("/login?error=1")
                return
            if path == "/api/login":
                form = parse_qs(body)
                if form.get("email") == [site.email] and form.get("password") == [site.password]:
                    time.sleep(site.scripted_login_delay_s or 0.0)
                    site.logins += 1
                    token = secrets.token_hex(8)
                    site.sessions.add(token)
                    self.send_response(204)
                    self.send_header("Set-Cookie", f"session={token}; Path=/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._send(401, b"")
                return
            if path == "/api/filaments" and self._signed_in():
                values = json.loads(body)
                new_id = site.next_id
                site.next_id += 1
                site.created.append({"id": new_id, **values})
                site.known_ids.add(new_id)
                self._send(200, json.dumps({"id": new_id}).encode(), "application/json")
                return
            if path.startswith("/api/spools/") and self._signed_in():
                tdfp_id = self._id(path, "/api/spools/")
                if tdfp_id in site.known_ids:
                    site.adds.append(tdfp_id)
                    site.in_spools.add(tdfp_id)
                    self._send(204, b"")
                    return
            self._send(404, b"")

        def _export(self, path: str, query: dict[str, list[str]]) -> None:
            if not self._signed_in():
                self._send(401, b"")
                return
            chosen = {key: values[0] for key, values in query.items()}
            site.exports.append({"id": path.removeprefix("/api/export/"), **chosen})
            text = EXPORT_FILE.read_text(encoding="utf-8") + " " * site.export_pad
            body = {"name": site.export_name, "text": text}
            self._send(200, json.dumps(body).encode(), "application/json")

    return Handler


class ReplicaServer:
    """`Replica` served on 127.0.0.1 at a free port, in a thread of this process;
    `close` stops it."""

    def __init__(self, site: Replica) -> None:
        self.site = site
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(site))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
