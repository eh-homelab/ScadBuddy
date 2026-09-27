"""The store behind `// file` parameters (#204): sniffing, sanitising, limits."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from scadbuddy.library.assets import (
    MAX_ASSET_BYTES,
    MAX_PNG_SIDE,
    AssetNotFoundError,
    AssetRejectedError,
    AssetStore,
    display_name,
    file_assets,
    sanitise_svg,
    sniff,
)
from scadbuddy.render.schema import CustomizerSchema, Parameter

HEART_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">
  <path d="M10 18 L2 8 A4 4 0 0 1 10 4 A4 4 0 0 1 18 8 Z"/>
</svg>
"""


def png_bytes(width: int, height: int, *, mode: str = "RGBA", text: str | None = None) -> bytes:
    image = Image.new(mode, (width, height))
    info = None
    if text is not None:
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", text)
    out = io.BytesIO()
    image.save(out, format="PNG", pnginfo=info)
    return out.getvalue()


@pytest.fixture
def store(tmp_path: Path) -> AssetStore:
    return AssetStore(tmp_path / "assets")


def test_an_svg_is_stored_under_the_hash_of_what_was_kept(store: AssetStore) -> None:
    meta = store.put(HEART_SVG, "heart.svg")

    stored = store.blob_path(meta).read_bytes()
    assert meta.kind == "svg"
    assert meta.name == "heart.svg"
    assert meta.id == hashlib.sha256(stored).hexdigest()
    assert meta.size == len(stored)
    assert b"<path" in stored
    assert store.get(meta.id) == meta


def test_the_same_content_is_stored_once(store: AssetStore) -> None:
    first = store.put(HEART_SVG, "a.svg")
    second = store.put(HEART_SVG, "b.svg")

    assert first.id == second.id
    assert sorted(p.name for p in store.root.iterdir()) == [f"{first.id}.json", f"{first.id}.svg"]


def test_the_svg_loses_everything_that_runs_or_fetches() -> None:
    hostile = b"""<?xml version="1.0"?>
<?xml-stylesheet href="http://evil.example/x.css"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN"
  "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     onload="alert(1)" width="10" height="10">
  <script>alert(1)</script>
  <style>@import url(http://evil.example/y.css);</style>
  <defs><linearGradient id="g"/></defs>
  <a xlink:href="javascript:alert(1)">
    <path id="p" d="M0 0L1 1Z" fill="url(http://evil.example/#x)" onclick="x()"/>
  </a>
  <rect width="1" height="1" fill="url(#g)"/>
  <use href="#p"/>
  <image href="http://evil.example/i.png"/>
  <foreignObject><div xmlns="http://www.w3.org/1999/xhtml">hi</div></foreignObject>
  <set attributeName="href" to="http://evil.example/"/>
</svg>"""

    cleaned = sanitise_svg(hostile).decode()

    for gone in (
        "script",
        "alert",
        "onload",
        "onclick",
        "evil.example",
        "@import",
        "foreignObject",
        "<image",
        "<set",
        "DOCTYPE",
        "xml-stylesheet",
    ):
        assert gone not in cleaned, gone
    # Geometry and in-document references survive.
    assert 'd="M0 0L1 1Z"' in cleaned
    assert 'href="#p"' in cleaned
    assert 'fill="url(#g)"' in cleaned


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]>'
            b'<svg xmlns="http://www.w3.org/2000/svg">&a;</svg>',
            "entities",
        ),
        (b'<?xml version="1.0"?><html><svg/></html>', "not an SVG"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'", "not well-formed"),
    ],
)
def test_a_hostile_or_broken_svg_is_refused(store: AssetStore, payload: bytes, reason: str) -> None:
    with pytest.raises(AssetRejectedError, match=reason):
        store.put(payload, "x.svg")


@pytest.mark.parametrize(
    "payload",
    [
        b"GIF89a\x01\x00\x01\x00",
        b"\xff\xd8\xff\xe0 a jpeg",
        b"cube(10);",
        b"%PDF-1.7",
        b"",
    ],
)
def test_only_svg_and_png_content_is_accepted_whatever_the_name(
    store: AssetStore, payload: bytes
) -> None:
    with pytest.raises(AssetRejectedError, match="only SVG and PNG"):
        store.put(payload, "looks-fine.png")


