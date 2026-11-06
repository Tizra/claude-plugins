"""MCP server exposing the Tizra admin API to Claude Code over stdio.

Configuration comes from TIZRA_BASE_URL and TIZRA_AUTH_TOKEN. Any that are missing are requested
from the user through MCP elicitation on the first tool call that needs the API.
Never read stdin or prompt on stderr here: under stdio transport stdin carries the protocol.
"""

import builtins
import ctypes
import io
import json
import logging
import os
import re
import threading
import traceback
from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.elicitation import AcceptedElicitation, CancelledElicitation, DeclinedElicitation
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from pydantic import BaseModel, Field, create_model

from tizra_client import TizraAuthError, TizraClient, TizraError, find_admin_base_url

T = TypeVar("T")

# httpx logs every request at INFO; on stderr that is just noise in Claude Code's MCP log.
logging.getLogger("httpx").setLevel(logging.WARNING)

CONFIG_VARS = {
    "base_url": (
        "TIZRA_BASE_URL",
        "The site's URL, e.g. https://www.example.com; its admin host is found from it",
    ),
    "auth_token": ("TIZRA_AUTH_TOKEN", "The site's APIToken site parameter"),
}

mcp = MCPServer(
    "tizra-admin",
    instructions="Read and modify Tizra content and meta-types through the admin API.",
)

_config: dict[str, str] = {
    key: value
    for key, (var, _) in CONFIG_VARS.items()
    if (value := os.environ.get(var, "").strip())
}
_client: TizraClient | None = None
"""The session's client, once the API has accepted its token."""

_lock = threading.Lock()
"""Guards _config and _client, which concurrent tool calls share from worker threads.

A check of the configuration and the write it justifies happen in one hold. Never held across
a request or a prompt: a call waiting on either must not stall the others.
"""

REJECTED_TOKEN = (
    "not authenticated: the site rejected the API token, so it has been forgotten; "
    "the next call will ask for it again"
)


def _missing() -> list[str]:
    return [key for key in CONFIG_VARS if key not in _config]


def _can_elicit(ctx: Context) -> bool:
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def ask_config(ctx: Context) -> Elicit[BaseModel] | None:
    """Ask the user for missing configuration, unless there is nothing to ask or no way to ask.

    A resolver rather than a direct ctx.elicit() call, so the SDK can elicit over either
    protocol: InputRequiredResult on >= 2026-07-28, a mid-call request on older ones.
    """
    missing = _missing()
    if _client is not None or not missing or not _can_elicit(ctx):
        return None
    fields: dict[str, Any] = {key: (str, Field(description=CONFIG_VARS[key][1])) for key in missing}
    names = ", ".join(CONFIG_VARS[key][0] for key in missing)
    return Elicit(f"The Tizra admin tools need: {names}.", create_model("TizraConfig", **fields))


def get_client(
    # Spelled out rather than `ElicitationResult[BaseModel] | None`: the SDK only hands over
    # the full outcome, decline included, when it can see the members of the union.
    answer: Annotated[
        AcceptedElicitation[BaseModel] | DeclinedElicitation | CancelledElicitation | None,
        Resolve(ask_config),
    ],
) -> TizraClient:
    """Return the session's client, or a new one built from env vars plus elicited values.

    A new client's token has not been checked yet; see get_checked_client.
    """
    if (client := _client) is not None:
        return client
    with _lock:
        missing = _missing()
        # When ask_config declines to ask, answer arrives as accepted with data None.
        if missing and isinstance(answer, AcceptedElicitation) and answer.data is not None:
            values = {key: str(getattr(answer.data, key, "")).strip() for key in missing}
            _config.update({key: value for key, value in values.items() if value})
            missing = _missing()
        config = dict(_config)
    if missing:
        names = ", ".join(CONFIG_VARS[key][0] for key in missing)
        raise ToolError(
            f"Tizra configuration is missing; set {names} in the MCP server environment."
        )
    try:
        # A public host only redirects the API to the admin host, so move to it up front.
        base_url = find_admin_base_url(config["base_url"])
        client = TizraClient(base_url, config["auth_token"])
    except ValueError as e:
        # Forget the rejected base URL so the next call asks again, unless it has changed.
        with _lock:
            if _config.get("base_url") == config["base_url"]:
                _config.pop("base_url")
        raise ToolError(str(e)) from e
    with _lock:
        if _config.get("base_url") == config["base_url"]:
            _config["base_url"] = base_url
    return client


