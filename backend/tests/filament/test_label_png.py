"""#7216: the labels as PNG (`filament/label_png.py`; plan
`docs/superpowers/plans/2026-11-25-label-outputs.md`, R-7216-5 to R-7216-7)."""

from __future__ import annotations

import asyncio
import io
import threading
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import pytest
from PIL import Image

from scadbuddy.filament import label_png
from scadbuddy.filament.label_pdf import CodeLabel, SpoolLabel, render_pdf
from scadbuddy.filament.label_png import png_names, png_pixels, render_pngs, zip_pngs
from scadbuddy.filament.label_templates import BY_ID

SPOOL = SpoolLabel(
    number=12, material="PLA Basic", colour="Jade White", brand="Bambu Lab", qr="scadbuddy:spool:x"
)
PLACE = CodeLabel(code="L-7", name="H2C AMS 1 slot 2", qr="https://sb.example/x/L-7")


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


def test_a_repeated_label_gets_a_numbered_entry_and_the_zip_warns_of_no_duplicate(
    recwarn: pytest.WarningsRecorder,
) -> None:
    names = png_names([SPOOL, SPOOL, PLACE, SPOOL, PLACE], sheet=False, pages=5)
    assert names == ["spool-12.png", "spool-12-2.png", "L-7.png", "spool-12-3.png", "L-7-2.png"]
    zip_pngs(names, [b"x"] * 5)
    assert len(recwarn) == 0


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


class _RefusingExecutor(ThreadPoolExecutor):
    """A default executor that refuses work: a rasterise must not use the shared pool."""

    def submit(self, *args: object, **kwargs: object) -> Future[Any]:
        raise AssertionError("a rasterise used the loop's default executor")


async def test_rasterises_wait_on_the_one_pdfium_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # R-7216-20: two PNG renders at once run one after the other on `_PDFIUM`, and the one
    # that waits holds no worker of the loop's default executor.
    release = threading.Event()
    first_in = threading.Event()
    running: list[str] = []
    seen: list[tuple[str, int]] = []
    guard = threading.Lock()

    def blocking(pdf: bytes, dpi: int) -> list[bytes]:
        with guard:
            running.append(threading.current_thread().name)
            seen.append((threading.current_thread().name, len(running)))
        first_in.set()
        release.wait()
        with guard:
            running.pop()
        return [pdf]

    monkeypatch.setattr(label_png, "render_pngs", blocking)
    loop = asyncio.get_running_loop()
    refusing = _RefusingExecutor(max_workers=1)
    loop.set_default_executor(refusing)
    try:
        first = asyncio.create_task(label_png.rasterise(b"a", 300))
        second = asyncio.create_task(label_png.rasterise(b"b", 300))
        while not first_in.is_set():
            if first.done():
                first.result()  # raises what stopped it, such as the refusing executor's error
            await asyncio.sleep(0.001)
        # The second is queued on the one PDFium thread, not running beside the first.
        assert label_png._PDFIUM._work_queue.qsize() == 1
        assert len(seen) == 1
        release.set()
        assert list(await asyncio.gather(first, second)) == [[b"a"], [b"b"]]
    finally:
        release.set()
        loop.set_default_executor(ThreadPoolExecutor())
        refusing.shutdown()
    assert [name.startswith("pdfium") for name, _ in seen] == [True, True]
    assert [count for _, count in seen] == [1, 1]
