# OMLi

**Open Model Local integrator.** A small Python core that runs language models inside deterministic gates and records every step as an immutable, append-only chain of artifacts.

> **Status: v0.1, early.** One maintainer. Tested in CI on Linux, macOS and Windows with Python 3.10 and 3.12 ([CI runs](https://github.com/travmcwilliams/omli-core/actions/workflows/ci.yml)). 1,039 tests pass on each CI runner. Read [Trust model and limitations](#trust-model-and-limitations) before relying on it.

## Who it is for

Engineers building agent or LLM workflows who want three things a prompt cannot give them:

- a model that is **never the first or last decision-maker** on whether something happens;
- workflow state you can **query from a log**, not reconstruct from an agent's memory;
- a record of what happened that **shows when it has been edited**.

OMLi is a Python library with two command-line entry points:

- `python -m omli.integrity`: seal and verify a log, and manage signing keys;
- `python -m omli.connections`: a dry run of the connection scope gate.

The repository also has an example script, `examples/walkthrough.py`. There is no web UI and no hosted service. OMLi runs on your machine with a local model server such as [Ollama](https://ollama.com), or with an OpenAI-compatible endpoint you configure.

## The problem

Most agent code puts the model in charge: it decides what to do, the code does it, and state lives in a conversation or a coordinator's memory. When something goes wrong you cannot answer basic questions. Did the model run? What did it see? Was this input already processed? Has anyone changed the record since?

OMLi moves those answers out of the model and into plain, testable code and an append-only log.

## How it works: five rules

The core is built around five design rules. Each one is code you can read and tests you can run. Where a rule has edges, they are stated here.

### 1. A generic core; domain logic lives in packs

Prompts, thresholds, required output fields and model selection live in YAML packs. The example pack opens with:

```yaml
# Example config pack — domain logic as DATA, not code (Primitive 1).
#
# This is the seam: everything domain-specific (identity, prompt, thresholds, required
# output) lives here. Swapping this file for another vertical's pack changes behavior
# without touching anything under omli/. To add a domain, add a pack like this one.
```

On the gated path (`Agent.run`), models are chosen only through a model registry. The lower-level helpers in `omli.models` (`OllamaModel`, `make_model`) accept a model name from the caller directly.

`tests/test_core_has_no_domain.py` guards this rule with a keyword list. It is a tripwire, not a proof.

### 2. Deterministic gates on both sides of every model call made through `Agent.run`

A pre-gate decides whether the model may run at all. A post-gate checks the output's required fields and score floors before anything is written. If either refuses, nothing is written.

From `examples/walkthrough.py`, which needs no model server:

```text
 CanonicalInput -> Spec
  event pre_gate
  event model_call model=example-local-4b
  event post_gate model=example-local-4b passed=True
  event artifact_written kind=Spec
  event wrote kind=Spec model=example-local-4b
...
 pre-gate, empty payload
  event pre_gate
  event blocked_pre reason=empty payload
  pre-gate block: 0 artifacts written
 post-gate, output missing a required field
  event pre_gate
  event model_call model=example-local-4b
  event post_gate model=example-local-4b passed=False
  event blocked_post reason=missing required field 'missing_field'
  post-gate block: 0 artifacts written
STEP 4: a local-only input never reaches a hosted model
  event pre_gate
  event blocked_pre model=hosted-burst reason=sensitivity-local-only: tags=['local-only'] block provider_kind='openai-compatible'
  local-only block: 0 provider calls, 0 artifacts written
```

The example model ids are placeholders, and EchoProvider stands in for both models. `...` marks lines left out.

**Local-only routing.** An input tagged `local-only` (or `local-only:<scope>`, or a tag your sensitivity policy adds) is refused by any model whose provider kind does not start with `local-`. The refusal happens before the first call and before each escalation, and it applies even when that model's endpoint is on loopback. Separately, a `local-*` registry entry whose endpoint is not loopback is refused for every input.

These checks cover calls made through `Agent.run`. The direct helpers in `omli.models` are not gated.

### 3. The artifact chain is the state machine

Work moves along a fixed chain:

```python
ARTIFACT_KINDS: tuple[str, ...] = (
    "CanonicalInput",
    "Spec",
    "DecisionRecord",
    "WorkResult",
    "OutcomeRecord",
)
```

There is no coordinator and no `workflow_state` field. "Is this done?" and "have we seen this source?" are lookups on the store: `store.is_terminal_done(artifact_id)` and `store.has_source(source_ref)`.

### 4. Immutable and append-only, with linked successors

Artifacts deep-freeze the mappings and lists in their payloads; sets and bytearrays are left as they are. To change something you write a successor that links to its predecessor:

```python
    pred_idx = ARTIFACT_KINDS.index(predecessor.kind)
    succ_idx = ARTIFACT_KINDS.index(kind)
    if succ_idx <= pred_idx:
        raise ValueError(
            f"{kind!r} does not follow {predecessor.kind!r} in the artifact chain"
        )
```

The store has no update or delete method, and writing an id the store already holds raises. Open an existing log with `ArtifactStore.from_log(path)`.

Sensitivity tags only accumulate through `succeed()`, and so through `Agent.run`. The store does not compare tags, so an artifact you build by hand with a predecessor link can carry fewer.

The on-disk log can be sealed with a hash chain over the exact bytes of every record. The seal can optionally be anchored and signed with ed25519. `python -m omli.integrity verify` prints `TAMPERED` and exits 1 when any byte inside the sealed prefix changes.

### 5. No agent rewrites its own memory

The Reflector is the package's only writer of learned "lessons". This is a convention of the call graph, not an enforced boundary: `MemoryStore.append` is public, so any code in the process can call it.

- **Approval.** Every lesson starts unapproved. Approval is an appended marker bound to the sha256 of the lesson's text, so editing an approved lesson withdraws its approval. The latest decision wins, so a later rejection overrides an earlier approval.
- **Consolidations.** A deterministic consolidation's text is rendered from the approved lessons themselves and refuses caller text. A model-written consolidation carries the text it was given and takes effect only after its own approval.
- **Approvers.** An approval records the approver's name. It does not authenticate it.

## Quickstart

You need Python 3.10 or newer. No model server is needed for the walkthrough or the tests.

```bash
git clone https://github.com/travmcwilliams/omli-core.git
cd omli-core
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,sign]"   # "sign" adds ed25519 signing via cryptography
python examples/walkthrough.py           # under a second; no network, no model server; exits 0
pytest -q                                # run from the repository root; about 40 seconds
```

**Notes:**

- **Use a virtual environment.** Recent Debian, Ubuntu and Homebrew Pythons refuse `pip install` outside one.
- **Upgrade pip first.** An editable install from `pyproject.toml` alone needs pip 21.3 or newer.
- **Setuptools warning.** The editable install may print a `BuildBackendWarning` about folders with the same name as the package. It is harmless.
- **Expected skips.** On Linux and macOS, `pytest` skips one test: the live Ollama test, which is opt-in. On Windows it may also skip a symlink test when the account cannot create symlinks.
- **Don't use `-O`.** The walkthrough checks itself with `assert`, so running it under `python -O` would skip those checks.

The walkthrough shows five things:

1. a full chain;
2. a pre-gate block and a post-gate block, each writing nothing;
3. a local-only input stopped before a hosted model, with 0 provider calls;
4. a sealed log verifying `OK`;
5. an edited copy verifying `TAMPERED`.

In the tampered case the output also prints `anchor: ANCHORED`. That is correct: the anchor covers the sealed head, not the edited copy. That verify step exits 1. Head digests and timestamps differ on every run. Paths print with `/` on Linux and macOS and `\` on Windows.

To use a real local model, install Ollama, pull a model, and point a registry entry at `http://localhost:11434`. Set `OMLI_LIVE_OLLAMA=1` to run the live test. It still skips when no server is reachable or no model is pulled.

## Configuration

| Variable | Purpose |
|---|---|
| `OMLI_HOME` | Root for OMLi's default writes. When unset, the current directory; never the package location. The one exception is the private signing key (next rows). Explicit path arguments always win. |
| `OMLI_SEAL_KEY` | Where `keygen` writes, and signing reads, the private key. Default `~/.omli/seal.key`, kept outside any project folder so it is not committed by accident. |
| `OMLI_SECRETS_DIR` | Directory for `secret://` files. With neither it nor `OMLI_HOME` set, only `OMLI_SECRET_*` environment variables resolve. |
| `OMLI_CONNECTION_REGISTRY` | The connection registry to use. Lookup order: this variable, then `$OMLI_HOME/packs/connection_registry.yaml` if it exists, then the example shipped in the package. |

The installed package includes four example files in `omli/defaults/`: a pack, two registries and a sensitivity policy. The repository's top-level `packs/` folder has more examples, which are not installed. Put your own packs and registries under `OMLI_HOME` or pass explicit paths.

## Trust model and limitations

Stated here so you do not have to find them.

- **Code in the same process is trusted.** Boundaries catch `Exception`, not `BaseException`. OMLi does not defend against code running alongside it, and the direct model helpers are not gated.
- **The seal covers the sealed prefix.**
  - Records appended after the last seal verify as `OK (+appended, not yet sealed)` until you seal again.
  - Rollback is detected only while the anchor is in use and pinned somewhere an editor cannot change it.
  - Signing proves nothing unless the keyring and public key are pinned the same way.
- **The lessons log is not sealed by default.** Anyone who can write it can append a valid decision marker. You can seal it by passing its path to `python -m omli.integrity`.
- **Approvals are recorded, not authenticated.**
- **Connections are default-deny.** A send or read happens only when all of these hold:
  - the entry was loaded through the registry;
  - the action matches a selected row;
  - every sensitivity tag on the action is allowed for that target;
  - a `secret://` credential resolves.

  Outcomes carry fixed fields only, never text the remote side returned. Messages read from a channel are returned as untrusted content in a separate field and are not scrubbed.
- **The hosted adapter scrubs its API key** from responses in raw, JSON short-escape, `\u`-escape, percent-encoded and HTML numeric-reference forms. Other encodings, such as base64, are not covered.
- **Hosted models are less tested than Ollama.** Every hosted-adapter test runs offline: all but one inject a fake transport, and that one uses the real urllib transport with a faked opener.
- **The domain guard is a keyword list, not a proof.**

## Known issues

- [#1: The hosted adapter has not been run against a real hosted provider](https://github.com/travmcwilliams/omli-core/issues/1)
- [#2: An operating-system error while reading a secret file is not caught, and its message carries the file's path](https://github.com/travmcwilliams/omli-core/issues/2)
- [#3: The API-key scrubber does not cover base64 or other encodings](https://github.com/travmcwilliams/omli-core/issues/3)
- Smaller test and comment fixes are open as [good first issues](https://github.com/travmcwilliams/omli-core/labels/good%20first%20issue) (#4 to #9).

## Project layout

```text
omli/
  artifacts/    the chain, provenance, the append-only store, a derived SQLite index
  gates/        pre-gate, post-gate, escalation, traffic guard
  agents/       Agent (the gated call path) and Reflector (the lesson writer)
  core/         the Router that resolves a pack's model selection against the registry
  integrity/    hash chain, anchor, keyring, ed25519 signing, CLI
  models/       Model Registry and provider adapters (Ollama, OpenAI-compatible)
  connections/  Connection Registry, scope gate, secrets, mail/Slack/HTTP wires, CLI
  packs/        pack schema and loader
  governance/   findings and promotion proposals
  events/       append-only event bus
  defaults/     an example pack, two registries and a sensitivity policy, shipped with the package
packs/          further example packs (not installed)
examples/       walkthrough.py
tests/
```

## Contributing, security and license

- How to propose changes: [CONTRIBUTING.md](CONTRIBUTING.md).
- How to report a vulnerability privately: [SECURITY.md](SECURITY.md).
- License: Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