def test_the_kind_comes_from_the_content_not_the_name(store: AssetStore) -> None:
    assert store.put(HEART_SVG, "picture.png").kind == "svg"
    assert sniff(png_bytes(4, 4)) == "png"


def test_a_png_is_downscaled_to_the_long_side_cap(store: AssetStore) -> None:
    meta = store.put(png_bytes(1024, 512), "wide.png")

    assert (meta.width, meta.height) == (MAX_PNG_SIDE, MAX_PNG_SIDE // 2)
    with Image.open(store.blob_path(meta)) as image:
        assert image.size == (MAX_PNG_SIDE, MAX_PNG_SIDE // 2)


def test_a_small_png_keeps_its_size_and_loses_its_metadata(store: AssetStore) -> None:
    meta = store.put(png_bytes(96, 96, text="secret camera serial"), "star.png")

    assert (meta.width, meta.height) == (96, 96)
    assert b"secret camera serial" not in store.blob_path(meta).read_bytes()


def test_a_palette_png_is_re_encoded_in_a_mode_surface_reads(store: AssetStore) -> None:
    meta = store.put(png_bytes(8, 8, mode="P"), "palette.png")
    with Image.open(store.blob_path(meta)) as image:
        assert image.mode == "RGBA"


def test_a_png_declaring_too_many_pixels_is_refused_before_decoding(store: AssetStore) -> None:
    with pytest.raises(AssetRejectedError, match="pixels"):
        store.put(png_bytes(6000, 5000, mode="1"), "huge.png")


def test_a_truncated_png_is_refused(store: AssetStore) -> None:
    with pytest.raises(AssetRejectedError, match="could not be decoded"):
        store.put(png_bytes(32, 32)[:60], "cut.png")


def test_an_oversized_upload_is_refused(store: AssetStore) -> None:
    with pytest.raises(AssetRejectedError, match="larger than"):
        store.put(b"<svg" + b" " * MAX_ASSET_BYTES, "big.svg")


def test_the_original_name_is_display_only() -> None:
    assert display_name("../../etc/passwd", "svg") == "passwd"
    assert display_name("C:\\Users\\me\\logo.svg", "svg") == "logo.svg"
    assert display_name("bad\x00name\n.png", "png") == "badname.png"
    assert display_name(None, "png") == "upload.png"
    assert display_name("", "svg") == "upload.svg"


@pytest.mark.parametrize("asset_id", ["", "../x", "A" * 64, "0" * 63, "0" * 64])
def test_an_unknown_or_malformed_id_is_not_found(store: AssetStore, asset_id: str) -> None:
    with pytest.raises(AssetNotFoundError):
        store.get(asset_id)


# ── file_assets: what a render may pass for a `file` parameter ────────────────


def _file_schema(initial: str = "", accept: tuple[str, ...] = ("svg", "png")) -> CustomizerSchema:
    return CustomizerSchema(
        parameters=[
            Parameter(name="overlay", type="file", initial=initial, accept=list(accept)),
            Parameter(name="label", type="string", initial="hi"),
        ]
    )


def test_a_file_parameter_takes_an_uploaded_id(store: AssetStore) -> None:
    meta = store.put(HEART_SVG, "heart.svg")
    assert file_assets(_file_schema(), {"overlay": meta.id, "label": "x"}, store) == {
        "overlay": meta
    }


def test_the_empty_value_and_the_models_own_default_need_no_upload(store: AssetStore) -> None:
    schema = _file_schema(initial="sample-overlay.svg")
    assert file_assets(schema, {"overlay": ""}, store) == {}
    assert file_assets(schema, {"overlay": "sample-overlay.svg"}, store) == {}
    assert file_assets(schema, {}, store) == {}


@pytest.mark.parametrize(
    "value",
    ["/etc/passwd", "../../secrets.svg", "other.svg", "a" * 64, 3, True],
)
def test_anything_else_is_refused(store: AssetStore, value: str | int | bool) -> None:
    with pytest.raises(ValueError, match="overlay"):
        file_assets(_file_schema(), {"overlay": value}, store)


def test_a_kind_the_parameter_does_not_accept_is_refused(store: AssetStore) -> None:
    meta = store.put(HEART_SVG, "heart.svg")
    with pytest.raises(ValueError, match="accepts png, not svg"):
        file_assets(_file_schema(accept=("png",)), {"overlay": meta.id}, store)
