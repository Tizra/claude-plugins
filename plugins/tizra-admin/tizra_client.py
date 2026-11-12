"""HTTP client for the Tizra admin REST API."""

import ssl
from typing import Any, BinaryIO
from urllib.parse import quote

import httpx
import truststore


class TizraError(Exception):
    """An error response from the Tizra API, normalized to status, message and detail."""

    def __init__(self, status: int, message: str, detail: Any = None):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "message": self.message, "detail": self.detail}


class TizraAuthError(TizraError):
    """The API rejected the token: it redirected to the login page."""


ADMIN_MAIN = "/admin/main"
"""The admin's front page; a site's public host redirects it to the site's admin host."""

MAX_ADMIN_HOPS = 5

KEYWORD_COLON = "\u02d0"
"""Stands for a colon inside a search term's value, where a colon would separate its parts."""


def _require_https(base_url: str) -> None:
    if httpx.URL(base_url).scheme != "https":
        raise ValueError(
            f"Tizra base URL must use https, not {base_url!r}: "
            "the server ignores the API token on insecure requests."
        )


def _verify() -> ssl.SSLContext:
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def find_admin_base_url(base_url: str, timeout: float = 30.0) -> str:
    """Return the base URL of the site's admin host, given the site's public or admin host.

    Follows the redirects of `/admin/main` while they stay on that path: a public host sends it
    to the admin host, possibly by way of another host such as www to the bare domain. The
    first redirect elsewhere, normally to the login page, means the admin host has been
    reached. Sends no token. Returns base_url unchanged when the redirects lead nowhere useful
    or the host cannot be reached; the API request that follows reports the problem.
    """
    _require_https(base_url)
    url = httpx.URL(base_url).copy_with(path=ADMIN_MAIN, query=None, fragment=None)
    try:
        with httpx.Client(verify=_verify(), timeout=timeout) as http:
            for _ in range(MAX_ADMIN_HOPS):
                response = http.get(url)
                location = response.headers.get("location")
                if not response.is_redirect or not location:
                    break
                target = url.join(location)
                if target.scheme != "https":
                    return base_url
                if target.path != ADMIN_MAIN:
                    break
                url = target
    except httpx.HTTPError:
        return base_url
    if url.scheme != "https" or url.host == httpx.URL(base_url).host:
        return base_url
    return str(url.copy_with(path="/", query=None, fragment=None)).rstrip("/")


