"""At-rest integrity for the artifact log — the on-disk half of Primitive 4.

A hash chain over the exact bytes of `var/artifacts.jsonl` makes corruption and naive edits
*detectable*: `seal` snapshots the chain, `verify` recomputes and reports ok / appended /
tampered / truncated / corrupt. A seal covers only the sealed prefix (later records read as
`appended`), and a rollback is caught only while the anchor log is in use and pinned. The seal itself is unsigned, so alone it is corruption detection, not
tamper-evidence; the first trust anchor is the git-tracked head log
(`anchor.py`) — a re-sealer must also forge git history. The durable anchor is the ed25519
head signature (`signer.py`): a **signed** seal is tamper-evidence, as long as the public key
or keyring is pinned where an editor cannot change it, because `verify` holds the seal's chain,
head and count to one another and a re-sealer cannot sign a new head without the off-repo
private key. See `ledger.py` (primitives),
`verifier.py` (seal/verify), `anchor.py` (anchored heads), `signer.py` (head signing).
"""

from omli.integrity.anchor import (
    DEFAULT_ANCHOR,
    AnchorEntry,
    AnchorReport,
    anchor_head,
    read_anchor,
    verify_anchor,
)
from omli.integrity.signer import (
    DEFAULT_PUBKEY,
    SignatureReport,
    generate_keypair,
    read_signature,
    sign_head,
    signing_preflight,
    verify_signature,
)
from omli.integrity.ledger import (
    ALGO,
    GENESIS,
    compute_chain,
    content_id,
    head,
    link,
    read_log_lines,
    read_log_records,
    split_records,
)
from omli.integrity.verifier import (
    IntegrityReport,
    SealResult,
    render,
    seal,
    verify,
)

__all__ = [
    "DEFAULT_ANCHOR",
    "AnchorEntry",
    "AnchorReport",
    "anchor_head",
    "read_anchor",
    "verify_anchor",
    "DEFAULT_PUBKEY",
    "SignatureReport",
    "generate_keypair",
    "read_signature",
    "sign_head",
    "signing_preflight",
    "verify_signature",
    "ALGO",
    "GENESIS",
    "compute_chain",
    "content_id",
    "head",
    "link",
    "read_log_lines",
    "read_log_records",
    "split_records",
    "IntegrityReport",
    "SealResult",
    "render",
    "seal",
    "verify",
]
