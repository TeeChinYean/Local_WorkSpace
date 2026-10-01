/* =============================================================================
 * /ide — AI panel (Ask / Edit modes, model switcher, SEARCH/REPLACE edit cards).
 * Loaded after ide.js; talks to the editor only through window.__ide.
 *
 * Pure, unit-testable helpers are exported on window.__ideAI:
 *   parseEditBlocks(text, opts) → [{path, search, replace, incomplete, rawPath, raw}]
 *   splitEditText(text, opts)   → {blocks, segments:[{type:'text',text}|{type:'edit',index}]}
 *   applyBlocks(content, blocks)→ {content, results:[{ok, tier, fuzzy, score, error, startLine, endLine}], applied, failed}
 *   unifiedDiff(a, b, ctx)      → {lines:[{type:'hunk'|'ctx'|'add'|'del', text}], added, removed}
 *   computeChange(a, b)         → {from, to, insert} | null   (single minimal CodeMirror change)
 *
 * Security: model output is only ever inserted with textContent or through the
 * escape-first mdInline() renderer. Nothing from the model becomes raw HTML.
 * ========================================================================== */
(function () {
  'use strict';

  // ================================================================= pure helpers
  const RE_SEARCH = /^\s*<{5,9}\s*SEARCH\b[\s:：]*(.*?)\s*$/i;
  const RE_DIVIDER = /^\s*={5,9}\s*$/;
  const RE_REPLACE = /^\s*>{5,9}\s*REPLACE\b.*$/i;
  const RE_FENCE = /^\s*(`{3,}|~{3,})\s*(.*?)\s*$/;
  const BARE_NAMES = /^(makefile|dockerfile|license|readme|procfile|gemfile|rakefile|jenkinsfile|vagrantfile|\.[\w.-]+)$/i;

  const normEol = (s) => String(s == null ? '' : s).replace(/\r\n?/g, '\n');

  /** Clean a "path line" written by a model; returns a relative-looking path or null. */
  function cleanPath(s, root) {
    let p = String(s || '').trim();
    if (!p || p.length > 400) return null;
    p = p.replace(/^#{1,6}\s+/, '').replace(/^[-*+]\s+/, '').replace(/^\d+[.)]\s+/, '');
    for (let i = 0; i < 3; i++) {
      p = p.replace(/^(?:\*\*|__)|(?:\*\*|__)$/g, '').trim();
      p = p.replace(/^(?:file\s*name|filename|file|path|文件名|文件路径|文件|路径)\s*[:：]\s*/i, '');
      p = p.replace(/^[`'"*_]+|[`'"*_]+$/g, '').trim();
      p = p.replace(/[:：]$/, '').trim();
    }
    p = p.replace(/\\/g, '/');
    if (root) {
      const r = String(root).replace(/\\/g, '/').replace(/\/+$/, '');
      if (r && p.toLowerCase().startsWith(r.toLowerCase() + '/')) p = p.slice(r.length + 1);
    }
    if (/\s/.test(p)) {
      const first = p.split(/\s+/)[0];
      if (/\/|\.[A-Za-z0-9]{1,10}$/.test(first)) p = first; else return null;
    }
    p = p.replace(/^\.\//, '');
    if (!p || p.length > 260 || /[<>|"?*`]/.test(p)) return null;
    if (!/[./]/.test(p) && !BARE_NAMES.test(p)) return null;
    if (/^\.+$/.test(p)) return null;
    return p;
  }

  /** A fence info string may carry the path: ```python app/x.py | ```app/x.py | ```python:app/x.py */
  function pathFromFenceInfo(info, root) {
    if (!info) return null;
    for (const tok of info.split(/[\s:]+/)) {
      if (!tok || !/[./]/.test(tok)) continue;
      const p = cleanPath(tok, root);
      if (p) return p;
    }
    return null;
  }

  function stripFencePair(lines) {
    if (lines.length >= 2 && RE_FENCE.test(lines[0]) && /^\s*(`{3,}|~{3,})\s*$/.test(lines[lines.length - 1])) {
      return lines.slice(1, -1);
    }
    return lines;
  }

  /**
   * Find the header (path line / fence opener) above a SEARCH marker at `idx`, not crossing `stop`.
   * Returns {path, start} where start is the first line index belonging to the header.
   */
  function findHeader(lines, idx, stop, root) {
    let path = null, start = idx, blanks = 0;
    for (let j = idx - 1; j >= stop && j >= idx - 5; j--) {
      const l = lines[j];
      if (!l.trim()) { if (++blanks > 2) break; continue; }
      const f = RE_FENCE.exec(l);
      if (f) {
        if (!path) path = pathFromFenceInfo(f[2], root);
        start = j;
        continue;
      }
      if (path) break;
      const p = cleanPath(l, root);
      if (!p) break;
      path = p; start = j;
    }
    return { path, start };
  }

  /**
   * Tolerant SEARCH/REPLACE parser. Accepts ``` fences around blocks, CRLF, 5-9 marker chars,
   * path lines with backticks / "File:" prefix / markdown bold, a path inside the fence info,
   * and a missing ">>>>>>> REPLACE" when the next block starts right away.
   */
  function scanBlocks(text, opts) {
    opts = opts || {};
    const root = opts.root || '';
    const lines = normEol(text).split('\n');
    const blocks = [];
    let i = 0, stop = 0, lastPath = null;
    while (i < lines.length) {
      const m = RE_SEARCH.exec(lines[i]);
      if (!m) { i++; continue; }
      const head = findHeader(lines, i, stop, root);
      let path = head.path || (m[1] ? cleanPath(m[1], root) : null);
      const rawPath = path;
      if (!path) path = lastPath || opts.defaultPath || null;
      const b = { path, rawPath, search: '', replace: '', incomplete: true, startLine: head.start, endLine: lines.length - 1 };
      const sLines = [], rLines = [];
      let j = i + 1, state = 'search';
      for (; j < lines.length; j++) {
        const l = lines[j];
        if (state === 'search') {
          if (RE_DIVIDER.test(l)) { state = 'replace'; continue; }
          if (RE_SEARCH.test(l)) break;                 // malformed: restart at the new marker
          sLines.push(l);
        } else {
          if (RE_REPLACE.test(l)) { b.incomplete = false; b.endLine = j; j++; break; }
          if (RE_SEARCH.test(l)) {                      // next block started without a REPLACE marker
            const nh = findHeader(lines, j, i + 1, root);
            rLines.length = Math.max(0, rLines.length - (j - nh.start));
            while (rLines.length && !rLines[rLines.length - 1].trim()) rLines.pop();
            if (rLines.length && /^\s*(`{3,}|~{3,})\s*$/.test(rLines[rLines.length - 1])) rLines.pop();
            b.incomplete = false; b.endLine = nh.start - 1; b.noEndMarker = true;
            break;
          }
          rLines.push(l);
        }
      }
      if (state === 'search' && j < lines.length && RE_SEARCH.test(lines[j])) { i = j; continue; }
      b.search = stripFencePair(sLines).join('\n');
      b.replace = stripFencePair(rLines).join('\n');
      if (!b.incomplete) {
        // swallow a closing fence right after the block
        let k = b.endLine + 1;
        while (k < lines.length && !lines[k].trim() && k <= b.endLine + 2) k++;
        if (k < lines.length && /^\s*(`{3,}|~{3,})\s*$/.test(lines[k]) && !b.noEndMarker) b.endLine = k;
      }
      b.raw = lines.slice(b.startLine, b.endLine + 1).join('\n');
      blocks.push(b);
      if (b.path) lastPath = b.path;
      stop = b.endLine + 1;
      i = b.noEndMarker ? b.endLine + 1 : Math.max(j, b.endLine + 1);
    }
    return { blocks, lines };
  }

  function parseEditBlocks(text, opts) { return scanBlocks(text, opts).blocks; }

  /** Split an answer into prose and edit-block segments (for rendering). */
  function splitEditText(text, opts) {
    const { blocks, lines } = scanBlocks(text, opts);
    const segments = [];
    let cur = 0;
    const pushText = (a, b) => {
      if (b <= a) return;
      let chunk = lines.slice(a, b);
      // drop orphan fence lines left next to removed blocks
      while (chunk.length && (!chunk[chunk.length - 1].trim() || RE_FENCE.test(chunk[chunk.length - 1]))) {
        if (RE_FENCE.test(chunk[chunk.length - 1]) && !/^\s*(`{3,}|~{3,})\s*\S*\s*$/.test(chunk[chunk.length - 1])) break;
        chunk.pop();
      }
      const t = chunk.join('\n');
      if (t.trim()) segments.push({ type: 'text', text: t });
    };
    blocks.forEach((b, idx) => {
      pushText(cur, b.startLine);
      segments.push({ type: 'edit', index: idx });
      cur = b.endLine + 1;
    });
    // leading orphan fence closer of the tail
    let tail = cur;
    while (blocks.length && tail < lines.length && (!lines[tail].trim() || /^\s*(`{3,}|~{3,})\s*$/.test(lines[tail]))) tail++;
    pushText(tail, lines.length);
    return { blocks, segments };
  }

  // ---------------------------------------------------------------- matching
  const leadWs = (s) => /^[ \t]*/.exec(s)[0];
  const squash = (s) => s.trim().replace(/\s+/g, ' ');

  function bigrams(s) {
    const m = new Map();
    for (let i = 0; i < s.length - 1; i++) { const g = s.substr(i, 2); m.set(g, (m.get(g) || 0) + 1); }
    return m;
  }
  function dice(a, b, ga, gb) {
    if (a === b) return 1;
    if (a.length < 2 || b.length < 2) return 0;
    ga = ga || bigrams(a); gb = gb || bigrams(b);
    let inter = 0;
    for (const [g, n] of ga) { const k = gb.get(g); if (k) inter += Math.min(n, k); }
    return (2 * inter) / (a.length - 1 + b.length - 1);
  }
  /** Line-level similarity of two equally long line lists (length-weighted mean of per-line Dice). */
  function similarity(aLines, bLines) {
    if (aLines.length !== bLines.length || !aLines.length) return 0;
    let num = 0, den = 0;
    for (let i = 0; i < aLines.length; i++) {
      const a = squash(aLines[i]), b = squash(bLines[i]);
      const w = Math.max(a.length, b.length) + 1;
      num += dice(a, b) * w; den += w;
    }
    return den ? num / den : 0;
  }

  function findSeq(L, S, eq) {
    const n = S.length;
    outer: for (let i = 0; i + n <= L.length; i++) {
      for (let k = 0; k < n; k++) if (!eq(L[i + k], S[k])) continue outer;
      return i;
    }
    return -1;
  }

  function reindent(R, fromIndent, toIndent) {
    if (fromIndent === toIndent) return R;
    if (toIndent.startsWith(fromIndent)) {
      const add = toIndent.slice(fromIndent.length);
      return R.map((l) => (l.trim() ? add + l : l));
    }
    if (fromIndent.startsWith(toIndent)) {
      const cut = fromIndent.slice(toIndent.length);
      return R.map((l) => (l.startsWith(cut) ? l.slice(cut.length) : l));
    }
    return R.map((l) => (l.startsWith(fromIndent) ? toIndent + l.slice(fromIndent.length) : l));
  }

  const FUZZY_MIN = 0.85;
  const FUZZY_MAX_WORK = 3e6;

  /** Locate S (lines) in L (lines). Returns {start, end, tier, score, indentFrom, indentTo} or null. */
  function locate(L, S) {
    const firstIdx = S.findIndex((l) => l.trim());
    if (firstIdx < 0) return null;
    let i = findSeq(L, S, (a, b) => a === b);
    if (i >= 0) return { start: i, end: i + S.length, tier: 'exact', score: 1 };
    i = findSeq(L, S, (a, b) => a.trimEnd() === b.trimEnd());
    if (i >= 0) return { start: i, end: i + S.length, tier: 'whitespace', score: 1 };
    i = findSeq(L, S, (a, b) => a.trim() === b.trim());
    if (i >= 0) return { start: i, end: i + S.length, tier: 'indent', score: 1, indentFrom: leadWs(S[firstIdx]), indentTo: leadWs(L[i + firstIdx]) };
    // ignore blank lines on both sides
    const NB = [];
    L.forEach((l, idx) => { if (l.trim()) NB.push(idx); });
    const SN = S.filter((l) => l.trim());
    const k = SN.length;
    const sq = SN.map(squash);
    const lsq = NB.map((idx) => squash(L[idx]));
    const firstNB = S[firstIdx];
    let w = findSeq(lsq, sq, (a, b) => a === b);
    if (w >= 0) {
      return { start: NB[w], end: NB[w + k - 1] + 1, tier: 'blank', score: 1, indentFrom: leadWs(firstNB), indentTo: leadWs(L[NB[w]]) };
    }
    // fuzzy window over non-blank lines
    if (!k || NB.length < k || NB.length * k > FUZZY_MAX_WORK) return null;
    const sg = sq.map(bigrams);
    const cache = new Array(lsq.length);
    const lg = (x) => cache[x] || (cache[x] = bigrams(lsq[x]));
    const weights = sq.map((s) => s.length + 1);
    let best = -1, bestScore = 0;
    for (let x = 0; x + k <= NB.length; x++) {
      let num = 0, den = 0, bail = false;
      for (let j = 0; j < k; j++) {
        const a = lsq[x + j], b = sq[j];
        const ww = Math.max(a.length + 1, weights[j]);
        num += (a === b ? 1 : dice(a, b, lg(x + j), sg[j])) * ww; den += ww;
        if (j === 0 && k > 2 && num / den < 0.4) { bail = true; break; }
      }
      if (bail) continue;
      const sc = num / den;
      if (sc > bestScore) { bestScore = sc; best = x; }
    }
    if (best < 0 || bestScore < FUZZY_MIN) return null;
    return { start: NB[best], end: NB[best + k - 1] + 1, tier: 'fuzzy', score: bestScore, indentFrom: leadWs(firstNB), indentTo: leadWs(L[NB[best]]) };
  }

  function trimBlankEdges(lines) {
    let a = 0, b = lines.length;
    while (a < b && !lines[a].trim()) a++;
    while (b > a && !lines[b - 1].trim()) b--;
    return lines.slice(a, b);
  }

  function applyOne(text, block) {
    const search = normEol(block.search), replace = normEol(block.replace);
    if (!search.trim()) {
      const body = replace.replace(/\n*$/, '');
      if (!text.trim()) return { ok: true, tier: 'create', content: body ? body + '\n' : '', startLine: 1, endLine: body.split('\n').length };
      const sep = text.endsWith('\n') ? '' : '\n';
      const start = text.split('\n').length - (text.endsWith('\n') ? 1 : 0) + 1;
      return { ok: true, tier: 'append', content: text + sep + (body ? body + '\n' : ''), startLine: start, endLine: start + body.split('\n').length - 1 };
    }
    const L = text.split('\n');
    const variants = [];
    const S0 = search.split('\n'), R0 = replace === '' ? [] : replace.split('\n');
    variants.push([S0, R0]);
    const S1 = trimBlankEdges(S0);
    if (S1.length !== S0.length) variants.push([S1, trimBlankEdges(R0)]);
    const S2 = stripFencePair(S1), R2 = stripFencePair(trimBlankEdges(R0));
    if (S2.length !== S1.length) variants.push([trimBlankEdges(S2), trimBlankEdges(R2)]);
    // a line-number prefix copied from somewhere ("12: code", "12 | code")
    if (S1.every((l) => !l.trim() || /^\s*\d+\s*[:|]\s?/.test(l))) {
      const un = (arr) => arr.map((l) => l.replace(/^\s*\d+\s*[:|]\s?/, ''));
      variants.push([un(S1), un(trimBlankEdges(R0))]);
    }
    // strict tiers first over all variants, fuzzy last
    let hit = null, R = null;
    for (const [S, RR] of variants) {
      const loc = locate(L, S);
      if (loc && loc.tier !== 'fuzzy') { hit = loc; R = RR; break; }
      if (loc && (!hit || loc.score > hit.score)) { hit = loc; R = RR; }
    }
    if (!hit) return { ok: false, error: 'not_found' };
    let RR = R;
    if (hit.indentFrom != null && hit.indentTo != null) RR = reindent(R, hit.indentFrom, hit.indentTo);
    const out = L.slice(0, hit.start).concat(RR, L.slice(hit.end));
    return {
      ok: true, tier: hit.tier, fuzzy: hit.tier === 'fuzzy', score: Math.round(hit.score * 1000) / 1000,
      content: out.join('\n'), startLine: hit.start + 1, endLine: hit.start + Math.max(RR.length, 1),
    };
  }

  /** Apply blocks (for ONE file) sequentially. Content is normalised to LF. */
  function applyBlocks(content, blocks) {
    let text = normEol(content);
    const results = [];
    let applied = 0, failed = 0;
    for (const b of blocks || []) {
      if (b.incomplete) { results.push({ ok: false, error: 'incomplete' }); failed++; continue; }
      const r = applyOne(text, b);
      if (r.ok) { text = r.content; applied++; } else failed++;
      const { content: _c, ...rest } = r; // eslint-disable-line no-unused-vars
      results.push(rest);
    }
    return { content: text, results, applied, failed };
  }

  // ---------------------------------------------------------------- diff
  function lineOps(a, b) {
    let p = 0;
    while (p < a.length && p < b.length && a[p] === b[p]) p++;
    let s = 0;
    while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
    const A = a.slice(p, a.length - s), B = b.slice(p, b.length - s);
    const ops = [];
    for (let i = 0; i < p; i++) ops.push(['ctx', a[i]]);
    const n = A.length, m = B.length;
    if (n && m && n * m <= 2e6) {
      const W = m + 1;
      const dp = new Uint32Array((n + 1) * W);
      for (let i = n - 1; i >= 0; i--) {
        for (let j = m - 1; j >= 0; j--) {
          dp[i * W + j] = A[i] === B[j] ? dp[(i + 1) * W + j + 1] + 1 : Math.max(dp[(i + 1) * W + j], dp[i * W + j + 1]);
        }
      }
      let i = 0, j = 0;
      while (i < n && j < m) {
        if (A[i] === B[j]) { ops.push(['ctx', A[i]]); i++; j++; }
        else if (dp[(i + 1) * W + j] >= dp[i * W + j + 1]) ops.push(['del', A[i++]]);
        else ops.push(['add', B[j++]]);
      }
      while (i < n) ops.push(['del', A[i++]]);
      while (j < m) ops.push(['add', B[j++]]);
    } else {
      for (const l of A) ops.push(['del', l]);
      for (const l of B) ops.push(['add', l]);
    }
    for (let i = a.length - s; i < a.length; i++) ops.push(['ctx', a[i]]);
    return ops;
  }

  function unifiedDiff(oldText, newText, ctx) {
    ctx = ctx == null ? 3 : ctx;
    const a = normEol(oldText).split('\n'), b = normEol(newText).split('\n');
    if (a.length && a[a.length - 1] === '' && b.length && b[b.length - 1] === '') { a.pop(); b.pop(); }
    const ops = lineOps(a, b);
    const out = [];
    let added = 0, removed = 0;
    const changed = [];
    ops.forEach((o, i) => { if (o[0] !== 'ctx') changed.push(i); if (o[0] === 'add') added++; if (o[0] === 'del') removed++; });
    if (!changed.length) return { lines: out, added, removed };
    // group change indices into hunks
    const hunks = [];
    let hs = Math.max(0, changed[0] - ctx), he = Math.min(ops.length, changed[0] + ctx + 1);
    for (let c = 1; c < changed.length; c++) {
      const i = changed[c];
      if (i - ctx <= he) he = Math.min(ops.length, i + ctx + 1);
      else { hunks.push([hs, he]); hs = Math.max(0, i - ctx); he = Math.min(ops.length, i + ctx + 1); }
    }
    hunks.push([hs, he]);
    // line numbers
    const oldNo = [], newNo = [];
    let on = 1, nn = 1;
    for (const o of ops) { oldNo.push(on); newNo.push(nn); if (o[0] !== 'add') on++; if (o[0] !== 'del') nn++; }
    for (const [s, e] of hunks) {
      let oc = 0, nc = 0;
      for (let i = s; i < e; i++) { if (ops[i][0] !== 'add') oc++; if (ops[i][0] !== 'del') nc++; }
      out.push({ type: 'hunk', text: '@@ -' + (oc ? oldNo[s] : oldNo[s] - 1) + ',' + oc + ' +' + (nc ? newNo[s] : newNo[s] - 1) + ',' + nc + ' @@' });
      for (let i = s; i < e; i++) {
        const [t, l] = ops[i];
        out.push({ type: t, text: (t === 'add' ? '+' : t === 'del' ? '-' : ' ') + l });
      }
    }
    return { lines: out, added, removed };
  }

  function computeChange(a, b) {
    if (a === b) return null;
    let p = 0;
    const max = Math.min(a.length, b.length);
    while (p < max && a.charCodeAt(p) === b.charCodeAt(p)) p++;
    let s = 0;
    while (s < a.length - p && s < b.length - p && a.charCodeAt(a.length - 1 - s) === b.charCodeAt(b.length - 1 - s)) s++;
    return { from: p, to: a.length - s, insert: b.slice(p, b.length - s) };
  }

  function extractThink(raw) {
    let think = '', answer = String(raw || '');
    const o = answer.indexOf('<think>');
    if (o >= 0) {
      const c = answer.indexOf('</think>', o);
      think += c < 0 ? answer.slice(o + 7) : answer.slice(o + 7, c);
      answer = answer.slice(0, o) + (c < 0 ? '' : answer.slice(c + 8));
    }
    const r = answer.indexOf('[Reasoning]');
    if (r >= 0) {
      const a = answer.indexOf('[Answer]', r);
      think += (think ? '\n' : '') + (a < 0 ? answer.slice(r + 11) : answer.slice(r + 11, a));
      answer = answer.slice(0, r) + (a < 0 ? '' : answer.slice(a + 8));
    } else answer = answer.replace(/^\s*\[Answer\]\s*/, '');
    return { think, answer };
  }

  const pure = { parseEditBlocks, splitEditText, applyBlocks, unifiedDiff, computeChange, similarity, cleanPath, extractThink };
  window.__ideAI = Object.assign(window.__ideAI || {}, pure);

  // ================================================================= UI
  const IDE = window.__ide;
  const $ = (id) => document.getElementById(id);
  if (!IDE || !IDE.util || !$('ai-panel')) return;
  const { h, api, toast, toastErr, copyText, baseName, normRel, lsGet, lsSet, qs } = IDE.util;

  const MAX_CTX_FILES = 8;
  const MAX_SEND_CHARS = 400000;
  const ai = {
    open: false, busy: false, ctrl: null, switching: false,
    mode: lsGet('aiMode', 'ask') === 'edit' ? 'edit' : 'ask',
    ctx: [], models: [], current: '', history: [], statusSeq: 0,
    tools: lsGet('aiTools', false) === true,
    prompts: [], activePrompt: '', promptMode: 'append', promptId: null, promptsOk: false,
  };

  // ---------------------------------------------------------------- panel
  function toggle(force) {
    ai.open = force != null ? !!force : !ai.open;
    $('ai-panel').hidden = !ai.open;
    $('ai-resizer').hidden = !ai.open;
    $('act-ai').setAttribute('aria-pressed', String(ai.open));
    lsSet('aiOpen', ai.open);
    if (ai.open) { $('ai-input').focus(); renderCtx(); refreshModels(); refreshPrompts(); }
    else IDE.focusEditor();
  }
  function aiList() {
    const l = $('ai-list');
    const empty = l.querySelector('.ai-empty');
    if (empty) empty.remove();
    return l;
  }
  function scrollAiBottom() {
    const l = $('ai-list');
    if (l.scrollHeight - l.scrollTop - l.clientHeight < 160) l.scrollTop = l.scrollHeight;
  }
  function aiNotice(text, before) {
    const el = h('div', { class: 'ai-msg notice' }, text);
    if (before && before.parentNode) before.parentNode.insertBefore(el, before); else aiList().appendChild(el);
    scrollAiBottom();
  }

  // ---------------------------------------------------------------- mode
  function setMode(m) {
    ai.mode = m === 'edit' ? 'edit' : 'ask';
    lsSet('aiMode', ai.mode);
    renderMode();
  }
  function renderMode() {
    const edit = ai.mode === 'edit';
    $('ai-mode-ask').setAttribute('aria-pressed', String(!edit));
    $('ai-mode-edit').setAttribute('aria-pressed', String(edit));
    $('ai-input').placeholder = edit
      ? '描述要做的修改（例如：给 foo 加上类型注解），Enter 发送'
      : '输入问题，Enter 发送，Shift+Enter 换行';
    $('ai-add-ctx2').title = edit ? '把当前文件加入“编辑”模式的上下文文件列表' : '把当前文件挂载到对话上下文 (/api/fs/mount)';
    $('ai-panel').classList.toggle('mode-edit', edit);
    $('ai-tools').hidden = edit;   // tools only in Ask mode
    renderPrompts();
    renderCtx();
  }

  // ---------------------------------------------------------------- tools toggle + prompt preset
  function setTools(on) {
    ai.tools = !!on;
    lsSet('aiTools', ai.tools);
    $('ai-tools').setAttribute('aria-pressed', String(ai.tools));
  }
  function promptById(id) { return ai.prompts.find((p) => p.id === id) || null; }
  function renderPrompts() {
    const wrap = $('ai-prompt-wrap'), sel = $('ai-prompt'), name = $('ai-prompt-name');
    if (!wrap) return;
    wrap.hidden = !ai.promptsOk || !ai.prompts.length;
    if (wrap.hidden) return;
    const edit = ai.mode === 'edit';
    sel.hidden = edit;
    name.hidden = !edit;
    const active = promptById(ai.activePrompt);
    name.textContent = '提示词：' + (active ? (active.name || active.id) : '默认') + '（追加）';
    if (ai.promptId && !promptById(ai.promptId)) ai.promptId = null;
    const want = ai.promptId || ai.activePrompt;
    sel.textContent = '';
    for (const p of ai.prompts) sel.appendChild(h('option', { value: p.id }, (p.name || p.id) + (p.id === ai.activePrompt ? '（默认）' : '')));
    sel.value = want && promptById(want) ? want : (ai.prompts[0] && ai.prompts[0].id) || '';
    const cur = promptById(sel.value);
    sel.title = '系统提示词：' + (cur ? (cur.name || cur.id) : '');
  }
  async function refreshPrompts() {
    try {
      const d = await api('GET', '/api/agent/prompts');
      ai.prompts = Array.isArray(d.prompts) ? d.prompts.filter((p) => p && p.id) : [];
      ai.activePrompt = String(d.active_prompt_id || '');
      ai.promptMode = d.prompt_mode === 'replace' ? 'replace' : 'append';
      ai.promptsOk = true;
    } catch (e) { ai.promptsOk = false; }   // older backend: hide the selector
    renderPrompts();
  }
  function currentPromptId() {
    if (!ai.promptsOk) return undefined;
    const id = ai.promptId || ai.activePrompt;
    return id && promptById(id) ? id : undefined;
  }

  // ---------------------------------------------------------------- context chips (Edit mode)
  function renderCtx() {
    const box = $('ai-ctx');
    if (!box) return;
    box.hidden = ai.mode !== 'edit';
    if (box.hidden) return;
    box.textContent = '';
    const act = IDE.activeFile();
    box.appendChild(h('span', { class: 'ai-ctx-label small muted' }, '上下文文件'));
    if (act) box.appendChild(h('span', { class: 'ai-chip current', title: act.path + '（当前文件，自动包含）' }, h('span', { class: 'ai-chip-name' }, baseName(act.path)), h('span', { class: 'muted' }, '当前')));
    for (const p of ai.ctx) {
      if (act && act.path === p) continue;
      box.appendChild(h('span', { class: 'ai-chip', title: p },
        h('span', { class: 'ai-chip-name' }, baseName(p)),
        h('button', { class: 'ai-chip-x', type: 'button', 'aria-label': '移除 ' + p, onclick: () => { ai.ctx = ai.ctx.filter((x) => x !== p); renderCtx(); } }, '×')));
    }
    if (!act && !ai.ctx.length) box.appendChild(h('span', { class: 'small muted' }, '（无 — 可让 AI 新建文件）'));
  }
  let ctxRaf = 0;
  const tabsEl = $('tabs');
  if (tabsEl && window.MutationObserver) {
    new MutationObserver(() => {
      if (ctxRaf || !ai.open || ai.mode !== 'edit') return;
      ctxRaf = requestAnimationFrame(() => { ctxRaf = 0; renderCtx(); });
    }).observe(tabsEl, { childList: true });
  }

  function addContext(path) {
    if (!path) { const f = IDE.activeFile(); path = f ? f.path : null; }
    if (!path) { toast('没有打开的文件', 'error'); return; }
    if (ai.mode !== 'edit') { mountToAI(path); return; }
    if (!ai.ctx.includes(path)) {
      if (ai.ctx.length >= MAX_CTX_FILES) { toast('上下文文件最多 ' + MAX_CTX_FILES + ' 个', 'error'); return; }
      ai.ctx.push(path);
    }
    renderCtx();
    toast('已加入编辑上下文：' + path, 'ok');
  }
  async function mountToAI(path) {
    const root = IDE.workspace && IDE.workspace.root;
    if (!root) { toast('工作区尚未加载', 'error'); return; }
    const sep = root.indexOf('\\') >= 0 ? '\\' : '/';
    const abs = root.replace(/[\\/]+$/, '') + sep + path.split('/').join(sep);
    try {
      const d = await api('POST', '/api/fs/mount', { path: abs });
      toast('已加入 AI 上下文：' + (d.filename || path), 'ok');
      aiNotice('📎 已挂载 ' + (d.filename || path) + (d.lines != null ? '（' + d.lines + ' 行）' : ''));
    } catch (e) { toastErr(e, '加入 AI 上下文失败'); }
  }

  // ---------------------------------------------------------------- model switcher
  function modelLabel(m) { return (m.name || m.id) + (m.tag ? ' · ' + m.tag : ''); }
  function renderModels() {
    const sel = $('ai-model');
    sel.textContent = '';
    const list = ai.models.slice();
    if (ai.current && !list.some((m) => m.id === ai.current)) list.unshift({ id: ai.current, name: ai.current });
    if (!list.length) sel.appendChild(h('option', { value: '' }, '模型未知'));
    for (const m of list) {
      const o = h('option', { value: m.id, title: [m.desc, m.speed, m.vram].filter(Boolean).join(' · ') }, modelLabel(m));
      sel.appendChild(o);
    }
    sel.value = ai.current || '';
    const cur = list.find((m) => m.id === ai.current);
    sel.title = cur ? '当前模型：' + modelLabel(cur) + (cur.vram ? '\n' + cur.vram : '') : '切换模型';
    sel.disabled = ai.switching || !list.length;
  }
  async function refreshModels() {
    if (ai.switching) return;
    const seq = ++ai.statusSeq;
    let st;
    try { st = await api('GET', '/api/status'); } catch (e) {
      if (seq === ai.statusSeq && !ai.models.length) { ai.current = ''; renderModels(); }
      return;
    }
    if (seq !== ai.statusSeq || ai.switching) return;
    ai.models = Array.isArray(st.available_models) ? st.available_models.filter((m) => m && m.id) : [];
    ai.current = String(st.llm_model || '');
    renderModels();
  }
  function setSwitching(b) {
    ai.switching = b;
    $('ai-model').disabled = b;
    $('ai-model-status').hidden = !b;
    updateSendState();
  }
  async function onModelChange() {
    const sel = $('ai-model');
    const want = sel.value, prev = ai.current;
    if (!want || want === prev || ai.switching) { sel.value = prev; return; }
    setSwitching(true);
    ai.statusSeq++;
    try {
      const r = await api('POST', '/api/model', { model: want });
      ai.current = String(r.current_model || want);
      toast(r.message || ('模型已切换为 ' + want), 'ok');
    } catch (e) {
      ai.current = prev;
      toast('模型切换失败：' + ((e && e.message) || e), 'error');
    } finally {
      setSwitching(false);
      renderModels();
    }
  }

  // ---------------------------------------------------------------- send state
  function updateSendState() {
    $('ai-send').hidden = ai.busy;
    $('ai-stop').hidden = !ai.busy;
    $('ai-send').disabled = ai.switching;
    $('ai-send').title = ai.switching ? '模型切换中，请稍候' : '';
  }
  function setBusy(b) { ai.busy = b; updateSendState(); }

  // ---------------------------------------------------------------- rendering (safe)
  function mdInline(s) {
    // SAFE: escape everything first, then add a tiny whitelist of tags.
    let html = IDE.util.escapeHtml(s);
    html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/^#{1,6} (.+)$/gm, '<strong>$1</strong>');
    html = html.replace(/\n/g, '<br>');
    const div = document.createElement('div');
    div.className = 'md';
    div.innerHTML = html;
    return div;
  }
  function codeBlock(lang, code) {
    return h('div', { class: 'codeblock' },
      h('div', { class: 'codeblock-bar' },
        h('span', { class: 'grow' }, lang || 'code'),
        h('button', { class: 'btn tiny', type: 'button', onclick: () => copyText(code).then((ok) => toast(ok ? '已复制' : '复制失败', ok ? 'ok' : 'error')) }, '复制'),
        h('button', { class: 'btn tiny', type: 'button', onclick: () => applyToEditor(code) }, '应用到编辑器')),
      h('pre', null, h('code', { text: code })));
  }
  function renderMd(text) {
    const frag = document.createDocumentFragment();
    const pushText = (s) => {
      s = s.replace(/^\n+/, '').replace(/\n+$/, '');
      if (s) frag.appendChild(mdInline(s));
    };
    let i = 0;
    while (i < text.length) {
      const start = text.indexOf('```', i);
      if (start < 0) { pushText(text.slice(i)); break; }
      if (start > i) pushText(text.slice(i, start));
      const nl = text.indexOf('\n', start + 3);
      if (nl < 0) { frag.appendChild(codeBlock(text.slice(start + 3).trim(), '')); break; }
      const lang = text.slice(start + 3, nl).trim();
      const end = text.indexOf('\n```', nl);
      if (end < 0) { frag.appendChild(codeBlock(lang, text.slice(nl + 1))); break; }
      frag.appendChild(codeBlock(lang, text.slice(nl + 1, end)));
      i = end + 4;
    }
    return frag;
  }
  /** Render prose; SEARCH/REPLACE blocks become small placeholders (cards are added when the stream ends). */
  function renderProse(el, text, opts) {
    el.textContent = '';
    const { blocks, segments } = splitEditText(text, opts);
    if (!blocks.length) { el.appendChild(renderMd(text)); return blocks; }
    for (const s of segments) {
      if (s.type === 'text') el.appendChild(renderMd(s.text));
      else {
        const b = blocks[s.index];
        el.appendChild(h('div', { class: 'ai-edit-ph small' }, '✏️ ', h('span', { class: 'ai-edit-ph-path' }, b.path || '（未指定文件）'),
          h('span', { class: 'muted' }, b.incomplete ? ' 生成中…' : ' · 修改建议见下方')));
      }
    }
    return blocks;
  }
  function makeAnswerEl() {
    const think = h('details', { class: 'ai-think small muted', hidden: true }, h('summary', null, '思考过程'), h('div', { class: 'ai-think-body' }));
    const tools = h('div', { class: 'ai-tools' });
    const prose = h('div', { class: 'ai-prose' }, h('span', { class: 'muted' }, '思考中…'));
    const cards = h('div', { class: 'ai-edits' });
    const foot = h('div', { class: 'ai-foot' });
    const body = h('div', { class: 'ai-body' }, think, tools, prose, cards, foot);
    return { body, think, tools, prose, cards, foot };
  }
  function setThink(A, text) {
    const t = String(text || '').trim();
    A.think.hidden = !t;
    const b = A.think.querySelector('.ai-think-body');
    if (b.textContent !== t) b.textContent = t;
  }
  function statsLine(stats) {
    const parts = [];
    if (stats.model) parts.push(String(stats.model).split('/').pop());
    if (stats.token_count != null) parts.push(stats.token_count + ' tok');
    if (stats.prompt_tokens_est != null) parts.push('提示 ~' + stats.prompt_tokens_est + ' tok');
    if (stats.tokens_per_sec != null) parts.push(Number(stats.tokens_per_sec).toFixed(1) + ' tok/s');
    if (stats.elapsed_sec != null) parts.push(Number(stats.elapsed_sec).toFixed(1) + ' s');
    if (Number(stats.tool_calls) > 0) parts.push('工具调用 ' + Number(stats.tool_calls) + ' 次');
    return parts.join(' · ');
  }

  // ---------------------------------------------------------------- tool-call cards (Ask mode)
  // Tool names / arguments / results are untrusted: text nodes only (h() never parses HTML).
  const TOOL_STATUS = { running: '运行中', pending_approval: '等待确认', done: '完成', denied: '已拒绝', error: '失败', cancelled: '已取消' };
  function prettyArgs(v) {
    if (v == null || v === '') return '{}';
    let o = v;
    if (typeof v === 'string') { try { o = JSON.parse(v); } catch (e) { return v; } }
    try { return JSON.stringify(o, null, 2); } catch (e) { return String(v); }
  }
  function toolSec(kind, label, text, max) {
    const s = String(text == null ? '' : text);
    const d = h('details', { class: 'ai-tool-sec ai-tool-' + kind }, h('summary', null, label), h('pre', { text: s.slice(0, max) }));
    if (s.length > max) d.appendChild(h('div', { class: 'ai-tool-trunc', text: '…已省略 ' + (s.length - max) + ' 个字符' }));
    return d;
  }
  function findToolCard(host, id) {
    return Array.from(host.querySelectorAll('.ai-tool-card')).find((c) => c.dataset.callId === id) || null;
  }
  function setToolStatus(card, st) {
    card.dataset.status = st;
    card.querySelector('.ai-tool-status').textContent = TOOL_STATUS[st] || st;
  }
  function newToolCard(id, name) {
    return h('div', { class: 'ai-tool-card', dataset: { callId: id }, role: 'group', 'aria-label': '工具调用：' + name },
      h('div', { class: 'ai-tool-head' },
        h('span', { 'aria-hidden': 'true' }, '🔧'),
        h('span', { class: 'ai-tool-title', title: name }, '调用 ', h('code', { class: 'ai-tool-name', text: name })),
        h('span', { class: 'ai-tool-ms' }),
        h('span', { class: 'ai-badge ai-tool-status', 'aria-live': 'polite' })));
  }
  function onToolCall(A, tc) {
    if (!tc || typeof tc !== 'object') return;
    const id = String(tc.id || ('call-' + (A.tools.children.length + 1)));
    const name = String(tc.name || '工具');
    let card = findToolCard(A.tools, id);
    if (!card) {
      card = newToolCard(id, name);
      card.appendChild(toolSec('args', '参数', prettyArgs(tc.arguments), 6000));
      A.tools.appendChild(card);
      A.toolCount = (A.toolCount || 0) + 1;
    }
    if (card._final) return;
    if (tc.status === 'pending_approval') {
      setToolStatus(card, 'pending_approval');
      if (!card.querySelector('.ai-tool-approval')) card.appendChild(approvalEl(card, id, tc));
    } else setToolStatus(card, 'running');
    scrollAiBottom();
  }
  function approvalEl(card, id, tc) {
    const mk = (label, cls, decision, remember) => h('button', { class: 'btn tiny ' + cls, type: 'button', onclick: () => decide(card, id, decision, remember) }, label);
    return h('div', { class: 'ai-tool-approval' },
      h('span', { class: 'ai-tool-approval-text' }, tc.read_only === false ? '这一步可能会修改文件或执行命令，需要你确认。' : '需要你确认后才会运行。'),
      h('span', { class: 'ai-tool-approval-btns' },
        mk('允许', 'primary ai-tool-allow', 'allow', false),
        mk('始终允许', 'ai-tool-always', 'allow', true),
        mk('拒绝', 'ai-tool-deny', 'deny', false)));
  }
  async function decide(card, id, decision, remember) {
    const box = card.querySelector('.ai-tool-approval');
    const btns = box ? Array.from(box.querySelectorAll('button')) : [];
    const text = box && box.querySelector('.ai-tool-approval-text');
    btns.forEach((b) => { b.disabled = true; });
    card._pendingDecision = decision;   // tool_result may arrive before the approve response
    try {
      await api('POST', '/api/agent/approve', { call_id: id, decision, remember });
      card._decided = true;
      box.classList.add('decided');
      if (decision === 'deny') { card._denied = true; setToolStatus(card, 'denied'); if (text) text.textContent = '已拒绝，不会执行。'; }
      else { if (card.dataset.status === 'pending_approval') setToolStatus(card, 'running'); if (text) text.textContent = remember ? '已允许（以后不再询问）。' : '已允许。'; }
    } catch (e) {
      if (e.status === 404) {
        card._final = true; box.classList.add('decided'); setToolStatus(card, 'cancelled');
        if (text) text.textContent = '这个请求已失效（可能已超时）。';
      } else { card._pendingDecision = null; btns.forEach((b) => { b.disabled = false; }); toastErr(e, '提交确认失败'); }
    }
  }
  function onToolResult(A, tr) {
    if (!tr || typeof tr !== 'object') return;
    const id = String(tr.id || '');
    let card = id ? findToolCard(A.tools, id) : null;
    if (!card) { card = newToolCard(id || ('call-' + Date.now()), String(tr.name || '工具')); A.tools.appendChild(card); A.toolCount = (A.toolCount || 0) + 1; }
    const ok = tr.ok !== false;
    card._final = true;
    if (card._pendingDecision === 'deny') card._denied = true;
    setToolStatus(card, card._denied ? 'denied' : (ok ? 'done' : 'error'));
    const box = card.querySelector('.ai-tool-approval');
    if (box && !card._decided && !card._pendingDecision) { box.querySelectorAll('button').forEach((b) => { b.disabled = true; }); box.classList.add('decided'); }
    const ms = Number(tr.elapsed_ms);
    card.querySelector('.ai-tool-ms').textContent = Number.isFinite(ms) && ms >= 0 ? (ms >= 1000 ? (ms / 1000).toFixed(1) + ' s' : Math.round(ms) + ' ms') : '';
    const old = card.querySelector('.ai-tool-result');
    if (old) old.remove();
    const sec = toolSec('result', ok ? '结果' : (card._denied ? '说明' : '错误信息'), tr.content, 4000);
    if (!ok && !card._denied) sec.open = true;
    card.appendChild(sec);
    scrollAiBottom();
  }
  function finalizeTools(A, aborted) {
    for (const card of A.tools.querySelectorAll('.ai-tool-card')) {
      const st = card.dataset.status;
      if (st !== 'pending_approval' && !(aborted && st === 'running')) continue;
      card._final = true;
      setToolStatus(card, 'cancelled');
      const box = card.querySelector('.ai-tool-approval');
      if (box) {
        box.querySelectorAll('button').forEach((b) => { b.disabled = true; });
        box.classList.add('decided');
        const t = box.querySelector('.ai-tool-approval-text');
        if (t && st === 'pending_approval') t.textContent = '对话已结束，这一步没有执行。';
      }
    }
  }

  // ---------------------------------------------------------------- SSE
  async function streamSSE(url, body, signal, onEvent) {
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, signal, body: JSON.stringify(body) });
    if (!res.ok) {
      let d = '';
      try { d = (await res.json()).detail; } catch (e) { /* ignore */ }
      if (Array.isArray(d)) d = d.map((x) => (x && x.msg) || JSON.stringify(x)).join('; ');
      throw new Error(typeof d === 'string' && d ? d : 'HTTP ' + res.status);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder('utf-8');
    let buf = '';
    const handle = (line) => {
      line = line.replace(/\r$/, '');
      if (!line.startsWith('data:')) return;
      const payload = line.slice(5).trim();
      if (!payload || payload === '[DONE]') return;
      let p;
      try { p = JSON.parse(payload); } catch (e) { return; }
      if (p && typeof p === 'object') onEvent(p);
    };
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) { handle(buf.slice(0, nl)); buf = buf.slice(nl + 1); }
    }
    if (buf) handle(buf);
  }

  // ---------------------------------------------------------------- send
  function send() {
    const inp = $('ai-input');
    const text = inp.value.trim();
    if (!text || ai.busy) return;
    if (ai.switching) { toast('模型切换中，请稍候', 'error'); return; }
    inp.value = '';
    if (ai.mode === 'edit') sendEdit(text); else sendAsk(text);
  }

  async function sendAsk(text) {
    let msg = text;
    const sel = $('ai-with-sel').checked ? IDE.selection() : null;
    if (sel) msg += '\n\n（来自文件 ' + sel.path + ' 的选中代码）\n```' + (sel.lang === 'plaintext' ? '' : sel.lang) + '\n' + sel.text + '\n```';
    const act = IDE.activeFile();
    const list = aiList();
    list.appendChild(h('div', { class: 'ai-msg user' }, h('span', { class: 'role' }, '你' + (sel ? ' · 附带 ' + baseName(sel.path) + ' 选中代码' : '')), text));
    const A = makeAnswerEl();
    const msgEl = h('div', { class: 'ai-msg ai' }, h('span', { class: 'role' }, 'AI'), A.body);
    list.appendChild(msgEl);
    list.scrollTop = list.scrollHeight;
    ai.ctrl = new AbortController();
    setBusy(true);
    let acc = '', raf = 0, stats = null, errText = '', stopped = false, noticed = false;
    const render = () => {
      const { think, answer } = extractThink(acc);
      setThink(A, think);
      if (answer.trim()) renderProse(A.prose, answer);
      else A.prose.textContent = '';
      if (!answer.trim() && !think.trim()) A.prose.appendChild(h('span', { class: 'muted' }, '思考中…'));
    };
    const schedule = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); scrollAiBottom(); }); };
    try {
      const reqBody = { message: msg, stream: true, tools_enabled: ai.tools };
      const pid = currentPromptId();
      if (pid) reqBody.prompt_id = pid;
      await streamSSE('/api/chat', reqBody, ai.ctrl.signal, (p) => {
        if (p.token) { acc += p.token; schedule(); }
        if (p.tool_call) onToolCall(A, p.tool_call);
        if (p.tool_result) onToolResult(A, p.tool_result);
        if (p.notice) aiNotice('ℹ ' + String(p.notice), msgEl);
        if (p.search_results && p.search_results.length && !noticed) { noticed = true; aiNotice('🔍 已联网检索 ' + p.search_results.length + ' 条结果', msgEl); }
        if (p.error) errText += String(p.error);
        if (p.done) stats = p.stats || null;
      });
    } catch (e) {
      if (e.name === 'AbortError') stopped = true; else errText = errText || ('请求失败：' + e.message);
    } finally {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      ai.ctrl = null;
      finalizeTools(A, stopped);
      const { answer } = extractThink(acc);
      if (acc) render(); else A.prose.textContent = stopped || errText ? '' : '（没有收到回复）';
      if (errText) A.foot.appendChild(h('div', { class: 'err' }, '⚠ ' + errText));
      if (stopped) A.foot.appendChild(h('div', { class: 'stats' }, '⏹ 已停止生成'));
      if (stats && stats.tool_calls == null && A.toolCount) stats.tool_calls = A.toolCount;
      if (stats) { const s = statsLine(stats); if (s) A.foot.appendChild(h('div', { class: 'stats' }, s)); }
      const blocks = answer ? parseEditBlocks(answer, { defaultPath: act ? act.path : null, root: wsRoot() }) : [];
      setBusy(false);
      if (blocks.length) await renderEditCards(A.cards, blocks, act ? [act.path] : []);
      scrollAiBottom();
    }
  }

  async function collectFiles() {
    const act = IDE.activeFile();
    const paths = [];
    if (act && act.editable) paths.push(act.path);
    for (const p of ai.ctx) if (!paths.includes(p)) paths.push(p);
    const files = [], skipped = [];
    for (const p of paths) {
      let content = IDE.fileText(p);
      if (content == null) {
        try {
          const d = await api('GET', '/api/ide/file?' + qs({ path: p }));
          if (d.content == null) { skipped.push(p); continue; }
          content = normEol(d.content);
        } catch (e) { skipped.push(p); continue; }
      }
      if (content.length > MAX_SEND_CHARS) content = content.slice(0, MAX_SEND_CHARS);
      files.push({ path: p, content });
    }
    return { files, skipped };
  }

  async function sendEdit(text) {
    const sel = $('ai-with-sel').checked ? IDE.selection() : null;
    const list = aiList();
    const userEl = h('div', { class: 'ai-msg user' }, h('span', { class: 'role' }, '你 · 编辑'), text);
    list.appendChild(userEl);
    const A = makeAnswerEl();
    const msgEl = h('div', { class: 'ai-msg ai edit' }, h('span', { class: 'role' }, 'AI · 编辑'), A.body);
    list.appendChild(msgEl);
    list.scrollTop = list.scrollHeight;
    ai.ctrl = new AbortController();
    setBusy(true);
    let acc = '', reasoning = '', raf = 0, stats = null, errText = '', stopped = false;
    let files = [];
    const render = () => {
      const { think, answer } = extractThink(acc);
      setThink(A, reasoning + (think ? (reasoning ? '\n' : '') + think : ''));
      if (answer.trim()) renderProse(A.prose, answer, { defaultPath: files[0] && files[0].path, root: wsRoot() });
      else { A.prose.textContent = ''; A.prose.appendChild(h('span', { class: 'muted' }, reasoning ? '思考中…' : '生成中…')); }
    };
    const schedule = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); scrollAiBottom(); }); };
    try {
      const c = await collectFiles();
      files = c.files;
      const role = userEl.querySelector('.role');
      role.textContent = '你 · 编辑 · ' + (files.length ? files.map((f) => baseName(f.path)).join('、') : '无文件') + (sel ? ' · 附带选中代码' : '');
      if (c.skipped.length) aiNotice('⚠ 以下文件无法作为文本发送，已跳过：' + c.skipped.join('、'), msgEl);
      const body = { instruction: text, files, history: ai.history.slice(-4) };
      if (sel) body.selection = { path: sel.path, from_line: sel.from_line, to_line: sel.to_line, text: sel.text };
      await streamSSE('/api/ide/ai/edit', body, ai.ctrl.signal, (p) => {
        if (p.context) {
          const cx = p.context, parts = [];
          for (const t of cx.truncated || []) parts.push(t.path + ' 仅发送第 ' + t.from_line + '-' + t.to_line + ' 行（共 ' + t.total_lines + ' 行）');
          for (const o of cx.omitted || []) parts.push(o + ' 未发送');
          if (parts.length) aiNotice('⚠ 上下文长度有限：' + parts.join('；'), msgEl);
        }
        if (p.reasoning) { reasoning += p.reasoning; schedule(); }
        if (p.token) { acc += p.token; schedule(); }
        if (p.error) errText += String(p.error);
        if (p.done) stats = p.stats || null;
      });
    } catch (e) {
      if (e.name === 'AbortError') stopped = true; else errText = errText || ('请求失败：' + e.message);
    } finally {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      ai.ctrl = null;
      const { answer } = extractThink(acc);
      if (acc || reasoning) render(); else A.prose.textContent = stopped || errText ? '' : '（没有收到回复）';
      if (errText) A.foot.appendChild(h('div', { class: 'err' }, '⚠ ' + errText));
      if (stopped) A.foot.appendChild(h('div', { class: 'stats' }, '⏹ 已停止生成'));
      if (stats) { const s = statsLine(stats); if (s) A.foot.appendChild(h('div', { class: 'stats' }, s)); }
      const blocks = answer ? parseEditBlocks(answer, { defaultPath: files[0] && files[0].path, root: wsRoot() }) : [];
      setBusy(false);
      if (blocks.length) await renderEditCards(A.cards, blocks, files.map((f) => f.path));
      else if (answer.trim() && !errText && !stopped) A.foot.appendChild(h('div', { class: 'stats' }, '（回答中没有可应用的 SEARCH/REPLACE 修改块）'));
      if (answer.trim()) {
        ai.history.push({ role: 'user', content: text }, { role: 'assistant', content: answer.slice(0, 4000) });
        if (ai.history.length > 8) ai.history = ai.history.slice(-8);
      }
      scrollAiBottom();
    }
  }

  function applyToEditor(code) {
    const title = IDE.replaceSelection(code);
    if (!title) { toast('请先在编辑器中打开一个文本文件', 'error'); return; }
    toast('已应用到 ' + title + '（尚未保存，Ctrl+S 保存）', 'ok');
  }

  // ---------------------------------------------------------------- edit cards
  function wsRoot() { return (IDE.workspace && IDE.workspace.root) || ''; }
  function resolvePath(raw, known) {
    if (!raw) return null;
    let p = String(raw).replace(/\\/g, '/');
    const root = wsRoot().replace(/\\/g, '/').replace(/\/+$/, '');
    if (root && p.toLowerCase().startsWith(root.toLowerCase() + '/')) p = p.slice(root.length + 1);
    if (/^[A-Za-z]:/.test(p)) return null;
    p = p.replace(/^\/+/, '');
    const n = normRel(p);
    if (!n || n.split('/').some((x) => x.includes(':'))) return null;
    if (known.includes(n)) return n;
    const suffix = known.filter((k) => k.endsWith('/' + n));
    if (suffix.length === 1) return suffix[0];
    return n;
  }

  async function loadBase(path) {
    const t = IDE.fileText(path);
    if (t != null) return { exists: true, text: t };
    try {
      const d = await api('GET', '/api/ide/file?' + qs({ path }));
      if (d.content == null) return { exists: true, text: null, error: d.binary ? '二进制文件，无法编辑' : '文件过大，无法在编辑器中打开' };
      return { exists: true, text: normEol(d.content) };
    } catch (e) {
      if (e.status === 404) return { exists: false, text: '' };
      return { exists: true, text: null, error: e.message };
    }
  }

  function renderDiffEl(oldText, newText) {
    const d = unifiedDiff(oldText, newText, 3);
    const box = h('div', { class: 'ai-edit-diff' });
    const MAX = 600;
    d.lines.slice(0, MAX).forEach((l) => {
      const c = 'diff-line' + (l.type === 'add' ? ' add' : l.type === 'del' ? ' del' : l.type === 'hunk' ? ' hunk' : '');
      box.appendChild(h('div', { class: c, text: l.text || ' ' }));
    });
    if (d.lines.length > MAX) box.appendChild(h('div', { class: 'diff-line meta', text: '…（还有 ' + (d.lines.length - MAX) + ' 行差异）' }));
    return { el: box, added: d.added, removed: d.removed };
  }

  function failEl(title, raw) {
    return h('div', { class: 'ai-edit-fail' },
      h('div', { class: 'ai-edit-fail-head' },
        h('span', { class: 'err grow' }, '⚠ ' + title),
        h('button', { class: 'btn tiny', type: 'button', onclick: () => copyText(raw).then((ok) => toast(ok ? '已复制' : '复制失败', ok ? 'ok' : 'error')) }, '复制')),
      h('pre', { class: 'ai-edit-raw' }, h('code', { text: raw })));
  }

  async function renderEditCards(host, blocks, sentPaths) {
    host.textContent = '';
    const known = Array.from(new Set((sentPaths || []).concat(IDE.openPaths())));
    const groups = new Map();
    const orphans = [];
    for (const b of blocks) {
      const p = resolvePath(b.path, known);
      if (!p) { orphans.push({ b, why: b.path ? '非法文件路径：' + b.path : '未指定文件路径' }); continue; }
      if (!groups.has(p)) groups.set(p, []);
      groups.get(p).push(b);
    }
    const cards = [];
    for (const [path, bl] of groups) {
      const base = await loadBase(path);
      const card = { path, blocks: bl, exists: base.exists, state: 'pending', el: null };
      const el = h('div', { class: 'ai-edit-card', dataset: { path } });
      card.el = el;
      const head = h('div', { class: 'ai-edit-head' },
        h('span', { class: 'ai-edit-title' }, '修改建议'),
        h('span', { class: 'ai-edit-path', title: path }, path));
      el.appendChild(head);
      if (base.text == null) {
        el.appendChild(failEl('无法编辑该文件：' + (base.error || ''), bl.map((b) => b.raw).join('\n\n')));
        host.appendChild(el);
        continue;
      }
      const r = applyBlocks(base.text, bl);
      card.okBlocks = bl.filter((b, i) => r.results[i].ok);
      if (!base.exists && card.okBlocks.length) head.appendChild(h('span', { class: 'ai-badge new' }, '新文件'));
      if (r.results.some((x) => x.fuzzy)) {
        const sc = Math.min.apply(null, r.results.filter((x) => x.fuzzy).map((x) => x.score));
        head.appendChild(h('span', { class: 'ai-badge fuzzy', title: '原文与文件内容不完全一致，已按相似度 ' + Math.round(sc * 100) + '% 定位，请仔细核对' }, '模糊匹配'));
      }
      if (card.okBlocks.length) {
        const d = renderDiffEl(base.text, r.content);
        head.appendChild(h('span', { class: 'ai-badge stat' }, '+' + d.added + ' −' + d.removed));
        el.appendChild(d.el);
      }
      r.results.forEach((res, i) => {
        if (res.ok) return;
        const why = res.error === 'incomplete' ? '修改块不完整（缺少 ======= 或 >>>>>>> REPLACE）'
          : (!base.exists ? '未找到要替换的原文（文件不存在：' + path + '）' : '未找到要替换的原文');
        el.appendChild(failEl(why, bl[i].raw));
      });
      if (card.okBlocks.length) {
        const status = h('span', { class: 'ai-edit-status small muted', 'aria-live': 'polite' });
        const bAcc = h('button', { class: 'btn tiny primary', type: 'button', onclick: () => acceptCard(card, false) }, '接受');
        const bSave = h('button', { class: 'btn tiny', type: 'button', onclick: () => acceptCard(card, true) }, '接受并保存');
        const bRej = h('button', { class: 'btn tiny', type: 'button', onclick: () => rejectCard(card) }, '拒绝');
        card.buttons = [bAcc, bSave, bRej];
        card.status = status;
        el.appendChild(h('div', { class: 'ai-edit-actions' }, bAcc, bSave, bRej, status));
        cards.push(card);
      }
      host.appendChild(el);
    }
    for (const o of orphans) {
      host.appendChild(h('div', { class: 'ai-edit-card' },
        h('div', { class: 'ai-edit-head' }, h('span', { class: 'ai-edit-title' }, '修改建议')),
        failEl(o.why, o.b.raw)));
    }
    if (cards.length > 1) {
      const allBtn = h('button', { class: 'btn tiny primary', type: 'button' }, '全部接受');
      allBtn.addEventListener('click', async () => {
        allBtn.disabled = true;
        for (const c of cards) if (c.state === 'pending') await acceptCard(c, false);
      });
      host.insertBefore(h('div', { class: 'ai-edits-head' }, h('span', { class: 'grow' }, '修改建议 · ' + cards.length + ' 个文件'), allBtn), host.firstChild);
    }
  }

  function setCardState(card, state, text) {
    card.state = state;
    card.el.classList.toggle('accepted', state === 'accepted');
    card.el.classList.toggle('rejected', state === 'rejected');
    const done = state !== 'pending';
    for (const b of card.buttons || []) b.disabled = done || state === 'busy';
    if (state === 'busy') for (const b of card.buttons || []) b.disabled = true;
    if (card.status) card.status.textContent = text || '';
  }
  function rejectCard(card) {
    if (card.state !== 'pending') return;
    setCardState(card, 'rejected', '已拒绝');
  }
  async function acceptCard(card, save) {
    if (card.state !== 'pending') return;
    setCardState(card, 'busy', '应用中…');
    try {
      if (!card.exists && IDE.fileText(card.path) == null) {
        try { await api('POST', '/api/ide/fs', { op: 'create_file', path: card.path }); } catch (e) { if (e.status !== 409) throw e; }
        card.exists = true;
        IDE.refreshTree();
      }
      const t = await IDE.openFile(card.path);
      if (!t || !t.editable) throw new Error('无法在编辑器中打开 ' + card.path);
      const cur = IDE.fileText(card.path);
      const r = applyBlocks(cur, card.okBlocks);
      if (r.failed) throw new Error('文件内容已变化，部分修改无法再定位，请重新生成');
      const ch = computeChange(cur, r.content);
      if (ch && !IDE.applyChange(card.path, ch)) throw new Error('无法写入编辑器');
      if (save) {
        const ok = await IDE.saveFile(card.path);
        setCardState(card, 'accepted', ok ? '已接受并保存' : '已应用到编辑器（未保存）');
      } else setCardState(card, 'accepted', '已接受（未保存，Ctrl+Z 可撤销）');
    } catch (e) {
      setCardState(card, 'pending', '');
      toastErr(e, '应用修改失败');
    }
  }

  // ---------------------------------------------------------------- wiring
  $('ai-close').addEventListener('click', () => toggle(false));
  $('ai-clear').addEventListener('click', () => {
    const l = $('ai-list');
    l.textContent = '';
    ai.history = [];
    l.appendChild(h('div', { class: 'ai-empty muted' }, '对话列表已清空（服务端会话记忆不受影响）。'));
  });
  $('ai-add-ctx').addEventListener('click', () => addContext());
  $('ai-add-ctx2').addEventListener('click', () => addContext());
  $('ai-send').addEventListener('click', send);
  $('ai-stop').addEventListener('click', () => { if (ai.ctrl) ai.ctrl.abort(); });
  $('ai-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  $('ai-input').addEventListener('focus', () => { if (ai.mode === 'edit') renderCtx(); });
  $('ai-mode-ask').addEventListener('click', () => setMode('ask'));
  $('ai-mode-edit').addEventListener('click', () => setMode('edit'));
  $('ai-model').addEventListener('change', onModelChange);
  $('ai-tools').addEventListener('click', () => setTools(!ai.tools));
  $('ai-prompt').addEventListener('change', () => {
    const v = $('ai-prompt').value;
    ai.promptId = v && v !== ai.activePrompt ? v : null;
    renderPrompts();
  });

  setTools(ai.tools);
  renderMode();
  renderModels();
  Object.assign(window.__ideAI, {
    toggle, addContext, setMode,
    get mode() { return ai.mode; },
    get isOpen() { return ai.open; },
  });
  // ide.js may have asked to open the panel before this file loaded
  if (IDE.aiPendingOpen != null) toggle(IDE.aiPendingOpen);
})();
