# Vendored front-end libraries for `/ide`

Everything under `app/static/vendor/` is served locally (no CDN at runtime, works offline).

| File | Source | Version | Size |
|---|---|---|---|
| `codemirror.min.js` | esbuild IIFE bundle of `cm-entry.js` (exposes `window.CM`) | see below | ~754 KB (≈265 KB gzip) |
| `xterm.js` | `@xterm/xterm/lib/xterm.js` (sourceMappingURL comment stripped) | 6.0.0 | ~477 KB |
| `xterm.css` | `@xterm/xterm/css/xterm.css` | 6.0.0 | ~7 KB |
| `addon-fit.js` | `@xterm/addon-fit/lib/addon-fit.js` (sourceMappingURL comment stripped) | 0.11.0 | ~1.5 KB |
| `addon-web-links.js` | `@xterm/addon-web-links/lib/addon-web-links.js` (sourceMappingURL comment stripped; exposes `window.WebLinksAddon`). Released together with xterm 6.0.0 (2025-12-22) | 0.12.0 | ~3 KB |
| `cm-entry.js` | esbuild entry file for the CodeMirror bundle (source, not loaded by the page) | – | – |

## Package versions used for the CodeMirror bundle

```
codemirror 6.0.2                  @codemirror/state 6.7.6
@codemirror/view 6.43.13          @codemirror/language 6.12.4
@codemirror/commands 6.11.1       @codemirror/search 6.7.2
@codemirror/autocomplete 6.20.3   @codemirror/theme-one-dark 6.1.3
@codemirror/lang-python 6.2.1     @codemirror/lang-javascript 6.2.5
@codemirror/lang-html 6.4.12      @codemirror/lang-css 6.3.1
@codemirror/lang-json 6.0.2       @codemirror/lang-markdown 6.5.2
@codemirror/lang-xml 6.1.0        @codemirror/lang-sql 6.10.0
@codemirror/lang-go 6.0.1         @codemirror/legacy-modes 6.5.4
@codemirror/lang-cpp 6.0.3 / lang-java 6.0.2 / lang-rust 6.0.2  (installed, NOT bundled – see note)
esbuild 0.28.2
```

Note: C/C++/Java/C#/Kotlin/Rust use the legacy stream modes (`clike`, `rust`) instead of the
Lezer grammars from `@codemirror/lang-cpp|java|rust` – those grammars add ~220 KB, pushing the
bundle over the 900 KB budget. PowerShell, shell, YAML, TOML, Dockerfile, INI/properties and diff
are legacy modes too; `.bat`/`.cmd` use a tiny custom stream mode defined in `cm-entry.js`
(no upstream legacy mode exists).

## Rebuild

```bash
mkdir -p /tmp/cmbuild && cd /tmp/cmbuild && npm init -y
npm i codemirror@6.0.2 @codemirror/state@6.7.6 @codemirror/view@6.43.13 @codemirror/language@6.12.4 \
  @codemirror/commands@6.11.1 @codemirror/search@6.7.2 @codemirror/autocomplete@6.20.3 \
  @codemirror/theme-one-dark@6.1.3 @codemirror/lang-python@6.2.1 @codemirror/lang-javascript@6.2.5 \
  @codemirror/lang-html@6.4.12 @codemirror/lang-css@6.3.1 @codemirror/lang-json@6.0.2 \
  @codemirror/lang-markdown@6.5.2 @codemirror/lang-xml@6.1.0 @codemirror/lang-sql@6.10.0 \
  @codemirror/lang-go@6.0.1 @codemirror/legacy-modes@6.5.4 esbuild@0.28.2 \
  @xterm/xterm@6.0.0 @xterm/addon-fit@0.11.0 @xterm/addon-web-links@0.12.0
V=<repo>/app/static/vendor
cp $V/cm-entry.js .
npx esbuild cm-entry.js --bundle --minify --format=iife --target=es2020 --legal-comments=none \
  --outfile=$V/codemirror.min.js
cp node_modules/@xterm/xterm/lib/xterm.js node_modules/@xterm/xterm/css/xterm.css \
   node_modules/@xterm/addon-fit/lib/addon-fit.js node_modules/@xterm/addon-web-links/lib/addon-web-links.js $V/
sed -i 's#//\# sourceMappingURL=.*##' $V/xterm.js $V/addon-fit.js $V/addon-web-links.js
```

## `window.CM` API (see `cm-entry.js`)

`EditorState, EditorView, EditorSelection, Compartment, keymap, basicSetup() (returns an extension
array), oneDark, indentUnit, openSearchPanel, gotoLine, languageFor(filename) → {id, ext},
languages` (list of language ids).