def _accepts(client: TizraClient) -> bool:
    """Whether the API accepts the client's token, checked once per client."""
    if not hasattr(client, "_accepted"):
        try:
            client.check_auth()
            client._accepted = True  # type: ignore[attr-defined]
        except TizraAuthError:
            client._accepted = False  # type: ignore[attr-defined]
        except TizraError as e:
            raise ToolError(json.dumps(e.to_dict())) from e
    return client._accepted  # type: ignore[attr-defined]


class TokenAgain(BaseModel):
    auth_token: str = Field(description=CONFIG_VARS["auth_token"][1])


def ask_token_again(
    client: Annotated[TizraClient, Resolve(get_client)], ctx: Context
) -> Elicit[TokenAgain] | None:
    """Ask for the token again when the API rejects a new client's token."""
    if client is _client or _accepts(client) or not _can_elicit(ctx):
        return None
    # Worded differently from ask_config: the SDK asks each distinct question once per call.
    return Elicit(
        f"The Tizra site at {client.base_url} rejected the API token. Enter it again, "
        "or cancel to stop.",
        TokenAgain,
    )


CREDENTIALS_CHANGED = (
    "the Tizra credentials changed while this call was waiting for a token; try the call again"
)


def _is_current(client: TizraClient) -> bool:
    """Whether the client was built for the site the configuration names now.

    A call can wait on a token prompt while another forgets the credentials and configures a
    different site; such a stale call must neither send its token to the new site nor change
    the new site's cached credentials. Call with _lock held.
    """
    return _config.get("base_url", "").rstrip("/") == client.base_url


def get_checked_client(
    client: Annotated[TizraClient, Resolve(get_client)],
    retry: Annotated[
        AcceptedElicitation[TokenAgain] | DeclinedElicitation | CancelledElicitation | None,
        Resolve(ask_token_again),
    ],
) -> TizraClient:
    """Return a client whose token the API accepts, asking once more if it rejects it."""
    global _client
    if client is _client:
        return client
    if not _accepts(client):
        with _lock:
            if not _is_current(client):
                raise ToolError(CREDENTIALS_CHANGED)
            _config.pop("auth_token", None)
        token = ""
        if isinstance(retry, AcceptedElicitation) and retry.data is not None:
            token = retry.data.auth_token.strip()
        if not token:
            raise ToolError(REJECTED_TOKEN)
        # The client's own base URL, not _config's: the prompt named this site.
        client = TizraClient(client.base_url, token)
        if not _accepts(client):
            raise ToolError(REJECTED_TOKEN)
    else:
        token = None
    with _lock:
        if not _is_current(client):
            raise ToolError(CREDENTIALS_CHANGED)
        if token is not None:
            _config["auth_token"] = token
        _client = client
    return client


def _forget_token(client: TizraClient) -> None:
    """Forget a token the API has stopped accepting, so the next call asks for it.

    Only while client is still the session's: a call that started before the credentials
    changed must not forget the new ones.
    """
    global _client
    with _lock:
        if client is _client:
            _client = None
            _config.pop("auth_token", None)


TizraApi = Annotated[TizraClient, Resolve(get_checked_client)]
"""Tool parameter type for the configured client; filled by the SDK, not by the model."""


def _api(client: TizraClient, call: Callable[[], T]) -> T:
    """Run a client call, surfacing API errors to the model as structured tool errors."""
    try:
        return call()
    except TizraAuthError as e:
        _forget_token(client)
        raise ToolError(json.dumps({**e.to_dict(), "message": REJECTED_TOKEN})) from e
    except TizraError as e:
        raise ToolError(json.dumps(e.to_dict())) from e


@mcp.tool()
def forget_credentials() -> str:
    """Forget the Tizra base URL and API token, including ones from environment variables.

    The next call to a Tizra tool asks the user for both, so this is how to switch to another
    site, or to re-enter a base URL that pointed at the wrong host. The user enters the new
    values; they are never passed as arguments.
    """
    global _client
    with _lock:
        _client = None
        _config.pop("base_url", None)
        _config.pop("auth_token", None)
    return "Forgot the Tizra base URL and API token; the next call will ask the user for them."


TYPE_SUMMARY_KEYS = ("name", "display-name", "base-type", "is-user-defined")

PROP_DEF_ORIGINS = """A type's property definitions (fields) come from three origins:
those declared on the type itself, those inherited from its chain of site super-types, and
default definitions from its chain of system base-types. Only a definition declared on the
type itself can be changed or removed there with upsert_prop_def or remove_prop_def."""


