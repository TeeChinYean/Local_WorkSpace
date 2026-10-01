/* =============================================================================
 * /ide — lightweight web IDE front-end (vanilla JS, no framework).
 *
 * RAM/GPU budget (4GB-VRAM laptop): ONE CodeMirror EditorView reused across
 * tabs (an EditorState per tab), max 12 tabs (LRU close of clean tabs), lazy
 * file tree, xterm scrollback 2000 with the DOM renderer, streaming AI answers
 * rendered at most once per animation frame, no tight polling.
 *
 * Security: every user/server string is inserted with textContent or passed
 * through escapeHtml() first. No raw HTML from the server is ever injected.
 * API contract: docs/IDE_API.md
 * ========================================================================== */
(function () {
  'use strict';

  const CM = window.CM;
  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- helpers
  function h(tag, props) {
    const el = document.createElement(tag);
    if (props) {
      for (const k of Object.keys(props)) {
        const v = props[k];
        if (v == null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k === 'dataset') Object.assign(el.dataset, v);
        else if (k === 'style') el.style.cssText = v;
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? '' : String(v));
      }
    }
    for (let i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach((x) => append(el, x)); return; }
    el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  const qs = (params) => new URLSearchParams(params).toString();
  const baseName = (p) => String(p).split('/').pop();
  const dirName = (p) => { const i = String(p).lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i); };
  const joinPath = (d, n) => (d ? d + '/' + n : n);
  function normRel(p) {
    const parts = String(p).replace(/\\/g, '/').split('/').filter((x) => x && x !== '.');
    if (!parts.length || parts.some((x) => x === '..')) return null;
    return parts.join('/');
  }
  function debounce(fn, ms) {
    let t = 0;
    return function () { const a = arguments; clearTimeout(t); t = setTimeout(() => fn.apply(null, a), ms); };
  }
  function lsGet(k, d) {
    try { const v = localStorage.getItem('ide.' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; }
  }
  function lsSet(k, v) {
    try { localStorage.setItem('ide.' + k, JSON.stringify(v)); } catch (e) { /* storage blocked */ }
  }
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) {
      const ta = h('textarea', { style: 'position:fixed;left:-9999px;top:0' });
      ta.value = text; document.body.appendChild(ta); ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
      ta.remove();
      return ok;
    }
  }

  async function api(method, url, body) {
    const init = { method, headers: {} };
    if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(url, init); } catch (e) {
      const err = new Error('无法连接到本地服务'); err.status = 0; throw err;
    }
    const text = await res.text();
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch (e) { data = { detail: text.slice(0, 300) }; } }
    if (!res.ok) {
      let d = data && data.detail;
      if (Array.isArray(d)) d = d.map((x) => (x && x.msg) || JSON.stringify(x)).join('; ');
      else if (d && typeof d === 'object') d = JSON.stringify(d);
      const err = new Error(d || ('HTTP ' + res.status)); err.status = res.status; err.data = data;
      throw err;
    }
    return data || {};
  }

  // ---------------------------------------------------------------- toasts
  function toast(msg, type, output) {
    const box = $('toasts');
    const t = h('div', { class: 'toast ' + (type || 'info'), role: type === 'error' ? 'alert' : 'status' }, h('div', { text: String(msg) }));
    if (output) t.appendChild(h('pre', { text: String(output).slice(0, 6000) }));
    const close = () => t.remove();
    t.appendChild(h('button', { class: 'icon-btn x', type: 'button', 'aria-label': '关闭通知', onclick: close }, '✕'));
    box.appendChild(t);
    while (box.children.length > 4) box.firstChild.remove();
    setTimeout(() => { t.classList.add('fade'); setTimeout(close, 200); }, (type === 'error' || output) ? 8000 : 3000);
  }
  const toastErr = (e, prefix) => toast((prefix ? prefix + '：' : '') + ((e && e.message) || e), 'error');

  // ---------------------------------------------------------------- modal
  let modalFinish = null;
  function modal(opts) {
    return new Promise((resolve) => {
      if (modalFinish) modalFinish(null);
      const m = $('modal'), inp = $('modal-input'), btns = $('modal-btns');
      $('modal-title').textContent = opts.title || '';
      $('modal-msg').textContent = opts.message || '';
      $('modal-msg').hidden = !opts.message;
      inp.hidden = !opts.input;
      if (opts.input) { inp.value = opts.input.value || ''; inp.placeholder = opts.input.placeholder || ''; }
      btns.textContent = '';
      const prevFocus = document.activeElement;
      const finish = (v) => {
        m.hidden = true; modalFinish = null;
        document.removeEventListener('keydown', onKey, true);
        m.removeEventListener('mousedown', onBg);
        if (prevFocus && prevFocus.focus && document.contains(prevFocus)) { try { prevFocus.focus(); } catch (e) { /* ignore */ } }
        resolve(v);
      };
      modalFinish = finish;
      const primary = opts.buttons.find((b) => b.primary) || opts.buttons[opts.buttons.length - 1];
      const onKey = (e) => {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(null); }
        else if (e.key === 'Enter' && opts.input && document.activeElement === inp) {
          e.preventDefault(); e.stopPropagation(); finish({ value: primary.value, input: inp.value });
        } else if (e.key === 'Tab') {
          const f = [...m.querySelectorAll('input:not([hidden]),button')];
          const i = f.indexOf(document.activeElement);
          e.preventDefault();
          f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
        }
      };
      const onBg = (e) => { if (e.target === m) finish(null); };
      for (const b of opts.buttons) {
        btns.appendChild(h('button', {
          type: 'button', class: 'btn' + (b.primary ? ' primary' : '') + (b.danger ? ' danger' : ''),
          onclick: () => finish({ value: b.value, input: inp.value }),
        }, b.label));
      }
      document.addEventListener('keydown', onKey, true);
      m.addEventListener('mousedown', onBg);
      m.hidden = false;
      if (opts.input) { inp.focus(); inp.select(); } else {
        const pb = [...btns.children].find((x) => x.classList.contains('primary')) || btns.lastChild;
        pb.focus();
      }
    });
  }
  async function confirmBox(title, message, okLabel, danger) {
    const r = await modal({ title, message, buttons: [{ label: '取消', value: false }, { label: okLabel || '确定', value: true, primary: !danger, danger: !!danger }] });
    return !!(r && r.value);
  }
  async function promptBox(title, message, value, placeholder) {
    const r = await modal({ title, message, input: { value: value || '', placeholder: placeholder || '' }, buttons: [{ label: '取消', value: false }, { label: '确定', value: true, primary: true }] });
    return r && r.value ? String(r.input).trim() : null;
  }

  // ---------------------------------------------------------------- context menu
  function showMenu(x, y, items) {
    const m = $('ctxmenu');
    m.textContent = '';
    for (const it of items) {
      if (!it) continue;
      if (it === '-') { m.appendChild(h('hr')); continue; }
      m.appendChild(h('button', { type: 'button', role: 'menuitem', onclick: () => { hideMenu(); it.run(); } }, it.label));
    }
    m.hidden = false;
    const r = m.getBoundingClientRect();
    m.style.left = Math.max(0, Math.min(x, innerWidth - r.width - 4)) + 'px';
    m.style.top = Math.max(0, Math.min(y, innerHeight - r.height - 4)) + 'px';
    const first = m.querySelector('button');
    if (first) first.focus();
  }
  function hideMenu() { $('ctxmenu').hidden = true; }
  $('ctxmenu').addEventListener('keydown', (e) => {
    const b = [...$('ctxmenu').querySelectorAll('button')];
    const i = b.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); b[(i + 1) % b.length].focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); b[(i - 1 + b.length) % b.length].focus(); }
    else if (e.key === 'Escape' || e.key === 'Tab') { e.preventDefault(); hideMenu(); }
  });
  document.addEventListener('mousedown', (e) => { if (!$('ctxmenu').hidden && !$('ctxmenu').contains(e.target)) hideMenu(); });

  // ---------------------------------------------------------------- file icons / git letters
  const ICONS = {
    py: '🐍', pyw: '🐍', js: '📜', mjs: '📜', cjs: '📜', jsx: '📜', ts: '📘', tsx: '📘', json: '🧾', md: '📝',
    html: '🌐', htm: '🌐', css: '🎨', scss: '🎨', less: '🎨', ps1: '⚡', psm1: '⚡', bat: '⚙️', cmd: '⚙️', sh: '💲',
    yml: '🔧', yaml: '🔧', toml: '🔧', ini: '🔧', cfg: '🔧', env: '🔧', txt: '📄', log: '📃', png: '🖼️', jpg: '🖼️',
    jpeg: '🖼️', gif: '🖼️', svg: '🖼️', ico: '🖼️', webp: '🖼️', pdf: '📕', zip: '📦', gz: '📦', '7z': '📦', exe: '⚙️',
    dll: '⚙️', gguf: '🧠', bin: '🧠', safetensors: '🧠', db: '🗄️', sqlite: '🗄️', sql: '🗄️', rs: '🦀', go: '🐹',
    java: '☕', kt: '☕', c: '🔷', h: '🔷', cpp: '🔷', hpp: '🔷', cs: '🔷', lock: '🔒', xml: '📰', csv: '📊',
    xlsx: '📊', docx: '📃', tv: '🧠', npy: '🧠',
  };
  function fileIcon(name) {
    const n = String(name).toLowerCase();
    if (n === 'dockerfile') return '🐳';
    if (n.startsWith('.git')) return '🔧';
    const i = n.lastIndexOf('.');
    return (i >= 0 && ICONS[n.slice(i + 1)]) || '📄';
  }
  function gitLetter(f) {
    const i = f.index || ' ', w = f.worktree || ' ';
    if (i === '?' || w === '?') return 'U';
    if (i === 'U' || w === 'U' || (i === 'A' && w === 'A') || (i === 'D' && w === 'D')) return '!';
    return w !== ' ' ? w : i;
  }
  const gitClass = (l) => (l === '!' ? 'g-X' : 'g-' + l);
  const GIT_TITLES = { M: '已修改', A: '已添加', D: '已删除', U: '未跟踪', R: '已重命名', C: '已复制', '!': '冲突' };

  // ---------------------------------------------------------------- state
  const S = {
    ws: null,
    git: { is_repo: false, files: [] },
    gitMap: new Map(),
    gitDirs: new Set(),
    view: 'explorer',
    sidebarOpen: true,
  };

  // ---------------------------------------------------------------- layout / resizers
  const rootStyle = document.documentElement.style;
  const LAYOUT = { sidebar: lsGet('sidebarW', 260), ai: lsGet('aiW', 360), panel: lsGet('panelH', 240) };
  function applyLayout() {
    rootStyle.setProperty('--sidebar-w', LAYOUT.sidebar + 'px');
    rootStyle.setProperty('--ai-w', LAYOUT.ai + 'px');
    rootStyle.setProperty('--panel-h', LAYOUT.panel + 'px');
  }
  function makeResizer(el, key, lsKey, axis, sign, min, maxFn) {
    const clamp = (v) => Math.round(Math.max(min, Math.min(maxFn(), v)));
    el.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      el.classList.add('dragging'); document.body.classList.add('resizing');
      const start = axis === 'x' ? e.clientX : e.clientY;
      const startV = LAYOUT[key];
      let raf = 0, last = startV;
      const move = (ev) => {
        const cur = axis === 'x' ? ev.clientX : ev.clientY;
        last = clamp(startV + sign * (cur - start));
        if (!raf) raf = requestAnimationFrame(() => { raf = 0; LAYOUT[key] = last; applyLayout(); });
      };
      const up = () => {
        el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); el.removeEventListener('pointercancel', up);
        el.classList.remove('dragging'); document.body.classList.remove('resizing');
        LAYOUT[key] = last; applyLayout(); lsSet(lsKey, last);
      };
      el.addEventListener('pointermove', move); el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up);
    });
    el.addEventListener('keydown', (e) => {
      const step = e.shiftKey ? 48 : 16;
      let d = 0;
      if (axis === 'x' && e.key === 'ArrowLeft') d = -step;
      if (axis === 'x' && e.key === 'ArrowRight') d = step;
      if (axis === 'y' && e.key === 'ArrowUp') d = -step;
      if (axis === 'y' && e.key === 'ArrowDown') d = step;
      if (!d) return;
      e.preventDefault();
      LAYOUT[key] = clamp(LAYOUT[key] + sign * d); applyLayout(); lsSet(lsKey, LAYOUT[key]);
    });
  }

  function showView(name, focus) {
    const already = S.sidebarOpen && S.view === name;
    if (already && !focus) { S.sidebarOpen = false; }
    else { S.sidebarOpen = true; S.view = name; }
    $('sidebar').classList.toggle('collapsed', !S.sidebarOpen);
    $('sidebar-resizer').hidden = !S.sidebarOpen;
    document.querySelectorAll('.act-btn[data-view]').forEach((b) => {
      const on = S.sidebarOpen && b.dataset.view === S.view;
      b.classList.toggle('active', on); b.setAttribute('aria-pressed', String(on));
    });
    document.querySelectorAll('.view').forEach((v) => { v.hidden = v.dataset.view !== S.view; });
    if (!S.sidebarOpen) return;
    if (S.view === 'search') { const i = $('search-q'); i.focus(); i.select(); }
    else if (S.view === 'scm') { renderScm(); loadBranches(); if ($('scm-history').open) loadLog(); if (focus) $('scm-msg').focus(); }
    else if (S.view === 'explorer' && focus) { focusTreeRow(); }
  }

  // ---------------------------------------------------------------- workspace
  function applyWs() {
    const w = S.ws || {};
    $('ws-name').textContent = w.name || '(未命名)';
    $('ws-name').title = w.root || '';
    document.title = (w.name ? w.name + ' — ' : '') + '本地 IDE';
  }
  async function switchWorkspace() {
    const p = await promptBox('切换工作区', '输入工作区文件夹的绝对路径：', S.ws ? S.ws.root : '', 'C:\\Projects\\my-app');
    if (!p) return;
    if (tabs.some((t) => t.dirty) && !(await confirmBox('存在未保存的文件', '切换工作区会关闭所有标签页，未保存的更改将丢失。是否继续？', '继续', true))) return;
    try {
      const w = await api('POST', '/api/ide/workspace', { root: p });
      for (const t of tabs.slice()) await closeTab(t.id, true);
      S.ws = w; applyWs();
      tree.children.clear(); tree.expanded = new Set(['']); tree.selected = null;
      await refreshTree();
      refreshGit(); loadBranches();
      if ($('scm-history').open) loadLog();
      toast('已切换到工作区：' + (w.name || w.root), 'ok');
    } catch (e) { toastErr(e, '切换失败'); }
  }

  // ================================================================= Explorer
  const tree = { children: new Map(), expanded: new Set(['']), selected: null };

  async function loadDir(path) {
    const d = await api('GET', '/api/ide/tree?' + qs({ path }));
    tree.children.set(path, d);
    return d;
  }
  async function refreshTree() {
    const paths = [...tree.expanded].filter((p) => p === '' || tree.children.has(p));
    await Promise.all(paths.map((p) => loadDir(p).catch((e) => {
      tree.children.delete(p); if (p) tree.expanded.delete(p);
      if (p === '') toastErr(e, '读取目录失败');
    })));
    // drop cached children of dirs that are no longer expanded (keeps memory low)
    for (const k of [...tree.children.keys()]) if (!tree.expanded.has(k)) tree.children.delete(k);
    renderTree();
  }
  async function toggleDir(path, force) {
    const open = force != null ? force : !tree.expanded.has(path);
    if (!open) {
      tree.expanded.delete(path);
      for (const k of [...tree.expanded]) if (k.startsWith(path + '/')) tree.expanded.delete(k);
      for (const k of [...tree.children.keys()]) if (k === path || k.startsWith(path + '/')) tree.children.delete(k);
      renderTree();
      return;
    }
    tree.expanded.add(path);
    if (!tree.children.has(path)) {
      renderTree();
      try { await loadDir(path); } catch (e) { tree.expanded.delete(path); toastErr(e, '读取目录失败'); }
    }
    renderTree();
  }
  function treeRow(e, depth) {
    const isDir = e.type === 'dir';
    const open = isDir && tree.expanded.has(e.path);
    const letter = S.gitMap.get(e.path);
    let cls = 'tree-row';
    if (e.ignored) cls += ' ignored';
    if (letter && !isDir) cls += ' ' + gitClass(letter);
    else if (isDir && S.gitDirs.has(e.path)) cls += ' gdir';
    if (tree.selected === e.path) cls += ' selected';
    return h('div', {
      class: cls, role: 'treeitem', tabindex: '-1', 'aria-level': depth + 1,
      'aria-expanded': isDir ? String(open) : null, 'aria-selected': String(tree.selected === e.path),
      dataset: { path: e.path, type: e.type }, style: 'padding-left:' + (depth * 12 + 4) + 'px',
      title: e.path + (letter ? ' • ' + (GIT_TITLES[letter] || letter) : ''),
    },
    h('span', { class: 'twisty', 'aria-hidden': 'true' }, isDir ? (open ? '▾' : '▸') : ''),
    h('span', { class: 'ficon', 'aria-hidden': 'true' }, isDir ? (open ? '📂' : '📁') : fileIcon(e.name)),
    h('span', { class: 'fname' }, e.name),
    letter && !isDir ? h('span', { class: 'gstat ' + gitClass(letter) }, letter) : null);
  }
  function renderTree() {
    const root = $('tree');
    const act = document.activeElement;
    const focusPath = root.contains(act) && act.dataset ? act.dataset.path : null;
    const frag = document.createDocumentFragment();
    const note = (text, depth) => frag.appendChild(h('div', { class: 'tree-note', style: 'padding-left:' + (depth * 12 + 24) + 'px' }, text));
    const walk = (path, depth) => {
      const d = tree.children.get(path);
      if (!d) { note('加载中…', depth); return; }
      for (const e of d.entries || []) {
        frag.appendChild(treeRow(e, depth));
        if (e.type === 'dir' && tree.expanded.has(e.path)) walk(e.path, depth + 1);
      }
      if (d.truncated) note('（条目过多，仅显示前 ' + (d.entries || []).length + ' 项）', depth);
      if (path === '' && !(d.entries || []).length) note('空文件夹', 0);
    };
    walk('', 0);
    root.textContent = '';
    root.appendChild(frag);
    const rows = root.querySelectorAll('.tree-row');
    let target = null;
    for (const r of rows) if (r.dataset.path === (focusPath || tree.selected)) { target = r; break; }
    (target || rows[0] || root).setAttribute('tabindex', '0');
    if (focusPath && target) target.focus({ preventScroll: true });
  }
  function rowByPath(path) {
    for (const r of $('tree').querySelectorAll('.tree-row')) if (r.dataset.path === path) return r;
    return null;
  }
  function focusTreeRow() {
    const r = $('tree').querySelector('.tree-row[tabindex="0"]') || $('tree').querySelector('.tree-row');
    if (r) r.focus();
  }
  function entryOf(row) { return row ? { path: row.dataset.path, type: row.dataset.type, name: baseName(row.dataset.path) } : null; }
  function selectRow(path) {
    tree.selected = path;
    for (const r of $('tree').querySelectorAll('.tree-row')) {
      const on = r.dataset.path === path;
      r.classList.toggle('selected', on); r.setAttribute('aria-selected', String(on));
      r.setAttribute('tabindex', on ? '0' : '-1');
    }
  }
  function activateRow(row, focusEditor) {
    const e = entryOf(row);
    if (!e) return;
    selectRow(e.path);
    if (e.type === 'dir') toggleDir(e.path);
    else openFile(e.path, { focus: focusEditor });
  }
  function treeMenu(e, x, y) {
    const dir = e ? (e.type === 'dir' ? e.path : dirName(e.path)) : '';
    showMenu(x, y, [
      { label: '新建文件', run: () => fsCreate('file', dir) },
      { label: '新建文件夹', run: () => fsCreate('dir', dir) },
      e && '-',
      e && { label: '重命名', run: () => fsRename(e) },
      e && { label: '删除', run: () => fsDelete(e) },
      '-',
      e && { label: '复制相对路径', run: () => copyText(e.path).then((ok) => toast(ok ? '已复制：' + e.path : '复制失败', ok ? 'ok' : 'error')) },
      e && e.type === 'file' && { label: '加入 AI 上下文', run: () => mountToAI(e.path) },
      { label: '在集成终端中打开', run: () => openInTerminal(dir) },
      { label: '刷新', run: () => { refreshTree(); refreshGit(); } },
    ]);
  }
  async function fsCreate(kind, dir) {
    const name = await promptBox(kind === 'file' ? '新建文件' : '新建文件夹', '位置：' + (dir ? dir + '/' : '（工作区根目录）'), '', kind === 'file' ? '例如 utils/helper.py' : '文件夹名');
    if (!name) return;
    const path = normRel(joinPath(dir, name));
    if (!path) { toast('名称无效', 'error'); return; }
    try {
      await api('POST', '/api/ide/fs', { op: kind === 'file' ? 'create_file' : 'create_dir', path });
      let p = dirName(path);
      while (p) { tree.expanded.add(p); p = dirName(p); }
      await refreshTree();
      selectRow(path);
      if (kind === 'file') openFile(path, { focus: true });
      refreshGit();
    } catch (e) { toastErr(e, '创建失败'); }
  }
  async function fsRename(e) {
    const name = await promptBox('重命名', e.path, e.name);
    if (!name || name === e.name) return;
    const np = normRel(joinPath(dirName(e.path), name));
    if (!np) { toast('名称无效', 'error'); return; }
    try {
      await api('POST', '/api/ide/fs', { op: 'rename', path: e.path, new_path: np });
      for (const t of tabs) {
        if (t.kind !== 'file') continue;
        if (t.path === e.path || t.path.startsWith(e.path + '/')) {
          t.path = np + t.path.slice(e.path.length); t.title = baseName(t.path);
        }
      }
      if (tree.expanded.has(e.path)) { tree.expanded.delete(e.path); tree.expanded.add(np); }
      await refreshTree(); selectRow(np);
      renderTabs(); renderBreadcrumb(); refreshGit();
    } catch (err) { toastErr(err, '重命名失败'); }
  }
  async function fsDelete(e) {
    if (!(await confirmBox('删除', '确定删除 “' + e.path + '” 吗？\n（将移入回收站 app/storage/trash）', '删除', true))) return;
    try {
      await api('POST', '/api/ide/fs', { op: 'delete', path: e.path });
      for (const t of tabs.slice()) {
        if (t.kind === 'file' && !t.dirty && (t.path === e.path || t.path.startsWith(e.path + '/'))) await closeTab(t.id, true);
      }
      await refreshTree(); refreshGit();
    } catch (err) { toastErr(err, '删除失败'); }
  }

  const treeEl = $('tree');
  treeEl.addEventListener('click', (ev) => {
    const row = ev.target.closest('.tree-row');
    if (row) activateRow(row, false);
  });
  treeEl.addEventListener('contextmenu', (ev) => {
    ev.preventDefault();
    const row = ev.target.closest('.tree-row');
    if (row) selectRow(row.dataset.path);
    treeMenu(entryOf(row), ev.clientX, ev.clientY);
  });
  treeEl.addEventListener('keydown', (ev) => {
    const rows = [...treeEl.querySelectorAll('.tree-row')];
    const cur = document.activeElement && document.activeElement.closest ? document.activeElement.closest('.tree-row') : null;
    const i = rows.indexOf(cur);
    const go = (r) => { if (r) { selectRow(r.dataset.path); r.focus(); } };
    const e = entryOf(cur);
    switch (ev.key) {
      case 'ArrowDown': go(rows[Math.min(i + 1, rows.length - 1)] || rows[0]); break;
      case 'ArrowUp': go(rows[Math.max(i - 1, 0)]); break;
      case 'Home': go(rows[0]); break;
      case 'End': go(rows[rows.length - 1]); break;
      case 'ArrowRight':
        if (e && e.type === 'dir') { if (!tree.expanded.has(e.path)) toggleDir(e.path, true); else go(rows[i + 1]); }
        break;
      case 'ArrowLeft':
        if (e && e.type === 'dir' && tree.expanded.has(e.path)) toggleDir(e.path, false);
        else if (e) go(rowByPath(dirName(e.path)));
        break;
      case 'Enter': case ' ': if (cur) activateRow(cur, ev.key === 'Enter'); break;
      case 'F2': if (e) fsRename(e); break;
      case 'Delete': if (e) fsDelete(e); break;
      case 'ContextMenu': {
        const r = cur ? cur.getBoundingClientRect() : treeEl.getBoundingClientRect();
        treeMenu(e, r.left + 24, r.bottom); break;
      }
      default:
        if (ev.key === 'F10' && ev.shiftKey) {
          const r = cur ? cur.getBoundingClientRect() : treeEl.getBoundingClientRect();
          treeMenu(e, r.left + 24, r.bottom); break;
        }
        return;
    }
    ev.preventDefault();
  });

  // ================================================================= Editor & tabs
  const MAX_TABS = 12;
  const tabs = [];
  let activeId = null, tabSeq = 0, view = null, viewTabId = null, baseExt = null;
  const LANG_NAMES = {
    python: 'Python', javascript: 'JavaScript', jsx: 'JavaScript JSX', typescript: 'TypeScript', tsx: 'TypeScript JSX',
    html: 'HTML', css: 'CSS', json: 'JSON', markdown: 'Markdown', xml: 'XML', sql: 'SQL', cpp: 'C/C++', java: 'Java',
    csharp: 'C#', kotlin: 'Kotlin', rust: 'Rust', go: 'Go', powershell: 'PowerShell', shell: 'Shell', yaml: 'YAML',
    toml: 'TOML', dockerfile: 'Dockerfile', ini: 'INI', diff: 'Diff', bat: 'Batch', plaintext: '纯文本',
  };

  const activeTab = () => tabs.find((t) => t.id === activeId) || null;
  function baseExtensions() {
    if (baseExt) return baseExt;
    baseExt = [
      CM.basicSetup(),
      CM.oneDark,
      CM.EditorState.tabSize.of(4),
      CM.indentUnit.of('    '),
      CM.EditorView.updateListener.of(onEditorUpdate),
    ];
    return baseExt;
  }
  function ensureView() {
    if (!view) view = new CM.EditorView({ parent: $('editor-host') });
    return view;
  }
  function onEditorUpdate(u) {
    const t = tabs.find((x) => x.id === viewTabId);
    if (!t) return;
    if (u.docChanged) {
      const d = !u.state.doc.eq(t.savedDoc);
      if (d !== t.dirty) { t.dirty = d; renderTabs(); }
    }
    if (u.docChanged || u.selectionSet) schedulePos();
  }
  let posRaf = 0;
  function schedulePos() {
    if (posRaf) return;
    posRaf = requestAnimationFrame(() => { posRaf = 0; updateStatusEditor(); });
  }

  function fillState(t, content) {
    const lang = CM.languageFor(t.path);
    t.lang = lang.id;
    t.state = CM.EditorState.create({ doc: content, extensions: [baseExtensions(), lang.ext] });
    t.savedDoc = t.state.doc;
    t.dirty = false;
    t.scroll = 0;
  }
  function makeFileTab(path, data) {
    const t = { id: ++tabSeq, kind: 'file', path, title: baseName(path), dirty: false, lastUsed: Date.now(), mtime: data.mtime, eol: data.eol || '\n', state: null };
    if (data.binary || data.too_large || data.content == null) {
      t.placeholder = data.binary ? 'binary' : 'too_large'; t.size = data.size; t.lang = 'plaintext';
    } else fillState(t, data.content);
    return t;
  }
  function addTab(t) {
    if (tabs.length >= MAX_TABS) {
      const cand = tabs.filter((x) => !x.dirty && x.id !== activeId).sort((a, b) => a.lastUsed - b.lastUsed)[0];
      if (cand) closeTab(cand.id, true);
      else toast('已打开 ' + tabs.length + ' 个有未保存修改的标签页，建议先保存', 'error');
    }
    const idx = tabs.findIndex((x) => x.id === activeId);
    tabs.splice(idx >= 0 ? idx + 1 : tabs.length, 0, t);
  }
  function stashView() {
    const t = tabs.find((x) => x.id === viewTabId);
    if (t && view && t.state) { t.state = view.state; t.scroll = view.scrollDOM.scrollTop; }
  }
  function releaseView() {
    if (view) view.setState(CM.EditorState.create({ doc: '' }));
    viewTabId = null;
  }

  async function openFile(path, opts) {
    opts = opts || {};
    let t = tabs.find((x) => x.kind === 'file' && x.path === path);
    if (!t) {
      let data;
      try { data = await api('GET', '/api/ide/file?' + qs({ path })); } catch (e) { toastErr(e, '打开失败'); return null; }
      t = tabs.find((x) => x.kind === 'file' && x.path === path);
      if (!t) { t = makeFileTab(path, data); addTab(t); }
    }
    activate(t.id);
    if (t.state && opts.line) gotoLineCol(opts.line, opts.col || 1);
    else if (t.state && opts.focus && view) view.focus();
    return t;
  }
  function gotoLineCol(line, col) {
    if (!view) return;
    const doc = view.state.doc;
    const ln = doc.line(Math.max(1, Math.min(line, doc.lines)));
    const pos = Math.min(ln.from + Math.max(0, (col || 1) - 1), ln.to);
    view.dispatch({ selection: { anchor: pos }, effects: CM.EditorView.scrollIntoView(pos, { y: 'center' }) });
    view.focus();
  }
  function activate(id) {
    const t = tabs.find((x) => x.id === id);
    if (!t) return;
    activeId = id;
    t.lastUsed = Date.now();
    showTabContent(t);
    renderTabs(); renderBreadcrumb(); updateStatusEditor();
  }
  function showTabContent(t) {
    const ed = $('editor-host'), df = $('diff-host'), ph = $('placeholder-host'), wl = $('welcome');
    ed.hidden = true; ph.hidden = true; wl.hidden = true;
    if (!t || t.kind === 'file') { df.hidden = true; df.textContent = ''; }
    if (!t) { wl.hidden = false; return; }
    if (t.kind === 'file' && t.state) {
      ed.hidden = false;
      ensureView();
      if (viewTabId !== t.id) {
        stashView();
        view.setState(t.state);
        viewTabId = t.id;
        const sc = t.scroll || 0;
        requestAnimationFrame(() => { if (view && viewTabId === t.id) view.scrollDOM.scrollTop = sc; });
      }
      view.requestMeasure();
    } else if (t.kind === 'file') {
      ph.hidden = false; ph.textContent = '';
      ph.appendChild(h('div', null,
        h('div', { style: 'font-size:32px' }, t.placeholder === 'binary' ? '🧱' : '📦'),
        h('p', null, t.placeholder === 'binary' ? '该文件是二进制文件，无法在编辑器中显示。' : '文件过大（> 2MB），为节省内存不在编辑器中打开。'),
        h('p', { class: 'muted small' }, t.path + (t.size != null ? '  ·  ' + fmtSize(t.size) : ''))));
    } else {
      df.hidden = false;
      renderDiffTab(t, df);
    }
  }
  function fmtSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }
  async function closeTab(id, force) {
    const t = tabs.find((x) => x.id === id);
    if (!t) return false;
    if (!force && t.dirty) {
      activate(id);
      const r = await modal({
        title: '未保存的更改', message: '是否保存对 “' + t.path + '” 的更改？\n不保存将丢失这些更改。',
        buttons: [{ label: '取消', value: 'cancel' }, { label: '不保存', value: 'discard', danger: true }, { label: '保存', value: 'save', primary: true }],
      });
      if (!r || r.value === 'cancel') return false;
      if (r.value === 'save' && !(await saveTab(t))) return false;
    }
    const i = tabs.indexOf(t);
    if (i < 0) return false;
    tabs.splice(i, 1);
    if (viewTabId === id) releaseView();
    t.state = null; t.savedDoc = null; t.diffText = null; t.commit = null;
    if (activeId === id) {
      activeId = null;
      const next = tabs[Math.min(i, tabs.length - 1)];
      if (next) activate(next.id); else showTabContent(null);
    }
    renderTabs(); renderBreadcrumb(); updateStatusEditor();
    return true;
  }
  function renderTabs() {
    const bar = $('tabs');
    bar.textContent = '';
    for (const t of tabs) {
      const on = t.id === activeId;
      bar.appendChild(h('div', {
        class: 'tab' + (on ? ' active' : '') + (t.dirty ? ' dirty' : '') + (t.kind !== 'file' ? ' diff' : ''),
        role: 'tab', tabindex: on ? '0' : '-1', 'aria-selected': String(on), title: (t.path || t.title) + (t.dirty ? '（未保存）' : ''),
        dataset: { id: String(t.id) },
      },
      h('span', { class: 'ficon', 'aria-hidden': 'true' }, t.kind === 'file' ? fileIcon(t.title) : (t.kind === 'commit' ? '⎇' : '±')),
      h('span', { class: 'tname' }, t.title),
      h('button', { class: 'tab-close', type: 'button', tabindex: '-1', 'aria-label': '关闭 ' + t.title }, h('span', { 'aria-hidden': 'true' }, '✕'))));
    }
    const a = bar.querySelector('.tab.active');
    if (a && a.scrollIntoView) a.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  function renderBreadcrumb() {
    const bc = $('breadcrumb');
    bc.textContent = '';
    const t = activeTab();
    if (!t) { bc.hidden = true; return; }
    bc.hidden = false;
    if (t.kind === 'file') {
      const parts = t.path.split('/');
      parts.forEach((p, i) => {
        if (i) bc.appendChild(h('span', { class: 'sep', 'aria-hidden': 'true' }, '›'));
        bc.appendChild(h('span', { class: i === parts.length - 1 ? 'last' : '' }, p));
      });
    } else bc.appendChild(h('span', { class: 'last' }, t.title));
  }
  const tabsEl = $('tabs');
  tabsEl.addEventListener('click', (ev) => {
    const tabEl = ev.target.closest('.tab');
    if (!tabEl) return;
    const id = Number(tabEl.dataset.id);
    if (ev.target.closest('.tab-close')) closeTab(id); else activate(id);
  });
  tabsEl.addEventListener('mousedown', (ev) => { if (ev.button === 1 && ev.target.closest('.tab')) ev.preventDefault(); });
  tabsEl.addEventListener('auxclick', (ev) => {
    const tabEl = ev.target.closest('.tab');
    if (ev.button === 1 && tabEl) { ev.preventDefault(); closeTab(Number(tabEl.dataset.id)); }
  });
  tabsEl.addEventListener('keydown', (ev) => {
    const tabEl = ev.target.closest('.tab');
    if (!tabEl) return;
    const all = [...tabsEl.querySelectorAll('.tab')];
    const i = all.indexOf(tabEl);
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
      const n = all[(i + (ev.key === 'ArrowRight' ? 1 : -1) + all.length) % all.length];
      activate(Number(n.dataset.id));
      const na = tabsEl.querySelector('.tab.active'); if (na) na.focus();
      ev.preventDefault();
    } else if (ev.key === 'Delete') { closeTab(Number(tabEl.dataset.id)); ev.preventDefault(); }
    else if (ev.key === 'Enter') { const t = activeTab(); if (t && t.state && view) view.focus(); ev.preventDefault(); }
  });

  async function saveTab(t, force) {
    if (!t || t.kind !== 'file' || !t.state) return false;
    const st = (viewTabId === t.id && view) ? view.state : t.state;
    let text = st.doc.toString();
    if (t.eol === '\r\n') text = text.replace(/\n/g, '\r\n');
    const body = { path: t.path, content: text };
    if (!force && t.mtime != null) body.expected_mtime = t.mtime;
    try {
      const r = await api('PUT', '/api/ide/file', body);
      t.mtime = r.mtime;
      t.savedDoc = st.doc;
      const curDoc = (viewTabId === t.id && view) ? view.state.doc : t.state.doc;
      t.dirty = !curDoc.eq(t.savedDoc);
      renderTabs();
      refreshGit();
      return true;
    } catch (e) {
      if (e.status === 409) {
        const r = await modal({
          title: '保存冲突', message: e.message + '\n“' + t.path + '” 已在磁盘上被其他程序修改。\n覆盖：用编辑器中的内容写入磁盘；重新加载：放弃编辑器中的修改。',
          buttons: [{ label: '取消', value: 'cancel' }, { label: '重新加载', value: 'reload' }, { label: '覆盖', value: 'overwrite', danger: true }],
        });
        if (r && r.value === 'overwrite') return saveTab(t, true);
        if (r && r.value === 'reload') await reloadTab(t);
        return false;
      }
      toastErr(e, '保存失败');
      return false;
    }
  }
  async function reloadTab(t) {
    try {
      const data = await api('GET', '/api/ide/file?' + qs({ path: t.path }));
      t.mtime = data.mtime; t.eol = data.eol || '\n';
      if (data.content == null) { t.placeholder = data.binary ? 'binary' : 'too_large'; t.state = null; if (viewTabId === t.id) releaseView(); }
      else {
        const sel = (viewTabId === t.id && view) ? view.state.selection.main.head : 0;
        fillState(t, data.content);
        if (viewTabId === t.id && view) {
          view.setState(t.state);
          view.dispatch({ selection: { anchor: Math.min(sel, t.state.doc.length) } });
        }
      }
      if (activeId === t.id) showTabContent(t);
      renderTabs(); updateStatusEditor();
    } catch (e) { toastErr(e, '重新加载失败'); }
  }
  /** Reload clean tabs after operations that change files on disk (checkout, pull, discard). */
  async function reloadCleanTabs(paths) {
    for (const t of tabs.slice()) {
      if (t.kind !== 'file' || t.dirty || (paths && !paths.includes(t.path))) continue;
      try {
        const data = await api('GET', '/api/ide/file?' + qs({ path: t.path }));
        if (data.mtime !== t.mtime) await reloadTab(t);
      } catch (e) { if (e.status === 404) closeTab(t.id, true); }
    }
  }

  // ---------------------------------------------------------------- Quick open (Ctrl+P)
  let qoItems = [], qoIdx = 0, qoSeq = 0, qoPrevFocus = null;
  function openQuickOpen() {
    qoPrevFocus = document.activeElement;
    $('quickopen').hidden = false;
    const inp = $('qo-input');
    inp.value = '';
    inp.focus();
    qoFetch('');
  }
  function closeQuickOpen(restore) {
    $('quickopen').hidden = true;
    qoItems = []; $('qo-list').textContent = '';
    if (restore && qoPrevFocus && document.contains(qoPrevFocus)) { try { qoPrevFocus.focus(); } catch (e) { /* ignore */ } }
  }
  async function qoFetch(text) {
    const seq = ++qoSeq;
    let items = [];
    try { items = (await api('GET', '/api/ide/files?' + qs({ q: text, limit: 50 }))).files || []; } catch (e) { items = []; }
    if (seq !== qoSeq || $('quickopen').hidden) return;
    qoItems = items; qoIdx = 0; qoRender();
  }
  const qoFetchDebounced = debounce(qoFetch, 120);
  function qoRender() {
    const list = $('qo-list');
    list.textContent = '';
    if (!qoItems.length) { list.appendChild(h('li', { class: 'qo-empty', role: 'presentation' }, '没有匹配的文件')); $('qo-input').removeAttribute('aria-activedescendant'); return; }
    qoItems.forEach((p, i) => {
      list.appendChild(h('li', { id: 'qo-opt-' + i, class: 'qo-item' + (i === qoIdx ? ' active' : ''), role: 'option', 'aria-selected': String(i === qoIdx), dataset: { i: String(i) } },
        h('span', { class: 'ficon', 'aria-hidden': 'true' }, fileIcon(baseName(p))),
        h('span', { class: 'fname' }, baseName(p)),
        h('span', { class: 'sr-dir' }, dirName(p))));
    });
    $('qo-input').setAttribute('aria-activedescendant', 'qo-opt-' + qoIdx);
    const a = list.children[qoIdx];
    if (a) a.scrollIntoView({ block: 'nearest' });
  }
  function qoPick(i) {
    const p = qoItems[i];
    if (p == null) return;
    closeQuickOpen(false);
    openFile(p, { focus: true });
  }
  $('qo-input').addEventListener('input', (e) => qoFetchDebounced(e.target.value.trim()));
  $('qo-input').addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!qoItems.length) return;
      qoIdx = (qoIdx + (e.key === 'ArrowDown' ? 1 : -1) + qoItems.length) % qoItems.length;
      qoRender();
    } else if (e.key === 'Enter') { e.preventDefault(); qoPick(qoIdx); }
    else if (e.key === 'Escape') { e.preventDefault(); closeQuickOpen(true); }
  });
  $('qo-list').addEventListener('mousedown', (e) => {
    const li = e.target.closest('.qo-item');
    if (li) { e.preventDefault(); qoPick(Number(li.dataset.i)); }
  });
  $('quickopen').addEventListener('mousedown', (e) => { if (e.target === $('quickopen')) closeQuickOpen(true); });

  // ================================================================= Search
  const searchOpts = { regex: false, case: false };
  let searchSeq = 0;
  function makeMatcher(text) {
    try {
      const src = searchOpts.regex ? text : text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      return new RegExp(src, searchOpts.case ? 'g' : 'gi');
    } catch (e) { return null; }
  }
  function highlightText(text, re) {
    const frag = document.createDocumentFragment();
    if (!re) { frag.appendChild(document.createTextNode(text)); return frag; }
    re.lastIndex = 0;
    let last = 0, m, guard = 0;
    while ((m = re.exec(text)) && guard++ < 50) {
      if (!m[0]) { re.lastIndex++; continue; }
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      frag.appendChild(h('mark', null, m[0]));
      last = m.index + m[0].length;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    return frag;
  }
  async function runSearch() {
    const text = $('search-q').value;
    const out = $('search-results');
    if (!text) { out.textContent = ''; $('search-summary').textContent = ''; return; }
    const seq = ++searchSeq;
    $('search-summary').textContent = '搜索中…';
    try {
      const d = await api('GET', '/api/ide/search?' + qs({ q: text, regex: searchOpts.regex ? 1 : 0, case: searchOpts.case ? 1 : 0, glob: $('search-glob').value.trim() }));
      if (seq !== searchSeq) return;
      renderSearch(d, text);
    } catch (e) {
      if (seq !== searchSeq) return;
      $('search-summary').textContent = '';
      toastErr(e, '搜索失败');
    }
  }
  function renderSearch(d, text) {
    const out = $('search-results');
    out.textContent = '';
    const results = d.results || [];
    const groups = new Map();
    for (const r of results) {
      if (!groups.has(r.path)) groups.set(r.path, []);
      groups.get(r.path).push(r);
    }
    $('search-summary').textContent = results.length
      ? results.length + ' 个结果，' + groups.size + ' 个文件' + (d.truncated ? '（结果已截断）' : '')
      : '未找到结果' + (d.files_scanned != null ? '（扫描 ' + d.files_scanned + ' 个文件）' : '');
    const re = makeMatcher(text);
    const frag = document.createDocumentFragment();
    for (const [path, items] of groups) {
      const lines = h('div', { role: 'group' });
      const head = h('div', { class: 'sr-file', role: 'button', tabindex: '0', 'aria-expanded': 'true', title: path },
        h('span', { class: 'twisty', 'aria-hidden': 'true' }, '▾'),
        h('span', { class: 'ficon', 'aria-hidden': 'true' }, fileIcon(baseName(path))),
        h('span', { class: 'fname' }, baseName(path)),
        h('span', { class: 'sr-dir' }, dirName(path)),
        h('span', { class: 'sr-count' }, String(items.length)));
      const toggle = () => {
        const open = lines.hidden;
        lines.hidden = !open; head.setAttribute('aria-expanded', String(open));
        head.firstChild.textContent = open ? '▾' : '▸';
      };
      head.addEventListener('click', toggle);
      head.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
      for (const r of items) {
        const txt = String(r.text == null ? '' : r.text);
        lines.appendChild(h('button', {
          class: 'sr-line', type: 'button', title: path + ':' + r.line,
          onclick: () => openFile(path, { line: r.line, col: r.col, focus: true }),
        }, h('span', { class: 'sr-ln' }, String(r.line)), highlightText(txt, re)));
      }
      frag.appendChild(head); frag.appendChild(lines);
    }
    out.appendChild(frag);
  }
  const searchDebounced = debounce(() => { if ($('search-q').value.length >= 2) runSearch(); }, 600);
  $('search-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); runSearch(); } });
  $('search-q').addEventListener('input', searchDebounced);
  $('search-glob').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); runSearch(); } });
  for (const [id, key] of [['search-case', 'case'], ['search-regex', 'regex']]) {
    $(id).addEventListener('click', () => {
      searchOpts[key] = !searchOpts[key];
      $(id).setAttribute('aria-pressed', String(searchOpts[key]));
      if ($('search-q').value) runSearch();
    });
  }

  // ================================================================= Git / SCM
  let gitInflight = null, gitPending = false, gitLast = 0, lastBranchLoaded = null;
  function refreshGit() {
    if (gitInflight) { gitPending = true; return gitInflight; }
    gitLast = Date.now();
    gitInflight = api('GET', '/api/ide/git/status')
      .then((st) => { S.git = st || { is_repo: false, files: [] }; applyGit(); })
      .catch(() => { /* backend may be restarting; keep last state */ })
      .finally(() => {
        gitInflight = null;
        if (gitPending) { gitPending = false; refreshGit(); }
      });
    return gitInflight;
  }
  function applyGit() {
    const st = S.git;
    const files = st.files || [];
    S.gitMap = new Map();
    S.gitDirs = new Set();
    for (const f of files) {
      S.gitMap.set(f.path, gitLetter(f));
      let p = dirName(f.path);
      while (p) { S.gitDirs.add(p); p = dirName(p); }
    }
    const n = files.length;
    const badge = $('scm-badge');
    badge.hidden = !n; badge.textContent = n > 999 ? '999+' : String(n);
    $('scm-badge').setAttribute('aria-label', n + ' 个更改');
    const chip = $('branch-chip');
    chip.hidden = !st.is_repo;
    $('branch-chip-text').textContent = st.branch || '';
    let sb = st.is_repo ? '⎇ ' + (st.branch || '(分离)') : '⎇ 非 Git 仓库';
    if (st.is_repo && (st.ahead || st.behind)) sb += '  ↑' + (st.ahead || 0) + ' ↓' + (st.behind || 0);
    $('sb-branch').textContent = sb;
    $('sb-changes').textContent = n + ' 更改';
    renderTree();
    renderScm();
    if (S.sidebarOpen && S.view === 'scm' && st.is_repo && st.branch !== lastBranchLoaded) loadBranches();
  }
  function iconBtn(label, aria, fn) {
    return h('button', { class: 'icon-btn', type: 'button', 'aria-label': aria, title: aria, onclick: (e) => { e.stopPropagation(); fn(); } }, label);
  }
  function renderScm() {
    const st = S.git;
    $('scm-norepo').hidden = !!st.is_repo;
    $('scm-body').hidden = !st.is_repo;
    if (!st.is_repo) return;
    const staged = [], changes = [];
    for (const f of st.files || []) {
      const i = f.index || ' ', w = f.worktree || ' ';
      if (i === '?' || w === '?') { changes.push({ f, letter: 'U', staged: false }); continue; }
      if (i !== ' ') staged.push({ f, letter: i === 'U' ? '!' : i, staged: true });
      if (w !== ' ') changes.push({ f, letter: w === 'U' ? '!' : w, staged: false });
    }
    S.scmStaged = staged; S.scmChanges = changes;
    const g = $('scm-groups');
    g.textContent = '';
    if (staged.length) g.appendChild(scmGroup('暂存的更改', staged, true));
    g.appendChild(scmGroup('更改', changes, false));
    if (!staged.length && !changes.length) g.appendChild(h('div', { class: 'tree-note' }, '没有更改'));
  }
  function scmGroup(title, items, staged) {
    const wrap = h('div', { class: 'scm-group', role: 'group', 'aria-label': title });
    wrap.appendChild(h('div', { class: 'scm-group-head' },
      h('span', { class: 'grow' }, title),
      items.length ? (staged
        ? iconBtn('−', '全部取消暂存', () => scmOp('unstage', []))
        : [iconBtn('↺', '全部放弃更改', () => scmOp('discard', items.map((x) => x.f.path))), iconBtn('+', '全部暂存', () => scmOp('stage', []))]) : null,
      h('span', { class: 'sr-count' }, String(items.length))));
    for (const it of items) wrap.appendChild(scmItem(it));
    return wrap;
  }
  function scmItem(it) {
    const p = it.f.path;
    const row = h('div', {
      class: 'scm-item', tabindex: '0', role: 'button', dataset: { path: p, staged: it.staged ? '1' : '0' },
      title: p + (it.f.orig_path ? '（重命名自 ' + it.f.orig_path + '）' : '') + ' • ' + (GIT_TITLES[it.letter] || it.letter),
      'aria-label': p + ' ' + (GIT_TITLES[it.letter] || it.letter) + '，打开差异',
    },
    h('span', { class: 'ficon', 'aria-hidden': 'true' }, fileIcon(baseName(p))),
    h('span', { class: 'fname ' + gitClass(it.letter) }, baseName(p)),
    h('span', { class: 'sr-dir' }, dirName(p)),
    h('span', { class: 'acts' },
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': '打开文件', title: '打开文件', onclick: (e) => { e.stopPropagation(); openFile(p, { focus: true }); } }, '📄'),
      it.staged
        ? iconBtn('−', '取消暂存', () => scmOp('unstage', [p]))
        : [iconBtn('↺', '放弃更改', () => scmOp('discard', [p])), iconBtn('+', '暂存更改', () => scmOp('stage', [p]))]),
    h('span', { class: 'gstat ' + gitClass(it.letter) }, it.letter));
    row.addEventListener('click', () => openDiff(p, it.staged));
    row.addEventListener('keydown', (e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target === row) { e.preventDefault(); openDiff(p, it.staged); } });
    return row;
  }
  async function scmOp(op, paths) {
    if (op === 'discard') {
      const what = paths.length === 1 ? '“' + paths[0] + '”' : paths.length + ' 个文件';
      if (!(await confirmBox('放弃更改', '确定放弃 ' + what + ' 的更改吗？\n已跟踪文件将恢复到上次提交/暂存的状态，未跟踪文件将移入回收站。', '放弃更改', true))) return;
    }
    try { await api('POST', '/api/ide/git/' + op, { paths }); } catch (e) {
      toastErr(e, { stage: '暂存失败', unstage: '取消暂存失败', discard: '放弃更改失败' }[op]);
    }
    await refreshGit();
    if (op === 'discard') { refreshTree(); reloadCleanTabs(paths.length ? paths : null); }
    refreshOpenDiffs();
  }
  async function commit() {
    const ta = $('scm-msg');
    const msg = ta.value.trim();
    if (!msg) { toast('请输入提交信息', 'error'); ta.focus(); return; }
    if (!(S.scmStaged || []).length) {
      if (!(S.scmChanges || []).length) { toast('没有可提交的更改', 'error'); return; }
      if (!(await confirmBox('没有暂存的更改', '是否暂存所有更改并直接提交？', '全部暂存并提交'))) return;
      try { await api('POST', '/api/ide/git/stage', { paths: [] }); } catch (e) { toastErr(e, '暂存失败'); return; }
    }
    const btn = $('scm-commit');
    btn.disabled = true;
    try {
      const r = await api('POST', '/api/ide/git/commit', { message: msg, amend: false });
      ta.value = '';
      toast('已提交 ' + (r.short || r.hash || ''), 'ok');
      if ($('scm-history').open) loadLog();
    } catch (e) { toastErr(e, '提交失败'); }
    btn.disabled = false;
    refreshGit();
    refreshOpenDiffs();
  }
  async function loadBranches() {
    if (!S.git.is_repo) return;
    try {
      const d = await api('GET', '/api/ide/git/branches');
      lastBranchLoaded = d.current;
      const sel = $('scm-branch');
      sel.textContent = '';
      for (const b of d.branches || []) {
        sel.appendChild(h('option', { value: b.name, selected: b.current ? true : null }, b.name + (b.upstream ? '  → ' + b.upstream : '')));
      }
      if (!(d.branches || []).length && d.current) sel.appendChild(h('option', { value: d.current, selected: true }, d.current));
      sel.appendChild(h('option', { value: '__new__' }, '＋ 新建分支…'));
      sel.dataset.current = d.current || '';
      if (d.current) sel.value = d.current;
    } catch (e) { /* ignore: branches are optional UI */ }
  }
  async function onBranchChange() {
    const sel = $('scm-branch');
    const v = sel.value, cur = sel.dataset.current || '';
    try {
      if (v === '__new__') {
        sel.value = cur;
        const name = await promptBox('新建分支', '基于当前分支 “' + cur + '” 创建并切换到新分支：', '', 'feature/xxx');
        if (!name) return;
        await api('POST', '/api/ide/git/branch', { name, checkout: true });
        toast('已创建并切换到分支 ' + name, 'ok');
      } else if (v !== cur) {
        await api('POST', '/api/ide/git/checkout', { branch: v });
        toast('已切换到分支 ' + v, 'ok');
      } else return;
    } catch (e) { sel.value = cur; toastErr(e, '切换分支失败'); return; }
    await refreshGit(); loadBranches(); refreshTree(); reloadCleanTabs(null);
    if ($('scm-history').open) loadLog();
  }
  async function pushPull(op) {
    const btn = $(op === 'push' ? 'scm-push' : 'scm-pull');
    btn.disabled = true;
    toast(op === 'push' ? '正在推送…' : '正在拉取…');
    try {
      const r = await api('POST', '/api/ide/git/' + op);
      toast(op === 'push' ? '推送完成' : '拉取完成', 'ok', r.output || '');
    } catch (e) { toastErr(e, op === 'push' ? '推送失败' : '拉取失败'); }
    btn.disabled = false;
    await refreshGit();
    if (op === 'pull') { refreshTree(); reloadCleanTabs(null); if ($('scm-history').open) loadLog(); }
  }
  async function loadLog() {
    const box = $('scm-log');
    try {
      const d = await api('GET', '/api/ide/git/log?' + qs({ limit: 50 }));
      box.textContent = '';
      const commits = d.commits || [];
      if (!commits.length) { box.appendChild(h('div', { class: 'tree-note' }, '暂无提交')); return; }
      for (const c of commits) {
        box.appendChild(h('button', { class: 'log-item', type: 'button', title: c.hash + '\n' + c.subject, onclick: () => openCommit(c.hash) },
          h('span', { class: 'log-subj' }, c.subject || '(无标题)'),
          h('span', { class: 'log-meta' }, (c.short || String(c.hash).slice(0, 7)) + ' · ' + (c.author || '') + ' · ' + fmtDate(c.date))));
      }
    } catch (e) { box.textContent = ''; box.appendChild(h('div', { class: 'tree-note' }, '加载历史失败：' + e.message)); }
  }
  function fmtDate(s) {
    const d = new Date(s);
    if (isNaN(d.getTime())) return String(s || '');
    const pad = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  // ---------------------------------------------------------------- diff / commit tabs
  async function openDiff(path, staged) {
    let d;
    try { d = await api('GET', '/api/ide/git/diff?' + qs({ path, staged: staged ? 1 : 0 })); } catch (e) { toastErr(e, '获取差异失败'); return; }
    let t = tabs.find((x) => x.kind === 'diff' && x.path === path && x.staged === !!staged);
    if (!t) {
      t = { id: ++tabSeq, kind: 'diff', path, staged: !!staged, title: baseName(path) + (staged ? '（已暂存）' : '（工作区）'), lastUsed: Date.now(), dirty: false };
      addTab(t);
    }
    t.diffText = d.diff || '';
    activate(t.id);
  }
  async function refreshOpenDiffs() {
    for (const t of tabs) {
      if (t.kind !== 'diff') continue;
      try { t.diffText = (await api('GET', '/api/ide/git/diff?' + qs({ path: t.path, staged: t.staged ? 1 : 0 }))).diff || ''; } catch (e) { /* ignore */ }
      if (t.id === activeId) showTabContent(t);
    }
  }
  async function openCommit(hash) {
    let t = tabs.find((x) => x.kind === 'commit' && x.hash === hash);
    if (!t) {
      let d;
      try { d = await api('GET', '/api/ide/git/show?' + qs({ hash })); } catch (e) { toastErr(e, '获取提交失败'); return; }
      t = { id: ++tabSeq, kind: 'commit', hash, title: '提交 ' + String(d.hash || hash).slice(0, 7), commit: d, diffText: d.diff || '', lastUsed: Date.now(), dirty: false };
      addTab(t);
    }
    activate(t.id);
  }
  function renderDiffLines(text) {
    const body = h('div', { class: 'diff-body' });
    if (!text) { body.appendChild(h('div', { class: 'diff-empty' }, '没有差异（文件可能与基准相同，或是二进制文件）。')); return body; }
    const lines = text.split('\n');
    if (lines.length && lines[lines.length - 1] === '') lines.pop();
    const MAX = 20000;
    const frag = document.createDocumentFragment();
    let inHunk = false;
    const n = Math.min(lines.length, MAX);
    for (let i = 0; i < n; i++) {
      const l = lines[i].replace(/\r$/, '');
      let c = 'diff-line';
      if (l.startsWith('diff ')) { inHunk = false; c += ' meta'; }
      else if (l.startsWith('@@')) { inHunk = true; c += ' hunk'; }
      else if (inHunk) {
        if (l[0] === '+') c += ' add';
        else if (l[0] === '-') c += ' del';
        else if (l[0] === '\\') c += ' meta';
      } else if (l.startsWith('+++') || l.startsWith('---') || l.startsWith('index ') || /^(new|deleted) file|^similarity|^rename |^Binary|^old mode|^new mode/.test(l)) c += ' meta';
      else if (l[0] === '+') c += ' add';
      else if (l[0] === '-') c += ' del';
      const d = document.createElement('div');
      d.className = c;
      d.textContent = l || ' ';
      frag.appendChild(d);
    }
    body.appendChild(frag);
    if (lines.length > MAX) body.appendChild(h('div', { class: 'diff-empty' }, '（差异过长，仅显示前 ' + MAX + ' 行）'));
    return body;
  }
  function renderDiffTab(t, host) {
    host.textContent = '';
    if (t.kind === 'diff') {
      host.appendChild(h('div', { class: 'diff-head' },
        h('h3', null, t.path),
        h('div', { class: 'row muted small' }, t.staged ? '已暂存的更改（索引 vs HEAD）' : '工作区更改（工作区 vs 索引）',
          h('button', { class: 'btn tiny', type: 'button', onclick: () => openFile(t.path, { focus: true }) }, '打开文件'),
          h('button', { class: 'btn tiny', type: 'button', onclick: () => openDiff(t.path, t.staged) }, '刷新'))));
    } else {
      const c = t.commit || {};
      const files = h('ul', { class: 'diff-files', 'aria-label': '变更文件' });
      for (const f of c.files || []) {
        files.appendChild(h('li', null,
          h('span', { class: 'gstat ' + gitClass(String(f.status || 'M')[0]) }, String(f.status || '')),
          h('span', { class: 'fname' }, f.path),
          h('button', { class: 'btn tiny', type: 'button', title: '把工作区中的该文件恢复为此提交中的版本', onclick: () => restoreFile(c.hash || t.hash, f.path) }, '恢复到此版本')));
      }
      host.appendChild(h('div', { class: 'diff-head' },
        h('h3', null, c.subject || '(无标题)'),
        h('div', { class: 'row muted small' },
          h('span', null, String(c.hash || t.hash)),
          h('span', null, (c.author || '') + ' · ' + fmtDate(c.date)),
          h('button', { class: 'btn tiny danger', type: 'button', onclick: () => revertCommit(c.hash || t.hash, c.subject) }, '回滚此提交')),
        files,
        c.truncated ? h('div', { class: 'muted small' }, '（差异过大，已截断）') : null));
    }
    host.appendChild(renderDiffLines(t.diffText || ''));
    host.scrollTop = 0;
  }
  async function revertCommit(hash, subject) {
    if (!(await confirmBox('回滚此提交', '将创建一个新提交来撤销 ' + String(hash).slice(0, 7) + '：\n“' + (subject || '') + '”\n是否继续？', '回滚', true))) return;
    try {
      const r = await api('POST', '/api/ide/git/revert', { hash });
      toast('已回滚 ' + String(hash).slice(0, 7), 'ok', r.output || '');
    } catch (e) { toastErr(e, '回滚失败'); }
    await refreshGit(); refreshTree(); reloadCleanTabs(null);
    if ($('scm-history').open) loadLog();
  }
  async function restoreFile(ref, path) {
    if (!(await confirmBox('恢复到此版本', '把 “' + path + '” 恢复为提交 ' + String(ref).slice(0, 7) + ' 中的版本？\n工作区中该文件的当前内容将被覆盖。', '恢复', true))) return;
    try {
      await api('POST', '/api/ide/git/restore_file', { path, ref });
      toast('已恢复 ' + path, 'ok');
    } catch (e) { toastErr(e, '恢复失败'); }
    await refreshGit(); refreshTree(); reloadCleanTabs([path]);
  }

  // ================================================================= Terminal
  // VS Code-like integrated terminal: shell profiles (+ / ˅), tabs (dbl-click rename), Windows
  // clipboard keys, Ctrl+click web links, relaunch on Enter after exit, per-instance ResizeObserver.
  const terms = [];
  let activeTermId = null, termSeq = 0, panelOpen = false, panelMax = false;
  const TERM = { profiles: null, def: '', max: 3, loading: null };
  // VS Code "Dark Modern" terminal palette (background/foreground follow the IDE palette)
  const TERM_ANSI = {
    black: '#000000', red: '#cd3131', green: '#0dbc79', yellow: '#e5e510', blue: '#2472c8', magenta: '#bc3fbc',
    cyan: '#11a8cd', white: '#e5e5e5', brightBlack: '#666666', brightRed: '#f14c4c', brightGreen: '#23d18b',
    brightYellow: '#f5f543', brightBlue: '#3b8eea', brightMagenta: '#d670d6', brightCyan: '#29b8db', brightWhite: '#e5e5e5',
  };
  const TERM_ICONS = { 'terminal-powershell': '❯', 'terminal-cmd': '›', 'terminal-bash': '$', 'terminal-git-bash': '$', 'terminal-linux': '🐧', terminal: '›' };
  const cssVar = (name, dflt) => (getComputedStyle(document.documentElement).getPropertyValue(name) || '').trim() || dflt;
  const isWinHost = () => /^[A-Za-z]:[\\/]/.test(String((S.ws && S.ws.root) || '')) || /Win/i.test(navigator.platform || '');

  async function loadProfiles(force) {
    if (TERM.profiles && !force) return TERM.profiles;
    if (!TERM.loading) {
      TERM.loading = api('GET', '/api/ide/term/profiles').then((d) => {
        TERM.profiles = Array.isArray(d.profiles) ? d.profiles : [];
        TERM.def = d.default || '';
        if (d.max_terminals) TERM.max = d.max_terminals;
        return TERM.profiles;
      }).catch((e) => { toastErr(e, '读取终端配置失败'); return TERM.profiles || []; })
        .finally(() => { TERM.loading = null; });
    }
    return TERM.loading;
  }
  function showPanel(open) {
    panelOpen = open;
    $('panel').hidden = !open;
    $('panel-resizer').hidden = !open || panelMax;
    $('act-term').setAttribute('aria-pressed', String(open));
    if (open) requestAnimationFrame(fitActiveTerm);
  }
  function setPanelMax(on) {
    panelMax = !!on;
    document.querySelector('.center').classList.toggle('panel-max', panelMax);
    $('panel-resizer').hidden = !panelOpen || panelMax;
    const b = $('panel-max');
    b.setAttribute('aria-pressed', String(panelMax));
    b.textContent = panelMax ? '⌄' : '⌃';
    b.title = panelMax ? '恢复面板大小' : '最大化面板大小';
    b.setAttribute('aria-label', b.title);
    requestAnimationFrame(fitActiveTerm);
  }
  function togglePanel() {
    if (panelOpen) {
      showPanel(false);
      const t = activeTab();
      if (t && t.state && view) view.focus();
      return;
    }
    showPanel(true);
    if (!terms.length) newTerminal();
    else { const t = terms.find((x) => x.id === activeTermId); if (t) t.term.focus(); }
  }
  function termLabel(t) {
    return t.customName || ((t.profileName || '终端') + (t.num ? ' ' + t.num : ''));
  }
  function renderTermTabs() {
    const bar = $('term-tabs');
    bar.textContent = '';
    for (const t of terms) {
      const on = t.id === activeTermId;
      const label = termLabel(t);
      const el = h('div', {
        class: 'term-tab' + (on ? ' active' : '') + (t.exited ? ' exited' : ''), role: 'tab', tabindex: '0', 'data-id': String(t.id),
        'aria-selected': String(on), title: label + (t.mode ? '（' + t.mode + (t.shell ? ' · ' + t.shell : '') + '）' : '') + (t.exited ? ' — 已退出' : '') + '\n双击重命名',
      },
      h('span', { class: 'term-ico', 'aria-hidden': 'true' }, TERM_ICONS[t.icon] || '›'),
      h('span', { class: 'term-label' }, label),
      t.exited ? h('span', { class: 'term-exit', title: '进程已退出' + (t.exitCode != null ? '，代码 ' + t.exitCode : '') }, t.exitCode != null ? '⏻ ' + t.exitCode : '⏻') : null,
      h('button', { class: 'tab-close', type: 'button', 'aria-label': '关闭 ' + label, title: '终止终端', onclick: (e) => { e.stopPropagation(); disposeTerm(t); } }, '✕'));
      el.addEventListener('click', (e) => {
        if (e.target.closest('input')) return;
        if (activeTermId === t.id) { if (t.term) t.term.focus(); } else activateTerm(t.id);
      });
      el.addEventListener('dblclick', (e) => { e.preventDefault(); renameTerm(t, el); });
      el.addEventListener('keydown', (e) => {
        if (e.target !== el) return;
        if (e.key === 'Enter') activateTerm(t.id);
        else if (e.key === 'F2') { e.preventDefault(); renameTerm(t, el); }
      });
      bar.appendChild(el);
    }
    updateStatusTerm();
  }
  function renameTerm(t, el) {
    const span = el.querySelector('.term-label');
    if (!span || el.querySelector('input')) return;
    const inp = h('input', { class: 'term-rename', type: 'text', 'aria-label': '重命名终端', spellcheck: 'false' });
    inp.value = termLabel(t);
    span.replaceWith(inp);
    inp.focus(); inp.select();
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      if (save) t.customName = inp.value.trim().slice(0, 60);  // empty -> back to the profile name
      renderTermTabs();
      if (t.term) t.term.focus();
    };
    inp.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Enter') { e.preventDefault(); finish(true); } else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    inp.addEventListener('blur', () => finish(true));
    inp.addEventListener('click', (e) => e.stopPropagation());
    inp.addEventListener('dblclick', (e) => e.stopPropagation());
  }
  function updateStatusTerm() {
    const t = terms.find((x) => x.id === activeTermId);
    const sb = $('sb-term');
    sb.hidden = !t || !t.mode;
    if (t && t.mode) sb.textContent = '终端: ' + t.mode + (t.shell ? ' · ' + t.shell : '');
  }
  function activateTerm(id) {
    activeTermId = id;
    for (const t of terms) t.box.hidden = t.id !== id;
    // update the tab strip in place (re-creating it would swallow the dblclick used for rename)
    const els = $('term-tabs').querySelectorAll('.term-tab');
    if (els.length === terms.length) {
      els.forEach((el) => { const on = el.dataset.id === String(id); el.classList.toggle('active', on); el.setAttribute('aria-selected', String(on)); });
      updateStatusTerm();
    } else renderTermTabs();
    const t = terms.find((x) => x.id === id);
    if (t) {
      requestAnimationFrame(() => {
        fitTerm(t);
        const a = document.activeElement;
        if (t.term && !(a && a.classList && a.classList.contains('term-rename'))) t.term.focus();  // don't kill an inline rename
      });
    }
  }
  function fitTerm(t) {
    if (!t || t.disposed || !panelOpen || t.box.hidden || !t.host.clientWidth || !t.host.clientHeight) return;
    try { t.fit.fit(); } catch (e) { /* not yet rendered */ }
  }
  const fitActiveTerm = () => fitTerm(terms.find((x) => x.id === activeTermId));
  function termSend(t, obj) {
    if (t.ws && t.ws.readyState === 1) { t.ws.send(JSON.stringify(obj)); return true; }
    return false;
  }
  function nextNum(t) {
    const used = new Set(terms.filter((x) => x !== t && x.profile === t.profile && x.num).map((x) => x.num));
    let n = 1;
    while (used.has(n)) n++;
    return n;
  }
  function termCopy(t) {
    const sel = t.term.getSelection();
    t.term.clearSelection();
    if (sel) copyText(sel).then((ok) => { if (!ok) toast('复制失败', 'error'); });
  }
  async function termPaste(t) {
    let text = null;
    try { text = await navigator.clipboard.readText(); } catch (e) { text = null; }
    if (text == null) { toast('无法读取剪贴板：请使用 Ctrl+V 粘贴', 'error'); return; }
    if (text && t.term) { t.term.paste(text); t.term.focus(); }
  }
  function openTermLink(ev, uri) {
    if (!(ev && (ev.ctrlKey || ev.metaKey))) return;  // like VS Code: Ctrl/Cmd + click follows the link
    if (!/^https?:\/\//i.test(uri)) return;
    const w = window.open(uri, '_blank', 'noopener,noreferrer');
    if (w) { try { w.opener = null; } catch (e) { /* ignore */ } }
  }
  function termKeyHandler(t, e) {
    if (e.type !== 'keydown') return true;
    const mod = e.ctrlKey || e.metaKey;
    if (!mod || e.altKey) return true;
    const k = e.code;
    if (k === 'KeyC' && (e.shiftKey || t.term.hasSelection())) {  // copy (and clear) instead of ^C
      e.preventDefault();
      if (t.term.hasSelection()) termCopy(t);
      return false;
    }
    if (k === 'KeyV') return false;  // let the browser fire a native paste event (xterm pastes it, bracketed)
    if (k === 'Backquote' || (k === 'KeyP' && !e.shiftKey)) return false;  // IDE shortcuts (handled globally)
    return true;
  }
  function newTerminal(opts) {
    opts = opts && !(opts instanceof Event) ? opts : {};
    if (!window.Terminal || !window.FitAddon) { toast('终端组件未加载', 'error'); return null; }
    if (terms.length >= TERM.max) { toast('最多 ' + TERM.max + ' 个终端', 'error'); return null; }
    if (!panelOpen) showPanel(true);
    const id = ++termSeq;
    const hint = h('div', { class: 'term-hint', role: 'status', hidden: '' },
      h('span', { class: 'term-hint-text' }),
      h('button', { class: 'icon-btn term-hint-close', type: 'button', 'aria-label': '关闭提示', title: '关闭提示', onclick: () => { hint.hidden = true; fitTerm(t); } }, '✕'));
    const host = h('div', { class: 'term-host' });
    const box = h('div', { class: 'term-inst', 'data-id': String(id) }, hint, host);
    $('term-body').appendChild(box);
    const term = new window.Terminal({
      scrollback: 2000, fontFamily: cssVar('--mono', '"Cascadia Code", Consolas, "Courier New", monospace'), fontSize: 13,
      lineHeight: 1.2, cursorBlink: false, allowProposedApi: false, macOptionIsMeta: true,
      theme: Object.assign({
        background: cssVar('--bg', '#1e1e1e'), foreground: cssVar('--fg', '#cccccc'), cursor: '#aeafad', cursorAccent: cssVar('--bg', '#1e1e1e'),
        selectionBackground: '#264f78', selectionInactiveBackground: '#3a3d41',
      }, TERM_ANSI),
    });
    const fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    const t = {
      id, term, fit, box, host, hint, ws: null, ro: null, mode: '', shell: '', icon: '', exited: false, exitCode: null, disposed: false,
      profile: opts.profile || '', profileName: '', num: 0, customName: '', cwd: opts.cwd || '', pending: '',
    };
    if (window.WebLinksAddon) {
      try {
        term.loadAddon(new window.WebLinksAddon.WebLinksAddon(openTermLink, {
          hover: () => { host.title = '按住 Ctrl 并单击以打开链接'; }, leave: () => { host.title = ''; },
        }));
      } catch (e) { /* optional */ }
    }
    term.open(host);
    terms.push(t);
    for (const x of terms) x.box.hidden = x.id !== id;
    activeTermId = id;
    try { fit.fit(); } catch (e) { /* ignore */ }
    term.attachCustomKeyEventHandler((e) => termKeyHandler(t, e));
    // right click: copy if there is a selection, else paste (Windows Terminal / VS Code on Windows)
    host.addEventListener('contextmenu', (e) => {
      e.preventDefault(); e.stopPropagation();
      if (t.disposed) return;
      if (term.hasSelection()) termCopy(t); else termPaste(t);
    }, true);
    host.addEventListener('focusin', () => { if (activeTermId !== t.id) activateTerm(t.id); });
    term.onData((d) => {
      if (t.exited) { if (d.indexOf('\r') >= 0) restartTerm(t); return; }
      if (!termSend(t, { type: 'input', data: d }) && t.ws && t.ws.readyState === 0 && t.pending.length < 65536) t.pending += d;
    });
    term.onResize(({ cols, rows }) => termSend(t, { type: 'resize', cols, rows }));
    if (window.ResizeObserver) {
      const onSize = debounce(() => fitTerm(t), 50);
      t.ro = new ResizeObserver(onSize);
      t.ro.observe(host);
    }
    connectTerm(t);
    renderTermTabs();
    requestAnimationFrame(() => { fitTerm(t); term.focus(); });
    return t;
  }
  function connectTerm(t) {
    const term = t.term;
    t.exited = false; t.exitCode = null; t.pending = '';
    const q = { cols: term.cols || 80, rows: term.rows || 24, cwd: t.cwd || '' };
    if (t.profile) q.profile = t.profile;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(proto + '//' + location.host + '/api/ide/term?' + qs(q));
    t.ws = ws;
    ws.onopen = () => {
      if (t.ws !== ws) return;
      if (t.pending) { const p = t.pending; t.pending = ''; termSend(t, { type: 'input', data: p }); }
    };
    ws.onmessage = (ev) => {
      if (t.ws !== ws || t.disposed) return;
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type === 'output') term.write(String(m.data || ''));
      else if (m.type === 'info') {
        t.mode = m.mode || ''; t.shell = m.shell || '';
        if (m.max_terminals) TERM.max = m.max_terminals;
        if (m.profile) t.profile = m.profile;
        const p = (TERM.profiles || []).find((x) => x.id === t.profile);
        t.profileName = m.name || (p && p.name) || String(t.shell || '终端').replace(/\.exe$/i, '');
        t.icon = (p && p.icon) || (/pwsh|powershell/i.test(t.shell) ? 'terminal-powershell' : /cmd/i.test(t.shell) ? 'terminal-cmd' : 'terminal-bash');
        if (!t.num) t.num = nextNum(t);
        // xterm 6: tell it the PTY is ConPTY so wrapping/reflow heuristics match Windows
        if (t.mode === 'pty' && isWinHost()) { try { term.options.windowsPty = { backend: 'conpty' }; } catch (e) { /* ignore */ } }
        const hintOn = t.mode === 'pipe' && !!m.hint;
        t.hint.querySelector('.term-hint-text').textContent = hintOn ? '⚠ ' + m.hint : '';
        t.hint.hidden = !hintOn;
        requestAnimationFrame(() => fitTerm(t));
        renderTermTabs();
      } else if (m.type === 'exit') {
        t.exited = true; t.exitCode = m.code;
        term.write('\r\n\x1b[2m[进程已退出，代码 ' + (m.code == null ? '?' : m.code) + ']  按 Enter 重新启动\x1b[0m\r\n');
        renderTermTabs();
      }
    };
    ws.onclose = (ev) => {
      if (t.ws !== ws || t.disposed) return;
      const msgs = { 4429: '最多 ' + TERM.max + ' 个终端', 4403: '安全校验失败', 4400: '无效的终端配置' };
      if (msgs[ev.code]) { toast(msgs[ev.code], 'error'); disposeTerm(t); return; }
      if (!t.exited) {
        t.exited = true;
        term.write('\r\n\x1b[2m[连接已关闭]  按 Enter 重新启动\x1b[0m\r\n');
      }
      renderTermTabs();
    };
  }
  function restartTerm(t) {
    if (t.disposed) return;
    const old = t.ws;
    t.ws = null;
    try { if (old && old.readyState <= 1) old.close(1000); } catch (e) { /* ignore */ }
    t.term.write('\x1b[0m\r\n');
    connectTerm(t);
    renderTermTabs();
  }
  function disposeTerm(t) {
    if (!t || t.disposed) return;
    t.disposed = true;
    try { if (t.ro) t.ro.disconnect(); } catch (e) { /* ignore */ }
    try { if (t.ws && t.ws.readyState <= 1) t.ws.close(1000); } catch (e) { /* ignore */ }
    try { t.term.dispose(); } catch (e) { /* ignore */ }
    t.box.remove();
    t.ws = null; t.term = null; t.fit = null; t.ro = null;
    const i = terms.indexOf(t);
    if (i >= 0) terms.splice(i, 1);
    if (activeTermId === t.id) {
      activeTermId = null;
      const n = terms[Math.min(i, terms.length - 1)];
      if (n) activateTerm(n.id);
    }
    renderTermTabs();
    if (!terms.length) {  // like VS Code: killing the last terminal closes the panel
      if (panelMax) setPanelMax(false);
      showPanel(false);
    }
  }
  async function showProfileMenu() {
    const btn = $('term-profiles');
    const r = btn.getBoundingClientRect();
    const list = await loadProfiles();
    if (!list.length) { toast('没有检测到可用的 shell', 'error'); return; }
    showMenu(r.left, r.bottom + 2, list.map((p) => ({
      label: p.name + (p.id === TERM.def ? '（默认）' : ''), run: () => newTerminal({ profile: p.id }),
    })));
  }
  function openInTerminal(dir) {
    newTerminal({ cwd: normRel(dir || '') || '' });
  }
  $('term-new').addEventListener('click', () => newTerminal());
  $('term-profiles').addEventListener('click', (e) => { e.stopPropagation(); showProfileMenu(); });
  $('term-kill').addEventListener('click', () => disposeTerm(terms.find((x) => x.id === activeTermId)));
  $('panel-max').addEventListener('click', () => setPanelMax(!panelMax));
  $('panel-close').addEventListener('click', () => { if (panelMax) setPanelMax(false); showPanel(false); });

  // ================================================================= AI panel
  // The panel itself (Ask / Edit modes, model switcher, edit cards) lives in ide-ai.js, loaded after
  // this file. It reaches the editor only through the hooks below (exported on window.__ide).
  const aiMod = () => window.__ideAI || null;
  let aiPendingOpen = null;
  function toggleAI(force) {
    const a = aiMod();
    if (a && a.toggle) a.toggle(force); else aiPendingOpen = force != null ? !!force : true;
  }
  function mountToAI(path) { const a = aiMod(); if (a && a.addContext) a.addContext(path); }
  function editorSelection() {
    const t = activeTab();
    if (!t || t.kind !== 'file' || !view || viewTabId !== t.id) return null;
    const r = view.state.selection.main;
    if (r.empty) return null;
    const text = view.state.sliceDoc(r.from, r.to);
    if (!text.trim()) return null;
    const doc = view.state.doc;
    return { path: t.path, lang: t.lang || '', text: text.slice(0, 20000), from_line: doc.lineAt(r.from).number, to_line: doc.lineAt(r.to).number };
  }
  const aiFileTab = (path) => tabs.find((x) => x.kind === 'file' && x.path === path && x.state) || null;
  const aiTabText = (t) => ((viewTabId === t.id && view) ? view.state.doc : t.state.doc).toString();
  /** Apply ONE CodeMirror change {from, to, insert} as a normal transaction → undoable with Ctrl+Z. */
  function aiApplyChange(path, change) {
    const t = aiFileTab(path);
    if (!t) return false;
    const spec = { changes: change, selection: { anchor: change.from + String(change.insert || '').length }, scrollIntoView: true, userEvent: 'input.ai' };
    if (viewTabId === t.id && view) view.dispatch(spec); // onEditorUpdate marks the tab dirty
    else { t.state = t.state.update(spec).state; t.dirty = !t.state.doc.eq(t.savedDoc); renderTabs(); }
    return true;
  }
  function aiReplaceSelection(code) {
    const t = activeTab();
    if (!t || t.kind !== 'file' || !t.state || !view || viewTabId !== t.id) return null;
    view.dispatch(view.state.replaceSelection(code), { scrollIntoView: true, userEvent: 'input.paste' });
    view.focus();
    return t.title;
  }
  const aiHooks = {
    util: { h, api, toast, toastErr, copyText, escapeHtml, baseName, normRel, qs, lsGet, lsSet },
    get workspace() { return S.ws; },
    get aiPendingOpen() { return aiPendingOpen; },
    activeFile: () => { const t = activeTab(); return t && t.kind === 'file' ? { path: t.path, title: t.title, lang: t.lang || '', editable: !!t.state } : null; },
    openPaths: () => tabs.filter((t) => t.kind === 'file').map((t) => t.path),
    selection: editorSelection,
    fileText: (path) => { const t = aiFileTab(path); return t ? aiTabText(t) : null; },
    openFile: async (path) => { const t = await openFile(path); return t ? { path: t.path, editable: !!t.state } : null; },
    applyChange: aiApplyChange,
    replaceSelection: aiReplaceSelection,
    saveFile: async (path) => { const t = aiFileTab(path); return t ? saveTab(t) : false; },
    refreshTree: () => refreshTree(),
    focusEditor: () => { const t = activeTab(); if (t && t.state && view) view.focus(); },
  };

  // ================================================================= Status bar
  function updateStatusEditor() {
    const t = activeTab();
    const pos = $('sb-pos'), lang = $('sb-lang'), eol = $('sb-eol');
    const isFile = !!(t && t.kind === 'file');
    pos.hidden = !(isFile && t.state && view && viewTabId === t.id);
    lang.hidden = !isFile; eol.hidden = !isFile;
    if (!isFile) return;
    if (!pos.hidden) {
      const st = view.state, head = st.selection.main.head, line = st.doc.lineAt(head);
      const selLen = st.selection.main.to - st.selection.main.from;
      pos.textContent = '行 ' + line.number + '，列 ' + (head - line.from + 1) + (selLen ? '（已选 ' + selLen + '）' : '');
    }
    lang.textContent = LANG_NAMES[t.lang] || t.lang || '纯文本';
    eol.textContent = t.eol === '\r\n' ? 'CRLF' : 'LF';
  }
  function startMemoryIndicator() {
    const pm = performance.memory;
    if (!pm) return;
    const el = $('sb-mem');
    el.hidden = false;
    const upd = () => { el.textContent = '内存 ' + Math.round(performance.memory.usedJSHeapSize / 1048576) + ' MB'; };
    upd();
    setInterval(() => { if (document.visibilityState === 'visible') upd(); }, 10000);
  }

  // ================================================================= Global shortcuts & wiring
  window.addEventListener('keydown', (e) => {
    if (!$('modal').hidden) return;
    if (e.key === 'Escape') { if (!$('ctxmenu').hidden) hideMenu(); if (!$('quickopen').hidden) closeQuickOpen(true); return; }
    const mod = e.ctrlKey || e.metaKey;
    if (!mod || e.altKey) return;
    const k = e.code;
    const inTerm = !!(e.target && e.target.closest && e.target.closest('.xterm'));
    // terminal focused: the shell owns every Ctrl chord except Ctrl+` / Ctrl+Shift+` / Ctrl+P (VS Code)
    if (inTerm && k !== 'Backquote' && !(k === 'KeyP' && !e.shiftKey)) return;
    let handled = true;
    if (e.shiftKey && k === 'KeyE') showView('explorer', true);
    else if (e.shiftKey && k === 'KeyF') showView('search', true);
    else if (e.shiftKey && k === 'KeyG') showView('scm', true);
    else if (!e.shiftKey && k === 'KeyL' && !inTerm) toggleAI();
    else if (!e.shiftKey && k === 'KeyP') { if ($('quickopen').hidden) openQuickOpen(); else closeQuickOpen(true); }
    else if (!e.shiftKey && k === 'KeyS') saveTab(activeTab());
    else if (k === 'Backquote') { if (e.shiftKey) newTerminal(); else togglePanel(); }
    else handled = false;
    if (handled) { e.preventDefault(); e.stopPropagation(); }
  }, true);

  document.querySelectorAll('.act-btn[data-view]').forEach((b) => b.addEventListener('click', () => showView(b.dataset.view, false)));
  $('act-ai').addEventListener('click', () => toggleAI());
  $('act-term').addEventListener('click', togglePanel);
  $('btn-switch-ws').addEventListener('click', switchWorkspace);
  $('branch-chip').addEventListener('click', () => showView('scm', true));
  $('sb-branch').addEventListener('click', () => showView('scm', true));
  $('sb-changes').addEventListener('click', () => showView('scm', true));

  $('ex-new-file').addEventListener('click', () => fsCreate('file', selectedDir()));
  $('ex-new-dir').addEventListener('click', () => fsCreate('dir', selectedDir()));
  $('ex-refresh').addEventListener('click', () => { refreshTree(); refreshGit(); });
  $('ex-collapse').addEventListener('click', () => {
    tree.expanded = new Set(['']);
    for (const k of [...tree.children.keys()]) if (k) tree.children.delete(k);
    renderTree();
  });
  function selectedDir() {
    const p = tree.selected;
    if (!p) return '';
    const r = rowByPath(p);
    return r && r.dataset.type === 'dir' ? p : dirName(p);
  }

  $('scm-init').addEventListener('click', async () => {
    try { await api('POST', '/api/ide/git/init'); toast('已初始化 Git 仓库', 'ok'); } catch (e) { toastErr(e, '初始化失败'); }
    await refreshGit(); loadBranches();
  });
  $('scm-commit').addEventListener('click', commit);
  $('scm-msg').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); commit(); } });
  $('scm-push').addEventListener('click', () => pushPull('push'));
  $('scm-pull').addEventListener('click', () => pushPull('pull'));
  $('scm-refresh').addEventListener('click', () => { refreshGit(); loadBranches(); if ($('scm-history').open) loadLog(); });
  $('scm-branch').addEventListener('change', onBranchChange);
  $('scm-history').addEventListener('toggle', () => { if ($('scm-history').open) loadLog(); });

  // AI panel buttons / input are wired in ide-ai.js

  makeResizer($('sidebar-resizer'), 'sidebar', 'sidebarW', 'x', 1, 160, () => Math.max(200, innerWidth * 0.6));
  makeResizer($('ai-resizer'), 'ai', 'aiW', 'x', -1, 260, () => Math.max(300, innerWidth * 0.6));
  makeResizer($('panel-resizer'), 'panel', 'panelH', 'y', -1, 80, () => Math.max(120, innerHeight - 200));

  window.addEventListener('beforeunload', (e) => {
    if (tabs.some((t) => t.dirty)) { e.preventDefault(); e.returnValue = ''; }
  });
  let lastFocusRefresh = 0;
  window.addEventListener('focus', () => {
    if (Date.now() - lastFocusRefresh < 2000) return;
    lastFocusRefresh = Date.now();
    refreshGit();
  });
  // at most every 15s while the page is visible (no tight polling)
  setInterval(() => {
    if (document.visibilityState === 'visible' && Date.now() - gitLast >= 15000) refreshGit();
  }, 5000);

  // expose a tiny read-only hook for tests / debugging
  window.__ide = {
    get tabs() { return tabs.map((t) => ({ id: t.id, kind: t.kind, path: t.path, dirty: t.dirty, title: t.title })); },
    get terminals() { return terms.length; },
    editorText: () => (view ? view.state.doc.toString() : ''),
  };
  Object.defineProperties(window.__ide, Object.getOwnPropertyDescriptors(aiHooks)); // hooks used by ide-ai.js

  // ================================================================= boot
  async function init() {
    applyLayout();
    showView('explorer', true);
    if (!CM) { toast('编辑器组件加载失败（vendor/codemirror.min.js）', 'error'); }
    try { S.ws = await api('GET', '/api/ide/workspace'); applyWs(); } catch (e) { toastErr(e, '加载工作区失败'); }
    await refreshTree();
    refreshGit();
    startMemoryIndicator();
    if (lsGet('aiOpen', false)) toggleAI(true);
  }
  init();
})();
