# Security policy

## Reporting a vulnerability

Please report security problems privately, through this repository's **Security** tab ("Report a vulnerability"). Do not open a public issue.

Include what you ran, what you expected, and what happened. A minimal script that reproduces it is the most useful thing you can send.

This is a one-maintainer project. I aim to acknowledge reports within 7 days and to agree on a disclosure date with you before anything is published. Those are targets, not guarantees.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |
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
- `api_key_env` in the model registry must name an environment variable outside the `OMLI_` prefix and outside the home-directory and proxy variables OMLi reads. The key is sent only in the `Authorization` header, and that header is never forwarded on a cross-origin redirect.
