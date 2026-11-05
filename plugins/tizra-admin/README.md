# tizra-admin

A Claude Code plugin that gives Claude read and write access to a Tizra site through the admin
REST API: meta-types and their property definitions, objects, search, publishing, and a Python
`execute_code` tool for batch work. It has no tools for source files (PDF/ePub uploads, page
text).

## Install

You need [`uv`](https://docs.astral.sh/uv/) on your `PATH`. It installs the plugin's Python
dependencies the first time the server starts.

```sh
claude plugin marketplace add Tizra/claude-plugins
claude plugin install tizra-admin@tizra
```

To pick up new releases, run `claude plugin marketplace update tizra`, or turn on auto-update for
the marketplace in `/plugin`.

## Configuration

| Variable           | Value                                                                 |
|--------------------|-----------------------------------------------------------------------|
| `TIZRA_BASE_URL`   | The site's URL, over **https**; its public or admin host              |
| `TIZRA_SITE`       | The `{site}` path segment; normally `_`, the default                  |
| `TIZRA_AUTH_TOKEN` | The site's API token                                                  |

You can export them before starting Claude Code, but you don't have to: any that are unset are
requested from you inside Claude Code on the first tool call, and kept for the session. Entered
that way, the token goes straight to the server and never passes through the model. If your
client cannot ask, the tool reports which variables to set.

The token is checked before first use. If the site rejects it, you are asked for it again in the
same call (cancel to stop); a second rejection forgets it, so the next call asks again. To switch
sites, or to re-enter a base URL, ask Claude to run `forget_credentials`: it drops the base URL
and token, including ones from environment variables, and the next call asks for both.

Three things to know about the configuration:

- **The admin host is found for you.** A site's public host redirects admin requests to its admin
  host, and the server follows that redirect (without sending the token) to find it. If that
  fails, the client reports where the API redirected so you can enter the admin host instead.
- **Use https.** The server only honors the token on secure requests, so the client rejects an
  `http://` base URL rather than failing later with a redirect to the login page.
- **A wrong token redirects to the login page.** The client reports that as "not authenticated"
  and asks for the token again. A token changed while a session is open is only noticed when the
  server's session ends.

TLS is verified against your operating system's trust store.

## The API token

The token is the value of the site's `APIToken` site setting, set by an administrator of the
site. A site with no `APIToken` accepts no token. Anyone holding the token has full admin access
to the site through the API, so treat it like an administrator's password.

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

Search terms are property expressions, not free text; the `search_objects` tool description
gives the grammar.

`execute_code` runs Python inside the plugin's server process on your machine. It is not a
sandbox, so read what Claude proposes to run before approving it.
