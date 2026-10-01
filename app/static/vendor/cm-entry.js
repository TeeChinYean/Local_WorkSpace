// esbuild entry for the vendored CodeMirror 6 bundle used by /ide.
// Rebuild: see VENDOR.md. Exposes a single global: window.CM
import { EditorState, Compartment, EditorSelection } from "@codemirror/state";
import {
  EditorView, keymap, lineNumbers, highlightActiveLineGutter, highlightSpecialChars,
  drawSelection, dropCursor, rectangularSelection, crosshairCursor, highlightActiveLine,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import {
  foldGutter, indentOnInput, syntaxHighlighting, defaultHighlightStyle, bracketMatching,
  foldKeymap, StreamLanguage, indentUnit,
} from "@codemirror/language";
import { searchKeymap, highlightSelectionMatches, openSearchPanel, gotoLine } from "@codemirror/search";
import { autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { oneDark } from "@codemirror/theme-one-dark";

import { python } from "@codemirror/lang-python";
import { javascript } from "@codemirror/lang-javascript";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { json } from "@codemirror/lang-json";
import { markdown } from "@codemirror/lang-markdown";
import { xml } from "@codemirror/lang-xml";
import { sql } from "@codemirror/lang-sql";
// C/C++/Java/C#/Kotlin/Rust use the (much smaller) legacy stream modes instead of
// @codemirror/lang-cpp / lang-java / lang-rust (~220KB of Lezer grammars) to stay < 900KB.
import { cpp, java, csharp, kotlin } from "@codemirror/legacy-modes/mode/clike";
import { rust } from "@codemirror/legacy-modes/mode/rust";
import { go } from "@codemirror/lang-go";

import { powerShell } from "@codemirror/legacy-modes/mode/powershell";
import { shell } from "@codemirror/legacy-modes/mode/shell";
import { yaml } from "@codemirror/legacy-modes/mode/yaml";
import { toml } from "@codemirror/legacy-modes/mode/toml";
import { dockerFile } from "@codemirror/legacy-modes/mode/dockerfile";
import { properties } from "@codemirror/legacy-modes/mode/properties";
import { diff } from "@codemirror/legacy-modes/mode/diff";

// Minimal Windows batch (.bat/.cmd) stream mode (no upstream legacy mode exists).
const batch = {
  name: "batch",
  token(stream) {
    if (stream.sol() && stream.match(/^\s*(rem\b|::).*/i)) return "comment";
    if (stream.eatSpace()) return null;
    if (stream.match(/^"[^"]*"?/)) return "string";
    if (stream.match(/^%~?[\w]+%?|^%%\w|^![\w]+!/)) return "variableName";
    if (stream.sol() && stream.match(/^:\w+/)) return "labelName";
    if (stream.match(/^@?(echo|set|if|else|goto|call|for|in|do|exit|not|exist|defined|errorlevel|setlocal|endlocal|shift|pushd|popd|cd|start|equ|neq|lss|leq|gtr|geq)\b/i)) return "keyword";
    if (stream.match(/^\d+/)) return "number";
    stream.next();
    return null;
  },
};

const legacy = (m) => () => StreamLanguage.define(m);
const L = {
  python: () => python(),
  javascript: () => javascript(),
  jsx: () => javascript({ jsx: true }),
  typescript: () => javascript({ typescript: true }),
  tsx: () => javascript({ typescript: true, jsx: true }),
  html: () => html(),
  css: () => css(),
  json: () => json(),
  markdown: () => markdown(),
  xml: () => xml(),
  sql: () => sql(),
  cpp: legacy(cpp),
  java: legacy(java),
  csharp: legacy(csharp),
  kotlin: legacy(kotlin),
  rust: legacy(rust),
  go: () => go(),
  powershell: legacy(powerShell),
  shell: legacy(shell),
  yaml: legacy(yaml),
  toml: legacy(toml),
  dockerfile: legacy(dockerFile),
  ini: legacy(properties),
  diff: legacy(diff),
  bat: legacy(batch),
};

const EXT = {
  py: "python", pyw: "python", pyi: "python",
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "jsx",
  ts: "typescript", mts: "typescript", cts: "typescript", tsx: "tsx",
  html: "html", htm: "html", vue: "html", svelte: "html",
  css: "css", scss: "css", less: "css",
  json: "json", jsonc: "json", ipynb: "json",
  md: "markdown", markdown: "markdown",
  xml: "xml", svg: "xml", xaml: "xml", csproj: "xml",
  sql: "sql",
  c: "cpp", h: "cpp", cc: "cpp", cpp: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp",
  java: "java", kt: "kotlin", kts: "kotlin", cs: "csharp",
  rs: "rust", go: "go",
  ps1: "powershell", psm1: "powershell", psd1: "powershell",
  sh: "shell", bash: "shell", zsh: "shell",
  yml: "yaml", yaml: "yaml",
  toml: "toml",
  ini: "ini", cfg: "ini", conf: "ini", properties: "ini", env: "ini",
  diff: "diff", patch: "diff",
  bat: "bat", cmd: "bat",
};
const NAMES = { dockerfile: "dockerfile", containerfile: "dockerfile", makefile: "shell", ".bashrc": "shell", ".env": "ini", ".gitignore": "ini" };

function languageFor(filename) {
  const base = String(filename || "").split(/[\\/]/).pop().toLowerCase();
  let id = NAMES[base];
  if (!id) {
    const i = base.lastIndexOf(".");
    if (i >= 0) id = EXT[base.slice(i + 1)];
  }
  if (!id && base.startsWith("dockerfile")) id = "dockerfile";
  if (!id || !L[id]) return { id: "plaintext", ext: [] };
  try { return { id, ext: L[id]() }; } catch (e) { return { id: "plaintext", ext: [] }; }
}

// basicSetup equivalent; cursorBlinkRate 0 = no infinite CSS blink animation (GPU/battery friendly).
const basicSetup = () => [
  lineNumbers(), highlightActiveLineGutter(), highlightSpecialChars(), history(), foldGutter(),
  drawSelection({ cursorBlinkRate: 0 }), dropCursor(), EditorState.allowMultipleSelections.of(true), indentOnInput(),
  syntaxHighlighting(defaultHighlightStyle, { fallback: true }), bracketMatching(), closeBrackets(),
  autocompletion(), rectangularSelection(), crosshairCursor(), highlightActiveLine(),
  highlightSelectionMatches(),
  keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap,
    ...foldKeymap, ...completionKeymap, indentWithTab]),
];

window.CM = {
  EditorState, EditorView, EditorSelection, Compartment, keymap, basicSetup, oneDark,
  indentUnit, openSearchPanel, gotoLine, languageFor, languages: Object.keys(L),
};
