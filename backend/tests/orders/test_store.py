"""The orders row store (#4249, spec 2026-10-18 §3.1-3.4, §6.1, §6.3)."""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from scadbuddy.library.libraries import ModelLibrary
from scadbuddy.orders import lifecycle, store
from scadbuddy.orders.models import (
    Actor,
    AddressInput,
    ChargeInput,
    ColourSlot,
    ContactInput,
    Order,
    OrderFilter,
    QuoteLine,
    Reference,
)
from scadbuddy.orders.store import OrderStore, RowNotOwnedError, revision_subject
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

BROWSER = Actor(kind="browser")
COMMIT = "a" * 40


def reference(slug: str = "demo") -> Reference:
    return Reference(
        slug=slug,
        commit=COMMIT,
        inputs={"params": {"width": 10}, "v": 1},
        libraries=[
            ModelLibrary(
                name="BOSL2", url="https://example.test/BOSL2.git", ref="v1", commit="b" * 40
            )
        ],
        template_name="Demo",
        render_key="c" * 64,
    )


def make_order(rows: OrderStore, customer_id: str, **fields: object) -> Order:
    with rows.transaction() as conn:
        return rows.insert_order(
            conn,
            customer_id=customer_id,
            currency="USD",
            created_by=BROWSER,
            **fields,  # type: ignore[arg-type]
        )


def test_a_customer_round_trips_with_contacts_and_addresses(rows: OrderStore) -> None:
    customer = rows.insert_customer(
        display_name="Ada",
        company="Lovelace Ltd",
        notes="",
        tags=["vip"],
        contacts=[
            ContactInput(kind="phone", value="+1 555 0100", label="work", primary=True),
            ContactInput(kind="phone", value="+1 555 0101"),
            ContactInput(kind="email", value="ada@example.test", primary=True),
        ],
        addresses=[
            AddressInput(
                label="home",
                recipient="Ada Lovelace",
                lines=["1 Analytical Way"],
                locality="London",
                postal_code="N1",
                country="GB",
                default_shipping=True,
            )
        ],
    )
    assert customer.display_name == "Ada"
    assert [c.position for c in customer.contacts] == [0, 1, 2]
    assert [c.primary for c in customer.contacts] == [True, False, True]
    assert customer.addresses[0].lines == ["1 Analytical Way"]
    assert rows.customer(customer.id) == customer
    assert [c.id for c in rows.customers(q="love")] == [customer.id]
    assert [c.id for c in rows.customers(q="0101")] == [customer.id]
    assert rows.customers(q="nobody") == []


def test_a_customer_has_at_most_one_primary_per_kind(rows: OrderStore) -> None:
    with pytest.raises(psycopg.errors.UniqueViolation):
        rows.insert_customer(
            display_name="Ada",
            company=None,
            notes="",
            tags=[],
            contacts=[
                ContactInput(kind="email", value="a@example.test", primary=True),
                ContactInput(kind="email", value="b@example.test", primary=True),
            ],
            addresses=[],
        )
    assert rows.customers() == []


def _ada_with_emails(rows: OrderStore, *values: str) -> tuple[str, list[str]]:
    customer = rows.insert_customer(
        display_name="Ada",
        company=None,
        notes="",
        tags=[],
        contacts=[
            ContactInput(kind="email", value=value, primary=i == 0)
            for i, value in enumerate(values)
        ],
        addresses=[],
    )
    return customer.id, [c.id for c in customer.contacts]


def test_replacing_the_primary_email_is_not_a_second_primary(rows: OrderStore) -> None:
    """Review: the partial unique index is checked per row, so a body that drops the
    primary email and brings a new one was refused while the old row still stood."""
    customer_id, _ = _ada_with_emails(rows, "old@example.test")
    with rows.transaction() as conn:
        contacts = rows.replace_contacts(
            conn, customer_id, [ContactInput(kind="email", value="new@example.test", primary=True)]
        )
    assert [(c.value, c.primary) for c in contacts] == [("new@example.test", True)]


