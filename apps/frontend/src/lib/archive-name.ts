// Mirrors vault_shared/archive_naming.py, so a bad name is caught while the
// user types; the server checks again.
const FORBIDDEN = /[<>:"/\\|?*]/;

function hasControlCharacters(text: string): boolean {
  return [...text].some((character) => character.charCodeAt(0) < 32);
}
const RESERVED = new Set([
  "con",
  "prn",
  "aux",
  "nul",
  ...Array.from({ length: 9 }, (_, n) => `com${n + 1}`),
  ...Array.from({ length: 9 }, (_, n) => `lpt${n + 1}`),
]);
const MAX_LENGTH = 200;

function stem(raw: string): string {
  const name = raw.trim();
  return name.toLowerCase().endsWith(".zip") ? name.slice(0, -4).trimEnd() : name;
}

export function withZip(raw: string): string {
  return `${stem(raw)}.zip`;
}

/** Why `raw` can't be used as an archive name, or null when it can. */
export function archiveNameProblem(raw: string): string | null {
  const name = stem(raw);
  if (!name || name.replace(/\./g, "") === "") return "Give the archive a name.";
  if (FORBIDDEN.test(name) || hasControlCharacters(name)) return 'An archive name can\'t contain < > : " / \\ | ? * characters.';
  if (/[. ]$/.test(name)) return "An archive name can't end with a dot or a space.";
  if (RESERVED.has((name.split(".")[0] ?? "").toLowerCase())) return `"${name}" is a name Windows reserves. Choose another.`;
  if (name.length + 4 > MAX_LENGTH) return `Keep the archive name under ${MAX_LENGTH} characters.`;
  return null;
}

/** "Project Docs" → "Project_Docs_Archive.zip". */
export function suggestArchiveName(subject: string): string {
  const base = [...subject]
    .filter((character) => character.charCodeAt(0) >= 32)
    .join("")
    .replace(/\.[^./\\]{1,8}$/, "")
    .replace(/[<>:"/\\|?*]+/g, " ")
    .trim()
    .replace(/\s+/g, "_")
    .slice(0, 150);
  return `${base || "Files"}_Archive.zip`;
}