@mcp.tool(
    description=f"""List the site's meta-types (content types such as Book or Video).

Returns name, display-name, base-type and is-user-defined for each type; with include_props,
returns the full type records. Their prop-defs are only the definitions declared on each type
itself, so a type can show none and still have many fields; get_meta_type, or list_prop_defs
with include_inherited, gives a type's complete set.

{PROP_DEF_ORIGINS}"""
)
def list_meta_types(client: TizraApi, include_props: bool = False) -> list[dict[str, Any]]:
    types = _api(client, lambda: client.list_types(include_props))
    if include_props:
        return types
    return [{k: t.get(k) for k in TYPE_SUMMARY_KEYS} for t in types]


@mcp.tool(
    description=f"""Get one meta-type: prop-defs, sources, name-prop-name and hierarchy.

The record is the API's, whose prop-defs are only those declared on the type itself (it is
also the format for writing a type). all-prop-defs is added: every definition the type has,
each labeled declared-by {{type, source}} with source "local", "super-type" or "base-type".

{PROP_DEF_ORIGINS}"""
)
def get_meta_type(client: TizraApi, type_name: str) -> dict[str, Any]:
    record = _api(client, lambda: client.get_type(type_name))
    return {**record, "all-prop-defs": _api(client, lambda: client.effective_prop_defs(type_name))}


@mcp.tool(
    description=f"""List a meta-type's property definitions (fields).

By default only the definitions declared on the type itself. With include_inherited, every
definition the type has, each labeled declared-by {{type, source}} with source "local",
"super-type" or "base-type".

{PROP_DEF_ORIGINS}"""
)
def list_prop_defs(
    client: TizraApi, type_name: str, include_inherited: bool = False
) -> list[dict[str, Any]]:
    if include_inherited:
        return _api(client, lambda: client.effective_prop_defs(type_name))
    return _api(client, lambda: client.list_prop_defs(type_name))


MULTI_TARGET_GUIDANCE = (
    "When the same change applies to several {targets}, first settle the strategy in the main "
    "conversation, then spawn one sub-agent per {target} to apply it, rather than looping here; "
    "for large mechanical batches, execute_code is the alternative."
)


@mcp.tool(
    description=f"""Add a property definition (field) to a meta-type, or update an existing one.

type is required when the property is new (one of: string, string-list, integer, float,
boolean, date, time, text, html, markdown, keyword, keyword-list, reference, reference-list,
json-value, json-array, json-hash, ...) and is ignored on update. Only the attributes you
pass are changed. A display_name starting with "!" makes the property super-user-only.
value_specification constrains values (with is_constrained). Returns the resulting definition.

{MULTI_TARGET_GUIDANCE.format(targets="meta-types", target="type")}"""
)
def upsert_prop_def(
    client: TizraApi,
    type_name: str,
    prop_name: str,
    display_name: str | None = None,
    type: str | None = None,
    is_searchable: bool | None = None,
    is_user_visible: bool | None = None,
    is_json_visible: bool | None = None,
    is_constrained: bool | None = None,
    value_specification: str | None = None,
    default_value: str | None = None,
    sort_field: str | None = None,
    inherit_value: bool | None = None,
) -> dict[str, Any]:
    attributes = {
        key.replace("_", "-"): value
        for key, value in locals().items()
        if key not in ("client", "type_name", "prop_name") and value is not None
    }
    return _api(client, lambda: client.upsert_prop_def(type_name, prop_name, attributes))


@mcp.tool()
def remove_prop_def(client: TizraApi, type_name: str, prop_name: str) -> str:
    """Remove a property definition declared locally on a meta-type."""
    return _api(client, lambda: client.remove_prop_def(type_name, prop_name))


SELECTION_GUIDANCE = """Records are complete unless narrowed.

Read property values from parsed-props, which holds JSON-native values (booleans, numbers,
arrays for list types) that update_object_props accepts as they are; the older props holds
Tizra's internal strings. parsed_props names the properties to return; fields names the
top-level record fields to return (e.g. tizra-id, name, url-id, meta-type, published-live,
sources, parsed-props, props). With both, each record holds only those fields plus
parsed-props, which is the smallest result; parsed_props alone keeps every field but narrows
both props and parsed-props. Raw strings: fields=["props"]. A property with no value may be
absent from parsed-props (an empty time, reference or json value parses to nothing)."""


