# IDE API contract (`/ide` page ↔ `app/ide_api.py`)

All paths are **relative to the workspace root**, use forward slashes, never start with `/`, never contain `..`.
Errors: HTTP 4xx/5xx with JSON `{"detail": "<中文说明>"}`. All JSON is UTF-8.
Security: served only on 127.0.0.1; the gateway middleware already rejects cross-site POSTs (Origin) and foreign Host headers for HTTP. **WebSockets are NOT covered by that middleware** → the terminal WS handler must itself call the injected `check_security(method="GET", client_host, host_header, origin, token)` and close with code 4403 if it returns a reason. It must additionally require an Origin header on WS that is local (localhost / 127.0.0.1 / [::1]) — browsers always send Origin on WS.

## Workspace
- `GET /api/ide/workspace` → `{root, name, is_git, branch, terminal_mode: "pty"|"pipe", shell}`
- `POST /api/ide/workspace {root}` → same shape. `root` is an absolute dir that must pass `is_allowed_path`. Persisted in `app/storage/ide_settings.json`. Default root = project root.

## Files
- `GET /api/ide/tree?path=<rel or "">` → `{path, entries:[{name, path, type:"dir"|"file", size, ignored:bool}]}`; dirs first then files, case-insensitive sort; `.git` hidden; `node_modules __pycache__ .venv venv .pytest_cache dist build` returned with `ignored:true` (UI shows dimmed, still expandable). Max 2000 entries (then `truncated:true`).
- `GET /api/ide/files?q=<text>&limit=50` → `{files:[rel paths]}` fuzzy/substring match on path for Quick Open (Ctrl+P); walks workspace skipping ignored dirs; stops after scanning 20000 files.
- `GET /api/ide/file?path=` → `{path, content, size, mtime, binary:false, too_large:false, eol:"\n"|"\r\n"}`. If binary (NUL in first 8KB) or > 2MB → `content:null` and the flag true. Content decoded utf-8 (fallback gb18030, latin-1); `eol` detected; content returned with original EOLs.
- `PUT /api/ide/file {path, content, expected_mtime?: number}` → `{path, size, mtime}`. If `expected_mtime` given and file mtime differs by > 1ms → **409** `{"detail": "文件已在磁盘上被修改"}`. Creates parent dirs. Write atomically (tmp + os.replace). Never writes inside `.git/`.
- `POST /api/ide/fs {op, path, new_path?}` ops: `create_file`, `create_dir`, `rename` (needs new_path), `delete` (moves into `app/storage/trash/<timestamp>/<path>` — never hard delete) → `{ok:true, path}`. 409 if target exists for create/rename.
- `GET /api/ide/search?q=&regex=0&case=0&glob=` → `{results:[{path, line, col, text}], truncated:bool, files_scanned}`; max 500 matches, skip binary / >1MB / ignored dirs; `text` trimmed to 300 chars; `glob` like `*.py` optional.

