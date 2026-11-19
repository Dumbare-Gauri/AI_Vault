# 17 — Gmail Connector Design (next phase)

**Status:** Design only. Nothing here is implemented, and no Gmail scope is requested anywhere in the code. This document is the plan for the next phase and the readiness gate for it (Gate 5). It is not a working integration.

## Goal

Let a user connect a Gmail account so that AI Vault can find emails that carry documents, index those attachments next to Drive and local files, and answer questions across all of them:

- "Find the invoice I received from this supplier."
- "Which invoice has a payment deadline next week?"
- "Find the email that contained this contract."
- "Show me the attachments related to this project."

The existing safety boundaries stay in place: tenant isolation, permission checks, retrieval-only grounding, and untrusted handling of content.

## What exists today

| Area | Current state | Where |
|---|---|---|
| OAuth scopes | `openid email profile https://www.googleapis.com/auth/drive`. No Gmail scope. | `vault_shared/settings.py:64`, `.env.example:52` |
| Consent flow | `access_type=offline`, `prompt=consent` and `include_granted_scopes=true`, so incremental authorization already works. | `vault_shared/connectors/google_workspace.py:56-67` |
| Token storage | Fernet-encrypted access and refresh tokens. `granted_scopes` is kept as a space-separated string. | `connector_credentials` table; `security/encryption.py` |
| Refresh | Refreshes when less than 60s remain. `invalid_grant` marks the connector `reauth_required`. A refresh does **not** update `granted_scopes`, which is a gap. | `vault_shared/connector_service.py:45-61` |
| Scope gating pattern | `write_access_problems()` checks `granted_scopes` before a mutation. | `storage/adapters/google_drive.py:236-245` |
| One connector per provider | `UniqueConstraint(organization_id, provider)`. | `db/models/storage_connector.py:81` |
| Storage abstraction | `StorageAdapter` is shaped like a file system: containers, scan, `check_changes`, `open_read`. Capabilities default to off, so a read-only adapter is honest about what it supports. | `storage/adapter.py`, `storage/capabilities.py` |
| Incremental sync | `storage_sources.change_token` plus the scanner's `check_changes` loop. | `worker/scanner/scan_service.py:192-277` |
| Dedupe | `GROUP BY files.checksum`. Drive stores MD5 and the local agent stores SHA-256 in the same column, with no record of which algorithm was used. | `file_repository.py` |

## Scopes

- **Request `https://www.googleapis.com/auth/gmail.readonly` only**, through incremental authorization when the user turns on Gmail. Drive users who never turn it on are never asked.
- Rule out `gmail.metadata`. It cannot download attachments and forbids the `q` search parameter, so it cannot meet the attachment use case.
- Never request `gmail.modify`, `gmail.send` or the full `mail.google.com` scope. AI Vault does not change mail. Every mutating capability of the adapter is off.
- `gmail.readonly` is a restricted scope, so Google's security assessment applies before public release. Plan for it.
- Gate every Gmail read on `granted_scopes`, following the `write_access_problems` pattern. Fix the refresh path so it also persists the `scope` Google returns.

## Connector identity

- Add `ConnectorProvider.GOOGLE_GMAIL`, which needs no migration because the column is a string. It gets its own connect route, so the one-connector-per-provider constraint holds.
- **Revocation caveat:** Google combines grants per OAuth client, so revoking the token on Gmail disconnect would also remove Drive access. Gmail disconnect must therefore delete Gmail data and credentials locally, but skip the provider revoke while the Drive connector still uses the grant.

## Representation

Messages are not forced into folders. Labels are many-to-many and threads are not hierarchical.

- **Mailbox:** one `storage_sources` row, with `drive_type="mailbox"` and `provider_drive_id="me"`.
- **Attachment:** one `files` row per attachment.
  - `provider_file_id = "{message_id}:{part_id}"`. This is stable. Gmail's `attachmentId` is not stable and is kept only as fetch metadata.
  - `provider_parent_id = thread_id`
  - `provider_created_at = internalDate`
  - `owner_email` is the account address, so the existing ownership filter applies.
  - `web_view_link` is the Gmail message URL.
- **Message metadata:** a new table `email_messages` with `id`, `storage_source_id`, `provider_message_id`, `thread_id`, `history_id`, `label_ids` (JSONB), `from_address`, `to_addresses`, `subject`, `internal_date`, `snippet`, `deleted_at`, and a unique key on (`storage_source_id`, `provider_message_id`).
- **Link table:** `email_attachments` with `file_id`, `message_id`, `part_id`, `attachment_id`, `filename`, `mime_type` and `size_bytes`.

