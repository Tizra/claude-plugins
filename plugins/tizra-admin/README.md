# tizra-admin-mcp

An MCP server that gives Claude Code read and write access to a Tizra site through the admin
REST API (`/admin-api/{site}/...`): meta-types and their property definitions, objects, search,
publishing, and a Python `execute_code` tool for batch work. It is a client of the API only; it
does not touch server code, and it has no tools for source files (PDF/ePub uploads, page text).

## Install

This directory is a Claude Code plugin, `tizra-admin`, released through two marketplaces. Both
need [`uv`](https://docs.astral.sh/uv/) on your `PATH`; it installs the dependencies pinned in
`uv.lock` on first start, into the plugin's data directory.

| Marketplace | For        | Holds                                                                   |
|-------------|------------|-------------------------------------------------------------------------|
| `tizra`     | Everyone   | A separate repo with a copy of each release, and only the releases      |
| `tizra-dev` | Developers | `.claude-plugin/marketplace.json` here, pinned to a `tizra-admin/v*` tag |

Install from one of them, not both; they register the same server.

```sh
claude plugin marketplace add <the tizra marketplace repo>
claude plugin install tizra-admin@tizra
# or, from a cubchicken checkout:
claude plugin marketplace add .
claude plugin install tizra-admin@tizra-dev
```

`tizra-dev` fetches only this directory at the pinned tag, not the whole repository. Added from a
local checkout, it follows the pin in whatever that checkout has checked out.

Inside this repository you get the working tree instead: the project's `.mcp.json` registers the
server as `tizra-admin`, and `.claude/settings.json` disables the installed plugin here, so the
two do not both run. Claude Code asks you to approve the server the first time it starts in this
project. To try the working tree as a plugin, run `claude --plugin-dir tools/tizra-admin-mcp`.

For development, `uv sync --directory tools/tizra-admin-mcp` creates `tools/tizra-admin-mcp/.venv`.

## Releases

`bin/plugin-release` makes both kinds of release. It commits and tags locally, and prints the
pushes it leaves to you.

- `mise plugin-release dev 0.2.0` sets the version in `.claude-plugin/plugin.json`,
  `pyproject.toml` and `uv.lock`, tags `tizra-admin/v0.2.0`, and pins `tizra-dev` to the tag.
  Developers see it once the tag is pushed and the pin reaches their checkout.
- `mise plugin-release official --marketplace ../claude-plugins` copies the newest dev release
  (or the tag you name) into a checkout of the `tizra` marketplace repo, then commits and tags it
  there. Set `TIZRA_PLUGIN_MARKETPLACE` to skip `--marketplace`.

Only a dev release can become an official one, so everyone gets a version developers have already
had.

## Configuration

| Variable           | Value                                                                 |
|--------------------|-----------------------------------------------------------------------|
| `TIZRA_BASE_URL`   | The site's **admin host**, over **https**                             |
| `TIZRA_SITE`       | The `{site}` path segment; `_` (the default in `.mcp.json`)           |
| `TIZRA_AUTH_TOKEN` | The site's `APIToken` site setting                                    |

Export them before starting Claude Code; `.mcp.json` passes them through. Any that are unset
are requested from you inside Claude Code (MCP elicitation) on the first tool call, and kept for
the session. If your client cannot ask, the tool reports which variables to set.

The token is checked before first use. If the site rejects it, you are asked for it again in the
same call (cancel to stop); a second rejection forgets it, so the next call asks again. To switch
sites, or to re-enter a base URL, ask Claude to run `forget_credentials`: it drops the base URL
and token, including ones from environment variables, and the next call asks for both.

Three things that look like configuration errors and are:

- **Use the admin host.** A site's public host redirects admin requests to its admin host, e.g.
  `https://systest.dev11.agilepublisher.com` to `https://s10069-admin.dev11.agilepublisher.com`.
  The client does not follow redirects; it reports the redirect target so you can fix the URL.
- **Use https.** The server only honors the token on secure requests, so the client rejects an
  `http://` base URL rather than failing later with a redirect to the login page.
- **A wrong token redirects to the login page.** The client reports that as "not authenticated"
  and asks for the token again. A token changed while a session is open is only noticed when the
  server's session ends, because the server honors its session cookie whatever token is sent.

TLS is verified against the operating system's trust store (via `truststore`), so the Vagrant
CA works once it is trusted by your OS, as it already is for browsers and `curl`.

## The API token

The token is not issued by any endpoint: it is the value of the site's `APIToken` site setting,
set by an administrator. A site with no `APIToken` accepts no token.

On the local system-test VM, set it through the server REPL (port 7889 inside the VM). The
setting is invisible until `SiteDisplay` has its definition, hence `enableSiteParameter`:

```sh
TOKEN=$(uuidgen)
vagrant ssh system-test -- 'timeout 20 nc -t localhost 7889' <<EOF
(require 'tizra.lib.site-lib 'tizra.backend-api)
(let [site (tizra.lib.site-lib/<-hostname "systest.dev11.agilepublisher.com")]
  (tizra.backend-api/hibernate-wrap
    (.enableSiteParameter site "APIToken")
    (.setCmsStringTagAndChangeDate (.getDefaultDisplayableObject site) "APIToken" "$TOKEN")))
EOF
```

`mise agent-do reset` wipes it. (`bin/vagrant-admin-token` does the same job but is out of
date: it uses the old `tizra.api.lib.site` namespace and skips `enableSiteParameter`.)

## Tools

| Tool                  | Does                                                                  |
|-----------------------|-----------------------------------------------------------------------|
| `list_meta_types`     | List types; `include_props` for full records (local `prop-defs` only) |
| `get_meta_type`       | One type's full record, plus its labeled `all-prop-defs`              |
| `list_prop_defs`      | A type's local property definitions; `include_inherited` for all      |
| `upsert_prop_def`     | Add or update a property definition                                   |
| `remove_prop_def`     | Remove a locally declared property definition                         |
| `query_objects`       | List objects of a type, optionally matching exact property values     |
| `search_objects`      | Search with `all`/`any`/`excluded` term lists (CMS state)             |
| `get_object`          | One object by tizra-id or url-id                                      |
| `create_object`       | Create an object                                                      |
| `update_object_props` | Set properties on an object                                           |
| `delete_object`       | Delete an object, after you confirm its title and URL in Claude Code  |
| `publish_object`      | `live`, `staging` or `unpublish`                                      |
| `execute_code`        | Run Python with a `client` for batch work (no deletes)                |
| `forget_credentials`  | Forget the base URL and token; the next call asks for them            |

The three object read tools return complete records unless narrowed. `parsed_props` names the
properties to return and `fields` the top-level record fields; together they give the smallest
record, e.g. `parsed_props: ["Title", "Authors"], fields: ["tizra-id"]`. Values come from
`parsed-props`, which holds JSON-native values (booleans, arrays for list types); the older
`props` holds Tizra's internal strings and is available as `fields: ["props"]`.
`update_object_props` and `create_object` take values in the same `parsed-props` form.

A type's property definitions come from the type itself, its chain of site super-types, and
default definitions from its chain of system base-types. `get_meta_type`'s `all-prop-defs`, and
`list_prop_defs` with `include_inherited`, label each with
`declared-by: {"type": "Title", "source": "base-type"}` (or `"local"`, `"super-type"`).

Search terms are property expressions, not free text; the `search_objects` description gives
the grammar, and `src/main/webapp/aa_admin/aa_scripts/tagpicker-terms.mjs` is the fuller
reference.

`execute_code` runs in the server process with `exec()`. That is not a sandbox: it is safe only
because whoever holds the token already has full admin access to the site.
