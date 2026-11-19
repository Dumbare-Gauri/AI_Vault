# 16 — Duplicate Cleanup Safety and Protected Files

**Status:** Implemented 2026-10-16 (Gate 1). Logged as an incident 2026-10-08.

## What happened

On 2026-10-08 at 06:43 UTC the founder asked Ask Vault to "delete duplicate". These are the facts from the `conversation_messages`, `execution_audits` and `agent_commands` tables:

1. `understanding.py` read the request as `clean_duplicates`.
2. Storage Intelligence had found 382 duplicate groups on the computer by content checksum alone.
3. Ask Vault proposed moving every copy except the kept one to Trash: **616 files**. The proposal showed only the first 25.
4. The user confirmed once. The proposal ran as three `ARCHIVE` (move-to-Trash) plans with no approval step (ADR-026), and the local agent moved each file into the `.aivault-trash` folder inside its authorized folder.

| Authorized folder | Files trashed | Notable contents |
|---|---|---|
| `C:\Projects` | 377 | Website images (TFW_WEB 296), 27 empty `__init__.py`, `.env`, `py.typed`, `tsconfig.json`, `.tf` and `.hcl` |
| `C:\Drivers` | 203 | Driver package files: `.inf`-adjacent `.strings`, `.forms`, `.dll`, `.exe` |
| `C:\project_docs` | 36 | Product images and one `.docx` |

About 20 minutes later, `C:\Projects` and `C:\Drivers` were removed from the agent. Removing a folder deleted its storage source, and the database cascade went on to delete files, execution steps, results and rollback records. So the record of what was trashed, and the Undo for it, were erased. Only `agent_commands` kept a trace.

## Root causes

1. **Identical content was treated as redundant.**
   - Every empty file has the same checksum, so every empty `__init__.py` in every project formed one "duplicate group".
   - A `tsconfig.json` shared by several services, or a driver file present in several packages, was also matched.
   - Duplicate detection had no notion of a file *needing to be where it is*.
2. **No protected assets.** Nothing in the planner or the Execution Engine knew what source code, a project or a driver package is.
3. **A bulk removal was a single click.** The proposal named 616 files but showed only 25, and there was no extra step for a large removal.
4. **History depended on the file row.** `execution_steps.target_file_id` was `ON DELETE CASCADE`.

## What is enforced now

- **Empty files are never duplicates.**
  - `FileRepository.list_duplicate_checksum_groups_for_organization` and `list_for_checksum_in_organization` skip `size_bytes = 0`.
- **Protected files.** `vault_shared/protection.py` decides this from the index, never from a model. A file is protected when any of these apply:
  - It is source code (`.py`, `.ts`, `.go`, `.sql`, `.tf` and so on).
  - It is a project or configuration file (manifests, lockfiles, `.env*`, keys and certificates, `Dockerfile`, `py.typed`, `.gitkeep`).
  - It is a program or driver file (`.dll`, `.sys`, `.inf`, `.exe`, `.msi`, `.appx` and so on).
  - It sits under `.git/`, `.github/`, `migrations/` or `alembic/`.
  - It is anywhere inside a detected project: a folder holding a `.git` folder or a manifest such as `package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml` or `*.sln`. This covers pictures inside a website project too.
- **Where protection applies:**
  - It is checked when a plan is created (`ExecutionPlanService._apply_protection`).
  - It is checked again in the Execution Engine right before every mutation (`ExecutionService._reject_if_protected`).
  - It covers removal (`ARCHIVE`, `REMOVE_DUPLICATE`, `PERMANENT_DELETE`, archive-and-remove-originals) and relocation (move, rename).
  - System-assembled sets (duplicate groups, recommendations) leave protected files out and record which in the audit.
  - A user's own selection is refused unless they acknowledge the protected files. That acknowledgment is stored on each step.
- **Ask Vault:**
  - It never removes files that only *resemble* a request. Removal targets come from exact matches or from the list just shown.
  - The model may never read a request as a removal.
  - Protected files are shown separately, with the reason.
  - Every affected file can be paged through.
  - A removal of more than 20 files needs an "I've checked these files" tick.
- **Organization:**
  - AI organize, organization recommendations and AI rename suggestions skip protected files. A project is only moved as a whole.
  - Folders the model is less than 50% sure of stay where they are.
- **Recommendations and workflows:**
  - "Old" and "large and unused" findings are review-only. Age and size are not evidence that a file is unneeded.
  - A workflow policy can no longer auto-execute a removal; it always waits for a person.
- **Revalidation:** a step is refused if the file was renamed or moved since it was reviewed. It was already refused if the content revision changed.
- **Permanent deletion:**
  - The request must carry the typed phrase `DELETE <n> FILE(S)`. The server checks it.
  - Emptying a Trash requires typing `EMPTY TRASH`.
- **History outlives files.** Migration 0029 makes `execution_steps.target_file_id` `ON DELETE SET NULL`. Each step's `pre_state` keeps the file's provider id, path, storage, size and checksum.
- **Truthful outcomes:** a change the storage did not confirm is reported as "could not be confirmed", never as "confirmed".

Regression tests:

- `tests/integration/backend/test_destructive_safety_integration.py`
- `tests/integration/worker/test_destructive_safety_integration.py`
- `tests/unit/shared/test_asset_protection.py`

## Recovery of the 2026-10-08 files

The files were never deleted. Each one is in its folder's `.aivault-trash/<id>/` with a manifest giving its original path. A check on 2026-10-16 found:

| Folder | Restorable to its original path | Original path already occupied |
|---|---|---|
| `C:\Projects` | 332 | 45 (AI_Vault files already restored from git) |
| `C:\Drivers` | 203 | 0 |
| `C:\project_docs` | 36 | 0 |

The UI cannot restore these, because their execution records were erased before migration 0029. They can be restored with the agent's own `LocalOperations.restore(root, trash_id)`, which refuses to overwrite anything.

**Warning:** emptying the computer's Trash from AI Vault permanently deletes the `C:\project_docs` items, because that folder is still authorized.

## Open questions

- Should the protected list be editable per organization?
- Should the Trash page list agent-Trash items that have no index row, so they can be restored from the UI?
