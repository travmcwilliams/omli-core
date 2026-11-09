/**
 * OMLi Connection Registry — CONTRACT SPEC (subordinate to the Python enforcement).
 *
 * Python is canonical. This file is written with zod, but nothing in this repository runs
 * it or installs zod: the Python package does not use zod, and the guardrail that runs is
 * `omli/connections/schema.py` + `omli/connections/scope_gate.py`. This file states the
 * contract for a TypeScript consumer (none ships here). `tests/test_connection_contract_sync.py`
 * asserts that its operation set and approval escalate-triggers match the Python side.
 * Do NOT edit the enums below without updating the Python source of truth.
 *
 * For EXTERNAL targets: the only place a
 * concrete external target + credential reference is named (the role the Model Registry plays
 * for models). Packs reference a connection by `connection_id`; core never names a target.
 * Local subprocess tools are NOT connections.
 *
 * IMMUTABILITY: a registry is immutable per version. Changing the selection checklist, the
 * credential, the blast radius, or the approval policy = a NEW draft version + human sign-off
 * (draft -> staging -> production). Never edit a published version.
 *
 * DEFAULT-DENY: nothing is mirrored or touched unless a resource is listed AND `selected: true`
 * AND the operation is in that row's `operations`. `forbidden_paths` and the blast-radius caps
 * override any selection. A change-class op (propose-pr/write-branch) requires `read` on the
 * same row — read never implies any write.
 */

import { z } from "zod";

const SECRET_REF = /^secret:\/\//; // credentials are references only — never inline

export const ConnectionRegistryMetadata = z.object({
  registry_id: z.string().min(1),
  registry_version: z.string(),
  authored_at: z.string().datetime(),
  authored_by: z.string().min(1),
  description: z.string().optional(),
  status: z.enum(["draft", "staging", "production", "deprecated"]),
});

// Operations a connection may permit, per selected resource. NO "merge" / "write-main".
// "connect" is egress reachability (container-egress only — may open a socket to this
// domain); it is NOT a change-class op. "post" is an outbound message to a slack channel
// (slack only); also NOT a change-class op. "send-email" is an outbound email to a recipient
// address (smtp and graph-mail only); also NOT a change-class op.
// "read-thread" is the INBOUND half of a slack channel — reading messages and thread replies
// back off it, so a question OMLi asked can be answered (slack only). It is deliberately a
// SEPARATE name from "read": that one is the read-before-change partner of
// propose-pr/write-branch, and a messaging grant must never satisfy that check.
// SYNC: must equal omli.connections.schema.CONNECTION_OPERATIONS.
export const ConnectionOperation = z.enum([
  "read",
  "propose-pr",
  "write-branch",
  "connect",
  "post",
  "send-email",
  "read-thread",
]);

export const SelectableResource = z
  .object({
    id: z.string().min(1),
    kind: z.enum(["path-glob", "branch", "endpoint", "scope", "domain", "channel", "address"]),
    label: z.string().optional(),
    selected: z.boolean(),
    operations: z.array(ConnectionOperation).default([]),
  })
  .refine((r) => r.selected || r.operations.length === 0, {
    message: "an unselected resource must not declare operations (it grants nothing)",
  })
  .refine(
    (r) =>
      !r.operations.some((o) => o === "propose-pr" || o === "write-branch") ||
      r.operations.includes("read"),
    { message: "a change-class op requires 'read' on the same resource" },
  );

// This contract requires at least one selected resource in every registry, whatever its
// status. The Python parser is more permissive in one case: it accepts zero selected
// resources when the registry's status is "deprecated".
export const ConnectionSelection = z
  .object({
    mode: z.literal("allowlist"),
    resources: z.array(SelectableResource),
  })
  .refine((s) => s.resources.some((r) => r.selected), {
    message: "a connection must select at least one resource (zero selected resources grants nothing)",
  });

// Change-shaped caps are required on git-repo / http-service targets; browse-shaped caps
// (pages/captures) are required on http-browse targets; container-egress requires only the
// deny overlay (forbidden_paths) + the structural falses. The Python validator enforces the
// per-kind requirement; here all cap pairs are optional at the type level.
export const BlastRadius = z.object({
  max_files_per_change: z.number().int().positive().optional(),
  max_lines_per_change: z.number().int().positive().optional(),
  forbidden_paths: z.array(z.string()),
  direct_push: z.literal(false),
  destructive_ops: z.literal(false),
  max_pages_per_session: z.number().int().positive().optional(),
  max_captures_per_domain: z.number().int().positive().optional(),
  max_sends_per_day: z.number().int().positive().optional(), // smtp and graph-mail send ceiling
});

// SYNC: the escalate_on enum must equal omli.connections.schema.ESCALATION_TRIGGERS.
export const ApprovalPolicy = z.object({
  mode: z.literal("pr-only"),
  required_reviewers: z.array(z.string()).min(1),
  auto_merge: z.literal(false),
  escalate_on: z.array(
    z.enum([
      "out-of-selection",
      "blast-radius-exceeded",
      "forbidden-path",
      "sensitivity-mismatch",
      "change-class-op",
    ]),
  ),
});

// credential_ref is required for every kind except the anonymous kinds http-browse
// (anonymous browse profile) and container-egress (a network boundary carries no credential);
// when present it must still be a secret:// ref. Python enforces the per-kind requirement.
// slack is credentialed (a bot token as a secret:// ref) and its rows are 'channel' rows
// granting only the messaging-layer ops 'post' and/or 'read-thread' — granted independently,
// so posting into a channel never implies permission to read it back. The two MAIL kinds — smtp
// (an app password) and graph-mail (an app client secret, used to obtain a token for the Graph
// sendMail call) — are both credentialed and share identical row semantics: 'address' rows granting only 'send-email',
// with an optional max_sends_per_day ceiling. They differ only in how OMLi dials.
// graph-mail's identifier carries "graph-mail:<tenant_id>:<client_id>".
export const ConnectionTarget = z.object({
  kind: z.enum(["git-repo", "http-service", "http-browse", "container-egress", "slack", "smtp",
                "graph-mail"]),
  identifier: z.string().min(1),
  default_branch: z.string().optional(),
  credential_ref: z.string().regex(SECRET_REF).optional(),
  principal: z.string().min(1),
  sensitivity_allowed: z.array(z.string()).default([]),
});

export const ConnectionEntry = z.object({
  connection_id: z.string().min(1),
  target: ConnectionTarget,
  selection: ConnectionSelection,
  blast_radius: BlastRadius,
  approval: ApprovalPolicy,
});

export const ConnectionRegistry = z
  .object({
    metadata: ConnectionRegistryMetadata,
    connections: z.array(ConnectionEntry),
  })
  .refine(
    (reg) =>
      new Set(reg.connections.map((c) => c.connection_id)).size === reg.connections.length,
    { message: "connection_id values must be unique within a registry" },
  );

/**
 * PROVENANCE FIELDS (defined in omli/artifacts/provenance.py, additive + optional):
 *   connection_id?:        which connection authorized the external action
 *   connection_version?:   which registry version
 *   selected_resource_id?: which ticked checklist row permitted it
 * The Python package reads them but never sets them; a caller performing an external action fills them.
 */
