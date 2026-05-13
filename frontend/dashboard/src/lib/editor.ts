// Editor jump helpers. The dashboard renders file paths as clickable links
// that open in the user's editor via OS-registered URI schemes. Configured via:
//   VITE_WORKSPACE_ROOT  — absolute path to the repo on the user's machine
//                          (e.g. "C:/Users/z5866/Documents/amingclaw/aming_claw")
//   VITE_EDITOR_SCHEME   — "vscode" (default), "vscode-insiders", "jetbrains-idea",
//                          "jetbrains-pycharm", or "cursor"
//
// When VITE_WORKSPACE_ROOT is unset the helpers return null and the UI
// falls back to plain monospace text + a "copy path" button.

declare const __DEFAULT_WORKSPACE_ROOT__: string | undefined;

const DEFAULT_ROOT = typeof __DEFAULT_WORKSPACE_ROOT__ === "string" ? __DEFAULT_WORKSPACE_ROOT__ : "";
const RAW_ROOT = (import.meta.env.VITE_WORKSPACE_ROOT as string | undefined) || DEFAULT_ROOT;
const EDITOR_SCHEME = ((import.meta.env.VITE_EDITOR_SCHEME as string | undefined) || "vscode").toLowerCase();

export const editorConfigured: boolean = RAW_ROOT.trim().length > 0;
export const editorScheme: string = EDITOR_SCHEME;

function normalizeRoot(root: string | undefined | null): string {
  if (!root) return "";
  // Normalize backslashes (Windows) to forward slashes for URI building.
  // Strip a trailing slash so we always join with a single one.
  return root.replace(/\\/g, "/").replace(/\/+$/, "");
}

const NORMALIZED_ROOT = normalizeRoot(RAW_ROOT);

export const workspaceRoot = NORMALIZED_ROOT;

export function isEditorConfigured(rootOverride?: string | null): boolean {
  return normalizeRoot(rootOverride || RAW_ROOT).length > 0;
}

function joinAbsolute(rel: string, rootOverride?: string | null): string {
  const root = normalizeRoot(rootOverride || RAW_ROOT);
  if (!root) return "";
  const cleanRel = rel.replace(/\\/g, "/").replace(/^\/+/, "");
  return `${root}/${cleanRel}`;
}

/**
 * Build the editor URI for the given repo-relative path.
 * Returns null when no workspace root is configured.
 *
 * Line + column are 1-based. Most editors silently ignore them when not
 * present in the URI.
 */
export function editorUrl(
  relPath: string,
  line?: number,
  col?: number,
  rootOverride?: string | null,
): string | null {
  if (!isEditorConfigured(rootOverride)) return null;
  const abs = joinAbsolute(relPath, rootOverride);
  // Windows absolute paths look like "C:/foo" — VS Code wants the URI
  // form "vscode://file/C:/foo". JetBrains expects an unencoded path
  // in a query string. We pick per scheme.
  switch (EDITOR_SCHEME) {
    case "vscode":
    case "code":
      return `vscode://file/${abs}${line ? `:${line}${col ? `:${col}` : ""}` : ""}`;
    case "vscode-insiders":
      return `vscode-insiders://file/${abs}${line ? `:${line}${col ? `:${col}` : ""}` : ""}`;
    case "cursor":
      return `cursor://file/${abs}${line ? `:${line}${col ? `:${col}` : ""}` : ""}`;
    case "jetbrains-idea":
    case "idea":
      return `idea://open?file=${encodeURIComponent(abs)}${line ? `&line=${line}` : ""}`;
    case "jetbrains-pycharm":
    case "pycharm":
      return `pycharm://open?file=${encodeURIComponent(abs)}${line ? `&line=${line}` : ""}`;
    default:
      // Fallback: vscode-flavoured URI; most editors ignore the scheme.
      return `vscode://file/${abs}${line ? `:${line}` : ""}`;
  }
}

/**
 * Try to map a `module::function` symbol to a repo-relative file path
 * using the node's primary_files[0] as the home of the symbol.
 * Returns null if the node has no primary file.
 */
export function functionUrl(
  symbol: string,
  primaryFiles: string[] | undefined,
): { url: string | null; file: string | null } {
  void symbol; // line number is not stored anywhere in the snapshot
  const file = primaryFiles?.[0] ?? null;
  if (!file) return { url: null, file: null };
  return { url: editorUrl(file), file };
}

export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // ignore — fallback below
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
