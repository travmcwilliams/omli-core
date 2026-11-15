# Security policy

## Reporting a vulnerability

Please report security problems privately, through this repository's **Security** tab ("Report a vulnerability"). Do not open a public issue.

Include what you ran, what you expected, and what happened. A minimal script that reproduces it is the most useful thing you can send.

This is a one-maintainer project. I aim to acknowledge reports within 7 days and to agree on a disclosure date with you before anything is published. Those are targets, not guarantees.

## Supported versions

| Version | Supported |
|---|---|
| 0.2.x | Yes |
| Earlier | No |

## What OMLi defends against, and what it does not

Read this before you rely on OMLi for anything that matters.

### In scope

- **Malformed or hostile replies from a model server or a remote service** (the "wire"). Failures become typed errors or fixed-field outcomes, and nothing the remote side returns is copied into an outcome's status fields.
- **Credentials leaking into errors or outcomes.** Credentials are referenced as `secret://` paths and never written inline. Resolution refuses references that would escape the secrets directory, symlinks included. Its error messages state the rule that was broken and repeat neither the reference nor the value. One known gap: an operating-system error while reading a secret file is not caught, and its message carries that file's path.
- **Data tagged local-only reaching a non-local model through `Agent.run`.** Before every call and every escalation, a model whose provider kind does not start with `local-` is refused for local-only inputs. A `local-*` registry entry must have a loopback endpoint. The direct helpers in `omli.models` are not checked.
- **Edits to a sealed artifact log.** The seal is a hash chain over exact bytes, and verification exits 1 on any change inside the sealed prefix.
- **Unapproved text taking effect as learned memory.**

### Out of scope, by design

- **Code running in the same Python process.** Callers, injected senders, readers and resolvers, and anything they import, are trusted. `BaseException` subclasses propagate.
- **Anyone with write access to your trust files.** That includes the keyring, the public key, the anchor log and the lessons log. Pin the keyring, public key and anchor where an editor cannot change them, for example in a separate repository or a write-once store.
- **Records appended after the last seal**, until you seal again.
- **Rollback** when verification runs with `--no-anchor` or the anchor file has been deleted.
- **Authentication of approvers.** An approval records a name; it does not prove who gave it.
- **Message content read from a channel.** It is returned as untrusted data and is not scrubbed.

### Operational notes

- With `OMLI_HOME` and `OMLI_SECRETS_DIR` both unset, file-based secrets are disabled and only `OMLI_SECRET_*` environment variables resolve. This prevents a planted `var/secrets` folder in an untrusted working directory from being read.
- The private signing key defaults to `~/.omli/seal.key`, outside any project folder, so it is not committed by accident. Set `OMLI_SEAL_KEY` to move it.
- **External programs.** OMLi starts an external program (`nvidia-smi` for GPU readings, and on macOS and Linux the `open` or `xdg-open` that hands the sign-in page to the browser) only from an absolute folder on PATH. It never runs a program from the current folder, or through a PATH entry that is empty, relative or the current folder. On Windows an entry counts as absolute only with a drive letter or a network share, never a bare leading backslash. Whether an entry, or the folder that really holds the program, is the current folder is decided by file identity, so other spellings of it (long-path or device prefixes, an admin share, a junction or link) are skipped too. The program runs with its own folder as its working folder and with a PATH holding only the accepted entries, so a helper it looks up by name does not come from the current folder either. On Windows the browser is started through the file association with the system folder as its working folder; because that call takes no environment, PATH is narrowed in the `omli-ui login` process for the moment of the call.
- `api_key_env` in the model registry must name an environment variable outside the `OMLI_` prefix and outside the home-directory and proxy variables OMLi reads. The key is sent only in the `Authorization` header, and that header is never forwarded on a cross-origin redirect.

## The local UI (`omli-ui`)

### The server (`omli-ui serve`)