## Git (git CLI via subprocess in workspace root, `GIT_TERMINAL_PROMPT=0`, `-c core.quotepath=false`)
- `GET /api/ide/git/status` → `{is_repo, branch, upstream, ahead, behind, files:[{path, index, worktree, orig_path}]}` (index/worktree are single chars from porcelain: `M A D R C U ?` or `" "`). Not a repo → `{is_repo:false, files:[]}`.
- `GET /api/ide/git/diff?path=&staged=0|1` → `{diff}` unified text (untracked file → diff against /dev/null).
- `GET /api/ide/git/file_at?path=&ref=HEAD` → `{content}` (404 if not in ref).
- `POST /api/ide/git/stage {paths:[...]}` / `POST /api/ide/git/unstage {paths}` → `{ok:true}`; empty `paths` = all.
- `POST /api/ide/git/discard {paths}` → tracked: `git restore --worktree --staged?` (restore worktree only); untracked: moved to trash. → `{ok:true}`
- `POST /api/ide/git/commit {message, amend:false}` → `{ok:true, hash, short}`; empty message → 400.
- `GET /api/ide/git/log?limit=50&path=` → `{commits:[{hash, short, author, date(ISO), subject}]}`
- `GET /api/ide/git/show?hash=` → `{hash, subject, author, date, files:[{path,status}], diff}` (diff capped at 400KB, `truncated`).
- `POST /api/ide/git/revert {hash}` → `git revert --no-edit` → `{ok, output}`
- `POST /api/ide/git/restore_file {path, ref}` → `git checkout <ref> -- <path>` → `{ok:true}`
- `GET /api/ide/git/branches` → `{current, branches:[{name, current, upstream}], remotes:[{name,url}]}`
- `POST /api/ide/git/branch {name, checkout:true}` → create (+switch); invalid name → 400.
- `POST /api/ide/git/checkout {branch}` → switch; dirty tree conflicts → 400 with git stderr.
- `POST /api/ide/git/push` / `POST /api/ide/git/pull` → `{ok, output}`; no remote → 400 `未配置远程仓库`; timeout 120s. push sets upstream automatically if missing (`-u origin <branch>`).
- `POST /api/ide/git/init` → `{ok:true}`.
- Hash/ref/branch params validated with `^[A-Za-z0-9._/\-~^]{1,100}$` and must not start with `-`.