class TizraClient:
    """Thin wrapper over the admin REST API; one method per endpoint."""

    def __init__(self, base_url: str, auth_token: str, timeout: float = 30.0):
        _require_https(base_url)
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            verify=_verify(),
            headers={"tizra-authentication-token": auth_token, "accept": "application/json"},
            timeout=timeout,
        )

    def check_auth(self) -> None:
        """Make one cheap authenticated request; raises TizraAuthError if the token is rejected."""
        self._request("GET", self.admin_path("types", "AdminTagged"))

    def admin_path(self, *segments: str) -> str:
        """Build an `/admin-api/_/...` path, escaping each segment.

        The `{site}` segment is always `_`, which names the site of the request's host.
        """
        return "/".join(quote(s, safe="") for s in ["", "admin-api", "_", *segments])

    def list_types(self, include_props: bool = False) -> Any:
        params = {"table": "true", **({"settable": "true"} if include_props else {})}
        return self._request("GET", self.admin_path("types"), params=params)

    def get_type(self, type_name: str) -> Any:
        path = self.admin_path("types", type_name)
        return self._request("GET", path, params={"settable": "true"})

    def list_prop_defs(self, type_name: str, include_inherited: bool = False) -> Any:
        listing = "all-prop-defs" if include_inherited else "prop-defs"
        return self._request("GET", self.admin_path("types", type_name, listing))

    def effective_prop_defs(self, type_name: str) -> list[dict[str, Any]]:
        """A type's complete prop-defs, each labeled with the nearest type that declares it.

        Definitions come from the type itself (source "local"), its chain of site super-types
        ("super-type"), and defaults from its chain of system base-types ("base-type"). Each
        record from all-prop-defs gets `declared-by: {type, source}`, or None when neither
        chain declares it.
        """
        defs = self._request("GET", self.admin_path("types", type_name, "all-prop-defs"))
        declared_by: dict[str, dict[str, str]] = {}

        def claim(record: dict[str, Any], source: str) -> None:
            # A type record lists its local prop-defs as a map of name to URL.
            for prop in record.get("prop-defs") or {}:
                declared_by.setdefault(prop, {"type": record["name"], "source": source})

        record = self._request("GET", self.admin_path("types", type_name))
        base = record.get("base-type")
        source, seen = "local", set()
        while record and record["name"] not in seen:
            seen.add(record["name"])
            claim(record, source)
            parent = record.get("super-type")
            record = parent and self._request("GET", self.admin_path("types", parent))
            source = "super-type"
        # A system type's base-type is its parent: system Book -> Title -> AdminTagged.
        seen = set()
        while base and base not in seen:
            seen.add(base)
            record = self._request("GET", self.admin_path("system-types", base))
            claim(record, "base-type")
            base = record.get("base-type")
        return [{**d, "declared-by": declared_by.get(d.get("name"))} for d in defs]

    def upsert_prop_def(self, type_name: str, prop_name: str, attributes: dict[str, Any]) -> Any:
        path = self.admin_path("types", type_name, "prop-defs", prop_name)
        return self._request("POST", path, json=attributes)

    def remove_prop_def(self, type_name: str, prop_name: str) -> Any:
        return self._request("DELETE", self.admin_path("types", type_name, "prop-defs", prop_name))

    def query_objects(
        self,
        meta_type: str,
        parsed_props: list[str] | None = None,
        match: list[dict[str, str]] | None = None,
        start: int = 0,
        limit: int = 20,
        fields: list[str] | None = None,
    ) -> Any:
        # No sorting: the match route ignores sort-prop. search_objects can sort.
        params: list[tuple[str, str | int]] = [
            ("meta-type", meta_type), ("start", start), ("limit", limit)
        ]
        params += _selection(parsed_props, fields)
        for m in match or []:
            params += [("match-prop", m["prop"]), ("match-value", m["value"])]
        # The trailing slash matters: "query" without it is the single-object route.
        listing = "match" if match else ""
        return self._request("GET", self.admin_path("query", listing), params=params)

    def get_object(
        self, tizra_id: str, parsed_props: list[str] | None = None, fields: list[str] | None = None
    ) -> Any:
        path = self.admin_path("objects", tizra_id)
        return self._request("GET", path, params=_selection(parsed_props, fields))

    def update_object_props(self, tizra_id: str, props: dict[str, Any]) -> Any:
        return self._request("PUT", self.admin_path("objects", tizra_id), json={"props": props})

    def create_object(self, meta_type: str, props: dict[str, Any]) -> Any:
        body = {"operation": "update-add", "data": {"meta-type": meta_type, "props": props}}
        return self._request("POST", self.admin_path("objects"), json=body)

    def delete_object(self, tizra_id: str) -> Any:
        return self._request("DELETE", self.admin_path("objects", tizra_id))

    def publish_object(self, tizra_id: str, operation: str) -> Any:
        path = self.admin_path("objects", tizra_id)
        if operation not in ("live", "staging", "unpublish"):
            raise ValueError("operation must be live, staging, or unpublish")
        return self._request("POST", path, json={"operation": operation})

    def search_objects(
        self,
        all: list[str] | None = None,
        any: list[str] | None = None,
        excluded: list[str] | None = None,
        meta_type: str | None = None,
        parsed_props: list[str] | None = None,
        start: int = 0,
        limit: int = 20,
        fields: list[str] | None = None,
        sort_prop: str | None = None,
        descending: bool = False,
    ) -> Any:
        if not (all or any or excluded):
            raise ValueError("search_objects needs at least one of all, any or excluded")
        # check-syntax makes the server reject malformed terms instead of silently dropping them.
        params: list[tuple[str, str | int]] = [
            ("check-syntax", "true"), ("start", start), ("limit", limit)
        ]
        params += [("all", t) for t in all or []]
        params += [("any", t) for t in any or []]
        params += [("excluded", t) for t in excluded or []]
        params += _selection(parsed_props, fields)
        if meta_type:
            params.append(("meta-type", meta_type))
        if sort_prop:
            # invert-order reverses the sort, so true is descending.
            params += [("sort-prop", sort_prop), ("invert-order", str(descending).lower())]
        return self._request("GET", self.admin_path("query", "search"), params=params)

    def find_exact(
        self, matches: list[tuple[str, str]], meta_type: str | None = None, limit: int = 20
    ) -> Any:
        """Objects in the CMS state whose properties hold every given value exactly.

        Each pair becomes a search term strict:AdminTagged:Prop:value. strict makes the match
        exact and case-sensitive; AdminTagged, the super-type of every content type, makes it
        apply to all types (the type in a term doesn't filter, so meta_type is sent apart). With
        check-syntax each term is parsed alone, so ; and \\ need no escaping, but : separates a
        term's parts: a colon in a value is sent as the look-alike \u02d0, which the server
        reads back as a colon.
        """
        if not matches:
            raise ValueError("find_exact needs at least one property and value")
        if any(not prop.strip() or not value.strip() for prop, value in matches):
            raise ValueError("find_exact needs a nonblank property and value in every pair")
        terms = [
            f"strict:AdminTagged:{prop}:{value.replace(':', KEYWORD_COLON)}"
            for prop, value in matches
        ]
        return self.search_objects(all=terms, meta_type=meta_type, limit=limit)

    def get_source(self, tizra_id: str, source: str) -> Any:
        """A source's record in the CMS state; its `files` lists the files in order."""
        return self._request("GET", self.admin_path("objects", tizra_id, source))

    def put_file(
        self, tizra_id: str, source: str, name: str, content: BinaryIO, size: int, content_type: str
    ) -> dict[str, Any]:
        """Create a file, or replace the content of the file of that name, streaming `content`.

        Returns `{"created": bool}`, or `{"external": url}` when the file's content is stored at
        an external URL, in which case the API redirects there and nothing is changed.
        """
        path = self.admin_path("objects", tizra_id, source, name)
        headers = {"content-type": content_type, "content-length": str(size)}
        response = self._send("PUT", path, content=content, headers=headers)
        if external := _external_location(response):
            return {"external": external}
        body = self._parse(response)
        status = body.get("status") if isinstance(body, dict) else None
        if isinstance(status, int) and status >= 400:
            raise TizraError(status, str(body.get("message") or "upload failed"))
        return {"created": status == 201 or response.status_code == 201}

    def download_file(self, tizra_id: str, source: str, name: str, out: BinaryIO) -> dict[str, Any]:
        """Write a file's content in the CMS state to `out`.

        Returns `{"content-type": str, "size": int}`, or `{"external": url}` when the content
        is stored at an external URL; nothing is then written.
        """
        path = self.admin_path("objects", tizra_id, source, name)
        try:
            with self._http.stream("GET", path) as response:
                if external := _external_location(response):
                    return {"external": external}
                if response.is_redirect or response.is_error:
                    response.read()
                    self._check(response)
                size = 0
                for chunk in response.iter_bytes():
                    out.write(chunk)
                    size += len(chunk)
                return {"content-type": response.headers.get("content-type"), "size": size}
        except httpx.HTTPError as e:
            raise TizraError(0, f"request failed: {e}") from e

    def update_file(self, tizra_id: str, source: str, name: str, changes: dict[str, Any]) -> Any:
        """Apply `changes` (name, content-type, sort-field, props) to a file's settings."""
        path = self.admin_path("objects", tizra_id, source, name)
        return self._request("POST", path, json={"operation": "update", **changes})

    def delete_file(self, tizra_id: str, source: str, name: str) -> Any:
        return self._request("DELETE", self.admin_path("objects", tizra_id, source, name))

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make a request and return its parsed body, raising TizraError on failure."""
        return self._parse(self._send(method, path, **kwargs))

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise TizraError(0, f"request failed: {e}") from e

    def _check(self, response: httpx.Response) -> None:
        """Raise TizraError for a redirect or an error response."""
        if response.is_redirect:
            # Never follow: a rejected token redirects to the login page, and a public host
            # redirects to the site's admin host. Either way the configuration is wrong.
            location = response.headers.get("location") or ""
            detail = {"location": location}
            if _is_login_redirect(response):
                message = "not authenticated: the API token does not match the site's APIToken"
                raise TizraAuthError(response.status_code, message, detail)
            message = (
                f"redirected to {location}; set the base URL to the site's admin host "
                "(the forget_credentials tool lets the user enter it again)"
            )
            raise TizraError(response.status_code, message, detail)
        if response.is_error:
            raise _normalize_error(response)

    def _parse(self, response: httpx.Response) -> Any:
        """The parsed body of a response, raising TizraError for a redirect or an error."""
        self._check(response)
        if not response.content:
            # Object updates answer with an empty body and the object's Location header.
            return {"status": response.status_code, "location": response.headers.get("location")}
        try:
            return response.json()
        except ValueError:
            return response.text


def _external_location(response: httpx.Response) -> str | None:
    """The URL of a file stored externally, which the API answers with an HTTP 307 to it.

    A redirect to the login page is a rejected token, not a file.
    """
    location = response.headers.get("location") or ""
    if response.status_code == 307 and location and not _is_login_redirect(response):
        return location
    return None


def _is_login_redirect(response: httpx.Response) -> bool:
    """Whether a redirect leads to the site's own login page, the API's answer to a bad token.

    Only a path under /login on the host that was asked counts: an external file's URL may
    contain /login anywhere, and must not be mistaken for a rejected token.
    """
    location = response.headers.get("location")
    if not location:
        return False
    target = response.request.url.join(location)
    path = target.path
    return target.host == response.request.url.host and (
        path == "/login" or path.startswith("/login/")
    )


def _normalize_error(response: httpx.Response) -> TizraError:
    """The API returns varying error shapes: a JSON map, a JSON string, or plain text."""
    status = response.status_code
    message = response.reason_phrase
    detail: Any = None
    try:
        body = response.json()
    except ValueError:
        body = response.text.strip() or None
    if isinstance(body, dict):
        message = str(body.get("message") or body.get("error") or message)
        detail = body.get("detail") or body.get("details") or {
            k: v for k, v in body.items() if k not in ("message", "error", "status")
        } or None
    elif body is not None:
        message = str(body)
    return TizraError(status, message, detail)


def _selection(parsed_props: list[str] | None, fields: list[str] | None) -> list[tuple[str, str]]:
    """Query params selecting properties (as parsed-props) and top-level record fields.

    The raw `props` selection param is deliberately not offered: the API ignores it whenever
    `parsed-props` is given, and parsed-props also narrows the record's `props`.
    """
    return [("parsed-props", p) for p in parsed_props or []] + [("fields", f) for f in fields or []]
