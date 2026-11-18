"""`/api/v1/filament` suppliers, products and prices (#6266)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response

from tests.api.test_settings_runtime import _state

pytestmark = pytest.mark.requires_postgres

URL = "/api/v1/filament"
MISSING = "00000000-0000-4000-8000-000000000001"


def _type(client: TestClient, code: str = "PLA-WHT") -> str:
    material = next(
        m["id"] for m in client.get(f"{URL}/catalog/materials").json() if m["name"] == "PLA"
    )
    made = client.post(
        f"{URL}/types",
        json={"code": code, "material_id": material, "rgba": "#FFFFFF", "label_weight_g": 1000},
    )
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def _supplier(client: TestClient, name: str = "Acme") -> str:
    made = client.post(f"{URL}/suppliers", json={"name": name})
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def _product(client: TestClient, supplier_id: str, type_id: str, sku: str = "S1") -> str:
    made = client.post(
        f"{URL}/suppliers/{supplier_id}/products", json={"type_id": type_id, "sku": sku}
    )
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def test_a_script_url_is_refused(client: TestClient) -> None:
    bad = client.post(f"{URL}/suppliers", json={"name": "X", "website": "javascript:alert(1)"})
    assert bad.status_code == 422, bad.text
    supplier_id = _supplier(client)
    refused = client.patch(
        f"{URL}/suppliers/{supplier_id}", json={"website": "javascript:alert(1)"}
    )
    assert refused.status_code == 422
    product = client.post(
        f"{URL}/suppliers/{supplier_id}/products",
        json={"type_id": _type(client), "sku": "S", "product_url": "data:text/html,x"},
    )
    assert product.status_code == 422, product.text


def test_a_price_defaults_to_the_shops_currency_and_today(client: TestClient) -> None:
    assert client.put("/api/v1/settings", json={"orders_currency": "EUR"}).status_code == 200
    supplier_id = _supplier(client)
    product_id = _product(client, supplier_id, _type(client))
    made = client.post(f"{URL}/products/{product_id}/prices", json={"price": "21.40"})
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["currency"] == "EUR"
    assert body["source"] == "manual"
    assert body["on_date"] == datetime.now(UTC).date().isoformat()


def test_a_price_is_a_string(client: TestClient) -> None:
    product_id = _product(client, _supplier(client), _type(client))
    made = client.post(
        f"{URL}/products/{product_id}/prices", json={"price": "21.40", "on_date": "2026-11-17"}
    )
    assert made.status_code == 201, made.text
    price = made.json()["price"]
    assert isinstance(price, str)
    assert price == "21.4000"
    assert made.json()["price_per_kg"] == "21.4000"
    listed = client.get(f"{URL}/products/{product_id}/prices").json()
    assert [p["price"] for p in listed] == ["21.4000"]


def test_a_supplier_is_added_changed_archived_and_refused_twice(client: TestClient) -> None:
    supplier_id = _supplier(client)
    assert client.post(f"{URL}/suppliers", json={"name": "acme"}).status_code == 409
    patched = client.patch(f"{URL}/suppliers/{supplier_id}", json={"note": "n", "archived": True})
    assert patched.json()["archived_at"] is not None
    assert client.get(f"{URL}/suppliers").json() == []
    assert len(client.get(f"{URL}/suppliers", params={"include_archived": True}).json()) == 1
    assert client.get(f"{URL}/suppliers/{supplier_id}").json()["note"] == "n"


def test_a_product_sheet_and_type_history(client: TestClient) -> None:
    type_id = _type(client)
    supplier_id = _supplier(client)
    product_id = _product(client, supplier_id, type_id)
    dup = client.post(
        f"{URL}/suppliers/{supplier_id}/products", json={"type_id": type_id, "sku": "s1"}
    )
    assert dup.status_code == 409
    no_type = client.post(
        f"{URL}/suppliers/{supplier_id}/products", json={"type_id": MISSING, "sku": "x"}
    )
    assert no_type.status_code == 422
    for price, day in (("20.00", "2026-10-01"), ("22.00", "2026-11-01")):
        client.post(f"{URL}/products/{product_id}/prices", json={"price": price, "on_date": day})
    (row,) = client.get(f"{URL}/suppliers/{supplier_id}/price-sheet").json()
    assert Decimal(row["change_pct"]) == Decimal(10)
    assert row["product"]["newest_price"]["price"] == "22.0000"
    (product,) = client.get(f"{URL}/suppliers/{supplier_id}/products").json()
    assert product["newest_price"]["on_date"] == "2026-11-01"
    history = client.get(f"{URL}/types/{type_id}/prices").json()
    assert [(p["supplier_name"], p["sku"]) for p in history] == [("Acme", "S1")] * 2
    price_id = history[0]["id"]
    assert client.delete(f"{URL}/prices/{price_id}").status_code == 204
    assert client.delete(f"{URL}/prices/{price_id}").status_code == 404
    archived = client.patch(f"{URL}/products/{product_id}", json={"archived": True})
    assert archived.json()["archived_at"] is not None
    assert client.get(f"{URL}/suppliers/{supplier_id}/price-sheet").json() == []


def test_missing_rows_are_404(client: TestClient) -> None:
    ghost = str(uuid4())
    for path in (
        f"/suppliers/{ghost}",
        f"/suppliers/{ghost}/products",
        f"/suppliers/{ghost}/price-sheet",
        f"/products/{ghost}/prices",
        f"/types/{ghost}/prices",
    ):
        assert client.get(f"{URL}{path}").status_code == 404, path
    assert client.patch(f"{URL}/suppliers/{ghost}", json={"note": "x"}).status_code == 404
    assert client.patch(f"{URL}/products/{ghost}", json={"note": "x"}).status_code == 404
    assert (
        client.post(
            f"{URL}/suppliers/{ghost}/products", json={"type_id": MISSING, "sku": "x"}
        ).status_code
        == 404
    )
    assert client.post(f"{URL}/products/{ghost}/prices", json={"price": "1"}).status_code == 404
    assert client.delete(f"{URL}/prices/{ghost}").status_code == 404


def test_null_for_a_required_field_is_422(client: TestClient) -> None:
    supplier_id = _supplier(client)
    product_id = _product(client, supplier_id, _type(client))
    assert client.patch(f"{URL}/suppliers/{supplier_id}", json={"name": None}).status_code == 422
    for field in ("type_id", "sku", "pack_size"):
        refused = client.patch(f"{URL}/products/{product_id}", json={field: None})
        assert refused.status_code == 422, field


def test_a_restore_onto_a_taken_name_or_sku_is_409(client: TestClient) -> None:
    first = _supplier(client)
    client.patch(f"{URL}/suppliers/{first}", json={"archived": True})
    _supplier(client)
    assert client.patch(f"{URL}/suppliers/{first}", json={"archived": False}).status_code == 409
    supplier_id = _supplier(client, "Other")
    type_id = _type(client)
    one = _product(client, supplier_id, type_id)
    client.patch(f"{URL}/products/{one}", json={"archived": True})
    _product(client, supplier_id, type_id)
    assert client.patch(f"{URL}/products/{one}", json={"archived": False}).status_code == 409


def test_the_type_history_marks_archived_rows(client: TestClient) -> None:
    type_id = _type(client)
    supplier_id = _supplier(client)
    product_id = _product(client, supplier_id, type_id)
    client.post(f"{URL}/products/{product_id}/prices", json={"price": "5"})
    client.patch(f"{URL}/products/{product_id}", json={"archived": True})
    client.patch(f"{URL}/suppliers/{supplier_id}", json={"archived": True})
    (point,) = client.get(f"{URL}/types/{type_id}/prices").json()
    assert (point["product_archived"], point["supplier_archived"]) == (True, True)


# -- invoices ---------------------------------------------------------------------------


def _invoice_body(supplier_id: str, **over: object) -> dict[str, object]:
    return {
        "supplier_id": supplier_id,
        "invoice_date": "2026-11-01",
        "currency": "USD",
        "subtotal": "100.00",
        "shipping": "10.00",
        "tax": "7.00",
        "total": "117.00",
        "lines": [
            {"description": "PLA white", "quantity": 1, "unit_price": "40.00"},
            {"description": "PLA black", "quantity": 1, "unit_price": "60.00"},
        ],
        **over,
    }


def test_an_invoice_is_made_with_string_money(client: TestClient) -> None:
    made = client.post(f"{URL}/invoices", json=_invoice_body(_supplier(client)))
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["reconciles"] is True
    assert body["minor_exponent"] == 2
    assert [line["allocated_cost"] for line in body["lines"]] == ["46.8000", "70.2000"]
    assert body["lines"][0]["tax_basis"] == "receipt-wide 7.0%"
    assert body["file"] is None
    assert client.get(f"{URL}/invoices/{body['id']}").json() == body
    assert [i["id"] for i in client.get(f"{URL}/invoices").json()] == [body["id"]]


def test_an_import_run_is_set_and_claimed_only_from_the_value_read(client: TestClient) -> None:
    draft = client.post(f"{URL}/invoices", json=_invoice_body(_supplier(client))).json()
    assert draft["import_run_id"] is None
    url = f"{URL}/invoices/{draft['id']}"
    first = client.patch(url, json={"import_run_id": "s-1", "if_import_run_id": None})
    assert first.status_code == 200, first.text
    assert first.json()["import_run_id"] == "s-1"
    # A second writer that read `null` loses: the value is not what it read (#6267).
    lost = client.patch(url, json={"import_run_id": "s-2", "if_import_run_id": None})
    assert lost.status_code == 409, lost.text
    assert client.get(url).json()["import_run_id"] == "s-1"
    # Replacing a dead run: the writer names the value it read.
    won = client.patch(url, json={"import_run_id": "s-3", "if_import_run_id": "s-1"})
    assert won.status_code == 200, won.text
    assert won.json()["import_run_id"] == "s-3"
    # With no condition the field is a plain write, and the condition alone changes nothing.
    assert client.patch(url, json={"import_run_id": None}).json()["import_run_id"] is None
    assert client.patch(url, json={"if_import_run_id": "x"}).status_code == 409


def test_an_import_run_already_on_another_invoice_is_a_409(client: TestClient) -> None:
    supplier = _supplier(client)
    one = client.post(f"{URL}/invoices", json=_invoice_body(supplier)).json()
    two = client.post(f"{URL}/invoices", json=_invoice_body(supplier)).json()
    assert (
        client.patch(f"{URL}/invoices/{one['id']}", json={"import_run_id": "s-1"}).status_code
        == 200
    )
    refused = client.patch(f"{URL}/invoices/{two['id']}", json={"import_run_id": "s-1"})
    assert refused.status_code == 409, refused.text
    assert client.get(f"{URL}/invoices/{two['id']}").json()["import_run_id"] is None


def test_mixed_line_tax_is_a_422(client: TestClient) -> None:
    lines = [
        {"description": "a", "quantity": 1, "unit_price": "40", "line_tax": "3"},
        {"description": "b", "quantity": 1, "unit_price": "60"},
    ]
    refused = client.post(f"{URL}/invoices", json=_invoice_body(_supplier(client), lines=lines))
    assert refused.status_code == 422, refused.text
    assert client.get(f"{URL}/invoices").json() == []


def test_the_same_number_twice_is_a_409_with_the_first_id(client: TestClient) -> None:
    supplier = _supplier(client)
    first = client.post(f"{URL}/invoices", json=_invoice_body(supplier, number="INV-1"))
    assert first.status_code == 201
    second = client.post(f"{URL}/invoices", json=_invoice_body(supplier, number="inv-1"))
    assert second.status_code == 409, second.text
    assert second.json()["existing_invoice_id"] == first.json()["id"]
    assert len(client.get(f"{URL}/invoices").json()) == 1


def test_a_number_edit_onto_a_taken_number_is_a_409(client: TestClient) -> None:
    supplier = _supplier(client)
    first = client.post(f"{URL}/invoices", json=_invoice_body(supplier, number="INV-1")).json()
    draft = client.post(f"{URL}/invoices", json=_invoice_body(supplier)).json()
    refused = client.patch(f"{URL}/invoices/{draft['id']}", json={"number": "INV-1"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["existing_invoice_id"] == first["id"]


def test_a_second_import_run_id_is_a_200_with_the_first(client: TestClient) -> None:
    supplier = _supplier(client)
    first = client.post(f"{URL}/invoices", json=_invoice_body(supplier, import_run_id="r1"))
    assert first.status_code == 201
    again = client.post(
        f"{URL}/invoices", json=_invoice_body(supplier, import_run_id="r1", lines=[])
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    assert len(again.json()["lines"]) == 2


def test_lines_are_replaced_and_a_missing_invoice_is_404(client: TestClient) -> None:
    made = client.post(f"{URL}/invoices", json=_invoice_body(_supplier(client))).json()
    put = client.put(
        f"{URL}/invoices/{made['id']}/lines",
        json=[{"description": "one", "quantity": 2, "unit_price": "50"}],
    )
    assert put.status_code == 200, put.text
    assert put.json()["lines"][0]["allocated_cost"] == "117.0000"
    assert put.json()["lines"][0]["unit_cost"] == "58.5000"
    assert client.put(f"{URL}/invoices/{MISSING}/lines", json=[]).status_code == 404
    assert client.get(f"{URL}/invoices/{MISSING}").status_code == 404
    assert client.delete(f"{URL}/invoices/{MISSING}").status_code == 404


def test_a_draft_deletes_and_a_confirmed_one_is_a_409(client: TestClient, pg_conninfo: str) -> None:
    supplier = _supplier(client)
    gone = client.post(f"{URL}/invoices", json=_invoice_body(supplier, number="G")).json()
    assert client.delete(f"{URL}/invoices/{gone['id']}").status_code == 204
    assert client.get(f"{URL}/invoices/{gone['id']}").status_code == 404
    kept = client.post(f"{URL}/invoices", json=_invoice_body(supplier, number="K")).json()
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE filament_invoices SET status = 'confirmed' WHERE id = %s", (kept["id"],)
        )
    url = f"{URL}/invoices/{kept['id']}"
    assert client.delete(url).status_code == 409
    assert client.put(f"{url}/lines", json=[]).status_code == 409
    assert client.patch(url, json={"shipping": "1"}).status_code == 409
    assert client.patch(url, json={"note": "paid"}).status_code == 200


def test_the_invoice_list_filters_by_supplier_and_status(
    client: TestClient, pg_conninfo: str
) -> None:
    one, two = _supplier(client, "One"), _supplier(client, "Two")
    first = client.post(f"{URL}/invoices", json=_invoice_body(one, number="A")).json()
    second = client.post(f"{URL}/invoices", json=_invoice_body(two, number="B")).json()
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE filament_invoices SET status = 'confirmed' WHERE id = %s", (second["id"],)
        )
    assert [i["id"] for i in client.get(f"{URL}/invoices", params={"supplier_id": one}).json()] == [
        first["id"]
    ]
    assert [
        i["id"] for i in client.get(f"{URL}/invoices", params={"status": "confirmed"}).json()
    ] == [second["id"]]
    assert client.get(f"{URL}/invoices", params={"status": "bogus"}).status_code == 422


# -- an invoice's file ------------------------------------------------------------------

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _draft(client: TestClient, number: str = "F1") -> str:
    made = client.post(f"{URL}/invoices", json=_invoice_body(_supplier(client), number=number))
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def _send(client: TestClient, invoice_id: str, data: bytes, name: str) -> Response:
    sent: Response = client.post(f"{URL}/invoices/{invoice_id}/file", files={"file": (name, data)})
    return sent


def _holds(pg_conninfo: str, invoice_id: str) -> list[str]:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = 'filament_invoice' AND holder_id = %s",
            (invoice_id,),
        ).fetchall()
    return [r[0] for r in rows]


def test_a_pdf_is_kept_and_listed(client: TestClient, pg_conninfo: str) -> None:
    invoice_id = _draft(client)
    sent = _send(client, invoice_id, PDF, "inv.pdf")
    pointer = {"name": "inv.pdf", "media_type": "application/pdf", "size": len(PDF)}
    assert sent.status_code == 200, sent.text
    assert sent.json() == pointer
    got = client.get(f"{URL}/invoices/{invoice_id}/file")
    assert got.content == PDF
    assert got.headers["content-type"] == "application/pdf"
    assert got.headers["content-disposition"].startswith("inline")
    assert client.get(f"{URL}/invoices/{invoice_id}").json()["file"] == pointer
    assert len(_holds(pg_conninfo, invoice_id)) == 1


def test_a_photo_is_kept(client: TestClient) -> None:
    invoice_id = _draft(client)
    sent = _send(client, invoice_id, PNG, "scan.png")
    assert sent.json()["media_type"] == "image/png"
    got = client.get(f"{URL}/invoices/{invoice_id}/file")
    assert got.content == PNG
    assert "sandbox" in got.headers["content-security-policy"]


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (b"<html><script>alert(1)</script></html>", "invoice.pdf"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>', "invoice.svg"),
        (b"", "empty.pdf"),
    ],
    ids=["html", "svg", "empty"],
)
def test_a_file_that_is_no_pdf_or_photo_is_refused(
    client: TestClient,
    pg_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    name: str,
) -> None:
    kept: list[object] = []

    async def spy(*args: object, **kwargs: object) -> str:
        kept.append(args)
        return "never"

    monkeypatch.setattr("scadbuddy.orders.attachments.keep_file", spy)
    invoice_id = _draft(client)
    assert _send(client, invoice_id, data, name).status_code == 422
    assert kept == []
    assert _holds(pg_conninfo, invoice_id) == []
    assert client.get(f"{URL}/invoices/{invoice_id}").json()["file"] is None
    assert client.get(f"{URL}/invoices/{invoice_id}/file").status_code == 404


def test_a_second_file_replaces_the_first(client: TestClient, pg_conninfo: str) -> None:
    invoice_id = _draft(client)
    _send(client, invoice_id, PDF, "a.pdf")
    first = _holds(pg_conninfo, invoice_id)
    _send(client, invoice_id, PNG, "b.png")
    second = _holds(pg_conninfo, invoice_id)
    assert len(second) == 1
    assert second != first
    assert client.get(f"{URL}/invoices/{invoice_id}/file").content == PNG


def test_the_file_is_served_uncached(client: TestClient) -> None:
    invoice_id = _draft(client)
    _send(client, invoice_id, PDF, "inv.pdf")
    got = client.get(f"{URL}/invoices/{invoice_id}/file")
    assert got.headers["cache-control"] == "private, no-cache"
    assert "immutable" not in got.headers["cache-control"]
    assert got.headers["x-content-type-options"] == "nosniff"


def test_a_confirmed_invoice_takes_a_file(client: TestClient, pg_conninfo: str) -> None:
    invoice_id = _draft(client)
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE filament_invoices SET status = 'confirmed' WHERE id = %s", (invoice_id,)
        )
    assert _send(client, invoice_id, PDF, "inv.pdf").status_code == 200


def test_deleting_a_draft_releases_its_blob(client: TestClient, pg_conninfo: str) -> None:
    invoice_id = _draft(client)
    _send(client, invoice_id, PDF, "inv.pdf")
    assert len(_holds(pg_conninfo, invoice_id)) == 1
    assert client.delete(f"{URL}/invoices/{invoice_id}").status_code == 204
    assert _holds(pg_conninfo, invoice_id) == []


def test_the_pointer_has_no_bytes(client: TestClient) -> None:
    invoice_id = _draft(client)
    _send(client, invoice_id, PDF, "inv.pdf")
    shown = client.get(f"{URL}/invoices/{invoice_id}")
    assert "file_key" not in shown.text
    assert "base64" not in shown.text


def test_a_missing_invoice_is_404_before_the_body_is_read(client: TestClient) -> None:
    assert _send(client, MISSING, PDF, "inv.pdf").status_code == 404
    assert client.get(f"{URL}/invoices/{MISSING}/file").status_code == 404


def test_a_file_past_the_media_upload_limit_is_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    invoice_id = _draft(client)
    state = _state(client)
    monkeypatch.setattr(
        state, "settings", state.settings.model_copy(update={"media_upload_max_bytes": 10})
    )
    answer = _send(client, invoice_id, PDF, "inv.pdf")
    assert answer.status_code == 413, answer.text
    assert "an invoice's file is at most" in answer.json()["detail"]


def test_an_upload_with_no_file_part_is_422(client: TestClient) -> None:
    invoice_id = _draft(client)
    answer = client.post(f"{URL}/invoices/{invoice_id}/file", files={"note": (None, "not a file")})
    assert answer.status_code == 422, answer.text


# -- confirming an invoice --------------------------------------------------------------


def _confirmable(client: TestClient, **over: object) -> str:
    tag = uuid4().hex[:6]
    type_id = _type(client, f"T-{tag}")
    line = {"description": "A00-B01", "quantity": 2, "unit_price": "20.00", "type_id": type_id}
    fields: dict[str, object] = {
        "subtotal": "40.00",
        "shipping": "0",
        "tax": "2.80",
        "total": "42.80",
        "lines": [line],
    }
    body = _invoice_body(_supplier(client, f"S-{tag}"), **(fields | over))
    made = client.post(f"{URL}/invoices", json=body)
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def test_confirming_makes_spools_and_a_repeat_is_200(client: TestClient) -> None:
    invoice_id = _confirmable(client)
    done = client.post(f"{URL}/invoices/{invoice_id}/confirm")
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "confirmed"
    assert len(body["lines"][0]["spool_ids"]) == 2
    spool = client.get(f"{URL}/spools/{body['lines'][0]['spool_ids'][0]}").json()
    assert spool["cost_per_kg"] == "21.4000"
    again = client.post(f"{URL}/invoices/{invoice_id}/confirm")
    assert again.status_code == 200
    assert again.json()["lines"][0]["spool_ids"] == body["lines"][0]["spool_ids"]


def test_supplier_stats_list_confirmed_suppliers_and_the_path_is_not_an_id(
    client: TestClient,
) -> None:
    assert client.get(f"{URL}/suppliers/stats").json() == []
    _confirmable(client)
    done = _confirmable(client)
    assert client.post(f"{URL}/invoices/{done}/confirm").status_code == 200
    [row] = client.get(f"{URL}/suppliers/stats").json()
    assert row["spool_count"] == 2
    assert row["spend"] == "42.8000"
    assert row["consumed_g"] == 0
    assert row["remaining_g"] == 2000


def test_confirming_a_missing_invoice_is_404(client: TestClient) -> None:
    assert client.post(f"{URL}/invoices/{MISSING}/confirm").status_code == 404


def test_confirming_an_empty_or_unreconciled_or_foreign_invoice_is_409(client: TestClient) -> None:
    supplier = _supplier(client)
    empty = client.post(
        f"{URL}/invoices",
        json=_invoice_body(supplier, number="E", lines=[], subtotal="0", total="0", tax="0"),
    ).json()
    assert client.post(f"{URL}/invoices/{empty['id']}/confirm").status_code == 409
    off = _confirmable(client, total="43.00")
    refused = client.post(f"{URL}/invoices/{off}/confirm")
    assert refused.status_code == 409
    assert "0.20" in refused.text
    foreign = _confirmable(client, currency="EUR")
    assert client.post(f"{URL}/invoices/{foreign}/confirm").status_code == 409
    assert client.get(f"{URL}/invoices/{foreign}").json()["status"] == "draft"


def test_a_duplicate_proposed_type_is_a_409_naming_the_line(client: TestClient) -> None:
    _type(client, "PLA-WHT")
    material = next(
        m["id"] for m in client.get(f"{URL}/catalog/materials").json() if m["name"] == "PLA"
    )
    proposed = {
        "code": "PLA-WHT",
        "material_id": material,
        "rgba": "#FFFFFF",
        "label_weight_g": 1000,
    }
    body = _invoice_body(
        _supplier(client),
        subtotal="40.00",
        shipping="0",
        tax="0",
        total="40.00",
        lines=[
            {"description": "x", "quantity": 1, "unit_price": "40.00", "proposed_type": proposed}
        ],
    )
    made = client.post(f"{URL}/invoices", json=body).json()
    refused = client.post(f"{URL}/invoices/{made['id']}/confirm")
    assert refused.status_code == 409, refused.text
    assert "line 1" in refused.text
    assert client.get(f"{URL}/invoices/{made['id']}").json()["status"] == "draft"


def test_a_zero_quantity_is_a_422(client: TestClient) -> None:
    refused = client.post(f"{URL}/shopping-list", json={"material": "PLA", "quantity_spools": 0})
    assert refused.status_code == 422
    item = client.post(f"{URL}/shopping-list", json={"material": "PLA"}).json()
    zero = client.patch(f"{URL}/shopping-list/{item['id']}", json={"quantity_spools": 0})
    assert zero.status_code == 422
    null = client.patch(f"{URL}/shopping-list/{item['id']}", json={"status": None})
    assert null.status_code == 422


def test_an_item_with_neither_type_nor_material_is_a_422(client: TestClient) -> None:
    assert client.post(f"{URL}/shopping-list", json={"note": "x"}).status_code == 422
    assert client.post(f"{URL}/shopping-list", json={"type_id": MISSING}).status_code == 422


def test_the_shopping_list_is_added_changed_filtered_and_cleared(client: TestClient) -> None:
    type_id = _type(client)
    from_type = client.post(f"{URL}/shopping-list", json={"type_id": type_id, "quantity_spools": 3})
    assert from_type.status_code == 201, from_type.text
    assert from_type.json()["material"] == "PLA"
    assert from_type.json()["quantity_spools"] == 3
    other = client.post(f"{URL}/shopping-list", json={"material": "PETG"}).json()
    bought = client.patch(f"{URL}/shopping-list/{other['id']}", json={"status": "purchased"})
    assert bought.status_code == 200
    assert bought.json()["purchased_at"] is not None
    assert [i["status"] for i in client.get(f"{URL}/shopping-list").json()] == [
        "pending",
        "purchased",
    ]
    only = client.get(f"{URL}/shopping-list", params={"status": "purchased"}).json()
    assert [i["id"] for i in only] == [other["id"]]
    assert client.get(f"{URL}/shopping-list", params={"status": "bogus"}).status_code == 422
    cleared = client.delete(f"{URL}/shopping-list", params={"purchased_only": "true"})
    assert (cleared.status_code, cleared.json()) == (200, {"removed": 1})
    pending_id = from_type.json()["id"]
    assert client.delete(f"{URL}/shopping-list/{pending_id}").status_code == 204
    assert client.delete(f"{URL}/shopping-list/{pending_id}").status_code == 404
    assert client.patch(f"{URL}/shopping-list/{pending_id}", json={"note": "x"}).status_code == 404
    client.post(f"{URL}/shopping-list", json={"material": "ABS"})
    assert client.delete(f"{URL}/shopping-list").json() == {"removed": 1}


def test_a_malformed_shopping_item_id_is_a_422(client: TestClient) -> None:
    assert client.patch(f"{URL}/shopping-list/nope", json={"note": "x"}).status_code == 422


def test_a_draft_changes_supplier_and_a_confirmed_one_is_a_409(client: TestClient) -> None:
    one, two = _supplier(client, "One"), _supplier(client, "Two")
    draft = client.post(f"{URL}/invoices", json=_invoice_body(one, number="M-1")).json()
    moved = client.patch(f"{URL}/invoices/{draft['id']}", json={"supplier_id": two})
    assert moved.status_code == 200, moved.text
    assert moved.json()["supplier_id"] == two
    assert (
        client.patch(f"{URL}/invoices/{draft['id']}", json={"supplier_id": None}).status_code == 422
    )
    missing = client.patch(f"{URL}/invoices/{draft['id']}", json={"supplier_id": str(uuid4())})
    assert missing.status_code == 422, missing.text
    client.patch(f"{URL}/suppliers/{one}", json={"archived": True})
    archived = client.patch(f"{URL}/invoices/{draft['id']}", json={"supplier_id": one})
    assert archived.status_code == 422, archived.text
    frozen = _confirmable(client)
    client.post(f"{URL}/invoices/{frozen}/confirm")
    refused = client.patch(f"{URL}/invoices/{frozen}", json={"supplier_id": two})
    assert refused.status_code == 409, refused.text


def test_a_move_onto_a_taken_number_is_a_409_with_the_first_id(client: TestClient) -> None:
    one, two = _supplier(client, "One"), _supplier(client, "Two")
    first = client.post(f"{URL}/invoices", json=_invoice_body(two, number="X-1")).json()
    draft = client.post(f"{URL}/invoices", json=_invoice_body(one, number="X-1")).json()
    refused = client.patch(f"{URL}/invoices/{draft['id']}", json={"supplier_id": two})
    assert refused.status_code == 409, refused.text
    assert refused.json()["existing_invoice_id"] == first["id"]


def test_the_invoice_list_carries_the_spool_count(client: TestClient) -> None:
    invoice_id = _confirmable(client)
    waiting = _confirmable(client)
    client.post(f"{URL}/invoices/{invoice_id}/confirm")
    rows = {row["id"]: row["spool_count"] for row in client.get(f"{URL}/invoices").json()}
    assert rows[invoice_id] == 2
    assert rows[waiting] == 0


def test_a_line_with_more_spools_than_its_quantity_is_a_422(client: TestClient) -> None:
    supplier = _supplier(client)
    ids = [str(uuid4()), str(uuid4())]
    line = {"description": "x", "quantity": 1, "unit_price": "5", "update_spool_ids": ids}
    made = client.post(f"{URL}/invoices", json=_invoice_body(supplier, lines=[line]))
    assert made.status_code == 422, made.text
    draft = client.post(f"{URL}/invoices", json=_invoice_body(supplier)).json()
    put = client.put(f"{URL}/invoices/{draft['id']}/lines", json=[line])
    assert put.status_code == 422, put.text


def test_a_line_total_or_invoice_total_out_of_range_is_a_422(client: TestClient) -> None:
    supplier = _supplier(client)
    huge = {"description": "x", "quantity": 1_000, "unit_price": "99999999.9999"}
    made = client.post(f"{URL}/invoices", json=_invoice_body(supplier, lines=[huge]))
    assert made.status_code == 422, made.text
    # Each subtotal is in range, their sum with the tax is not: the database refuses it.
    near = {"description": "y", "quantity": 1, "unit_price": "99999999.9999"}
    draft = client.post(f"{URL}/invoices", json=_invoice_body(supplier)).json()
    put = client.put(
        f"{URL}/invoices/{draft['id']}/lines",
        json=[{**near, "line_tax": "99999999.9999"}],
    )
    assert put.status_code == 422, put.text


def test_a_patched_tax_or_shipping_past_the_line_range_is_a_422(client: TestClient) -> None:
    supplier = _supplier(client)
    line = {"description": "x", "quantity": 1, "unit_price": "60000000"}
    draft = client.post(f"{URL}/invoices", json=_invoice_body(supplier, lines=[line])).json()
    for field in ("tax", "shipping"):
        patched = client.patch(f"{URL}/invoices/{draft['id']}", json={field: "60000000"})
        assert patched.status_code == 422, patched.text
    assert client.get(f"{URL}/invoices/{draft['id']}").json()["tax"] != "60000000.0000"


def test_a_price_point_past_its_range_is_a_409_naming_the_line(client: TestClient) -> None:
    supplier = _supplier(client)
    type_id = _type(client)
    product = client.post(
        f"{URL}/suppliers/{supplier}/products",
        json={"type_id": type_id, "sku": "BIG", "pack_size": 1000},
    )
    assert product.status_code == 201, product.text
    line = {
        "description": "x",
        "quantity": 1,
        "unit_price": "200000",
        "type_id": type_id,
        "supplier_product_id": product.json()["id"],
    }
    body = _invoice_body(
        supplier, subtotal="200000", shipping="0", tax="0", total="200000", lines=[line]
    )
    made = client.post(f"{URL}/invoices", json=body)
    assert made.status_code == 201, made.text
    refused = client.post(f"{URL}/invoices/{made.json()['id']}/confirm")
    assert refused.status_code == 409, refused.text
    assert "price point" in refused.text
    assert client.get(f"{URL}/invoices/{made.json()['id']}").json()["status"] == "draft"


def test_an_invoice_for_an_archived_supplier_is_a_422(client: TestClient) -> None:
    supplier = _supplier(client)
    assert client.patch(f"{URL}/suppliers/{supplier}", json={"archived": True}).status_code == 200
    made = client.post(f"{URL}/invoices", json=_invoice_body(supplier))
    assert made.status_code == 422, made.text
    assert "archived" in made.text


def test_confirming_an_invoice_of_an_archived_supplier_is_a_409(client: TestClient) -> None:
    invoice_id = _confirmable(client)
    supplier = client.get(f"{URL}/invoices/{invoice_id}").json()["supplier_id"]
    assert client.patch(f"{URL}/suppliers/{supplier}", json={"archived": True}).status_code == 200
    refused = client.post(f"{URL}/invoices/{invoice_id}/confirm")
    assert refused.status_code == 409, refused.text
    assert "archived" in refused.text
    assert client.get(f"{URL}/invoices/{invoice_id}").json()["status"] == "draft"


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _table(client: TestClient, monkeypatch: pytest.MonkeyPatch, where: Path) -> None:
    state = _state(client)
    monkeypatch.setattr(
        state, "settings", state.settings.model_copy(update={"slicer_profiles_dir": where})
    )


def _profiles(tmp_path: Path) -> Path:
    table = tmp_path / "BBL" / "filament"
    table.mkdir(parents=True)
    (table / "filaments_color_codes.json").write_bytes(
        (FIXTURES / "bambu_filaments_color_codes.json").read_bytes()
    )
    return tmp_path


def test_a_code_gives_its_rgba_and_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _table(client, monkeypatch, _profiles(tmp_path))
    found = client.get(f"{URL}/bambu-colour-codes", params={"code": "13100"})
    assert found.status_code == 200, found.text
    assert found.json() == {
        "code": "13100",
        "name": "Translucent Yellow",
        "rgba": ["#F4EE2A80"],
        "fila_type": "PLA Translucent",
        "fila_id": "GFA17",
    }


def test_an_unknown_code_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _table(client, monkeypatch, _profiles(tmp_path))
    missing = client.get(f"{URL}/bambu-colour-codes", params={"code": "GFA17"})
    assert missing.status_code == 404, missing.text
    assert "GFA17" in missing.text


def test_a_missing_table_is_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _table(client, monkeypatch, tmp_path / "nowhere")
    refused = client.get(f"{URL}/bambu-colour-codes", params={"code": "10101"})
    assert refused.status_code == 503, refused.text
    assert "nowhere" in refused.text


def test_candidates_over_http_and_a_missing_invoice_is_404(client: TestClient) -> None:
    type_id = _type(client)
    spool = client.post(f"{URL}/spools", json={"type_id": type_id})
    assert spool.status_code == 201, spool.text
    line = {"description": "PLA white", "quantity": 1, "unit_price": "40.00", "type_id": type_id}
    made = client.post(
        f"{URL}/invoices",
        json=_invoice_body(
            _supplier(client), subtotal="40", shipping="0", tax="0", total="40", lines=[line]
        ),
    )
    assert made.status_code == 201, made.text
    found = client.get(f"{URL}/invoices/{made.json()['id']}/candidates")
    assert found.status_code == 200, found.text
    (row,) = found.json()
    assert row["position"] == 1
    assert [s["id"] for s in row["uncosted"]] == [spool.json()["id"]]
    assert row["costed"] == []
    assert client.get(f"{URL}/invoices/{MISSING}/candidates").status_code == 404
