"""The prime tower choice on its own (#3628, #3770).

Apart from :mod:`scadbuddy.bambuddy.prime_tower`, which imports the Bambuddy client:
Arrange's workflow payloads (`workflows/models.py`) carry this model, and the client
must not enter the workflow sandbox.
"""

from __future__ import annotations

from pydantic import BaseModel, model_validator


class PrimeTowerChoice(BaseModel):
    """The prime tower for one print, over whatever the template declares."""

    #: Whether the slicer prints a prime tower when the plate changes filament.
    enabled: bool
    #: With the tower on, print it only on layers that change filament; omitted keeps
    #: the template's, else the process preset's (off). Ignored with the tower off.
    no_sparse_layers: bool | None = None

    @model_validator(mode="after")
    def _off_is_only_off(self) -> PrimeTowerChoice:
        # With the tower off, "No sparse layers" means nothing: dropped, so the two ways
        # of saying "off" are one print with one run key (#3648 review).
        if not self.enabled:
            self.no_sparse_layers = None
        return self
