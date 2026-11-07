"""The user's credentials file: Tizra sites by host, each with its API token.

The file is written by the user, never by the server, and read afresh whenever it is consulted,
so edits take effect without a restart. No message built here includes a token.
"""

import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

import httpx

CREDENTIALS_PATH = Path.home() / ".config" / "tizra" / "sites.toml"
"""Where the file lives; tests point this elsewhere."""

EXAMPLE = """[sites."www.example.com"]
token = "the site's APIToken setting\""""

_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]+)?")
_TOML_LOCATION = re.compile(r"\(at line (\d+), column (\d+)\)|\(at end of document\)")


class CredentialsFileError(ValueError):
    """The file is missing or unusable; the message names the path and says what to do."""


@dataclass(frozen=True)
class Sites:
    entries: dict[str, str]
    """Valid entries: lowercased host to token."""
    invalid: dict[str, str]
    """Invalid entries: key as written to the reason it cannot be used."""
    warning: str | None
    """Set when users other than the owner can access the file."""


def path() -> Path:
    return CREDENTIALS_PATH


def format_help() -> str:
    return f"Create {path()} with an entry for each site, e.g.\n\n{EXAMPLE}\n\nthen chmod 600 it."


def load() -> Sites:
    """Read the file afresh, separating the entries that can be used from those that can't."""
    file = path()
    try:
        data = file.read_bytes()
        mode = file.stat().st_mode
    except FileNotFoundError:
        raise CredentialsFileError(f"There is no Tizra credentials file. {format_help()}") from None
    except OSError as e:
        raise CredentialsFileError(f"Cannot read {file}: {e.strerror}.") from None
    try:
        document = tomllib.loads(data.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        # Only the location: the parser's message can quote the line, which may hold a token.
        found = _TOML_LOCATION.search(str(e)) if isinstance(e, tomllib.TOMLDecodeError) else None
        where = found.group(0)[1:-1] if found else "somewhere"
        raise CredentialsFileError(f"{file} is not valid TOML: there is an error {where}.") from None
    sites = document.get("sites")
    if not isinstance(sites, dict):
        raise CredentialsFileError(f"{file} has no [sites] table. {format_help()}")
    entries: dict[str, str] = {}
    invalid: dict[str, str] = {}
    for key, entry in sites.items():
        host = key.strip().lower()
        token = entry.get("token") if isinstance(entry, dict) else None
        if not _HOST.fullmatch(host):
            invalid[key] = "the key must be a host, like www.example.com, not a URL"
        elif not isinstance(token, str) or not token.strip():
            invalid[key] = "the entry has no token"
        elif any(c != "\t" and not (0x20 <= ord(c) <= 0x7e) for c in token.strip()):
            invalid[key] = "the token contains characters that cannot be sent in an HTTP header"
        elif host in entries:
            invalid[key] = f"another entry is also for {host}"
        else:
            entries[host] = token.strip()
    warning = None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        warning = f"Other users on this computer can access {file}; run: chmod 600 '{file}'"
    return Sites(entries, invalid, warning)


def host_of(name: str) -> str:
    """The lowercased host, with any port, that a site name refers to.

    A name with a scheme is a URL, which must be https; one without is a host, possibly followed
    by a path, which is ignored.
    """
    name = name.strip()
    if "://" in name:
        url = httpx.URL(name)
        if url.scheme != "https":
            raise ValueError(
                f"Tizra sites are only reached over https, not {name!r}: "
                "the server ignores the API token on insecure requests."
            )
        return url.netloc.decode().lower()
    return name.split("/", 1)[0].lower()


def match(name: str, entries: dict[str, str]) -> tuple[str, str] | None:
    """The (host, token) of the entry a name refers to: exactly, else with www. toggled."""
    host = host_of(name)
    toggled = host.removeprefix("www.") if host.startswith("www.") else f"www.{host}"
    for candidate in (host, toggled):
        if candidate in entries:
            return candidate, entries[candidate]
    return None
