"""#7216: the labels as PNG (`filament/label_png.py`; plan
`docs/superpowers/plans/2026-11-25-label-outputs.md`, R-7216-5 to R-7216-7)."""

from __future__ import annotations

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from scadbuddy.filament.label_pdf import PlaceLabel, SpoolLabel, render_pdf
from scadbuddy.filament.label_png import png_names, png_pixels, render_pngs, zip_pngs
from scadbuddy.filament.label_templates import BY_ID

SPOOL = SpoolLabel(
    number=12, material="PLA Basic", colour="Jade White", brand="Bambu Lab", qr="scadbuddy:spool:x"
)
PLACE = PlaceLabel(code="L-7", name="H2C AMS 1 slot 2", qr="https://sb.example/x/L-7")


def test_one_roll_label_is_one_png_of_its_size_at_its_dpi() -> None:
    pdf = render_pdf([SPOOL], BY_ID["roll_50x25"], "roll")
    [png] = render_pngs(pdf, 300)
    image = Image.open(io.BytesIO(png))
    assert image.mode == "L"
    assert image.size == (round(50 / 25.4 * 300), round(25 / 25.4 * 300))
    assert image.info["dpi"] == pytest.approx((300, 300), abs=0.5)
    assert min(image.getdata()) == 0  # the QR's black modules are drawn


def test_each_page_is_one_png() -> None:
    assert len(render_pngs(render_pdf([SPOOL, PLACE], BY_ID["box_62x29"], "roll"), 203)) == 2


def test_roll_entries_are_named_by_the_label_and_sheet_entries_by_page() -> None:
    assert png_names([SPOOL, PLACE], sheet=False, pages=2) == ["spool-12.png", "L-7.png"]
    assert png_names([SPOOL] * 31, sheet=True, pages=2) == ["sheet-1.png", "sheet-2.png"]


def test_the_zip_is_stored_and_its_bytes_do_not_depend_on_the_date() -> None:
    one = zip_pngs(["a.png"], [b"x"])
    with zipfile.ZipFile(io.BytesIO(one)) as archive:
        [info] = archive.infolist()
        assert info.compress_type == zipfile.ZIP_STORED
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
    assert zip_pngs(["a.png"], [b"x"]) == one


def test_the_pixels_count_every_page() -> None:
    roll = round(50 / 25.4 * 600) * round(25 / 25.4 * 600)
    assert png_pixels(BY_ID["roll_50x25"], "roll", 100, 600) == 100 * roll
    letter = round(215.9 / 25.4 * 300) * round(279.4 / 25.4 * 300)
    assert png_pixels(BY_ID["avery_5160"], "roll", 31, 300) == 2 * letter


def test_two_rasterisations_at_once_both_give_pngs() -> None:
    pdf = render_pdf([SPOOL], BY_ID["roll_50x25"], "roll")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: render_pngs(pdf, 300), range(2)))
    assert all(r[0].startswith(b"\x89PNG") for r in results)