## Terminal (WebSocket) — VS Code-like integrated terminal
- `GET /api/ide/term/profiles` → `{default, profiles:[{id, name, path, args, icon}], mode:"pty"|"pipe", max_terminals}` (like VS Code `terminal.integrated.profiles.*`).
  - Windows detection: PowerShell 7 (`pwsh.exe` on PATH or `%ProgramFiles%\PowerShell\7\pwsh.exe`) → `pwsh`; Windows PowerShell (`%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`) → `powershell`; Command Prompt (`%ComSpec%`, args `/K "chcp 65001>nul"` so UTF-8 output isn't garbled) → `cmd`; Git Bash (`%ProgramFiles%\Git\bin\bash.exe`, `%LOCALAPPDATA%\Programs\Git\bin\bash.exe`, or derived from `git.exe` as `..\bin\bash.exe` / `..\..\bin\bash.exe`; args `--login -i`; env `CHERE_INVOKING=1` so it stays in cwd) → `gitbash`; WSL (`System32\wsl.exe`) → `wsl`. Default = `pwsh` if found else `powershell` (VS Code default).
  - POSIX: `bash` / `zsh` / `sh` from `$SHELL`, `/bin`, `/usr/bin`, `/usr/local/bin`; default = basename of `$SHELL`, else bash.
  - Env `TV_IDE_SHELL` (command line) adds profile `custom` at the front and makes it the default.
  - `icon` is a codicon-like name: `terminal-powershell|terminal-cmd|terminal-bash|terminal-git-bash|terminal-linux|terminal`.
- `WS /api/ide/term?cols=120&rows=30&cwd=<rel>&profile=<id>` (`profile` omitted → default profile; unknown id → close **4400**).
- server → client JSON:
  - `{"type":"info","mode":"pty"|"pipe","shell":"pwsh.exe","pid":123,"profile":"pwsh","name":"pwsh","cwd":"<rel>","max_terminals":3, "backend"?:"conpty"|"default", "hint"?:"..."}` — `hint` is present in pipe mode only: `未检测到 pywinpty：在终端运行 pip install pywinpty 后，新开一个终端即可 (无需重启服务)` on Windows without pywinpty; other texts when forced by `TV_IDE_TERM_MODE=pipe` or when the PTY spawn failed (error appended). `backend` is set for pywinpty sessions.
  - `{"type":"output","data":"..."}` (batched every ~16ms), `{"type":"exit","code":0}`
- client → server JSON: `{"type":"input","data":"..."}`, `{"type":"resize","cols":N,"rows":N}`
- PTY: Windows via `pywinpty`. `import winpty` is re-attempted on **every** spawn (and `/workspace`, `/term/profiles`) after `importlib.invalidate_caches()`, purging a previously failed/partial `winpty*` import and adding a newly created user site dir → `pip install pywinpty` takes effect for the next new terminal without restarting the server. `PtyProcess.spawn(argv, cwd, env, dimensions=(rows, cols), backend=Backend.ConPTY)` is tried first (pywinpty ≥ 2), then the default backend; all guarded. POSIX `pty` + `subprocess` (tests). No PTY → "pipe" mode (subprocess pipes, stderr merged, resize ignored; PowerShell gets `-Command -`, POSIX shells `-i`; cmd/Git Bash/WSL pipes decoded as UTF-8, PowerShell as the OEM code page).
- argv: PowerShell `-NoLogo` only in PTY mode (no `-Command`, so the PSReadLine prompt `PS C:\...>` works).
- Child env (like VS Code): `TERM=xterm-256color`, `COLORTERM=truecolor`, `TERM_PROGRAM=vscode`, `TERM_PROGRAM_VERSION`; POSIX also `LANG=en_US.UTF-8` unless `LC_ALL`/`LANG` already is UTF-8. cwd defaults to the workspace root.
- Max `MAX_TERMINALS` concurrent sessions (default 3, env `TV_IDE_MAX_TERMINALS`, 1–20; one more → close 4429). Process tree killed when WS closes. Output buffer per session capped (drop oldest) to bound RAM.
- `GET /api/ide/workspace` additionally returns `default_profile`; `shell` is the default profile's executable.

## Implementation notes (app/ide_api.py) — clarifications, all additive
- Wiring: `app.include_router(create_ide_router(project_root=PROJECT_ROOT, storage_dir=STORAGE_DIR, is_allowed_path=_is_allowed_path, check_security=check_request_security))`. HTTP routes also run `check_security` themselves (defence in depth).
- Path errors: malformed (absolute, `..`, drive letter, NUL, Windows ADS `a:b`, trailing dot/space, device names like `nul`/`con.txt`) → 400; resolves outside root (symlink/junction) or write inside `.git/` → 403. Walkers never follow symlinks/junctions and skip `<storage>/trash`.
- `GET /file` adds `encoding` (`utf-8`|`utf-8-sig`|`gb18030`|`latin-1`; BOM stripped from `content`). `PUT /file` accepts optional `encoding`; if omitted the file's existing encoding is kept (GBK files stay GBK). If `expected_mtime` is given and the file no longer exists → 409 too.
- `POST /fs delete` also returns `trash` (absolute trash path). Deleting a dir that contains the trash → 400.
- `GET /search`: one result per matching line; `line` and `col` are 1-based (col counted in characters). Also stops after 20000 files / 20s (`truncated:true`).
- Git: `status` adds `detached`, `truncated` (>5000 files); `diff`/`show` add `truncated` (diff cap 1MB / 400KB); `file_at` adds `binary`/`too_large` (content null, cap 2MB); `show.files[]` has `orig_path` for R/C. A workspace that is a subdirectory of a repo works (paths are workspace-relative, outside files omitted).
- `push`/`pull`/`revert`: git failure (rejected push, conflict, …) → **400** with git's combined output as `detail`; success → `{ok:true, output}`. Timeout → 504.
- `discard`/`stage`/`unstage` with empty `paths` = everything in the workspace. `ref` empty → `HEAD`.
- Terminal WS: token via `?token=` (or `Authorization: Bearer` / `X-TV-Token`). Other close codes: 4400 bad `cwd` or unknown `profile`, 1011 spawn failure (after an `output` message with the error), 1000 after `exit`. Env `TV_IDE_TERM_MODE=pipe` forces pipe mode. Pipe mode does local echo / backspace / Enter line editing itself (no PTY to do it).

## RAM budget
- Backend: no file content caching; git/search are streaming/limited; ≤`MAX_TERMINALS` (default 3) shells.
- Frontend: one CodeMirror EditorView reused across tabs (EditorState per tab), ≤12 tabs (LRU close of non-dirty), xterm scrollback 2000, lazy tree.
