import dataclasses
import inspect
from typing import List, Optional

from fastapi import Depends, HTTPException, Query, Request, APIRouter

from ..query_registration import name_to_query_type
from .authentication import (
    get_current_user,
    get_user_for_token,
    new_token,
    revoke_token,
)
from .utils import (
    construct_array_response,
    construct_entries_response,
    construct_resource,
    get_chunk,
    # get_dask_client,
    get_entry,
    NoEntry,
    WrongTypeForRoute,
    UnsupportedMediaTypes,
)
from . import models


router = APIRouter()


@router.post("/token", response_model=models.Token)
async def create_token(username: str, current_user=Depends(get_current_user)):
    "Generate an API access token."
    if (username != current_user) and (current_user != "admin"):
        raise HTTPException(
            status_code=403, detail="Only admin can generate tokens for other users."
        )
    return {"access_token": new_token(username), "token_type": "bearer"}


@router.delete("/token")
async def delete_token(token: models.Token, current_user=Depends(get_current_user)):
    "Generate an API access token."
    username = get_user_for_token(token.access_token)
    if (username != current_user) and (current_user != "admin"):
        raise HTTPException(
            status_code=403, detail="Only admin can delete other users' tokens."
        )
    revoke_token(token.access_token)
    return


def declare_search_route(router):
    """
    This is done dynamically at router startup.

    We check the registry of known search query types, which is user
    configurable, and use that to define the allowed HTTP query parameters for
    this route.
    """

    async def search(
        path: Optional[str] = "/",
        fields: Optional[List[models.EntryFields]] = Query(list(models.EntryFields)),
        offset: Optional[int] = Query(0, alias="page[offset]"),
        limit: Optional[int] = Query(10, alias="page[limit]"),
        current_user=Depends(get_current_user),
        **filters,
    ):
        try:
            return construct_entries_response(
                path,
                offset,
                limit,
                fields,
                filters,
                current_user,
            )
        except NoEntry:
            raise HTTPException(status_code=404, detail="No such entry.")
        except WrongTypeForRoute as err:
            raise HTTPException(status_code=404, detail=err.msg)

    # Black magic here! FastAPI bases its validation and auto-generated swagger
    # documentation on the signature of the route function. We do not know what
    # that signature should be at compile-time. We only know it once we have a
    # chance to check the user-configurable registry of query types. Therefore,
    # we modify the signature here, at runtime, just before handing it to
    # FastAPI in the usual way.

    # When FastAPI calls the function with these added parameters, they will be
    # accepted via **filters.

    # Make a copy of the original parameters.
    signature = inspect.signature(search)
    parameters = list(signature.parameters.values())
    # Drop the **filters parameter from the signature.
    del parameters[-1]
    # Add a parameter for each field in each type of query.
    for name, query in name_to_query_type.items():
        for field in dataclasses.fields(query):
            # The structured "alias" here is based on
            # https://mglaman.dev/blog/using-json-router-query-your-search-router-indexes
            injected_parameter = inspect.Parameter(
                name=f"filter___{name}___{field.name}",
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=Query(None, alias=f"filter[{name}][condition][{field.name}]"),
                annotation=Optional[field.type],
            )
        parameters.append(injected_parameter)
    search.__signature__ = signature.replace(parameters=parameters)
    # End black magic

    # Register the search route.
    router.get("/search/{path:path}", response_model=models.Response)(search)
    router.get("/search", response_model=models.Response, include_in_schema=False)(
        search
    )


@router.get("/metadata/{path:path}", response_model=models.Response)
@router.get("/metadata", response_model=models.Response, include_in_schema=False)
async def metadata(
    path: Optional[str] = "/",
    fields: Optional[List[models.EntryFields]] = Query(list(models.EntryFields)),
    current_user=Depends(get_current_user),
):
    "Fetch the metadata for one Catalog or Data Source."

    path = path.rstrip("/")
    *_, key = path.rpartition("/")
    try:
        entry = get_entry(path, current_user)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such entry.")

    resource = construct_resource(key, entry, fields)
    return models.Response(data=resource)


@router.get("/entries/{path:path}", response_model=models.Response)
@router.get("/entries", response_model=models.Response, include_in_schema=False)
async def entries(
    path: Optional[str] = "/",
    offset: Optional[int] = Query(0, alias="page[offset]"),
    limit: Optional[int] = Query(10, alias="page[limit]"),
    fields: Optional[List[models.EntryFields]] = Query(list(models.EntryFields)),
    current_user=Depends(get_current_user),
):
    "List the entries in a Catalog, which may be sub-Catalogs or DataSources."

    try:
        return construct_entries_response(
            path,
            offset,
            limit,
            fields,
            {},
            current_user,
        )
    except NoEntry:
        raise HTTPException(status_code=404, detail="No such entry.")
    except WrongTypeForRoute as err:
        raise HTTPException(status_code=404, detail=err.msg)


def datasource(
    path: str,
    current_user: str = Depends(get_current_user),
):
    "Specify a path parameter and use it to look up a datasource."
    try:
        return get_entry(path, current_user)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such entry.")


def block(
    # Ellipsis as the "default" tells FastAPI to make this parameter required.
    block: str = Query(..., min_length=1, regex="^[0-9](,[0-9])*$"),
):
    "Specify and parse a block index parameter."
    parsed_block = tuple(map(int, block.split(",")))
    return parsed_block


@router.get("/blob/array/{path:path}", response_model=models.Response, name="array")
def blob_array(
    request: Request,
    datasource=Depends(datasource),
    block=Depends(block),
):
    """
    Fetch a chunk of array-like data.
    """
    try:
        chunk = datasource.read().blocks[block]
    except IndexError:
        raise HTTPException(status_code=422, detail="Block index out of range")
    array = get_chunk(chunk)
    try:
        return construct_array_response(array, request.headers)
    except UnsupportedMediaTypes as err:
        # TODO Should we just serve a default representation instead of
        # returning this error codde?
        raise HTTPException(status_code=406, detail=", ".join(err.supported_types))


@router.get(
    "/blob/variable/{path:path}", response_model=models.Response, name="variable"
)
def blob_variable(
    request: Request,
    datasource=Depends(datasource),
    block=Depends(block),
):
    """
    Fetch a chunk of array-like data.
    """
    try:
        # Lookup block on the `data` attribute of the Variable.
        chunk = datasource.read().data.blocks[block]
    except IndexError:
        raise HTTPException(status_code=422, detail="Block index out of range")
    array = get_chunk(chunk)
    try:
        return construct_array_response(array, request.headers)
    except UnsupportedMediaTypes as err:
        # TODO Should we just serve a default representation instead of
        # returning this error codde?
        raise HTTPException(status_code=406, detail=", ".join(err.supported_types))
