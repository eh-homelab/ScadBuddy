"""`/api/v1/payments/providers` (#4957): the payment providers ScadBuddy offers and the
shop's account at each. Connecting, disconnecting and choosing the default are a
person's own (``auth/routes.py`` ``PERSON_ENDPOINTS``, plan gap 12). No answer carries a
secret. Problem types are ``https://scadbuddy.dev/problems/payments/<slug>``.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio

from fastapi import APIRouter, Response, status

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.core.problems import ApiError
from scadbuddy.payments.component import PaymentsDep
from scadbuddy.payments.credentials import KekMissingError, ProviderBusyError
from scadbuddy.payments.provider import ProviderError
from scadbuddy.payments.service import (
    CurrencyMismatchError,
    CurrencyUnknownError,
    FieldError,
    ProviderConnect,
    ProviderNotFoundError,
    ProviderStatus,
)
from scadbuddy.payments.venmo import VenmoPageMissingError

PROBLEMS = "https://scadbuddy.dev/problems/payments/"

router = APIRouter(prefix="/payments", tags=["payments"])


def not_found(error: ProviderNotFoundError) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, str(error), type_=PROBLEMS + "provider_not_found")


@router.get(
    "/providers",
    response_model=list[ProviderStatus],
    summary="The payment providers and the shop's account at each",
)
async def list_providers(payments: PaymentsDep) -> list[ProviderStatus]:
    return await asyncio.to_thread(payments.service.statuses)


@router.put(
    "/providers/{provider}",
    response_model=ProviderStatus,
    summary="Connect the shop's account at a provider",
    description=(
        "The provider checks the account first. 409 `kek_missing` without"
        " SCADBUDDY_SECRET_KEY_FILE; 409 `currency_mismatch` when the account takes"
        " payments in one currency and it is not `orders_currency`; 409"
        " `currency_unknown` when such an account names no currency; 409"
        " `venmo_page_missing` for PayPal's `venmo` `on` until the pay page is switched"
        " on (`PAY_PAGE_READY`); a field left out that has a `default` takes it; 422 with the"
        " provider's own text when it refuses."
    ),
)
async def connect_provider(
    provider: str, body: ProviderConnect, payments: PaymentsDep, settings: SettingsStoreDep
) -> ProviderStatus:
    currency = await asyncio.to_thread(lambda: settings.snapshot().runtime.orders_currency)
    try:
        return await payments.service.connect(provider, body, shop_currency=currency)
    except ProviderNotFoundError as error:
        raise not_found(error) from None
    except FieldError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(error),
            errors=[{"loc": ["body", *error.loc], "msg": str(error)}],
        ) from None
    except KekMissingError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT, str(error), type_=PROBLEMS + "kek_missing"
        ) from None
    except CurrencyMismatchError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT, str(error), type_=PROBLEMS + "currency_mismatch"
        ) from None
    except VenmoPageMissingError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT, str(error), type_=PROBLEMS + "venmo_page_missing"
        ) from None
    except CurrencyUnknownError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT, str(error), type_=PROBLEMS + "currency_unknown"
        ) from None
    except ProviderError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(error),
            errors=[{"loc": ["body"], "msg": str(error)}],
        ) from None


@router.delete(
    "/providers/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Disconnect a provider",
    description=(
        "409 `provider_has_open_requests` while an open, pending or failed payment"
        " request names it."
    ),
)
async def disconnect_provider(provider: str, payments: PaymentsDep) -> Response:
    try:
        await asyncio.to_thread(payments.service.disconnect, provider)
    except ProviderNotFoundError as error:
        raise not_found(error) from None
    except ProviderBusyError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT, str(error), type_=PROBLEMS + "provider_has_open_requests"
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/providers/{provider}/default",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Make a connected provider the default",
)
async def default_provider(provider: str, payments: PaymentsDep) -> Response:
    try:
        await asyncio.to_thread(payments.service.set_default, provider)
    except ProviderNotFoundError as error:
        raise not_found(error) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
