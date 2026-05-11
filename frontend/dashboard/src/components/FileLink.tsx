import { useState } from "react";
import { copyToClipboard, editorConfigured, editorScheme, editorUrl } from "../lib/editor";

interface Props {
  path: string;
  line?: number;
  className?: string;
  showCopy?: boolean;
}

export default function FileLink({ path, line, className = "", showCopy = true }: Props) {
  const [copied, setCopied] = useState(false);
  const url = editorUrl(path, line);

  async function onCopy(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    const ok = await copyToClipboard(path);
    if (!ok) return;
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  }

  const tooltip = editorConfigured
    ? `Open in ${editorScheme}\n${path}${line ? ` : ${line}` : ""}`
    : `${path}\n(set VITE_WORKSPACE_ROOT to enable editor jump)`;

  return (
    <span className={`file-link ${className}`} title={tooltip}>
      {url ? (
        <a className="file-link-anchor" href={url}>
          <span className="file-link-name">{path}</span>
          <span className="file-link-cta">↗</span>
        </a>
      ) : (
        <span className="file-link-anchor file-link-anchor-disabled">
          <span className="file-link-name">{path}</span>
        </span>
      )}
      {showCopy ? (
        <button className="file-link-copy" onClick={onCopy} title="Copy path">
          {copied ? "✓" : "⧉"}
        </button>
      ) : null}
    </span>
  );
}
