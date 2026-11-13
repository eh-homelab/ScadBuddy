"""The shapes of Bambuddy's MakerWorld routes, and ScadBuddy's own over them (#6119).

Bambuddy's are ``backend/app/schemas/makerworld.py`` of maziggy/bambuddy, read at tag
``v1.3.2b1-daily.20261112`` (prod runs Bambuddy's ``daily`` image):
https://github.com/maziggy/bambuddy/blob/v1.3.2b1-daily.20261112/backend/app/schemas/makerworld.py
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Bambuddy(BaseModel):
    model_config = ConfigDict(extra="ignore")


class BambuddyMakerWorldStatus(_Bambuddy):
    """``GET /makerworld/status`` (``MakerWorldStatus``)."""

    has_cloud_token: bool
    can_download: bool
    sign_in_expired: bool = False


class BambuddyMakerWorldResolved(_Bambuddy):
    """``POST /makerworld/resolve`` (``MakerWorldResolvedModel``). ``design`` and
    ``instances`` are MakerWorld's own, passed through by Bambuddy as they came."""

    model_id: int
    profile_id: int | None = None
    design: dict[str, Any]
    instances: list[dict[str, Any]]
    already_imported_library_ids: list[int] = Field(default_factory=list)


class BambuddyMakerWorldImported(_Bambuddy):
    """``POST /makerworld/import`` (``MakerWorldImportResponse``)."""

    library_file_id: int
    filename: str
    folder_id: int | None = None
    profile_id: int | None = None
    was_existing: bool


# ScadBuddy's routes


class MakerWorldStatus(BaseModel):
    """Whether Bambuddy can download from MakerWorld for ScadBuddy's API key."""

    has_cloud_token: bool = Field(
        description="Bambuddy holds a Bambu Cloud token for the key's owner (or for itself"
        " while its auth is off)"
    )
    can_download: bool = Field(
        description="An import can download: false means no Bambu Cloud sign-in in"
        " Bambuddy, or (Bambuddy's auth on) an API key without its cloud access"
    )
    sign_in_expired: bool = Field(
        description="A token is stored but Bambu Cloud refused it: sign in again in Bambuddy"
    )


class MakerWorldResolveRequest(BaseModel):
    url: str = Field(
        min_length=1,
        max_length=2048,
        description="A MakerWorld model link, e.g. https://makerworld.com/en/models/123"
        "#profileId-456",
    )


class MakerWorldResolved(BaseModel):
    model_id: int = Field(description="The MakerWorld design id, for the import")
    profile_id: int | None = Field(
        description="The print profile the link's `#profileId-` names, if it names one"
    )
    design: dict[str, Any] = Field(
        description="MakerWorld's own record of the design, as Bambuddy passed it on"
    )
    instances: list[dict[str, Any]] = Field(
        description="MakerWorld's print profiles (plate sets); each `profileId` is an"
        " import's `profile_id`"
    )
    already_imported_bambuddy_ids: list[int] = Field(
        description="Bambuddy library files an earlier import of this model made"
    )
    already_imported_library_ids: list[int] = Field(
        description="On `library_backend` native: ScadBuddy's own library files holding the"
        " same bytes as those Bambuddy files, the ids the order and print tools take. On"
        " `bambuddy` the same as `already_imported_bambuddy_ids`."
    )
    library: Literal["native", "bambuddy"] = Field(
        description="Which library `already_imported_library_ids` names"
    )


class MakerWorldImportRequest(BaseModel):
    model_id: int = Field(ge=1, description="The design id `resolve` answered")
    profile_id: int | None = Field(
        default=None,
        ge=1,
        description="The print profile (an instance's `profileId`); Bambuddy takes the"
        " design's first without one",
    )
    folder_id: int | None = Field(
        default=None,
        ge=1,
        description="On `library_backend` native: the ScadBuddy library folder the file is"
        " put in (a `MakerWorld` folder at the top level without one). Refused on"
        " `bambuddy`, where `bambuddy_folder_id` places it.",
    )
    bambuddy_folder_id: int | None = Field(
        default=None,
        ge=1,
        description="The Bambuddy library folder Bambuddy saves its copy in, such as a"
        " print project's `folder_id`; Bambuddy's own `MakerWorld` folder without one",
    )


class MakerWorldImported(BaseModel):
    library: Literal["native", "bambuddy"] = Field(
        description="Which library `library_file_id` and `folder_id` are in"
    )
    library_file_id: int = Field(
        description="The library file to print or quote: on native, ScadBuddy's own (what"
        " `order_item_add`'s `library_file_id` takes); on bambuddy, Bambuddy's"
    )
    folder_id: int | None = Field(description="Its folder in that library; null is the top")
    filename: str
    profile_id: int | None = Field(description="The profile Bambuddy imported")
    bambuddy_library_file_id: int = Field(description="Bambuddy's copy of the file")
    bambuddy_folder_id: int | None = Field(description="The Bambuddy folder of that copy")
    was_existing: bool = Field(
        description="Nothing new was downloaded or stored: Bambuddy had this profile already"
        " and, on native, ScadBuddy's library already held the same bytes"
    )