- **Where it listens.** The server binds `127.0.0.1` only. No setting, flag or variable changes the bind address or turns login off.
- **Host check.** Every request and every WebSocket must carry exactly one `Host` header equal to `127.0.0.1:<port>` or `localhost:<port>`; anything else gets 400. This defeats DNS rebinding, where a page on another name resolves to this machine but still sends its own Host.
- **Origin check.** Every request that is not GET or HEAD, and every WebSocket, must carry exactly one `Origin` equal to this server's own origin; anything else gets 403. No CORS header is ever sent, so no other origin can read a response.
- **Session check.** Outside the login routes, a live session cookie whose key is still registered is required: 401 for the API, a redirect to `/login` for pages, a closed WebSocket otherwise. The cookie is `HttpOnly` and `SameSite=Strict` and lasts 8 hours. Sessions live in the server's memory, so a restart signs everyone out.
- **Response headers.** Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` and a Content-Security-Policy that allows scripts, connections and images from the server's own origin only (plus `data:` images and inline styles), forbids framing, and sets `base-uri 'none'`.
- **The page's files** are served from an exact list read from the package at start-up; a request path never reaches the file system.
- **Where the data lives.** The server and every `omli-ui` command keep their data in `OMLI_HOME` when it is set, and otherwise in `.omli/data` in your home folder. They never use the folder they are started in, so a folder someone else prepared cannot supply login keys, a project list, a model registry or boards. Run them as `omli-ui`. Started with `python -m`, Python searches that folder first for the modules it loads at start-up, and for `omli` itself, before any OMLi code runs, so a folder someone else prepared could run code as you; if you must use that form, run `python -P -m omli.ui` (Python 3.11 or later) or `python -I -m omli.ui`.

### Login

- **Keys, not passwords.** `omli-ui init` makes an ed25519 key pair. The private key is encrypted with your passphrase and kept in your home folder (`ui_login.key` in its `.omli` folder, or wherever `OMLI_UI_KEY` points); it never goes to the browser. The public key is registered in `ui/keys.jsonl` in the data folder. With no registered key, nobody can sign in.
- **Challenge and answer.** The login page asks for a one-time challenge (valid 120 seconds); `omli-ui sign` signs it on this computer, and the page sends the signature back. A challenge is spent on its first use, whether the answer is right or wrong.
- **The one-step ticket.** `omli-ui login` signs a challenge itself and exchanges it for a ticket that works once, within 30 seconds. It writes a small page holding the sign-in link into a new temporary folder that only your user can read, and starts the browser with that file's path, so the ticket appears on no command line. The page sends the browser to `/login#ticket=...`: a fragment is never sent to a server, so the ticket appears in no request line and no server log. The login page removes the ticket from the address, redeems it for the session cookie, and goes to `/`. The command removes the page and its folder once the ticket has expired, or at once if no browser opens. It talks to 127.0.0.1 directly and ignores proxy settings. At most 8 tickets can be open at once.
- **Known limit of the ticket:** for its 30 seconds the ticket is in that page file, which programs running as your user can read, and the browser may keep the final address in its history, by which time the ticket has been used or has expired.
- **`init --replace`** makes a new key and revokes every key registered before it. A session signed in with a revoked key ends at its next request.
- **Passphrase from the environment.** `OMLI_UI_KEY_PASSPHRASE` supplies the passphrase without a prompt. Use it for scripts only: other programs you run can read the process environment.

### Limits on open challenges and sessions

- At most 32 sign-in challenges can be open at once, and at most 32 sessions. Past the cap, a new challenge is refused (429) until one is used or expires.
- **Known limit:** a program already running as your user can fill the challenge cap and block sign-in for a challenge's two-minute lifetime, and for as long as it keeps refilling it. A web page cannot, because the server refuses requests from other origins.

### Other known limits

- **The session cookie is sent to every port on 127.0.0.1.** Browsers do not separate cookies by port, so another local web service that your browser visits receives the session cookie and can use it until it expires (8 hours) or you sign out. Only a program running on your own computer can do this. Pages from other ports still cannot change anything, because the Origin check refuses them.

### What a signed-in user can do

A session has every power listed here; there are no roles in this release.

- Read, save and restore every board under `boards/` in the data folder, and record checkpoints. Every change is appended with the signing key's id; nothing earlier is rewritten.
- Add and remove folders on the project list, and scan a folder for git repositories (up to 6 levels deep and 20,000 folders). Adding or scanning reads the folder you name, which need not be on the list yet.
- Run retrieval over text files inside listed project folders only, and send the matching snippets to the configured model. Retrieval never reads hidden folders or files, `.git`, files whose names look like keys, certificates, environment files or credential and secret stores (patterns in the `mindmap` pack), files over its size limit, or the data folder. It reads only regular files, visits each real folder once in a request (so a junction or link loop ends, and overlapping sources are read once), and stops at the folder (examined), file and real-depth limits in the same pack, counted across all the sources of a request.
- Drop a file onto a board: its text is extracted by code and, with a model configured, sent to that model to be titled and summarized.
- Ask the configured model about a board or a node. Each call runs between the pre-gate and the post-gate, and its input and output are written to the artifact chain. Content tagged `local-only` is refused for a non-local model.
- Nothing in the UI opens a terminal. The one program it can start is the core's GPU sampler: each model call may run `nvidia-smi`, found only in an absolute folder on PATH, at most once every 2 seconds to record GPU load. Set `OMLI_GPU_TELEMETRY=0` to turn this off. Apart from adding and scanning folders, it reads files only in the data folder, inside listed project folders, and in the installed package.

### Out of scope for the UI

- **Other people on the same computer** who can read your home folder, the data folder or the server process.
- **What a hosted model provider does with what it receives.** A model call sends board text and retrieved snippets to the endpoint your registry names.
- **Pictures linked from another host.** A board picture whose address is on another host appears as a placeholder. Choosing it starts a new browser tab at that address, which then talks to that host.