class PropMatch(BaseModel):
    prop: str = Field(description="Property name, e.g. Authors")
    value: str = Field(description="Value the property must have")


@mcp.tool(
    description=f"""List objects of a meta-type, published or not.

match keeps only objects whose properties have all the given values. Results are paged by
start/limit; count the result array, not size, to determine the page length. A full page means
there may be more. Results cannot be sorted; for sorted results use search_objects, where a
match on Prop with value v is the term Prop:v in all. Unlike search_objects, only one value for
each property can be specified. Repeated values will be ignored.

{SELECTION_GUIDANCE}"""
)
def query_objects(
    client: TizraApi,
    meta_type: str,
    parsed_props: list[str] | None = None,
    fields: list[str] | None = None,
    match: list[PropMatch] | None = None,
    start: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    matches = [m.model_dump() for m in match or []]
    return _api(
        client,
        lambda: client.query_objects(meta_type, parsed_props, matches, start, limit, fields)
    )


@mcp.tool(
    description=f"""Get one object by tizra-id or url-id.

The record holds its properties, publication status and sources.

{SELECTION_GUIDANCE}"""
)
def get_object(
    client: TizraApi,
    tizra_id: str,
    parsed_props: list[str] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    return _api(client, lambda: client.get_object(tizra_id, parsed_props, fields))


@mcp.tool(
    description=f"""Set properties on one existing object, leaving properties not named unchanged.

props maps property names to values in their parsed-props form: true/false for booleans,
numbers for numeric types, an array for list types (e.g. Authors), a string otherwise. A value
read from an object's parsed-props can be written back unchanged. Changes land in the CMS
state; use publish_object to make them visible on staging or live.

{MULTI_TARGET_GUIDANCE.format(targets="objects", target="object")}"""
)
def update_object_props(client: TizraApi, tizra_id: str, props: dict[str, Any]) -> dict[str, Any]:
    return _api(client, lambda: client.update_object_props(tizra_id, props))


@mcp.tool()
def create_object(client: TizraApi, meta_type: str, props: dict[str, Any]) -> dict[str, Any]:
    """Create a new, unpublished object of a meta-type with initial property values.

    props takes the same shape as update_object_props: values in their parsed-props form.
    Returns the new object's tizra-id, url-id and management-url.
    """
    return _api(client, lambda: client.create_object(meta_type, props))


class DeleteConfirmation(BaseModel):
    confirm: bool = Field(description="Delete this object")


def ask_delete(
    tizra_id: str, client: TizraApi, ctx: Context
) -> Elicit[DeleteConfirmation] | None:
    """Ask the user to confirm a deletion, naming the object; None when the client can't ask."""
    if not _can_elicit(ctx):
        return None
    obj = _api(client, lambda: client.get_object(tizra_id))
    if tizra_id != obj.get("tizra-id"):
        raise ToolError(
            f"Deletion requires a canonical tizra-id; call delete_object again with {obj['tizra-id']}."
        )
    props = obj.get("parsed-props") or {}
    meta_type = _api(client, lambda: client.get_type(obj["meta-type"]))
    name_prop = meta_type.get("name-prop-name") or "Title"
    title = props.get(name_prop) or props.get("Title") or obj.get("name")
    # Without a custom URL, the API reports the tizra-id as the url-id.
    url_id = obj.get("url-id")
    custom = url_id and url_id != obj.get("tizra-id")
    where = f"url: {url_id}" if custom else f"tizra-id: {tizra_id}"
    return Elicit(f"Delete '{title}' ({where})?", DeleteConfirmation)


@mcp.tool()
def delete_object(
    client: TizraApi,
    tizra_id: str,
    confirmation: Annotated[
        AcceptedElicitation[DeleteConfirmation] | DeclinedElicitation | CancelledElicitation | None,
        Resolve(ask_delete),
    ],
) -> str:
    """Delete an object. The user is asked to confirm, shown the object's title and URL.

    Deletion only happens if the user confirms; there is no way to confirm on the user's behalf.
    """
    accepted = isinstance(confirmation, AcceptedElicitation)
    # A resolver that returns None instead of asking arrives as accepted with no data.
    if confirmation is None or (accepted and confirmation.data is None):
        raise ToolError(
            "Not deleted: this client cannot ask the user for confirmation (MCP elicitation)."
        )
    if not (accepted and confirmation.data.confirm):
        return f"Deletion of {tizra_id} cancelled by the user; nothing was deleted."
    result = _api(client, lambda: client.delete_object(tizra_id))
    return f"Deleted {tizra_id}: {result.get('message') if isinstance(result, dict) else result}"


@mcp.tool()
def publish_object(
    client: TizraApi, tizra_id: str, operation: Literal["live", "staging", "unpublish"]
) -> Any:
    """Change an object's publication status.

    live publishes to the public site, staging publishes for administrative preview, and
    unpublish withdraws it. Publishing copies the object's current CMS state.
    """
    return _api(client, lambda: client.publish_object(tizra_id, operation))


@mcp.tool(
    description=f"""Search site content in the CMS state, so unpublished objects are included.

all, any and excluded are lists of search terms: every term in all must match, at least
one in any, and none in excluded. Each term is a property expression, not free text:
  Prop:value             exact, case-sensitive match on the whole value
  (Prop:words)           word match within a text property, e.g. (Title:fixture)
  {{FullText:words}}       search the text of the pages
  prefix:Type:Prop:val   prefix match; wildcard:Type:Prop:*val* and
                         range:Type:Prop:lo;hi likewise; Type is the meta-type
  {{has-value:Prop}}       the property is set; put it in excluded to find objects with no
                         value for Prop. The braces are required: has-value:Prop without
                         them is accepted but matches nothing.
A bare word matches AdminTags only. meta_type limits results to that type; when omitted,
all types are searched. Malformed terms are reported as errors. sort_prop sorts the results
by that property's value, ascending unless descending is set.

{SELECTION_GUIDANCE}"""
)
def search_objects(
    client: TizraApi,
    all: list[str] | None = None,
    any: list[str] | None = None,
    excluded: list[str] | None = None,
    meta_type: str | None = None,
    parsed_props: list[str] | None = None,
    fields: list[str] | None = None,
    start: int = 0,
    limit: int = 20,
    sort_prop: str | None = None,
    descending: bool = False,
) -> dict[str, Any]:
    try:
        return _api(
            client,
            lambda: client.search_objects(
                all, any, excluded, meta_type, parsed_props, start, limit, fields, sort_prop,
                descending,
            )
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


@mcp.tool()
def execute_code(client: TizraApi, code: str, timeout: float = 60) -> dict[str, Any]:
    """Run Python code against the Tizra admin API, for batch work over many objects.

    The code runs with these names defined: client (a TizraClient, with methods list_types,
    get_type, list_prop_defs, effective_prop_defs, upsert_prop_def, remove_prop_def,
    query_objects, get_object, update_object_props, create_object, publish_object,
    search_objects; deletion is deliberately not available here), json, re, and results (a
    list to append output to).
    Client methods raise tizra_client.TizraError with status, message and detail.
    Returns what the code printed and the final contents of results. Code still running
    after timeout seconds is abandoned and reported as a timeout.
    """
    output = io.StringIO()
    results: list[Any] = []

    def capture_print(*args: Any, **kwargs: Any) -> None:
        print(*args, **{**kwargs, "file": output})

    namespace: dict[str, Any] = {
        "__builtins__": {**vars(builtins), "print": capture_print},
        "client": _BatchClient(client),
        "json": json,
        "re": re,
        "results": results,
    }
    failure: list[str] = []

    def run() -> None:
        try:
            exec(compile(code, "<execute_code>", "exec"), namespace)
        except BaseException as e:
            if isinstance(e, TizraAuthError):
                _forget_token(client)
            error = traceback.format_exc()
            if isinstance(e, TizraError):
                error = f"{json.dumps(e.to_dict())}\n{error}"
            failure.append(error)

    thread = threading.Thread(target=run, name="execute_code", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        # Threads can't be killed; ask it to stop at its next bytecode, then abandon it.
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(thread.ident), ctypes.py_object(TimeoutError)
        )
        raise ToolError(
            f"Timed out after {timeout}s. Output so far:\n{output.getvalue()}"
            f"\nresults so far: {_jsonable(results)}"
        )
    if failure:
        raise ToolError(f"{failure[0]}\nOutput before the error:\n{output.getvalue()}")
    return {"stdout": output.getvalue(), "results": _jsonable(results)}


class _BatchClient:
    """The client as execute_code sees it: everything except delete, which needs confirmation."""

    def __init__(self, client: TizraClient):
        self._client = client

    def __getattribute__(self, name: str) -> Any:
        if name == "delete_object" or name.startswith("_"):
            raise AttributeError(f"{name} is not available in execute_code; use delete_object")
        return getattr(object.__getattribute__(self, "_client"), name)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
