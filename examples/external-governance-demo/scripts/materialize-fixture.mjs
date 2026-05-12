import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, join, normalize, relative, resolve, sep } from "node:path";

const ROOT = resolveArg("--root") ?? process.cwd();
const ARTIFACT = resolveArg("--artifact") ?? join(ROOT, "artifacts", "l4-smoke-fixture.md");

function resolveArg(name) {
  const index = process.argv.indexOf(name);
  if (index === -1) return null;
  const value = process.argv[index + 1];
  if (!value || value.startsWith("--")) {
    throw new Error(`${name} requires a value`);
  }
  return resolve(value);
}

function assertInsideRoot(path) {
  const rel = relative(ROOT, path);
  if (rel === "" || rel.startsWith("..") || isAbsolute(rel)) {
    throw new Error(`refusing to write outside fixture root: ${path}`);
  }
}

function parseHints(markdown) {
  const hints = [];
  const re = /<!--\s*governance-hint\s*([\s\S]*?)\s*-->/g;
  for (const match of markdown.matchAll(re)) {
    const raw = match[1].trim();
    if (!raw) continue;
    hints.push(JSON.parse(raw));
  }
  return hints;
}

function parseFiles(markdown) {
  const files = [];
  const re = /(`{4,})file\s+path="([^"]+)"\r?\n([\s\S]*?)\r?\n\1/g;
  for (const match of markdown.matchAll(re)) {
    const relPath = normalize(match[2]).split(sep).join("/");
    if (!relPath || relPath.startsWith("../") || isAbsolute(relPath)) {
      throw new Error(`invalid artifact file path: ${match[2]}`);
    }
    files.push({ relPath, content: match[3] });
  }
  return files;
}

const markdown = readFileSync(ARTIFACT, "utf8");
const hints = parseHints(markdown);
const files = parseFiles(markdown);

if (!hints.length) {
  throw new Error(`missing governance-hint block in ${ARTIFACT}`);
}
if (!files.length) {
  throw new Error(`missing file blocks in ${ARTIFACT}`);
}

for (const file of files) {
  const target = resolve(ROOT, file.relPath);
  assertInsideRoot(target);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, `${file.content}\n`, "utf8");
}

console.log(`materialized ${files.length} files from ${relative(ROOT, ARTIFACT).split(sep).join("/")}`);
console.log(`loaded ${hints.length} governance hints`);
