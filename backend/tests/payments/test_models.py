"""The payments seam's wire shapes (#4957): what a provider may hand ScadBuddy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.payments.models import HostedRequest, ProviderEvent


def test_a_hosted_url_must_be_https() -> None:
    with pytest.raises(ValidationError):
        HostedRequest(provider_ref="r1", hosted_url="http://pay.example/r1")  # type: ignore[arg-type]
    ok = HostedRequest(provider_ref="r1", hosted_url="https://pay.example/r1")  # type: ignore[arg-type]
    assert ok.hosted_url.scheme == "https"


@pytest.mark.parametrize(
    "bad",
    [
        {"amount_minor": 0},
        {"fee_minor": -1},
        {"event_id": ""},
        {"event_id": "e" * 201},
    ],
)
def test_a_provider_event_refuses_out_of_range_values(bad: dict[str, object]) -> None:
    fields: dict[str, object] = {"event_id": "e1", "kind": "paid"} | bad
    with pytest.raises(ValidationError):
        ProviderEvent.model_validate(fields)


def test_a_secret_field_with_choices_is_refused() -> None:
    """#4960: choices are offered in a list; a secret is typed and never shown."""
    from scadbuddy.payments.provider import ProviderField

    with pytest.raises(ValidationError):
        ProviderField(name="key", label="Key", secret=True, choices=["a", "b"])
    field = ProviderField(name="mode", label="Mode", secret=False, choices=["a", "b"])
    assert field.choices == ["a", "b"]


@pytest.mark.parametrize(
    "bad",
    [
        {"choices": ["a", "b"], "default": "c"},
        {"choices": ["a", "b"], "unavailable": {"c": "why"}},
        {"choices": None, "unavailable": {"a": "why"}},
    ],
    ids=["default", "unavailable", "no-choices"],
)
def test_a_default_or_an_unavailable_choice_must_be_one_of_the_choices(
    bad: dict[str, object],
) -> None:
    """#5769 review: a typo would save a value the form cannot show."""
    from scadbuddy.payments.provider import ProviderField

    with pytest.raises(ValidationError):
        ProviderField.model_validate({"name": "mode", "label": "Mode", "secret": False} | bad)
    ok = ProviderField(
        name="mode", label="Mode", secret=False, choices=["a", "b"], default="a",
        unavailable={"b": "why"},
    )  # fmt: skip
    assert (ok.default, ok.unavailable) == ("a", {"b": "why"})


def test_a_provider_event_takes_the_edges() -> None:
    event = ProviderEvent(event_id="e" * 200, kind="paid", amount_minor=1, fee_minor=0)
    assert (event.amount_minor, event.fee_minor) == (1, 0)
