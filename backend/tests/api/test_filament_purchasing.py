"""`/api/v1/filament` suppliers, products and prices (#6266)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

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
