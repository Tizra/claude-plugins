# tizra-admin

A Claude Code plugin that gives Claude read and write access to a Tizra site through the admin
REST API: meta-types and their property definitions, objects, search, publishing, the files
attached to objects, and a Python `execute_code` tool for batch work. It has no tools for page
text.

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

The server needs each site's URL and API token. It gets them in one of two ways, decided when it
starts: from a credentials file, which can list any number of sites, or from environment
variables, which pin the session to one.

### The credentials file

List your sites in `~/.config/tizra/sites.toml`, one entry per host with that site's token:

```toml
[sites."www.example.com"]
token = "the site's APIToken setting"

[sites."books.example.org"]
token = "..."
```

Then run `chmod 600 ~/.config/tizra/sites.toml`. If other users can read the file, the server
says so in its tool results, but carries on using it. It reads the file afresh whenever it needs
it, so edits take effect without a restart, and it never writes to it.

Ask Claude to work with a site and it calls `list_sites`, which shows your hosts but never their
tokens, then `select_credentials` with the site you named. A name matches a host exactly, or with
a leading `www.` added or removed, and a URL on the host works too. The site is reached at
`https://` followed by the host as written in the file. Its token is checked before the session
switches to it, and a rejected token leaves the site in use unchanged. Until a site is selected,
the other tools fail and say how to select one.

Claude can switch among the sites in the file at any time. Content Claude reads on one site could
therefore steer it to act on another. If you want a session locked to one site, use the
environment variables instead.

For a site that is not in the file, `enter_credentials` asks you to type its URL and token in
Claude Code; they go straight to the server, never through the model. If the site rejects the
token, you are asked for it once more (cancel to stop). Treat it as a last resort: see the note on
the desktop application below. `forget_credentials` drops the selected site, leaving none.

### Environment variables

| Variable           | Value                                                                 |
|--------------------|-----------------------------------------------------------------------|
| `TIZRA_BASE_URL`   | The site's URL, over **https**; its public or admin host              |
| `TIZRA_AUTH_TOKEN` | The site's API token                                                  |

When either variable is set, the session is pinned to that site. `select_credentials`,
`enter_credentials` and `forget_credentials` refuse, so Claude cannot move the session to another
site through those tools. That is not a sandbox: code run by `execute_code` can still read the
credentials file and reach any site in it. With `TIZRA_BASE_URL` alone, the token is taken from the
credentials file's entry for that host; if other users can read the file, `list_sites` says so. Anything still missing is requested from you inside Claude Code on the first tool
call, and kept for the session; entered that way, the token goes straight to the server. If your
client cannot ask, the tool reports which variables to set.

In a pinned session, the token is checked before first use. If the site rejects it, you are asked
for it again in the same call (cancel to stop), and a second rejection forgets it, so the next
call asks again.

### The Claude desktop application

The desktop application says it can ask you for input, but it never passes your answers back to
the server, so a request for credentials there never completes. Use the credentials file. For
the same reason, `delete_object` and `delete_file`, which need your confirmation, cannot delete
anything there.

### Hosts, https and tokens

Three things to know whichever way a site is configured:

- **The admin host is found for you.** A site's public host redirects admin requests to its admin
  host, and the server follows that redirect (without sending the token) to find it. If that
  fails, the client reports where the API redirected so you can enter the admin host instead.
- **Use https.** The server only honors the token on secure requests, so the client rejects an
  `http://` base URL rather than failing later with a redirect to the login page.
- **A wrong token redirects to the login page.** The client reports that as "not authenticated".
  A token changed while a session is open is only noticed when the server's session ends.

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
| `delete_object`       | Delete by tizra-id or exact `match`; you choose among the matches     |
| `publish_object`      | `live`, `staging` or `unpublish`                                      |
| `list_files`          | The files in one of an object's sources, or in all of them            |
| `upload_file`         | Create or replace a file from a local path                            |
| `download_file`       | Save a file's content to a local path                                 |
| `update_file`         | Rename a file, or set its content type, sort field or props           |
| `delete_file`         | Delete one file, named exactly, after you confirm                     |
| `execute_code`        | Run Python with a `client` for batch work (no deletes)                |
| `list_sites`          | The sites in your credentials file, and the one in use                |
| `select_credentials`  | Use a site from the credentials file                                  |
| `enter_credentials`   | Type a site and token into Claude Code; a last resort                 |
| `forget_credentials`  | Drop the selected site; refused in a pinned session                   |

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

An object's files are kept in its sources: `PdfSource` for the content readers see,
`Attachments` and `FreeAttachments` for display assets and downloads, and `PropSource` for
files that are the values of file properties such as `CoverImage`. Files go between your disk and
the site directly, never through the conversation, so give Claude full paths. Uploads change
the unpublished (CMS) state; ask Claude to publish to show them. Claude uploads into `PropSource`
only under the name of a file property, unless you ask for another name explicitly.

`execute_code` runs Python inside the plugin's server process on your machine. It is not a
sandbox, so read what Claude proposes to run before approving it.