A sibling interface (`MailSource`) serves message metadata. The `StorageAdapter` read side serves attachments: `scan`, `check_changes` and `open_read`.

## Source references

An answer grounded in an attachment cites all of the following:

- the attachment's file name;
- the message subject, sender and date;
- the page, sheet or row inside the attachment, through the existing citation location fields;
- a link to the message.

A message body is cited by message id and passage.

## Dedupe

- Compute MD5 while streaming an attachment, and store it in `files.checksum`.
- Add a `checksum_algorithm` column, or prefix the stored value, to fix the current MD5/SHA-256 ambiguity.
- An attachment that is byte-identical to a Drive file then lands in the same duplicate group across connectors.
- The Gate 1 rules still apply: duplicates are never cleaned up automatically, and Gmail has no mutating capability at all.

## Incremental sync

1. **First sync:** `messages.list` with `q=has:attachment`, paginated, then `users.getProfile` to store the `historyId` in `change_token`.
2. **Incremental sync:** `history.list` from that `historyId`, covering messageAdded, messageDeleted and labelsChanged, through the existing `_ingest_incremental` loop.
3. **Expired cursor:** Gmail returns 404 for a stale `startHistoryId`. Add a `StorageCursorExpiredError` that falls back to a full resync.

## Revocation, deletion and reauthorization

- `invalid_grant` goes through the existing `reauth_required` path. Nothing is deleted, and syncing stops until the user reconnects.
- **messageDeleted** marks the message's attachments `permanently_deleted_at`, sets `email_messages.deleted_at`, and purges extracted text, embeddings and citations derived from them.
- **Message moved to Gmail Trash** sets `trashed` on its attachments.
- **Disconnect** deletes the source, cascading through files and messages. Execution history survives because of migration 0029.

## What reaches the model

- Never a mailbox.
- **Selection is deterministic:** sender, date, labels and attachment type pick the candidates through indexed metadata. Retrieval then picks passages.
- Only the relevant passages of the relevant attachments go to the model, wrapped as untrusted data by `ai_gateway/boundary.py`, the same as file content today.
- Email bodies and attachments can carry prompt injection. They are data, never instructions, and an email can never trigger an action.
- **Sensitive data:**
  - Sender and recipient addresses are not put in model context unless the question needs them.
  - Subjects and snippets are not logged.

## Minimal migrations (0030+)

1. `email_messages` and `email_attachments` tables.
2. `files.checksum_algorithm`.
3. `insight_records` gains `status`, `dismissed_at`, `dismissed_by`, `evidence` (JSONB) and `fingerprint`. This makes proactive insights, including "payment due" ones, dismissible and deduplicated.

## Structured fields for deadlines

"Which invoice is due next week?" needs extracted fields, not similarity search. Add a `document_fields` table with `file_id`, `field` (`due_date`, `amount`, `currency`, `invoice_number`, `counterparty`), `value`, `source_location` and `confidence`.

- It is filled by deterministic patterns first, with the model only confirming a candidate.
- It is queried with ordinary date filters.
- It applies equally to Drive and local files, so it can be built before Gmail.

## Tests required before Gmail is called working

Contract and unit tests, using fakes:

- The storage contract suite (`tests/storage_contract.py`) against a fake Gmail client. Every mutation raises `StorageUnsupportedError`.
- Scope gating: with only `gmail.metadata` granted, attachment fetch is reported as a problem.
- Incremental authorization keeps the Drive scopes after the Gmail grant.
- Refresh persists `granted_scopes`.
- A history 404 falls back to a full resync.
- `messageDeleted` purges derived text, embeddings and citations.
- An attachment identical to a Drive file joins its duplicate group.
- Tenant isolation: organization B cannot read organization A's messages or attachments through any `get_owned_*` path.
- Disconnect deletes Gmail data but does not revoke the shared Drive grant.
- An injected instruction inside an email is never followed and never triggers an action.

**Real-account test:** against a disposable Gmail account seeded with known messages and attachments, it connects, syncs, answers "find the invoice from X" with a correct citation, picks up a newly received message on incremental sync, and survives disconnect and reconnect.

Gmail integration is **not** complete until the real-account test passes.
