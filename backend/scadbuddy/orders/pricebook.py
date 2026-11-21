"""The price book in force (#4250, spec 2026-10-18 §5.1, plan Ruling 4): the policy
and the estimate factor (stored settings, read at each use: ``APPLIES`` says ``live``)
and the filament records not archived, read in one place, so the routes, the quote and
the estimate price with the same book."""

from __future__ import annotations

from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.models import PriceBook, PricingPolicy
from scadbuddy.orders.store import OrderStore


class PriceBookReader:
    def __init__(self, settings: SettingsStore, store: OrderStore) -> None:
        self.settings = settings
        self.store = store

    def policy(self) -> tuple[str, PricingPolicy, str]:
        """``orders_currency``, the policy and ``rough_estimate_factor``, as stored now."""
        runtime = self.settings.snapshot().runtime
        policy = PricingPolicy(
            machine_rate_per_hour=runtime.pricing_machine_rate_per_hour,
            setup_fee=runtime.pricing_setup_fee,
            labour_per_item=runtime.pricing_labour_per_item,
            markup_percent=runtime.pricing_markup_percent,
            rounding=runtime.pricing_rounding,
        )
        return runtime.orders_currency, policy, runtime.rough_estimate_factor

    def snapshot(self, *, include_archived: bool = False) -> PriceBook:
        """The book in one read; ``include_archived`` adds the archived filaments, for
        the editor that unarchives them (#4977). Blocking: call it in a thread."""
        currency, policy, factor = self.policy()
        return PriceBook(
            currency=currency,
            minor_exponent=minor_exponent(currency),
            policy=policy,
            rough_estimate_factor=factor,
            filaments=self.store.filaments(include_archived=include_archived),
        )