def test_moving_the_primary_does_not_depend_on_the_bodys_order(rows: OrderStore) -> None:
    """Review: the new primary listed before the old one was refused, since its row was
    written while the old one was still primary."""
    customer_id, (a, b) = _ada_with_emails(rows, "a@example.test", "b@example.test")
    with rows.transaction() as conn:
        contacts = rows.replace_contacts(
            conn,
            customer_id,
            [
                ContactInput(id=b, kind="email", value="b@example.test", primary=True),
                ContactInput(id=a, kind="email", value="a@example.test", primary=False),
            ],
        )
    assert [(c.id, c.primary) for c in contacts] == [(b, True), (a, False)]


def test_replacing_contacts_keeps_rows_by_id(rows: OrderStore) -> None:
    customer = rows.insert_customer(
        display_name="Ada",
        company=None,
        notes="",
        tags=[],
        contacts=[
            ContactInput(kind="email", value="a@example.test"),
            ContactInput(kind="phone", value="1"),
        ],
        addresses=[AddressInput(label="home", recipient="Ada", lines=["1 Way"])],
    )
    email, phone = customer.contacts
    home = customer.addresses[0]
    with rows.transaction() as conn:
        contacts = rows.replace_contacts(
            conn,
            customer.id,
            [
                ContactInput(id=email.id, kind="email", value="ada@example.test"),
                ContactInput(kind="website", value="https://ada.example.test"),
            ],
        )
        addresses = rows.replace_addresses(
            conn,
            customer.id,
            [AddressInput(id=home.id, label="home", recipient="A. Lovelace", lines=["1 Way"])],
        )
    assert [(c.id == email.id, c.value, c.position) for c in contacts] == [
        (True, "ada@example.test", 0),
        (False, "https://ada.example.test", 1),
    ]
    assert phone.id not in {c.id for c in contacts}
    assert [(a.id, a.recipient) for a in addresses] == [(home.id, "A. Lovelace")]

    other = rows.insert_customer(
        display_name="Bob", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    with pytest.raises(RowNotOwnedError), rows.transaction() as conn:
        rows.replace_addresses(
            conn, other.id, [AddressInput(id=home.id, label="x", recipient="B", lines=[])]
        )
    assert rows.customer(customer.id) is not None
    assert [a.id for a in rows.customer(customer.id).addresses] == [home.id]  # type: ignore[union-attr]


def test_archiving_hides_a_customer_from_the_default_list(rows: OrderStore) -> None:
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    archived = rows.archive_customer(customer.id)
    assert archived is not None and archived.archived_at is not None
    assert rows.customers() == []
    assert [c.id for c in rows.customers(include_archived=True)] == [customer.id]
    assert rows.archive_customer("missing") is None


def test_an_order_takes_a_number_and_copies_nothing_from_the_customer(rows: OrderStore) -> None:
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    order = make_order(rows, customer.id, notes="rush")
    year = datetime.now(UTC).year
    assert order.number == f"ORD-{year}-1"
    assert order.status == "draft"
    assert order.currency == "USD"
    assert order.created_by == BROWSER
    assert order.shipping_address is None
    assert rows.order(order.id) == order
    with rows.transaction() as conn:
        locked = rows.lock_order(conn, order.id)
        assert locked == order
        assert rows.set_order_status(conn, order.id, "quoted").status == "quoted"
    assert rows.order(order.id).status == "quoted"  # type: ignore[union-attr]


def test_order_list_pages_newest_first_by_cursor(rows: OrderStore) -> None:
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    other = rows.insert_customer(
        display_name="Bob", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    made = [make_order(rows, customer.id if n % 2 else other.id) for n in range(5)]
    ids = [o.id for o in made]

    first, cursor = rows.orders(OrderFilter(), None, 2)
    assert [o.id for o in first] == ids[4:2:-1]
    assert cursor is not None
    second, cursor = rows.orders(OrderFilter(), cursor, 2)
    assert [o.id for o in second] == ids[2:0:-1]
    assert cursor is not None
    third, cursor = rows.orders(OrderFilter(), cursor, 2)
    assert [o.id for o in third] == ids[0:1]
    assert cursor is None

    mine, _ = rows.orders(OrderFilter(customer_id=customer.id), None, 50)
    assert [o.id for o in mine] == [ids[3], ids[1]]
    quoted, _ = rows.orders(OrderFilter(status=["quoted"]), None, 50)
    assert quoted == []
    by_number, _ = rows.orders(OrderFilter(q=made[0].number), None, 50)
    assert [o.id for o in by_number] == [ids[0]]
    by_name, _ = rows.orders(OrderFilter(q="bob"), None, 50)
    assert [o.id for o in by_name] == [ids[4], ids[2], ids[0]]
    later = datetime.now(UTC) + timedelta(hours=1)
    assert rows.orders(OrderFilter(since=later), None, 50)[0] == []
    assert len(rows.orders(OrderFilter(until=later), None, 50)[0]) == 5
    with pytest.raises(ValueError, match="cursor"):
        rows.orders(OrderFilter(), "nonsense", 2)


def test_items_are_positioned_and_a_draft_one_deletes(rows: OrderStore) -> None:
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    order = make_order(rows, customer.id)
    with rows.transaction() as conn:
        first = rows.insert_item(
            conn,
            order.id,
            reference=reference(),
            colours=[ColourSlot(slot=0, colour="#ff0000")],
            output_id=None,
            quantity=2,
            title=None,
            notes="",
        )
        second = rows.insert_item(
            conn,
            order.id,
            reference=reference("other"),
            colours=[],
            output_id="out1",
            quantity=1,
            title="Second",
            notes="",
        )
    assert (first.position, second.position) == (0, 1)
    assert first.title == "Demo"  # the template's name by default
    assert first.reference and second.reference
    assert first.reference.libraries[0].name == "BOSL2"
    assert first.colours == [ColourSlot(slot=0, colour="#ff0000", filament_id=None)]
    assert [i.id for i in rows.items(order.id)] == [first.id, second.id]
    assert list(rows.item_inputs()) == [first.reference.inputs, second.reference.inputs]

    with rows.transaction() as conn:
        updated = rows.update_item(conn, first.id, quantity=3, status="quoted")
        assert (updated.quantity, updated.status) == (3, "quoted")
        assert rows.delete_draft_item(conn, first.id) is False  # quoted now
        assert rows.delete_draft_item(conn, second.id) is True
    assert [i.id for i in rows.items(order.id)] == [first.id]


def test_an_item_keeps_at_most_one_accepted_quote(rows: OrderStore) -> None:
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    order = make_order(rows, customer.id)
    with rows.transaction() as conn:
        item = rows.insert_item(
            conn, order.id, reference=reference(), colours=[], output_id=None, quantity=2
        )
        line = QuoteLine(
            kind="manual", label="typed", quantity=1, unit_minor=1500, amount_minor=1500
        )
        one = rows.insert_quote(
            conn,
            item.id,
            source="manual",
            lines=[line],
            unit_cost_minor=1500,
            unit_price_minor=1500,
            quantity=2,
            created_by=BROWSER,
        )
        two = rows.insert_quote(
            conn,
            item.id,
            source="manual",
            lines=[line],
            unit_cost_minor=1600,
            unit_price_minor=1600,
            quantity=2,
            created_by=BROWSER,
        )
        assert one.total_minor == 3000
        assert rows.accept_quote(conn, item.id, one.id).accepted is True
        assert rows.accept_quote(conn, item.id, two.id).accepted is True
    assert [(q.id, q.accepted) for q in rows.quotes(item.id)] == [(one.id, False), (two.id, True)]
    # The partial unique index is the backstop behind `accept_quote`'s one statement.
    with pytest.raises(psycopg.errors.UniqueViolation), rows.transaction() as conn:
        conn.execute("UPDATE order_item_quotes SET accepted = true WHERE id = %s", (one.id,))
    with rows.transaction() as conn:
        rows.unaccept_quotes(conn, item.id)
    assert all(not q.accepted for q in rows.quotes(item.id))


def test_fulfilment_attempts_are_numbered_per_item(rows: OrderStore) -> None:
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    order = make_order(rows, customer.id)
    with rows.transaction() as conn:
        a = rows.insert_item(
            conn, order.id, reference=reference(), colours=[], output_id=None, quantity=2
        )
        b = rows.insert_item(
            conn, order.id, reference=reference(), colours=[], output_id=None, quantity=1
        )
        a1 = rows.insert_fulfilment(conn, a.id, quantity=1, outcome="failed", created_by=BROWSER)
        a2 = rows.insert_fulfilment(conn, a.id, quantity=2, outcome="succeeded", created_by=BROWSER)
        b1 = rows.insert_fulfilment(conn, b.id, quantity=1, outcome="succeeded", created_by=BROWSER)
        assert (a1.attempt, a2.attempt, b1.attempt) == (1, 2, 1)
        accepted = rows.accept_fulfilment(conn, a2.id, datetime.now(UTC))
        assert accepted.accepted_at is not None
        edited = rows.update_fulfilment(conn, a1.id, failure_reason="spaghetti", notes="n")
        assert (edited.failure_reason, edited.notes) == ("spaghetti", "n")
    assert [f.attempt for f in rows.fulfilments(a.id)] == [1, 2]


def test_shipments_charges_payments_and_the_audit_line(rows: OrderStore) -> None:
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    order = make_order(rows, customer.id)
    with rows.transaction() as conn:
        item = rows.insert_item(
            conn, order.id, reference=reference(), colours=[], output_id=None, quantity=2
        )
        charges = rows.replace_charges(
            conn,
            order.id,
            [
                ChargeInput(kind="shipping", label="Post", amount_minor=500),
                ChargeInput(kind="discount", label="Friends", amount_minor=-200),
            ],
        )
        payment = rows.insert_payment(
            conn,
            order.id,
            amount_minor=1000,
            method="cash",
            reference=None,
            received_at=None,
            notes="",
        )
        shipment = rows.insert_shipment(
            conn,
            order.id,
            carrier="Post",
            service=None,
            tracking="T1",
            shipped_at=None,
            address=None,
            cost_minor=500,
            notes="",
            items=[(item.id, 1)],
        )
        shipment = rows.update_shipment(conn, shipment.id, tracking="T2", items=[(item.id, 2)])
        event = rows.insert_event(
            conn, order.id, BROWSER, "order.status", {"from": "draft", "to": "quoted"}
        )
    assert [(c.kind, c.amount_minor, c.position) for c in charges] == [
        ("shipping", 500, 0),
        ("discount", -200, 1),
    ]
    assert rows.charges(order.id) == charges
    assert rows.payments(order.id) == [payment]
    assert shipment.tracking == "T2"
    assert [(s.order_item_id, s.quantity) for s in shipment.items] == [(item.id, 2)]
    assert rows.shipments(order.id) == [shipment]
    assert rows.events(order.id) == [event]
    assert event.detail == {"from": "draft", "to": "quoted"}
    assert event.by == BROWSER


def _item_with_reference(rows: OrderStore, **fields: object) -> tuple[str, str]:
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    order = make_order(rows, customer.id)
    with rows.transaction() as conn:
        item = rows.insert_item(
            conn,
            order.id,
            reference=reference(),
            colours=[ColourSlot(slot=0, colour="#ff0000")],
            output_id=None,
            quantity=1,
            **fields,  # type: ignore[arg-type]
        )
    return order.id, item.id


def _request_item(rows: OrderStore) -> str:
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    order = make_order(rows, customer.id)
    with rows.transaction() as conn:
        item = rows.insert_item(
            conn,
            order.id,
            kind="request",
            reference=None,
            colours=[],
            output_id=None,
            quantity=1,
            title="A dragon",
        )
    return item.id


def test_a_request_item_has_no_reference_until_its_design(rows: OrderStore) -> None:
    item = rows.item(_request_item(rows))
    assert item is not None
    assert item.kind == "request" and item.reference is None and item.approval == "design_first"


def test_the_table_refuses_a_half_designed_request(rows: OrderStore, pool: PgPool) -> None:
    item_id = _request_item(rows)
    with pytest.raises(psycopg.errors.CheckViolation), rows.transaction() as conn:
        conn.execute("UPDATE order_items SET slug = 'demo' WHERE id = %s", (item_id,))


def test_revisions_are_numbered_per_item(rows: OrderStore) -> None:
    _, item_id = _item_with_reference(rows)
    other_id = _request_item(rows)
    with rows.transaction() as conn:
        for _ in range(2):
            rows.insert_revision(
                conn, item_id, reference=reference(), colours=[], note="", created_by=BROWSER
            )
        rows.insert_revision(
            conn, other_id, reference=reference(), colours=[], note="", created_by=BROWSER
        )
    assert [r.number for r in rows.revisions(item_id)] == [1, 2]
    assert rows.revisions(other_id)[0].number == 1


def test_set_design_copies_the_revision_onto_the_item(rows: OrderStore) -> None:
    item_id = _request_item(rows)
    colours = [ColourSlot(slot=0, colour="#00ff00")]
    with rows.transaction() as conn:
        revision = rows.insert_revision(
            conn, item_id, reference=reference(), colours=colours, note="v1", created_by=BROWSER
        )
        moved = rows.set_design(conn, item_id, revision, output_id=None)
    assert moved.reference == revision.reference and moved.colours == revision.colours
    assert moved.current_revision_id == revision.id
    assert moved.kind == "request"
    assert rows.revision(revision.id) == revision


def test_set_design_makes_a_custom_item_a_model_item(rows: OrderStore) -> None:
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    order = make_order(rows, customer.id)
    with rows.transaction() as conn:
        custom = rows.insert_custom_item(conn, order.id, title="Bracket", quantity=1)
        revision = rows.insert_revision(
            conn, custom.id, reference=reference(), colours=[], note="", created_by=BROWSER
        )
        moved = rows.set_design(conn, custom.id, revision, output_id=None)
    assert moved.kind == "model" and moved.requires_printing


def test_stamp_revision_sets_only_what_it_is_given(rows: OrderStore) -> None:
    item_id = _request_item(rows)
    sent = datetime.now(UTC)
    with rows.transaction() as conn:
        revision = rows.insert_revision(
            conn, item_id, reference=reference(), colours=[], note="", created_by=BROWSER
        )
        stamped = rows.stamp_revision(conn, revision.id, sent_at=sent)
        answered = rows.stamp_revision(
            conn,
            revision.id,
            answer="changes_requested",
            answer_note="bigger",
            answered_at=sent,
            answered_by=BROWSER,
        )
    assert stamped.sent_at == sent and stamped.answer is None
    assert answered.sent_at == sent and answered.answer == "changes_requested"
    assert answered.answer_note == "bigger" and answered.answered_by == BROWSER


def test_fulfilments_leave_out_test_attempts_unless_asked(rows: OrderStore) -> None:
    _, item_id = _item_with_reference(rows)
    with rows.transaction() as conn:
        revision = rows.insert_revision(
            conn, item_id, reference=reference(), colours=[], note="", created_by=BROWSER
        )
        rows.insert_fulfilment(conn, item_id, quantity=1, outcome="succeeded", created_by=BROWSER)
        rows.insert_fulfilment(
            conn,
            item_id,
            quantity=0,
            outcome="unknown",
            created_by=BROWSER,
            purpose="test",
            revision_id=revision.id,
        )
    assert [a.purpose for a in rows.fulfilments(item_id)] == ["production"]
    assert [a.purpose for a in rows.fulfilments(item_id, purpose=None)] == [
        "production",
        "test",
    ]


def test_a_test_attempt_needs_its_revision(rows: OrderStore) -> None:
    _, item_id = _item_with_reference(rows)
    with pytest.raises(psycopg.errors.CheckViolation), rows.transaction() as conn:
        rows.insert_fulfilment(
            conn, item_id, quantity=0, outcome="unknown", created_by=BROWSER, purpose="test"
        )


def test_item_inputs_name_every_revisions_inputs(rows: OrderStore) -> None:
    item_id = _request_item(rows)
    with rows.transaction() as conn:
        for asset in ("asset-1", "asset-2"):
            ref = reference().model_copy(update={"inputs": {"params": {"logo": asset}}})
            rows.insert_revision(
                conn, item_id, reference=ref, colours=[], note="", created_by=BROWSER
            )
    inputs = list(rows.item_inputs())
    assert {"params": {"logo": "asset-1"}} in inputs
    assert {"params": {"logo": "asset-2"}} in inputs


def test_a_revision_attachment_names_its_revision_and_item(rows: OrderStore) -> None:
    order_id, item_id = _item_with_reference(rows)
    with rows.transaction() as conn:
        revision = rows.insert_revision(
            conn, item_id, reference=reference(), colours=[], note="", created_by=BROWSER
        )
        attachment = rows.insert_attachment(
            conn,
            attachment_id="att1",
            order_id=order_id,
            subject=revision_subject(revision.id),
            kind="image",
            role="render",
            filename="r.png",
            media_type="image/png",
            size_bytes=1,
            sha256="d" * 64,
            blob_key="attachment-" + "d" * 64,
            caption="",
            created_by=BROWSER,
            subject_item_id=item_id,
        )
    assert attachment.revision_id == revision.id and attachment.item_id == item_id
    assert attachment.fulfilment_id is None


def _functions(path: Path) -> Iterator[ast.FunctionDef]:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef):
            yield node


def _sql_by_function(path: Path) -> Iterator[tuple[str, str]]:
    """Each function's name with the string constants in its body joined."""
    for fn in _functions(path):
        parts = [
            node.value
            for node in ast.walk(fn)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        yield fn.name, "".join(parts)


#: The columns that are an item's design (spec 2026-11-02 §4.1).
DESIGN_COLUMNS = frozenset(
    {
        "slug",
        "commit",
        "inputs",
        "libraries",
        "template_name",
        "render_key",
        "colours",
        "current_revision_id",
    }
)


def _keys_written(fn: ast.FunctionDef) -> set[str]:
    """The names a function puts in a dict: literal keys, ``d["k"] = …`` and
    ``d.update(k=…)``."""
    keys: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            keys |= {
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                keys.add(node.slice.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
        ):
            keys |= {kw.arg for kw in node.keywords if kw.arg is not None}
    return keys


def _updates_order_items(fn: ast.FunctionDef) -> bool:
    """Whether the function calls ``_update(conn, "order_items", …)``."""
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_update"
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "order_items"
        for node in ast.walk(fn)
    )


def _callers(path: Path, attr: str) -> set[str]:
    """The functions whose body calls ``<something>.attr(``."""
    return {
        fn.name
        for fn in _functions(path)
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr
            for node in ast.walk(fn)
        )
    }


def test_only_the_revision_writer_sets_an_items_design() -> None:
    # Spec §4.1: past insert_item's INSERT, set_design is the one UPDATE of the design
    # columns, and Lifecycle._revise its one caller.
    store_path = Path(store.__file__)
    store_writers = {
        fn
        for fn, text in _sql_by_function(store_path)
        if re.search(
            r"UPDATE order_items SET[^\"]*\b(slug|commit|inputs|render_key|colours)\b", text
        )
    }
    assert store_writers == {"set_design"}
    # The generic writer's fields come from dict keys, not SQL text.
    generic = {
        fn.name: _keys_written(fn) & DESIGN_COLUMNS
        for fn in _functions(store_path)
        if _updates_order_items(fn)
    }
    assert generic and not any(generic.values()), generic
    assert {"reference", "colours"}.isdisjoint(inspect.signature(OrderStore.update_item).parameters)
    package = Path(lifecycle.__file__).parents[1]
    callers = {
        (str(path.relative_to(package)), name)
        for path in sorted(package.rglob("*.py"))
        for name in _callers(path, "set_design")
    }
    assert callers == {("orders/lifecycle.py", "_revise")}
