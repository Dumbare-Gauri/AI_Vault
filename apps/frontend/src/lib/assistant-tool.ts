/** What Ask Vault did to answer, in words — keyed by the request kind the
 * backend records as `tool_name` ("vault:duplicates"). */
const KIND_LABELS: Record<string, string> = {
  count_files: "File count",
  storage_summary: "Storage overview",
  breakdown: "Storage breakdown",
  largest_files: "Largest files",
  find_files: "File search",
  duplicates: "Duplicates",
  clean_duplicates: "Duplicate cleanup",
  old_files: "Old files",
  inactive_files: "Inactive files",
  cleanup_candidates: "Cleanup candidates",
  archive_candidates: "Archive candidates",
  create_folder: "New folder",
  move: "Move",
  rename: "Rename",
  organize: "Organize",
  archive: "Archive",
  trash: "Trash",
  restore: "Restore",
  what_changed: "Changes",
  action_result: "Changes",
  action_cancelled: "Cancelled",
};

export function assistantToolLabel(toolName: string): string {
  const kind = toolName.startsWith("vault:") ? toolName.slice("vault:".length) : toolName;
  return KIND_LABELS[kind] ?? kind;
}
