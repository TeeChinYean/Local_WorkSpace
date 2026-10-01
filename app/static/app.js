document.addEventListener('DOMContentLoaded', () => {
  'use strict';

  // ======================================================================
  // 安全与通用工具
  // 所有插入 innerHTML 的服务端 / 模型 / 网络数据都必须经过 escapeHtml 转义；
  // 能用 textContent / setAttribute 的地方优先使用 DOM API。
  // ======================================================================
  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // 仅允许 http/https 链接，阻止 javascript: / data: 等危险协议
  function safeUrl(u) {
    try {
      const parsed = new URL(String(u), window.location.href);
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return String(u);
    } catch (e) { /* 无效 URL */ }
    return '#';
  }

  // localStorage 在隐私模式 / 禁用站点数据时可能抛异常
  function lsGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function lsSet(key, value) {
    try { window.localStorage.setItem(key, String(value)); } catch (e) { /* ignore */ }
  }

  const $ = (id) => document.getElementById(id);
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  const reducedMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  const scrollBehavior = () => (reducedMotion.matches ? 'auto' : 'smooth');
  const timeNow = () => new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  const ICONS = {
    send: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>',
    stop: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2.5"/></svg>',
    copy: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    check: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>',
    regen: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><path d="M20.5 15a9 9 0 1 1-2.1-9.4L23 10"/></svg>',
    info: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9.5"/><line x1="12" y1="11" x2="12" y2="16.5"/><circle cx="12" cy="7.6" r=".6" fill="currentColor"/></svg>',
    save: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>',
    spark: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg>'
  };

  // ======================================================================
  // 元素引用
  // ======================================================================
  const root = document.documentElement;
  const chatMessages = $('chat-messages');
  const emptyState = $('empty-state');
  const chatInput = $('chat-input');
  const sendBtn = $('send-btn');
  const composer = $('composer');
  const composerHint = $('composer-hint');
  const chatTitle = $('chat-title');
  const scrollBottomBtn = $('scroll-bottom-btn');
  const connectionStatus = $('connection-status');
  const connectionDot = $('connection-dot');
  const connectionText = $('connection-text');

  const sidebar = $('app-sidebar');
  const sidebarScrim = $('sidebar-scrim');
  const btnToggleSidebar = $('btn-toggle-sidebar');
  const btnCollapseSidebar = $('btn-collapse-sidebar');
  const btnNewChat = $('btn-new-chat');
  const fileList = $('file-list');
  const fileListEmpty = $('file-list-empty');
  const memoryCountEl = $('memory-count');
  const btnFlush = $('btn-flush');
  const btnClearRag = $('btn-clear-rag');
  const themeToggle = $('theme-toggle');
  const themeLabel = $('theme-label');

  const questionsSidebar = $('questions-sidebar');
  const questionsList = $('questions-list');
  const questionsCountBadge = $('questions-count-badge');
  const btnToggleOutline = $('btn-toggle-outline');

  const fileInput = $('file-input');
  const ocrCheckbox = $('ocr-checkbox');
  const webSearchToggleBtn = $('web-search-toggle-btn');
  const thinkingToggleBtn = $('thinking-toggle-btn');
  const modelSelect = $('model-select');
  const modelChip = $('model-chip');
  const modelChipStatus = $('model-chip-status');

  const settingsModal = $('settings-modal');
  const settingThinkingDefault = $('setting-thinking-default');
  const exaApiKeyInput = $('exa-api-key-input');
  const btnSaveExaKey = $('btn-save-exa-key');
  const exaKeyStatus = $('exa-key-status');
  const searchNumResults = $('search-num-results');
  const searchAutoSaveCheckbox = $('search-auto-save-checkbox');
  const searchDefaultEnableCheckbox = $('search-default-enable-checkbox');
  const searchHfEnableCheckbox = $('search-hf-enable-checkbox');
  const manualSearchInput = $('manual-search-input');
  const manualSearchSubmit = $('manual-search-submit');
  const manualSearchResults = $('manual-search-results');
  const devInfo = $('dev-info');
  const devInfoList = $('dev-info-list');

  const urlModal = $('url-modal');
  const urlModalInput = $('url-modal-input');
  const urlModalSubmit = $('url-modal-submit');
  const urlModalStatus = $('url-modal-status');

  const localFsModal = $('local-fs-modal');
  const fsFileList = $('fs-file-list');
  const fsBreadcrumb = $('fs-breadcrumb');
  const fsSearchInput = $('fs-search-input');
  const fsCountBadge = $('fs-count-badge');
  const fsPreview = $('fs-preview');
  const fsPreviewName = $('fs-preview-name');
  const fsPreviewMeta = $('fs-preview-meta');
  const fsPreviewContent = $('fs-preview-content');
  const fsPreviewClose = $('fs-preview-close');
  const fsMountBtn = $('fs-mount-btn');
  const fsOcrLabel = $('fs-ocr-label');
  const fsOcrCheckbox = $('fs-ocr-checkbox');
  const fsToggleEditBtn = $('fs-toggle-edit-btn');
  const fsSaveFileBtn = $('fs-save-file-btn');
  const fsEditorContent = $('fs-editor-content');
  const fsEditorFooter = $('fs-editor-footer');
  const fsBackupCheckbox = $('fs-backup-checkbox');
  const fsSaveStatus = $('fs-save-status');
  const fsModalStatus = $('fs-modal-status');

  const writeBackModal = $('write-back-modal');
  const writeBackCancelBtn = $('write-back-cancel-btn');
  const writeBackSubmitBtn = $('write-back-submit-btn');
  const writeBackFileSelect = $('write-back-file-select');
  const writeBackPathInput = $('write-back-path-input');
  const writeBackCodePreview = $('write-back-code-preview');
  const writeBackCodeStats = $('write-back-code-stats');
  const writeBackBackupCheckbox = $('write-back-backup-checkbox');
  const writeBackStatusMsg = $('write-back-status-msg');

  const confirmModal = $('confirm-modal');
  const confirmTitle = $('confirm-title');
  const confirmBody = $('confirm-body');
  const confirmOk = $('confirm-ok');
  const confirmCancel = $('confirm-cancel');

  // ======================================================================
  // 状态
  // ======================================================================
  const DEFAULT_PLACEHOLDER = '给助手发消息…';
  const DEFAULT_HINT = composerHint ? composerHint.textContent : '';
  let isGenerating = false;
  let currentAbortController = null;
  let isUploading = false;
  let isFetchingUrl = false;
  let isSwitchingModel = false;   // 模型切换进行中 (阻止发送消息与状态轮询覆盖)
  let webSearchEnabled = lsGet('webSearchEnabled') === 'true';
  let thinkingDefault = lsGet('thinkingDefault') === 'true';
  let thinkingEnabled = thinkingDefault;
  let userQuestions = [];
  let msgSeq = 0;
  let lastStatus = null;      // 最近一次 /api/status
  let lastCtxStats = null;    // 最近一次生成的上下文统计 (开发者信息)
  let cachedActiveFiles = [];
  let currentTriggerWriteBackBtn = null;
  // Agent (工具 / MCP / 技能 / 系统提示词)
  let toolsEnabled = lsGet('toolsEnabled') === 'true';
  let settingsTab = 'general';
  let agentTools = [];
  let mcpServers = [];
  let mcpPollTimer = 0;
  let mcpEditingId = null;
  let skills = [];
  let skillEditingSlug = null;
  let promptsState = { prompts: [], active_prompt_id: '', prompt_mode: 'append' };
  let promptOverrideId = null;   // 输入框里为本页对话临时选择的预设 (null = 使用设置中的默认预设)
  let promptEditingId = null;
  let kgPollTimer = 0;           // 知识图谱标签页的状态轮询 (仅标签页打开时)

  // ======================================================================
  // 主题：浅色 / 深色 / 跟随系统 (手动选择保存在 localStorage)
  // ======================================================================
  const darkMQ = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : { matches: false };
  const THEME_LABELS = { system: '跟随系统', light: '浅色', dark: '深色' };
  let themePref = THEME_LABELS[root.getAttribute('data-theme-pref')] ? root.getAttribute('data-theme-pref') : 'system';

  function applyTheme() {
    const dark = themePref === 'dark' || (themePref === 'system' && darkMQ.matches);
    root.setAttribute('data-theme', dark ? 'dark' : 'light');
    root.setAttribute('data-theme-pref', themePref);
    if (themeLabel) themeLabel.textContent = THEME_LABELS[themePref];
    if (themeToggle) themeToggle.setAttribute('aria-label', `主题：${THEME_LABELS[themePref]}（点击切换）`);
  }
  if (themeToggle) {
    themeToggle.addEventListener('click', () => {
      // 第一次点击总会带来可见变化：跟随系统 → 与系统相反 → 与系统相同 → 跟随系统
      const order = darkMQ.matches ? ['system', 'light', 'dark'] : ['system', 'dark', 'light'];
      themePref = order[(order.indexOf(themePref) + 1) % order.length];
      lsSet('theme', themePref);
      applyTheme();
    });
  }
  if (darkMQ.addEventListener) darkMQ.addEventListener('change', () => { if (themePref === 'system') applyTheme(); });
  applyTheme();

  // ======================================================================
  // 弹窗 (Modal) 与确认对话框
  // ======================================================================
  const modalStack = [];
  const topModal = () => modalStack[modalStack.length - 1] || null;
  const isOpen = (m) => Boolean(m && !m.hidden);

  function openModal(modal, focusTarget) {
    if (!modal || !modal.hidden) return;
    closeAllMenus();
    modal._opener = document.activeElement;
    modal.hidden = false;
    modalStack.push(modal);
    const card = modal.querySelector('.modal-card');
    if (card && !card.hasAttribute('tabindex')) card.setAttribute('tabindex', '-1');
    setTimeout(() => {
      const target = focusTarget || card;
      if (target && typeof target.focus === 'function') target.focus();
    }, 30);
  }

  function closeModal(modal) {
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    const i = modalStack.indexOf(modal);
    if (i !== -1) modalStack.splice(i, 1);
    if (modal === writeBackModal) currentTriggerWriteBackBtn = null;
    if (modal === settingsModal) { stopMcpPoll(); stopKgPoll(); clearSettingsHash(); }
    if (modal === confirmModal && confirmResolve) {
      const r = confirmResolve;
      confirmResolve = null;
      r(false);
    }
    const opener = modal._opener;
    modal._opener = null;
    if (opener && document.contains(opener) && typeof opener.focus === 'function') opener.focus();
  }

  document.querySelectorAll('.modal-backdrop').forEach(modal => {
    // 仅当按下与松开都在遮罩上时才关闭，避免拖选文字时误关
    modal.addEventListener('mousedown', e => { modal._downOnBackdrop = e.target === modal; });
    modal.addEventListener('click', e => {
      if (e.target === modal && modal._downOnBackdrop) closeModal(modal);
    });
    modal.querySelectorAll('[data-close]').forEach(btn => btn.addEventListener('click', () => closeModal(modal)));
  });

  function trapFocus(e) {
    const modal = topModal();
    if (!modal) return;
    const items = Array.from(modal.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), summary'
    )).filter(n => n.offsetParent !== null && n.tabIndex >= 0);
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    const inside = modal.contains(document.activeElement);
    if (e.shiftKey && (!inside || document.activeElement === first || document.activeElement === modal.querySelector('.modal-card'))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (!inside || document.activeElement === last)) {
      e.preventDefault();
      first.focus();
    }
  }

  let confirmResolve = null;
  function confirmDialog({ title, body, okText = '确定', danger = false }) {
    return new Promise(resolve => {
      if (confirmResolve) confirmResolve(false);
      confirmTitle.textContent = title;
      confirmBody.textContent = body;
      confirmOk.textContent = okText;
      confirmOk.classList.toggle('btn-danger', danger);
      confirmOk.classList.toggle('btn-primary', !danger);
      confirmResolve = resolve;
      openModal(confirmModal, danger ? confirmCancel : confirmOk);
    });
  }
  function settleConfirm(value) {
    const r = confirmResolve;
    confirmResolve = null;
    closeModal(confirmModal);
    if (r) r(value);
  }
  confirmOk.addEventListener('click', () => settleConfirm(true));
  confirmCancel.addEventListener('click', () => settleConfirm(false));

  // ======================================================================
  // 下拉菜单 (＋ 添加内容 / 记忆管理)
  // ======================================================================
  const menus = [];
  function setupMenu(btn, menu) {
    if (!btn || !menu) return;
    const api = {
      btn, menu,
      open() {
        closeAllMenus();
        menu.hidden = false;
        btn.setAttribute('aria-expanded', 'true');
        const first = menu.querySelector('[role="menuitem"]');
        if (first) first.focus();
      },
      close(returnFocus) {
        if (menu.hidden) return;
        menu.hidden = true;
        btn.setAttribute('aria-expanded', 'false');
        if (returnFocus) btn.focus();
      }
    };
    btn.addEventListener('click', e => {
      e.stopPropagation();
      if (menu.hidden) api.open(); else api.close();
    });
    menu.addEventListener('click', e => { if (e.target.closest('.menu-item')) api.close(); });
    menu.addEventListener('keydown', e => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      const items = Array.from(menu.querySelectorAll('[role="menuitem"], [role="menuitemradio"], input')).filter(n => n.offsetParent !== null);
      const i = items.indexOf(document.activeElement);
      const next = e.key === 'ArrowDown' ? (i + 1) % items.length : (i - 1 + items.length) % items.length;
      e.preventDefault();
      items[next].focus();
    });
    menus.push(api);
    return api;
  }
  function closeAllMenus(returnFocus) {
    let closed = false;
    menus.forEach(m => { if (!m.menu.hidden) { m.close(returnFocus); closed = true; } });
    return closed;
  }
  document.addEventListener('click', e => {
    menus.forEach(m => {
      if (!m.menu.hidden && !m.menu.contains(e.target) && !m.btn.contains(e.target)) m.close();
    });
  });
  const attachMenu = setupMenu($('btn-attach'), $('attach-menu'));
  setupMenu($('btn-memory-menu'), $('memory-menu'));

  // ======================================================================
  // 侧边栏：桌面可收起；窄屏 (<900px) 为覆盖式抽屉
  // ======================================================================
  const narrowMQ = window.matchMedia ? window.matchMedia('(max-width: 899px)') : { matches: false };
  const isNarrow = () => narrowMQ.matches;

  function syncSidebarToggle() {
    if (!btnToggleSidebar) return;
    const open = isNarrow() ? sidebar.classList.contains('open') : !root.classList.contains('sidebar-collapsed');
    btnToggleSidebar.setAttribute('aria-expanded', String(open));
    btnToggleSidebar.setAttribute('aria-label', isNarrow() && open ? '关闭侧边栏' : '打开侧边栏');
    btnToggleSidebar.title = btnToggleSidebar.getAttribute('aria-label');
  }
  function setMobileSidebarOpen(open) {
    sidebar.classList.toggle('open', open);
    if (sidebarScrim) sidebarScrim.hidden = !open;
    syncSidebarToggle();
  }
  function closeMobileSidebar() {
    if (sidebar.classList.contains('open')) setMobileSidebarOpen(false);
  }
  function setDesktopCollapsed(collapsed) {
    root.classList.toggle('sidebar-collapsed', collapsed);
    lsSet('sidebarCollapsed', collapsed);
    syncSidebarToggle();
  }
  btnToggleSidebar.addEventListener('click', e => {
    e.stopPropagation();
    if (isNarrow()) setMobileSidebarOpen(!sidebar.classList.contains('open'));
    else setDesktopCollapsed(false);
  });
  btnCollapseSidebar.addEventListener('click', () => {
    if (isNarrow()) { setMobileSidebarOpen(false); btnToggleSidebar.focus(); }
    else { setDesktopCollapsed(true); btnToggleSidebar.focus(); }
  });
  // 点击抽屉外部区域关闭
  document.addEventListener('click', e => {
    if (!sidebar.classList.contains('open')) return;
    if (sidebar.contains(e.target) || btnToggleSidebar.contains(e.target)) return;
    if (topModal()) return;
    closeMobileSidebar();
  });
  if (narrowMQ.addEventListener) narrowMQ.addEventListener('change', () => { if (!isNarrow()) closeMobileSidebar(); syncSidebarToggle(); });
  syncSidebarToggle();

  // ======================================================================
  // 右侧提问目录 (默认隐藏)
  // ======================================================================
  function isOutlineOpen() { return !questionsSidebar.classList.contains('collapsed'); }
  function setOutlineOpen(open) {
    questionsSidebar.classList.toggle('collapsed', !open);
    btnToggleOutline.classList.toggle('active', open);
    btnToggleOutline.setAttribute('aria-expanded', String(open));
  }
  btnToggleOutline.addEventListener('click', () => setOutlineOpen(!isOutlineOpen()));
  $('btn-close-outline').addEventListener('click', () => { setOutlineOpen(false); btnToggleOutline.focus(); });

  function renderOutlineEmpty() {
    const empty = el('div', 'outline-empty');
    empty.id = 'questions-empty';
    empty.appendChild(el('p', '', '还没有提问'));
    empty.appendChild(el('small', '', '发送消息后，这里会列出你问过的问题，点一下就能跳回去。'));
    questionsList.replaceChildren(empty);
  }

  function addQuestionToOutline(text, qId, qIndex) {
    const emptyEl = $('questions-empty');
    if (emptyEl) emptyEl.remove();
    userQuestions.push({ id: qId, text, index: qIndex });
    questionsCountBadge.textContent = String(userQuestions.length);

    const card = el('button', 'question-nav-card');
    card.type = 'button';
    card.setAttribute('data-target', qId);
    const meta = el('span', 'question-nav-meta');
    meta.appendChild(el('span', 'question-nav-idx', `#${qIndex}`));
    meta.appendChild(el('span', 'question-nav-time', timeNow()));
    const t = el('span', 'question-nav-text', text);
    t.title = text;
    card.appendChild(meta);
    card.appendChild(t);
    card.addEventListener('click', () => {
      questionsList.querySelectorAll('.question-nav-card').forEach(c => c.classList.remove('active'));
      card.classList.add('active');
      const targetRow = $(qId);
      if (targetRow) {
        targetRow.scrollIntoView({ behavior: scrollBehavior(), block: 'center' });
        targetRow.classList.add('jump-highlight');
        setTimeout(() => targetRow.classList.remove('jump-highlight'), 1200);
      }
      if (window.matchMedia && window.matchMedia('(max-width: 1099px)').matches) setOutlineOpen(false);
    });
    questionsList.appendChild(card);
    questionsList.scrollTop = questionsList.scrollHeight;
  }

  function resetQuestionOutline() {
    userQuestions = [];
    questionsCountBadge.textContent = '0';
    renderOutlineEmpty();
  }
  renderOutlineEmpty();

  // ======================================================================
  // 输入框：自动增高、快捷键、联网 / 深度思考开关、推荐问题
  // ======================================================================
  function autoGrow() {
    chatInput.style.height = 'auto';
    chatInput.style.height = Math.min(chatInput.scrollHeight, 220) + 'px';
    sendBtn.classList.toggle('ready', chatInput.value.trim().length > 0);
  }
  chatInput.addEventListener('input', autoGrow);
  chatInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      // 生成中按 Enter 不做任何事 (仅停止按钮可中止生成)
      if (isGenerating || e.isComposing) return;
      sendMessage();
    }
  });
  sendBtn.addEventListener('click', () => sendMessage());

  function setWebSearch(on, persist = true) {
    webSearchEnabled = Boolean(on);
    if (persist) lsSet('webSearchEnabled', webSearchEnabled);
    webSearchToggleBtn.classList.toggle('active', webSearchEnabled);
    webSearchToggleBtn.setAttribute('aria-pressed', String(webSearchEnabled));
  }
  function setThinking(on) {
    thinkingEnabled = Boolean(on);
    thinkingToggleBtn.classList.toggle('active', thinkingEnabled);
    thinkingToggleBtn.setAttribute('aria-pressed', String(thinkingEnabled));
  }
  webSearchToggleBtn.addEventListener('click', () => setWebSearch(!webSearchEnabled));
  thinkingToggleBtn.addEventListener('click', () => setThinking(!thinkingEnabled));
  setWebSearch(webSearchEnabled, false);
  setThinking(thinkingEnabled);

  // 推荐问题：填入输入框 (不直接发送)，方便用户补充内容
  document.querySelectorAll('.suggestion-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const prompt = chip.getAttribute('data-prompt') || '';
      if (!prompt) return;
      if (chip.getAttribute('data-web-search') === 'true') setWebSearch(true);
      chatInput.value = prompt;
      autoGrow();
      chatInput.focus();
      chatInput.setSelectionRange(prompt.length, prompt.length);
    });
  });

  function setGeneratingState(generating) {
    isGenerating = generating;
    if (generating) {
      sendBtn.disabled = false;
      sendBtn.classList.add('stop-mode');
      sendBtn.title = '停止生成';
      sendBtn.setAttribute('aria-label', '停止生成');
      sendBtn.innerHTML = ICONS.stop;
    } else {
      sendBtn.disabled = isSwitchingModel;   // 模型切换进行中时保持禁用
      sendBtn.classList.remove('stop-mode');
      sendBtn.title = '发送 (Enter)';
      sendBtn.setAttribute('aria-label', '发送消息');
      sendBtn.innerHTML = ICONS.send;
      currentAbortController = null;
    }
  }

  function setComposerBusy(text) {
    if (!composerHint) return;
    composerHint.textContent = text || DEFAULT_HINT;
    composerHint.classList.toggle('busy', Boolean(text));
  }

  function setConnection(state) {
    if (!connectionStatus) return;
    if (state === 'ok') {
      connectionStatus.hidden = true;
      return;
    }
    connectionStatus.hidden = false;
    connectionDot.className = `dot ${state}`;
    connectionText.textContent = state === 'switching' ? '正在切换模型…' : '连接不上本地服务';
  }

  // ======================================================================
  // 系统提示 (文件已添加、模型切换等)，以轻量的居中提示条显示
  // ======================================================================
  function hideEmptyState() {
    if (emptyState && emptyState.parentNode) emptyState.remove();
  }

  function appendSystemNotice(markdownText, tone = 'info') {
    hideEmptyState();
    const row = el('div', `notice notice-${tone}`);
    row.setAttribute('role', tone === 'error' ? 'alert' : 'status');
    const body = el('div', 'notice-body');
    body.innerHTML = formatMarkdown(markdownText);
    row.appendChild(body);
    chatMessages.appendChild(row);
    scrollToBottom();
    return row;
  }

  // ======================================================================
  // 工作区文件 (侧边栏)
  // ======================================================================
  function getFileIcon(ext) {
    const m = {'.py':'🐍','.js':'📜','.ts':'📘','.json':'🧾','.md':'📄','.txt':'📝',
               '.pdf':'📕','.docx':'📃','.doc':'📃','.csv':'📊','.yaml':'⚙️','.yml':'⚙️',
               '.html':'🌐','.css':'🎨','.sql':'🗄️','.sh':'🔧','.go':'🐹','.rs':'🦀',
               '.java':'☕','.c':'⚙️','.cpp':'⚙️','.log':'📋'};
    return Object.prototype.hasOwnProperty.call(m, ext) ? m[ext] : '📄';
  }
  function formatBytes(b) {
    if (!b || b === 0) return '';
    if (b < 1024) return `${b}B`;
    if (b < 1048576) return `${(b / 1024).toFixed(1)}KB`;
    return `${(b / 1048576).toFixed(1)}MB`;
  }
  function bareFileName(filename) {
    const s = String(filename || '');
    // 去掉挂载 / PDF 上传时附加的图标前缀，按裸文件名去重 (与 /api/status 的 active_files 对齐)
    const m = s.match(/^(📂|📄)/u);
    return m ? s.slice(m[0].length) : s;
  }
  function extOf(name) {
    const i = name.lastIndexOf('.');
    return i > 0 ? name.slice(i).toLowerCase() : '';
  }
  function syncFileListEmpty() {
    if (fileListEmpty) fileListEmpty.hidden = fileList.children.length > 0;
  }

  // 新增或更新一个工作区文件条目 (按裸文件名去重)
  function addWorkspaceFile(filename, info = {}) {
    const bareName = bareFileName(filename);
    if (!bareName) return;
    const existing = Array.from(fileList.children).find(li => li.dataset.filename === bareName);
    const localPath = info.localPath || (existing && existing.dataset.localPath) || '';
    const lines = info.lines || (existing && Number(existing.dataset.lines)) || 0;
    const inRag = info.inRag !== undefined ? info.inRag : (existing && existing.dataset.inRag === 'true');

    const li = existing || el('li', 'file-item');
    li.dataset.filename = bareName;
    li.dataset.localPath = localPath;
    li.dataset.lines = String(lines);
    li.dataset.inRag = String(Boolean(inRag));

    const isWeb = bareName.startsWith('🌐');
    const item = el(localPath ? 'button' : 'div', 'file-entry');
    if (localPath) {
      item.type = 'button';
      item.title = `查看或编辑：${localPath}`;
      item.addEventListener('click', () => {
        closeMobileSidebar();
        openLocalFs();
        fsSelectFile({ name: bareName, path: localPath, ext: extOf(bareName) });
      });
    } else {
      item.title = bareName;
    }
    item.appendChild(el('span', 'file-icon', isWeb ? '🌐' : getFileIcon(extOf(bareName))));
    item.appendChild(el('span', 'file-name', isWeb ? bareName.replace(/^🌐\s*/u, '') : bareName));
    const metaParts = [];
    if (lines) metaParts.push(`${lines} 行`);
    if (inRag) metaParts.push('已存入记忆');
    if (metaParts.length) item.appendChild(el('span', 'file-meta', metaParts.join(' · ')));
    li.replaceChildren(item);
    if (!existing) fileList.appendChild(li);
    syncFileListEmpty();
  }

  // 以 /api/status 的 active_files 为准同步列表
  function syncWorkspaceFiles(files) {
    const names = new Set(files.map(f => bareFileName(f.filename)));
    Array.from(fileList.children).forEach(li => { if (!names.has(li.dataset.filename)) li.remove(); });
    files.forEach(f => addWorkspaceFile(f.filename, { lines: f.lines, localPath: f.local_path, inRag: f.in_rag }));
    syncFileListEmpty();
  }

  function clearWorkspaceFiles() {
    fileList.replaceChildren();
    cachedActiveFiles = [];
    syncFileListEmpty();
  }

  // ======================================================================
  // 上传文件：.pdf / .docx / .doc 走 /api/upload_pdf，其余走 /api/upload
  // ======================================================================
  const isDocFile = (f) => /\.(pdf|docx|doc)$/i.test(f.name || '');

  async function routeFiles(files) {
    const docs = files.filter(isDocFile);
    const texts = files.filter(f => !isDocFile(f));
    if (docs.length > 0) await handlePdfUpload(docs);
    if (texts.length > 0) await handleFilesUpload(texts);
  }

  $('upload-btn').addEventListener('click', () => { if (!isUploading) fileInput.click(); });
  fileInput.addEventListener('change', () => {
    const files = Array.from(fileInput.files || []);
    fileInput.value = '';
    if (files.length > 0) routeFiles(files);
  });

  // 拖拽文件到输入框
  ['dragenter', 'dragover'].forEach(name => composer.addEventListener(name, e => {
    e.preventDefault();
    composer.classList.add('drag-over');
  }));
  ['dragleave', 'drop'].forEach(name => composer.addEventListener(name, e => {
    e.preventDefault();
    composer.classList.remove('drag-over');
  }));
  composer.addEventListener('drop', e => {
    const dt = e.dataTransfer;
    if (dt && dt.files && dt.files.length > 0) routeFiles(Array.from(dt.files));
  });

  function afterFilesAdded() {
    fetchStatus();
    if (!chatInput.value.trim()) chatInput.placeholder = '就刚添加的内容提问，例如：帮我总结要点';
  }

  function uploadErrorDetail(data, status) {
    let d = data && data.detail !== undefined ? data.detail : '';
    if (d && typeof d !== 'string') d = JSON.stringify(d);
    return d || `HTTP ${status}`;
  }

  async function handleFilesUpload(files) {
    if (isUploading || files.length === 0) return;
    isUploading = true;
    setComposerBusy('正在添加文件…');
    const formData = new FormData();
    files.forEach(f => formData.append('files', f));
    try {
      const res = await fetch('/api/upload', { method: 'POST', body: formData });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(uploadErrorDetail(data, res.status));
      (data.files || []).forEach(f => {
        addWorkspaceFile(f.filename, { lines: f.lines });
        appendSystemNotice(`已添加文件 \`${f.filename}\`${f.lines ? `（${f.lines} 行）` : ''}，现在可以就它提问了。`, 'success');
      });
      if (data.flushed_to_rag) appendSystemNotice('工作区快满了，较早的文件已自动存入长期记忆。');
      if ((data.files || []).length) afterFilesAdded();
    } catch (err) {
      console.error('上传出错:', err);
      appendSystemNotice(`文件没能添加成功：${err.message}`, 'error');
    } finally {
      isUploading = false;
      setComposerBusy('');
    }
  }

  async function handlePdfUpload(files) {
    if (isUploading || files.length === 0) return;
    isUploading = true;
    const useOcr = Boolean(ocrCheckbox && ocrCheckbox.checked);
    setComposerBusy(useOcr ? '正在识别文档文字，可能需要一会儿…' : '正在读取文档…');
    const formData = new FormData();
    files.forEach(f => formData.append('files', f));
    try {
      const res = await fetch(`/api/upload_pdf?ocr=${useOcr}`, { method: 'POST', body: formData });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(uploadErrorDetail(data, res.status));
      (data.files || []).forEach(f => {
        addWorkspaceFile(f.filename, { lines: f.lines });
        const pages = f.pages ? `${f.pages} 页 · ` : '';
        appendSystemNotice(`已添加文档 \`${f.filename}\`（${pages}${f.lines || 0} 行），现在可以就它提问了。`, 'success');
      });
      if (data.flushed_to_rag) appendSystemNotice('工作区快满了，较早的文件已自动存入长期记忆。');
      if ((data.files || []).length) afterFilesAdded();
    } catch (err) {
      console.error('文档上传失败:', err);
      appendSystemNotice(`文档没能添加成功：${err.message}`, 'error');
    } finally {
      isUploading = false;
      setComposerBusy('');
    }
  }

  // ======================================================================
  // 导入网页
  // ======================================================================
  $('url-fetch-btn').addEventListener('click', () => {
    urlModalInput.value = '';
    showUrlModalStatus('', '');
    openModal(urlModal, urlModalInput);
  });
  urlModalInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleUrlFetch(); }
  });
  urlModalSubmit.addEventListener('click', handleUrlFetch);

  function showUrlModalStatus(type, msg) {
    urlModalStatus.hidden = !msg;
    urlModalStatus.className = `status-line ${type}`;
    urlModalStatus.textContent = msg;
  }

  async function handleUrlFetch() {
    if (isFetchingUrl) return;
    const url = urlModalInput.value.trim();
    if (!url) { showUrlModalStatus('error', '请先粘贴一个网址。'); return; }
    if (!/^https?:\/\//i.test(url)) { showUrlModalStatus('error', '网址需要以 http:// 或 https:// 开头。'); return; }

    isFetchingUrl = true;
    urlModalSubmit.disabled = true;
    urlModalSubmit.textContent = '导入中…';
    showUrlModalStatus('info', '正在读取网页…');
    try {
      const res = await fetch('/api/fetch_url', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(uploadErrorDetail(data, res.status));
      showUrlModalStatus('success', `导入成功：《${data.title}》`);
      addWorkspaceFile(data.filename, { lines: data.lines });
      appendSystemNotice(`已导入网页《${data.title}》（${data.chars || 0} 字），现在可以就它提问了。\n- 来源：${data.url || url}`, 'success');
      fetchStatus();
      setTimeout(() => {
        closeModal(urlModal);
        if (!chatInput.value.trim()) chatInput.placeholder = `就《${data.title}》提问，例如：这篇文章讲了什么？`;
        chatInput.focus();
      }, 900);
    } catch (err) {
      console.error('抓取网页失败:', err);
      showUrlModalStatus('error', `没能导入：${err.message}`);
    } finally {
      isFetchingUrl = false;
      urlModalSubmit.disabled = false;
      urlModalSubmit.textContent = '导入';
    }
  }

  // ======================================================================
  // 本地文件浏览、预览、在线编辑与添加到对话
  // ======================================================================
  let fsCurrentPath = '';
  let fsParentPath = '';
  let fsSelectedFile = null;
  let fsAllEntries = [];

  function setFsModalStatus(type, msg) {
    fsModalStatus.className = `fs-save-status ${type}`;
    fsModalStatus.textContent = msg || '';
  }
  function openLocalFs() {
    fsSearchInput.value = '';
    setFsModalStatus('', '');
    openModal(localFsModal, fsSearchInput);
  }
  $('local-fs-btn').addEventListener('click', () => { openLocalFs(); fsLoadDir(''); });

  fsSearchInput.addEventListener('input', () => {
    const kw = fsSearchInput.value.trim().toLowerCase();
    if (!kw) fsRenderList(fsAllEntries, false);
    else fsRenderList(fsAllEntries.filter(e => (e.name || '').toLowerCase().includes(kw)), true);
  });

  async function fsLoadDir(path) {
    fsCurrentPath = path;
    fsSearchInput.value = '';
    fsPreview.hidden = true;
    fsFileList.innerHTML = '<div class="fs-loading">正在读取…</div>';
    fsCountBadge.textContent = '';
    try {
      const res = await fetch('/api/fs/list', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, show_hidden: false })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(uploadErrorDetail(data, res.status));
      fsParentPath = data.parent || '';
      fsAllEntries = data.entries || [];
      buildBreadcrumb(data.path || '');
      fsRenderList(fsAllEntries, false);
    } catch (err) {
      fsFileList.innerHTML = `
        <div class="fs-loading error">
          <p>无法打开这个文件夹：${escapeHtml(err.message)}</p>
          <button type="button" class="btn btn-sm fs-back-root-btn">回到「此电脑」</button>
        </div>`;
      fsFileList.querySelector('.fs-back-root-btn').addEventListener('click', () => fsLoadDir(''));
    }
  }

  function fsRenderList(entries, isFiltered) {
    fsFileList.replaceChildren();
    if (!entries || entries.length === 0) {
      fsFileList.innerHTML = '<div class="fs-loading">这里没有文件，或没有匹配的结果</div>';
      fsCountBadge.textContent = '0 项';
      return;
    }
    const MAX_RENDER = 150;   // DOM 保护
    fsCountBadge.textContent = entries.length > MAX_RENDER ? `显示前 ${MAX_RENDER} / 共 ${entries.length} 项` : `共 ${entries.length} 项`;

    if (fsCurrentPath && !isFiltered) {
      const up = el('button', 'fs-entry fs-dir');
      up.type = 'button';
      up.innerHTML = '<span class="fs-icon" aria-hidden="true">↩</span><span class="fs-name">返回上一级</span>';
      up.addEventListener('click', () => fsLoadDir(fsParentPath));
      fsFileList.appendChild(up);
    }

    let currentCategory = null;
    entries.slice(0, MAX_RENDER).forEach(entry => {
      if (!fsCurrentPath && entry.category && entry.category !== currentCategory) {
        currentCategory = entry.category;
        fsFileList.appendChild(el('div', 'fs-section-title', currentCategory === 'quick' ? '常用位置' : '磁盘'));
      }
      const isDir = entry.type === 'dir';
      const row = el('button', `fs-entry ${isDir ? 'fs-dir' : 'fs-file'}${entry.category ? ` fs-${entry.category}` : ''}`);
      row.type = 'button';
      const icon = isDir ? (entry.category === 'drive' ? '💾' : '📁') : getFileIcon(entry.ext);
      row.appendChild(el('span', 'fs-icon', icon));
      const name = el('span', 'fs-name', entry.name);
      name.title = entry.path || '';
      row.appendChild(name);
      if (!isDir) row.appendChild(el('span', 'fs-size', formatBytes(entry.size)));
      if (entry.modified) row.appendChild(el('span', 'fs-mod', entry.modified));
      row.addEventListener('click', () => (isDir ? fsLoadDir(entry.path) : fsSelectFile(entry)));
      fsFileList.appendChild(row);
    });
  }

  // 面包屑 (Windows 盘符根目录 C: 需补全为 C:\)
  function buildBreadcrumb(path) {
    fsBreadcrumb.replaceChildren();
    const rootBtn = el('button', 'crumb', '此电脑');
    rootBtn.type = 'button';
    rootBtn.addEventListener('click', () => fsLoadDir(''));
    fsBreadcrumb.appendChild(rootBtn);
    if (!path) { rootBtn.classList.add('current'); return; }

    const parts = path.replace(/\\/g, '/').split('/').filter(Boolean);
    let accumulated = '';
    parts.forEach((part, i) => {
      accumulated += (accumulated ? '/' : '') + part;
      const targetPath = /^[a-zA-Z]:$/.test(accumulated) ? accumulated + '\\' : accumulated.replace(/\//g, '\\');
      fsBreadcrumb.appendChild(el('span', 'crumb-sep', '›'));
      const crumb = el(i < parts.length - 1 ? 'button' : 'span', i < parts.length - 1 ? 'crumb' : 'crumb current', part);
      if (i < parts.length - 1) {
        crumb.type = 'button';
        crumb.addEventListener('click', () => fsLoadDir(targetPath));
      }
      fsBreadcrumb.appendChild(crumb);
    });
  }

  function setFsEditing(editing) {
    fsToggleEditBtn.textContent = editing ? '预览' : '编辑';
    fsToggleEditBtn.classList.toggle('active', editing);
    fsPreviewContent.hidden = editing;
    fsEditorContent.hidden = !editing;
    fsSaveFileBtn.hidden = !editing;
    fsEditorFooter.hidden = !editing;
  }

  const TEXT_EXTS = new Set(['.txt','.md','.py','.js','.ts','.json','.csv','.yaml','.yml',
    '.html','.css','.sh','.sql','.rs','.go','.java','.c','.cpp',
    '.pdf','.docx','.doc','.log','.ps1','.bat','.cmd','.xml','.toml','.ini','.env']);

  async function fsSelectFile(entry) {
    fsSelectedFile = entry;
    fsPreviewName.textContent = entry.name;
    fsPreviewMeta.textContent = entry.ext || '';
    fsPreviewContent.textContent = '正在加载预览…';
    fsEditorContent.value = '';
    fsPreview.hidden = false;
    setFsEditing(false);
    fsSaveStatus.textContent = '';
    fsSaveStatus.className = 'fs-save-status';
    fsOcrLabel.hidden = entry.ext !== '.pdf';

    if (!TEXT_EXTS.has(entry.ext)) {
      fsPreviewContent.textContent = `（${entry.ext || '二进制'} 文件无法预览，但可以直接添加到对话）`;
      fsToggleEditBtn.hidden = true;
      return;
    }
    fsToggleEditBtn.hidden = false;
    try {
      const res = await fetch('/api/fs/read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: entry.path, max_chars: 100000 })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(uploadErrorDetail(data, res.status));
      const preview = data.content || '';
      fsPreviewContent.textContent = preview;
      fsEditorContent.value = preview;
      fsPreviewMeta.textContent = `${data.lines || preview.split('\n').length} 行 · ${data.size_kb} KB`;
    } catch (err) {
      fsPreviewContent.textContent = `预览失败：${err.message}`;
    }
  }

  fsToggleEditBtn.addEventListener('click', () => {
    const editing = !fsEditorContent.hidden;
    if (editing) fsPreviewContent.textContent = fsEditorContent.value;
    setFsEditing(!editing);
    if (!editing) fsEditorContent.focus();
  });

  async function saveFsEditorFile() {
    if (!fsSelectedFile || !fsSelectedFile.path) return;
    fsSaveFileBtn.disabled = true;
    fsSaveFileBtn.textContent = '保存中…';
    fsSaveStatus.className = 'fs-save-status';
    fsSaveStatus.textContent = '正在保存…';
    try {
      const res = await fetch('/api/fs/write', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: fsSelectedFile.path, content: fsEditorContent.value, create_backup: fsBackupCheckbox.checked })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(uploadErrorDetail(data, res.status));
      fsSaveFileBtn.textContent = '已保存';
      fsSaveStatus.className = 'fs-save-status success';
      fsSaveStatus.textContent = `已保存（${data.lines} 行）${data.backup_filename ? `，备份：${data.backup_filename}` : ''}`;
      fsPreviewContent.textContent = fsEditorContent.value;
      fsPreviewMeta.textContent = `${data.lines} 行 · ${Math.round(data.size_bytes / 1024 * 10) / 10} KB`;
      appendSystemNotice(`已保存本地文件 \`${data.filename}\`${data.synced_active_file ? '，对话里的内容也已同步更新' : ''}。`, 'success');
      fetchStatus();
    } catch (err) {
      fsSaveStatus.className = 'fs-save-status error';
      fsSaveStatus.textContent = `保存失败：${err.message}`;
    } finally {
      setTimeout(() => { fsSaveFileBtn.textContent = '保存'; fsSaveFileBtn.disabled = false; }, 1500);
    }
  }
  fsSaveFileBtn.addEventListener('click', saveFsEditorFile);
  fsEditorContent.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      saveFsEditorFile();
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      const start = fsEditorContent.selectionStart;
      const end = fsEditorContent.selectionEnd;
      fsEditorContent.value = fsEditorContent.value.substring(0, start) + '    ' + fsEditorContent.value.substring(end);
      fsEditorContent.selectionStart = fsEditorContent.selectionEnd = start + 4;
    }
  });
  fsPreviewClose.addEventListener('click', () => { fsPreview.hidden = true; fsSelectedFile = null; });

  fsMountBtn.addEventListener('click', async () => {
    if (!fsSelectedFile) return;
    fsMountBtn.disabled = true;
    fsMountBtn.textContent = '添加中…';
    setFsModalStatus('', '');
    try {
      const res = await fetch('/api/fs/mount', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: fsSelectedFile.path, use_ocr: fsOcrCheckbox.checked })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(uploadErrorDetail(data, res.status));
      addWorkspaceFile(data.filename, { lines: data.lines, localPath: data.local_path });
      appendSystemNotice(`已添加本地文件 \`${data.filename}\`（${data.lines || 0} 行），现在可以就它提问了。\n- 位置：${data.local_path}`, 'success');
      closeModal(localFsModal);
      fetchStatus();
    } catch (err) {
      setFsModalStatus('error', `没能添加：${err.message}`);
    } finally {
      fsMountBtn.disabled = false;
      fsMountBtn.textContent = '添加到对话';
    }
  });

  // ======================================================================
  // 代码块「写回文件」
  // ======================================================================
  function showWriteBackStatus(type, msg) {
    writeBackStatusMsg.hidden = !msg;
    writeBackStatusMsg.className = `status-line ${type}`;
    writeBackStatusMsg.textContent = msg;
  }

  function openWriteBackModal(codeText, codeLang, triggerBtn) {
    writeBackCodePreview.value = codeText;
    writeBackCodeStats.textContent = `${codeText.split('\n').length} 行 · ${codeText.length} 字符${codeLang ? ` · ${codeLang}` : ''}`;
    showWriteBackStatus('', '');

    // 候选目标：已添加到工作区的本地文件 (优先匹配代码语言扩展名)
    writeBackFileSelect.replaceChildren(el('option', '', '从工作区文件中选择…'));
    writeBackFileSelect.firstChild.value = '';
    let bestMatchPath = '';
    cachedActiveFiles.forEach(f => {
      if (!f.local_path) return;
      const opt = el('option', '', `${f.filename}（${f.local_path}）`);
      opt.value = f.local_path;
      writeBackFileSelect.appendChild(opt);
      if (!bestMatchPath) bestMatchPath = f.local_path;
      if (codeLang && String(f.filename).toLowerCase().endsWith('.' + codeLang.toLowerCase())) bestMatchPath = f.local_path;
    });
    if (bestMatchPath) {
      writeBackFileSelect.value = bestMatchPath;
      writeBackPathInput.value = bestMatchPath;
    }
    openModal(writeBackModal, writeBackPathInput);
    currentTriggerWriteBackBtn = triggerBtn;
  }

  writeBackFileSelect.addEventListener('change', () => {
    if (writeBackFileSelect.value) writeBackPathInput.value = writeBackFileSelect.value;
  });
  writeBackCancelBtn.addEventListener('click', () => closeModal(writeBackModal));

  writeBackSubmitBtn.addEventListener('click', async () => {
    const targetPath = writeBackPathInput.value.trim();
    if (!targetPath) {
      showWriteBackStatus('error', '请先选择或输入目标文件路径。');
      writeBackPathInput.focus();
      return;
    }
    writeBackSubmitBtn.disabled = true;
    writeBackSubmitBtn.textContent = '写入中…';
    showWriteBackStatus('info', '正在写入…');
    try {
      const res = await fetch('/api/fs/write', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: targetPath, content: writeBackCodePreview.value, create_backup: writeBackBackupCheckbox.checked })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(uploadErrorDetail(data, res.status));
      writeBackSubmitBtn.textContent = '已写入';
      showWriteBackStatus('success', data.message || `已写入 ${data.filename}`);
      if (currentTriggerWriteBackBtn) {
        currentTriggerWriteBackBtn.classList.add('written');
        currentTriggerWriteBackBtn.innerHTML = `${ICONS.check}<span>已写回</span>`;
      }
      appendSystemNotice(
        `已把代码写入 \`${data.filename}\`（${data.lines} 行）。\n- 位置：\`${data.path}\`` +
        (data.backup_filename ? `\n- 原文件已备份为 \`${data.backup_filename}\`` : ''),
        'success'
      );
      fetchStatus();
      setTimeout(() => {
        closeModal(writeBackModal);
        writeBackSubmitBtn.disabled = false;
        writeBackSubmitBtn.textContent = '写入文件';
      }, 1000);
    } catch (err) {
      writeBackSubmitBtn.disabled = false;
      writeBackSubmitBtn.textContent = '写入文件';
      showWriteBackStatus('error', `写入失败：${err.message}`);
    }
  });

  // ======================================================================
  // 设置：深度思考默认值、联网搜索 (/api/mcp_settings)、开发者信息 (/api/status)
  // ======================================================================
  function openSettings(tab) {
    closeMobileSidebar();
    settingThinkingDefault.checked = thinkingDefault;
    openModal(settingsModal);
    selectSettingsTab(typeof tab === 'string' ? tab : 'general');
    loadMcpSettings();
    fetchStatus();
  }
  $('btn-open-settings').addEventListener('click', () => openSettings());
  devInfo.addEventListener('toggle', () => { if (devInfo.open) fetchStatus(); });

  settingThinkingDefault.addEventListener('change', () => {
    thinkingDefault = settingThinkingDefault.checked;
    lsSet('thinkingDefault', thinkingDefault);
    setThinking(thinkingDefault);
    flashSaved(settingThinkingDefault);
  });

  function flashSaved(control) {
    const row = control.closest('.switch-row');
    if (!row) return;
    row.classList.add('saved-flash');
    setTimeout(() => row.classList.remove('saved-flash'), 900);
  }

  async function loadMcpSettings() {
    try {
      const res = await fetch('/api/mcp_settings');
      if (!res.ok) return;
      const data = await res.json();
      if (data.has_exa_key) {
        exaKeyStatus.className = 'field-hint ok';
        exaKeyStatus.textContent = `已设置密钥（${data.exa_api_key_masked}），搜索会使用 Exa。`;
      } else {
        exaKeyStatus.className = 'field-hint';
        exaKeyStatus.textContent = '未设置密钥，会使用免费的普通网页搜索。';
      }
      searchAutoSaveCheckbox.checked = Boolean(data.auto_save_to_turbovec);
      searchDefaultEnableCheckbox.checked = Boolean(data.web_search_enabled);
      searchHfEnableCheckbox.checked = data.hf_search_enabled !== false;
      const n = String(data.num_results || 4);
      if (!Array.from(searchNumResults.options).some(o => o.value === n)) {
        const opt = el('option', '', `${n} 条`);
        opt.value = n;
        searchNumResults.appendChild(opt);
      }
      searchNumResults.value = n;
    } catch (err) {
      console.warn('获取联网搜索设置失败:', err);
    }
  }

  function searchSettingsPayload() {
    return {
      auto_save_to_turbovec: searchAutoSaveCheckbox.checked,
      web_search_enabled: searchDefaultEnableCheckbox.checked,
      hf_search_enabled: searchHfEnableCheckbox.checked,
      num_results: Number(searchNumResults.value) || 4
    };
  }

  async function postMcpSettings(payload) {
    const res = await fetch('/api/mcp_settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    return res.ok;
  }

  btnSaveExaKey.addEventListener('click', async () => {
    const payload = searchSettingsPayload();
    const keyVal = exaApiKeyInput.value.trim();
    if (keyVal) payload.exa_api_key = keyVal;
    btnSaveExaKey.disabled = true;
    btnSaveExaKey.textContent = '保存中…';
    try {
      if (await postMcpSettings(payload)) {
        exaApiKeyInput.value = '';
        await loadMcpSettings();
        if (payload.web_search_enabled && !webSearchEnabled) setWebSearch(true);
        btnSaveExaKey.textContent = '已保存';
      } else {
        exaKeyStatus.className = 'field-hint error';
        exaKeyStatus.textContent = '保存失败，请稍后再试。';
        btnSaveExaKey.textContent = '保存';
      }
    } catch (err) {
      exaKeyStatus.className = 'field-hint error';
      exaKeyStatus.textContent = `保存出错：${err.message}`;
      btnSaveExaKey.textContent = '保存';
    } finally {
      btnSaveExaKey.disabled = false;
      setTimeout(() => { btnSaveExaKey.textContent = '保存'; }, 1500);
    }
  });

  // 开关类设置即时保存
  [searchAutoSaveCheckbox, searchDefaultEnableCheckbox, searchHfEnableCheckbox, searchNumResults].forEach(ctrl => {
    ctrl.addEventListener('change', async () => {
      const payload = searchSettingsPayload();
      try {
        if (await postMcpSettings(payload)) {
          flashSaved(ctrl);
          // 「默认开启联网搜索」同步到输入框的「🌐 联网」开关
          if (ctrl === searchDefaultEnableCheckbox && payload.web_search_enabled !== webSearchEnabled) {
            setWebSearch(payload.web_search_enabled);
          }
        }
      } catch (err) {
        console.warn('[自动保存] 设置保存失败:', err);
      }
    });
  });

  // 试一试搜索
  async function handleManualSearch() {
    const q = manualSearchInput.value.trim();
    if (!q) return;
    manualSearchSubmit.disabled = true;
    manualSearchSubmit.textContent = '搜索中…';
    manualSearchResults.hidden = false;
    manualSearchResults.innerHTML = '<p class="field-hint">正在搜索…</p>';
    try {
      const res = await fetch('/api/web_search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q, num_results: 4 })
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.results && data.results.length > 0) renderManualSearchResults(q, data.results);
      else manualSearchResults.innerHTML = '<p class="field-hint">没有找到相关结果。</p>';
    } catch (err) {
      manualSearchResults.innerHTML = `<p class="field-hint error">搜索失败：${escapeHtml(err.message)}</p>`;
    } finally {
      manualSearchSubmit.disabled = false;
      manualSearchSubmit.textContent = '搜索';
    }
  }
  manualSearchSubmit.addEventListener('click', handleManualSearch);
  manualSearchInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); handleManualSearch(); }
  });

  function renderManualSearchResults(query, results) {
    manualSearchResults.innerHTML = `
      <div class="manual-search-top">
        <span class="field-hint">找到 ${escapeHtml(results.length)} 条结果</span>
        <button class="btn btn-sm" type="button" id="btn-save-all-rag">全部存入长期记忆</button>
      </div>`;
    const btnSaveAll = manualSearchResults.querySelector('#btn-save-all-rag');
    btnSaveAll.addEventListener('click', async () => {
      btnSaveAll.disabled = true;
      btnSaveAll.textContent = '保存中…';
      try {
        const res = await fetch('/api/save_search_to_rag', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query, results })
        });
        btnSaveAll.textContent = res.ok ? '已存入' : '保存失败';
        if (res.ok) fetchStatus();
      } catch (e) {
        btnSaveAll.textContent = '保存失败';
      }
    });
    results.forEach((r, idx) => {
      const card = el('div', 'manual-search-card');
      card.innerHTML = `
        <div class="manual-search-card-top">
          <a href="${escapeHtml(safeUrl(r.url))}" target="_blank" rel="noopener noreferrer" class="manual-search-card-title">${idx + 1}. ${escapeHtml(r.title)}</a>
          <span class="manual-search-badge">${escapeHtml(r.source || 'Web')}</span>
        </div>
        <div class="manual-search-card-snippet">${escapeHtml(r.snippet || '（无摘要）')}</div>`;
      manualSearchResults.appendChild(card);
    });
  }

  // 开发者信息：所有技术细节集中在这里，仅在设置打开时刷新
  function renderDevInfo() {
    if (!isOpen(settingsModal)) return;
    const s = lastStatus || {};
    const profile = getModelProfile(s.llm_model || currentModelId);
    const v = s.vram_info || {};
    const ctx = lastCtxStats || {};
    const ctxTotal = Number(ctx.total_context_tokens || ((Number(ctx.prompt_tokens) || 0) + (Number(ctx.token_count) || 0))) || 0;
    const nCtx = Number(s.server_n_ctx || ctx.server_n_ctx) || 0;
    const rows = [
      ['当前模型 ID', s.llm_model || currentModelId || '-'],
      ['模型档案', [profile.fullName, profile.tag, profile.speed, profile.vram].filter(Boolean).join(' · ')],
      ['上下文窗口 (n_ctx)', nCtx ? `${nCtx.toLocaleString()} tokens` : '-'],
      ['最近一次上下文占用', ctxTotal && nCtx ? `${ctxTotal.toLocaleString()} / ${nCtx.toLocaleString()} (${Math.min(100, ctxTotal / nCtx * 100).toFixed(1)}%)` : '-'],
      ['显存 (VRAM)', v.total_gb !== undefined
        ? (v.available ? `已用 ${v.used_gb} GB / 共 ${v.total_gb} GB（剩余 ${v.free_gb} GB）` : `剩余 ${v.free_gb} GB（估算）`)
        : '-'],
      ['长期记忆条数', s.memory_count !== undefined ? String(s.memory_count) : '-'],
      ['本次会话轮数', s.session_turns !== undefined ? String(s.session_turns) : '-'],
      ['工作区缓存 (buffer)', s.buffer_mb !== undefined ? `${s.buffer_mb} MB / ${Number(s.max_buffer_gb || 0).toFixed(2)} GB` : '-'],
      ['语义路由 (router)', s.has_laya ? 'Laya-typed-decisions (System 1)' : 'BGE 语义路由'],
      ['向量存储', 'Turbovec 4-bit'],
      ['向量模型', s.embedding_model || '-'],
      ['推理后端', s.active_llm_base || '-']
    ];
    devInfoList.replaceChildren(...rows.flatMap(([k, val]) => [el('dt', '', k), el('dd', '', val)]));
  }

  // ======================================================================
  // Agent：工具 / MCP / 技能 / 系统提示词 (/api/agent/*)
  // 工具名、描述、参数、结果、服务器名、技能内容都是不可信数据：
  // 只通过 textContent / el() 写入 DOM，绝不拼接进 innerHTML。
  // ======================================================================
  async function apiJson(method, url, body) {
    const init = { method, headers: {} };
    if (body !== undefined) {
      if (typeof FormData !== 'undefined' && body instanceof FormData) init.body = body;
      else { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    }
    const res = await fetch(url, init);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(uploadErrorDetail(data, res.status));
      err.status = res.status;
      throw err;
    }
    return data;
  }
  function makeBtn(text, className, onClick) {
    const b = el('button', className, text);
    b.type = 'button';
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }
  function showStatusLine(node, type, msg) {
    if (!node) return;
    node.hidden = !msg;
    node.className = `status-line ${type || ''}`;
    node.textContent = msg || '';
  }
  function emptyNote(text, isError) {
    return el('p', `agent-empty${isError ? ' error' : ''}`, text);
  }
  function firstLine(s, max = 140) {
    const line = String(s || '').split(/\r?\n/).find(l => l.trim()) || '';
    return line.length > max ? line.slice(0, max) + '…' : line;
  }
  function makeSwitch(checked, label, onChange, className = '') {
    const input = el('input', `switch ${className}`.trim());
    input.type = 'checkbox';
    input.checked = Boolean(checked);
    input.setAttribute('aria-label', label);
    input.addEventListener('change', () => onChange(input));
    return input;
  }
  const encSeg = (s) => encodeURIComponent(String(s));

  // ---------- 设置标签页 ----------
  const SETTINGS_TABS = ['general', 'tools', 'mcp', 'skills', 'kg', 'prompts'];
  const settingsTabBtns = Array.from(settingsModal.querySelectorAll('.settings-tab'));
  const settingsBody = settingsModal.querySelector('.modal-body');

  function selectSettingsTab(name, focus) {
    if (!SETTINGS_TABS.includes(name)) name = 'general';
    settingsTab = name;
    settingsTabBtns.forEach(b => {
      const on = b.dataset.tab === name;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    });
    SETTINGS_TABS.forEach(t => { $(`settings-panel-${t}`).hidden = t !== name; });
    if (settingsBody) settingsBody.scrollTop = 0;
    if (name !== 'mcp') stopMcpPoll();
    if (name !== 'kg') stopKgPoll();
    if (name === 'tools') loadAgentTools();
    else if (name === 'mcp') loadMcpServers();
    else if (name === 'skills') loadSkills();
    else if (name === 'prompts') loadPrompts();
    else if (name === 'kg') loadKg();
  }
  settingsTabBtns.forEach(b => b.addEventListener('click', () => selectSettingsTab(b.dataset.tab)));
  settingsModal.querySelector('.settings-tabs').addEventListener('keydown', e => {
    const i = settingsTabBtns.findIndex(b => b.dataset.tab === settingsTab);
    let next = -1;
    if (e.key === 'ArrowRight') next = (i + 1) % settingsTabBtns.length;
    else if (e.key === 'ArrowLeft') next = (i - 1 + settingsTabBtns.length) % settingsTabBtns.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = settingsTabBtns.length - 1;
    if (next < 0) return;
    e.preventDefault();
    selectSettingsTab(settingsTabBtns[next].dataset.tab, true);
  });

  // /#settings-tools 等地址直接打开对应标签 (IDE 面板的「管理…」链接)
  function settingsTabFromHash() {
    const m = /^#settings(?:-([a-z]+))?$/.exec(window.location.hash || '');
    if (!m) return null;
    return SETTINGS_TABS.includes(m[1]) ? m[1] : 'general';
  }
  function openSettingsFromHash() {
    const tab = settingsTabFromHash();
    if (!tab) return;
    if (isOpen(settingsModal)) selectSettingsTab(tab);
    else openSettings(tab);
  }
  function clearSettingsHash() {
    if (!settingsTabFromHash()) return;
    try { history.replaceState(null, '', window.location.pathname + window.location.search); } catch (e) { /* ignore */ }
  }
  window.addEventListener('hashchange', openSettingsFromHash);

  // ---------- 知识图谱 (/api/kg/*) ----------
  // 实体名、关系、证据片段都来自模型抽取与用户记忆 = 不可信数据：只通过 el() / textContent 写入 DOM。
  const KG_TYPES = { person: '人物', org: '组织', place: '地点', concept: '概念', file: '文件', code: '代码', product: '产品', event: '事件', other: '其他' };
  const KG_SOURCES = { chat: '对话', file: '文件', web: '网页' };
  const KG_PAGE = 50;
  const kgStateDot = $('kg-state-dot');
  const kgStateText = $('kg-state-text');
  const kgCounts = $('kg-counts');
  const kgStateHint = $('kg-state-hint');
  const kgLastError = $('kg-last-error');
  const kgLastErrorText = $('kg-last-error-text');
  const kgEnabled = $('kg-enabled');
  const kgPaused = $('kg-paused');
  const kgIdle = $('kg-idle');
  const kgActionStatus = $('kg-action-status');
  const kgClearErrors = $('kg-clear-errors');
  const kgSearch = $('kg-search');
  const kgListView = $('kg-list-view');
  const kgListMeta = $('kg-list-meta');
  const kgEntityList = $('kg-entity-list');
  const kgMore = $('kg-more');
  const kgDetail = $('kg-detail');
  const kgMini = $('kg-mini-status');
  let kgItems = [];
  let kgTotal = 0;
  let kgDetailId = null;
  let kgSearchTimer = 0;
  let kgListSeq = 0;
  let kgMergeSeq = 0;

  function stopKgPoll() {
    if (kgPollTimer) { clearTimeout(kgPollTimer); kgPollTimer = 0; }
  }
  function scheduleKgPoll() {
    stopKgPoll();
    if (isOpen(settingsModal) && settingsTab === 'kg') {
      kgPollTimer = setTimeout(() => { kgPollTimer = 0; refreshKgStatus(); }, 3000);
    }
  }
  function kgTypeLabel(t) { return KG_TYPES[t] || KG_TYPES.other; }
  function kgPending(q) { return (Number(q && q.pending) || 0) + (Number(q && q.running) || 0); }
  function idleLabel(sec) {
    const n = Number(sec) || 0;
    return n >= 60 ? `${Math.round(n / 60)} 分钟` : `${n} 秒`;
  }

  function loadKg() {
    refreshKgStatus();
    if (kgDetailId !== null) openKgEntity(kgDetailId);
    else loadKgEntities(true);
  }

  async function refreshKgStatus() {
    try {
      renderKgStatus(await apiJson('GET', '/api/kg/status'));
    } catch (err) {
      kgStateDot.className = 'status-dot error';
      kgStateText.textContent = '没能读取状态';
      kgStateHint.textContent = err.message;
    } finally {
      scheduleKgPoll();
    }
  }

  function kgStateOf(s) {
    if (!s.enabled) return ['off', '已关闭'];
    if (s.paused) return ['paused', '已暂停'];
    if (s.running) return ['running', '正在后台整理'];
    return ['idle', '空闲中'];
  }

  function kgHint(s) {
    const pending = kgPending(s.queue);
    if (!s.enabled) return '打开「启用知识图谱」后，助手会在你空闲时整理记忆。';
    if (s.paused) return pending ? `还有 ${pending} 条记忆等待整理。` : '';
    if (s.running) return '你一发消息，整理就会马上让位，不会拖慢回答。';
    if (!pending) return s.entities ? '记忆都已整理好。' : '还没有可整理的记忆。和助手聊几句，或点「整理已有记忆」。';
    switch (s.waiting) {
      case 'busy': return `等你停止对话 ${idleLabel(s.idle_seconds)}后开始整理。`;
      case 'llm_unavailable': return '本地模型还没有启动，启动后会自动开始整理。';
      case 'switching': return '正在切换模型，稍后继续整理。';
      case 'ctx_too_small': return '当前模型的上下文太小（不足 4096），暂不整理。';
      default: return '马上开始整理…';
    }
  }

  function renderKgStatus(s) {
    if (!s || typeof s !== 'object') return;
    const [cls, text] = kgStateOf(s);
    kgStateDot.className = `status-dot kg-${cls}`;
    kgStateText.textContent = text;
    const q = s.queue || {};
    const errors = Number(q.error) || 0;
    kgCounts.textContent = `实体 ${Number(s.entities) || 0} · 关系 ${Number(s.relations) || 0} · 待处理 ${kgPending(q)}`
      + (errors ? ` · 失败 ${errors}` : '');
    kgStateHint.textContent = kgHint(s);
    kgLastError.hidden = !s.last_error;
    kgLastErrorText.textContent = String(s.last_error || '');
    kgClearErrors.hidden = !errors;
    if (document.activeElement !== kgEnabled) kgEnabled.checked = Boolean(s.enabled);
    if (document.activeElement !== kgPaused) kgPaused.checked = Boolean(s.paused);
    kgPaused.disabled = !s.enabled;
    const idle = String(Number(s.idle_seconds) || 20);
    if (!Array.from(kgIdle.options).some(o => o.value === idle)) {
      const opt = el('option', '', idleLabel(idle));
      opt.value = idle;
      kgIdle.appendChild(opt);
    }
    if (document.activeElement !== kgIdle) kgIdle.value = idle;
    renderKgMini({ enabled: s.enabled, paused: s.paused, running: s.running, entities: s.entities, pending: kgPending(q) });
  }

  // 侧边栏里的一行小字：知识图谱：N 个实体 · 整理中
  function renderKgMini(k) {
    if (!kgMini) return;
    const show = Boolean(k && k.enabled && ((Number(k.entities) || 0) > 0 || k.running || (Number(k.pending) || 0) > 0));
    kgMini.hidden = !show;
    if (!show) return;
    let tail = '';
    if (k.running) tail = ' · 整理中';
    else if (k.paused) tail = ' · 已暂停';
    else if (Number(k.pending) > 0) tail = ` · ${Number(k.pending)} 条待整理`;
    kgMini.textContent = `知识图谱：${Number(k.entities) || 0} 个实体${tail}`;
  }

  async function saveKgSettings(patch, control) {
    control.disabled = true;
    try {
      renderKgStatus(await apiJson('POST', '/api/kg/settings', patch));
      flashSaved(control);
    } catch (err) {
      if (control.type === 'checkbox') control.checked = !control.checked;
      showStatusLine(kgActionStatus, 'error', `保存失败：${err.message}`);
    } finally {
      control.disabled = false;
      if (control === kgPaused && !kgEnabled.checked) kgPaused.disabled = true;
    }
  }
  kgEnabled.addEventListener('change', () => saveKgSettings({ enabled: kgEnabled.checked }, kgEnabled));
  kgPaused.addEventListener('change', () => saveKgSettings({ paused: kgPaused.checked }, kgPaused));
  kgIdle.addEventListener('change', () => saveKgSettings({ idle_seconds: Number(kgIdle.value) || 20 }, kgIdle));

  async function kgRebuild(mode, btn) {
    if (mode === 'all') {
      const ok = await confirmDialog({
        title: '重新整理全部记忆？',
        body: '现有的实体和关系会被清空，然后按全部记忆重新整理（你手动做的合并和删除也会丢失）。整理只在你空闲时进行，可能需要一段时间。',
        okText: '重新整理',
        danger: true
      });
      if (!ok) return;
    }
    btn.disabled = true;
    try {
      const r = await apiJson('POST', '/api/kg/rebuild', { mode });
      const n = Number(r.enqueued) || 0;
      showStatusLine(kgActionStatus, 'success', n
        ? `已加入 ${n} 条记忆，会在你空闲时整理。`
        : (mode === 'all' ? '没有可整理的记忆。' : '没有需要整理的新记忆。'));
      if (mode === 'all') { kgDetailId = null; showKgList(); }
      await refreshKgStatus();
      loadKgEntities(true);
    } catch (err) {
      showStatusLine(kgActionStatus, 'error', `操作失败：${err.message}`);
    } finally {
      btn.disabled = false;
    }
  }
  $('kg-rebuild-missing').addEventListener('click', e => kgRebuild('missing', e.currentTarget));
  $('kg-rebuild-all').addEventListener('click', e => kgRebuild('all', e.currentTarget));
  kgClearErrors.addEventListener('click', async () => {
    kgClearErrors.disabled = true;
    try {
      const r = await apiJson('POST', '/api/kg/queue/clear_errors');
      showStatusLine(kgActionStatus, 'success', `已清除 ${Number(r.cleared) || 0} 个失败任务。点「整理已有记忆」可以重试。`);
      await refreshKgStatus();
    } catch (err) {
      showStatusLine(kgActionStatus, 'error', `操作失败：${err.message}`);
    } finally {
      kgClearErrors.disabled = false;
    }
  });

  // 实体列表
  async function loadKgEntities(reset) {
    const seq = ++kgListSeq;
    const q = kgSearch.value.trim();
    const offset = reset ? 0 : kgItems.length;
    try {
      const d = await apiJson('GET', `/api/kg/entities?q=${encodeURIComponent(q)}&limit=${KG_PAGE}&offset=${offset}`);
      if (seq !== kgListSeq) return;   // 输入更快：丢弃过期结果
      const items = Array.isArray(d.items) ? d.items.filter(x => x && x.id !== undefined) : [];
      kgItems = reset ? items : kgItems.concat(items);
      kgTotal = Number(d.total) || 0;
      renderKgEntities(q);
    } catch (err) {
      if (seq !== kgListSeq) return;
      kgEntityList.replaceChildren(emptyNote(`没能读取实体：${err.message}`, true));
      kgMore.hidden = true;
    }
  }

  function renderKgEntities(q) {
    if (kgItems.length === 0) {
      kgEntityList.replaceChildren(emptyNote(q ? `没有找到和「${q}」相关的实体。` : '还没有实体。助手整理记忆后，这里会列出提到过的人物、项目和概念。'));
      kgListMeta.textContent = '';
      kgMore.hidden = true;
      return;
    }
    kgListMeta.textContent = q ? `找到 ${kgTotal} 个实体` : `共 ${kgTotal} 个实体，按关系数排序`;
    kgEntityList.replaceChildren(...kgItems.map(kgEntityRow));
    kgMore.hidden = kgItems.length >= kgTotal;
  }

  function kgEntityRow(e) {
    const row = el('button', 'kg-entity-row');
    row.type = 'button';
    row.dataset.id = String(e.id);
    const head = el('span', 'kg-entity-head');
    head.appendChild(el('span', 'kg-entity-name', e.name));
    head.appendChild(el('span', `badge kg-type kg-type-${KG_TYPES[e.type] ? e.type : 'other'}`, kgTypeLabel(e.type)));
    row.appendChild(head);
    const meta = el('span', 'kg-entity-meta', `${Number(e.relation_count) || 0} 条关系 · 提到 ${Number(e.mention_count) || 0} 次`);
    row.appendChild(meta);
    if (e.description) row.appendChild(el('span', 'kg-entity-desc', e.description));
    row.addEventListener('click', () => openKgEntity(e.id));
    return row;
  }

  kgSearch.addEventListener('input', () => {
    clearTimeout(kgSearchTimer);
    kgSearchTimer = setTimeout(() => loadKgEntities(true), 250);
  });
  kgSearch.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); clearTimeout(kgSearchTimer); loadKgEntities(true); }
  });
  kgMore.addEventListener('click', () => loadKgEntities(false));

  function showKgList() {
    kgDetail.hidden = true;
    kgDetail.replaceChildren();
    kgListView.hidden = false;
  }

  // 实体详情：关系 A —关系→ B（可删除）、来源片段、合并到…、删除实体
  async function openKgEntity(id) {
    try {
      const d = await apiJson('GET', `/api/kg/entities/${encSeg(id)}`);
      kgDetailId = d.entity.id;
      renderKgDetail(d);
      kgListView.hidden = true;
      kgDetail.hidden = false;
    } catch (err) {
      kgDetailId = null;
      showKgList();
      if (err.status === 404) loadKgEntities(true);
      showStatusLine(kgActionStatus, 'error', err.status === 404 ? '这个实体已经不存在了。' : `没能读取实体：${err.message}`);
    }
  }

  function kgBackToList() {
    kgDetailId = null;
    showKgList();
    loadKgEntities(true);
    kgSearch.focus();
  }

  function renderKgDetail(d) {
    const ent = d.entity;
    const rels = Array.isArray(d.relations) ? d.relations : [];
    const back = makeBtn('← 返回列表', 'link-btn kg-back', kgBackToList);

    const head = el('div', 'kg-detail-head');
    const title = el('h5', 'kg-detail-name', ent.name);
    head.appendChild(title);
    head.appendChild(el('span', `badge kg-type kg-type-${KG_TYPES[ent.type] ? ent.type : 'other'}`, kgTypeLabel(ent.type)));

    const info = el('div', 'kg-detail-info');
    if (ent.description) info.appendChild(el('p', 'kg-detail-desc', ent.description));
    info.appendChild(el('p', 'field-hint', `提到 ${Number(ent.mention_count) || 0} 次 · ${rels.length} 条关系`));
    const aliases = Array.isArray(ent.aliases) ? ent.aliases : [];
    if (aliases.length) info.appendChild(el('p', 'field-hint kg-aliases', `也叫：${aliases.join('、')}`));

    const actions = el('div', 'mcp-actions kg-detail-actions');
    const mergeBtn = makeBtn('合并到…', 'btn btn-sm kg-merge-btn');
    const delBtn = makeBtn('删除实体', 'btn btn-sm btn-ghost-danger kg-delete-entity', () => kgDeleteEntity(ent));
    actions.appendChild(mergeBtn);
    actions.appendChild(delBtn);

    const mergeBox = kgMergeBox(ent);
    mergeBox.hidden = true;
    mergeBtn.addEventListener('click', () => {
      mergeBox.hidden = !mergeBox.hidden;
      if (!mergeBox.hidden) { mergeBox.reload(''); mergeBox.querySelector('input').focus(); }
    });

    const list = el('ul', 'kg-rel-list');
    if (rels.length === 0) list.appendChild(el('li', 'agent-empty', '这个实体还没有关系。'));
    rels.forEach(r => list.appendChild(kgRelationRow(r, ent)));

    kgDetail.replaceChildren(back, head, info, actions, mergeBox, el('h5', 'kg-rel-title', '关系'), list);
  }

  function kgEndpoint(node, current) {
    const name = node && node.name !== undefined ? node.name : '';
    if (!node || node.id === current.id) return el('span', 'kg-rel-end is-current', name);
    const b = makeBtn(name, 'kg-rel-end kg-rel-link', () => openKgEntity(node.id));
    b.title = `查看「${name}」`;
    return b;
  }

  function kgRelationRow(r, current) {
    const li = el('li', 'kg-rel-row');
    li.dataset.id = String(r.id);
    const top = el('div', 'kg-rel-top');
    const line = el('div', 'kg-rel-line');
    line.appendChild(kgEndpoint(r.head, current));
    line.appendChild(el('span', 'kg-rel-verb', ` —${r.relation}→ `));
    line.appendChild(kgEndpoint(r.tail, current));
    top.appendChild(line);
    const del = makeBtn('删除', 'btn btn-sm btn-ghost-danger kg-rel-delete', () => kgDeleteRelation(r, li, del));
    del.setAttribute('aria-label', `删除关系：${r.head.name} ${r.relation} ${r.tail.name}`);
    top.appendChild(del);
    li.appendChild(top);

    const ev = Array.isArray(r.evidence) ? r.evidence : [];
    if (ev.length) {
      const det = el('details', 'kg-evidence');
      det.appendChild(el('summary', '', `${Number(r.evidence_count) || ev.length} 条来源`));
      const ul = el('ul', 'kg-evidence-list');
      ev.forEach(e => {
        const item = el('li');
        const src = el('span', 'badge kg-source', KG_SOURCES[e.source_kind] || '来源');
        item.appendChild(src);
        if (e.source_kind === 'file' && e.source_ref) item.appendChild(el('span', 'kg-source-ref', e.source_ref));
        item.appendChild(el('span', 'kg-snippet', e.snippet || '（无片段）'));
        ul.appendChild(item);
      });
      det.appendChild(ul);
      li.appendChild(det);
    }
    return li;
  }

  async function kgDeleteRelation(r, li, btn) {
    btn.disabled = true;
    try {
      await apiJson('DELETE', `/api/kg/relations/${encSeg(r.id)}`);
      li.remove();
      const list = kgDetail.querySelector('.kg-rel-list');
      if (list && !list.querySelector('.kg-rel-row')) list.replaceChildren(el('li', 'agent-empty', '这个实体还没有关系。'));
      refreshKgStatus();
    } catch (err) {
      btn.disabled = false;
      showStatusLine(kgActionStatus, 'error', `删除失败：${err.message}`);
    }
  }

  async function kgDeleteEntity(ent) {
    const ok = await confirmDialog({
      title: '删除这个实体？',
      body: `「${ent.name}」和它的所有关系都会被删除。以后的对话里再次提到时，助手可能会重新整理出来。`,
      okText: '删除',
      danger: true
    });
    if (!ok) return;
    try {
      await apiJson('DELETE', `/api/kg/entities/${encSeg(ent.id)}`);
      showStatusLine(kgActionStatus, 'success', `已删除「${ent.name}」。`);
      kgBackToList();
      refreshKgStatus();
    } catch (err) {
      showStatusLine(kgActionStatus, 'error', `删除失败：${err.message}`);
    }
  }

  // 合并到…：把当前实体并入另一个实体 (保留目标实体，当前名字作为别名)
  function kgMergeBox(ent) {
    const box = el('div', 'agent-form kg-merge-box');
    box.appendChild(el('h4', 'agent-form-title', `把「${ent.name}」合并到另一个实体`));
    box.appendChild(el('p', 'field-hint', '合并后两者的关系会放在一起，「' + ent.name + '」会作为别名保留。'));
    const field = el('div', 'field');
    const input = el('input', 'text-input kg-merge-search');
    input.type = 'search';
    input.placeholder = '搜索要合并到的实体…';
    input.setAttribute('aria-label', '搜索要合并到的实体');
    input.autocomplete = 'off';
    const select = el('select', 'select-input kg-merge-select');
    select.setAttribute('aria-label', '合并到的实体');
    field.appendChild(input);
    field.appendChild(select);
    box.appendChild(field);
    const status = el('p', 'status-line');
    status.hidden = true;
    status.setAttribute('role', 'status');
    box.appendChild(status);
    const acts = el('div', 'agent-form-actions');
    const cancel = makeBtn('取消', 'btn btn-sm', () => { box.hidden = true; });
    const ok = makeBtn('确定合并', 'btn btn-sm btn-primary kg-merge-confirm');
    acts.appendChild(cancel);
    acts.appendChild(ok);
    box.appendChild(acts);

    async function reload(q) {
      const seq = ++kgMergeSeq;
      try {
        const d = await apiJson('GET', `/api/kg/entities?q=${encodeURIComponent(q)}&limit=20&offset=0`);
        if (seq !== kgMergeSeq) return;
        const opts = (Array.isArray(d.items) ? d.items : []).filter(x => x && x.id !== ent.id);
        select.replaceChildren(...opts.map(x => {
          const o = el('option', '', `${x.name}（${kgTypeLabel(x.type)} · ${Number(x.relation_count) || 0} 条关系）`);
          o.value = String(x.id);
          return o;
        }));
        ok.disabled = opts.length === 0;
        if (!opts.length) showStatusLine(status, '', '没有找到可以合并的实体。');
        else showStatusLine(status, '', '');
      } catch (err) {
        if (seq === kgMergeSeq) showStatusLine(status, 'error', `没能读取实体：${err.message}`);
      }
    }
    box.reload = reload;
    let t = 0;
    input.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => reload(input.value.trim()), 250); });
    ok.addEventListener('click', async () => {
      const target = Number(select.value);
      if (!target) return;
      const targetName = select.options[select.selectedIndex] ? select.options[select.selectedIndex].textContent : '';
      ok.disabled = true;
      try {
        const r = await apiJson('POST', '/api/kg/entities/merge', { keep_id: target, merge_ids: [ent.id] });
        const keptName = r && r.entity && r.entity.name ? r.entity.name : targetName;
        showStatusLine(kgActionStatus, 'success', `已把「${ent.name}」合并到「${keptName}」。`);
        await openKgEntity(target);
        refreshKgStatus();
      } catch (err) {
        ok.disabled = false;
        showStatusLine(status, 'error', `合并失败：${err.message}`);
      }
    });
    return box;
  }

  // ---------- 工具 ----------
  const agentToolsList = $('agent-tools-list');

  async function loadAgentTools() {
    try {
      const [t, m] = await Promise.all([
        apiJson('GET', '/api/agent/tools'),
        apiJson('GET', '/api/agent/mcp').catch(() => null)
      ]);
      agentTools = Array.isArray(t.tools) ? t.tools.filter(x => x && x.name) : [];
      if (m && Array.isArray(m.servers)) mcpServers = m.servers;
      renderAgentTools();
    } catch (err) {
      agentToolsList.replaceChildren(emptyNote(`没能读取工具列表：${err.message}`, true));
    }
  }

  function toolSourceLabel(source) {
    const src = String(source || '');
    if (src === 'builtin') return '内置';
    if (src === 'skill') return '技能';
    if (src.startsWith('mcp:')) {
      const id = src.slice(4);
      const s = mcpServers.find(x => x && x.id === id);
      return `MCP · ${s && s.name ? s.name : id}`;
    }
    return src || '其他';
  }

  function renderAgentTools() {
    if (agentTools.length === 0) {
      agentToolsList.replaceChildren(emptyNote('还没有可用的工具。'));
      return;
    }
    const order = (src) => (src === 'builtin' ? 0 : String(src).startsWith('mcp:') ? 1 : src === 'skill' ? 2 : 3);
    const groups = new Map();
    agentTools.forEach(t => {
      const src = String(t.source || 'builtin');
      if (!groups.has(src)) groups.set(src, []);
      groups.get(src).push(t);
    });
    const sections = Array.from(groups.entries())
      .sort((a, b) => order(a[0]) - order(b[0]))
      .map(([src, tools]) => {
        const section = el('section', 'agent-group');
        section.dataset.source = src;
        const title = el('h5', 'agent-group-title');
        title.appendChild(el('span', '', toolSourceLabel(src)));
        title.appendChild(el('span', 'count', `${tools.length} 个`));
        const rows = el('div', 'agent-group-rows');
        tools.forEach(t => rows.appendChild(toolRow(t)));
        section.appendChild(title);
        section.appendChild(rows);
        return section;
      });
    agentToolsList.replaceChildren(...sections);
  }

  function toolRow(t) {
    const row = el('div', 'agent-row tool-row');
    row.dataset.tool = t.name;
    row.classList.toggle('disabled', t.enabled === false);
    const title = t.title || t.name;
    const main = el('div', 'agent-row-main');
    const head = el('div', 'agent-row-title');
    const name = el('span', 'tool-title', title);
    name.title = t.name;
    head.appendChild(name);
    head.appendChild(el('span', t.read_only ? 'badge badge-ro' : 'badge badge-confirm', t.read_only ? '只读' : '需确认'));
    main.appendChild(head);
    const desc = el('div', 'agent-row-desc', firstLine(t.description) || '（没有说明）');
    desc.title = String(t.description || '');
    main.appendChild(desc);

    const ctrls = el('div', 'agent-row-ctrls');
    if (!t.read_only) {
      const wrap = el('label', 'mini-switch');
      const always = makeSwitch(t.always_allow, `始终允许 ${title}（不再询问）`, input => updateTool(t, { always_allow: input.checked }, input), 'tool-always');
      wrap.appendChild(always);
      wrap.appendChild(el('span', '', '始终允许'));
      wrap.title = '开启后执行这个工具前不再询问';
      ctrls.appendChild(wrap);
    }
    ctrls.appendChild(makeSwitch(t.enabled !== false, `启用 ${title}`, input => updateTool(t, { enabled: input.checked }, input), 'tool-enabled'));
    row.appendChild(main);
    row.appendChild(ctrls);
    return row;
  }

  async function updateTool(tool, patch, input) {
    input.disabled = true;
    try {
      await apiJson('POST', `/api/agent/tools/${encSeg(tool.name)}`, patch);
      Object.assign(tool, patch);
      const row = input.closest('.agent-row');
      if (row && 'enabled' in patch) row.classList.toggle('disabled', !patch.enabled);
    } catch (err) {
      input.checked = !input.checked;   // 失败时还原
      appendSettingsError(agentToolsList, `保存失败：${err.message}`);
    } finally {
      input.disabled = false;
    }
  }

  function appendSettingsError(listNode, msg) {
    const prev = listNode.parentNode.querySelector('.settings-inline-error');
    if (prev) prev.remove();
    const p = el('p', 'status-line error settings-inline-error', msg);
    p.setAttribute('role', 'alert');
    listNode.parentNode.insertBefore(p, listNode);
    setTimeout(() => p.remove(), 5000);
  }

  // ---------- MCP ----------
  const MCP_TRANSPORTS = { stdio: '本地命令', http: 'HTTP', sse: 'SSE' };
  const MCP_STATUS_TEXT = { connected: '已连接', connecting: '连接中', error: '错误', disabled: '已停用' };
  const mcpServerList = $('mcp-server-list');
  const mcpForm = $('mcp-form');
  const mcpFormStatus = $('mcp-form-status');
  const mcpTransport = $('mcp-transport');
  const mcpImportBox = $('mcp-import-box');
  const mcpImportJson = $('mcp-import-json');
  const mcpImportStatus = $('mcp-import-status');

  function stopMcpPoll() {
    if (mcpPollTimer) { clearTimeout(mcpPollTimer); mcpPollTimer = 0; }
  }
  function scheduleMcpPoll() {
    stopMcpPoll();
    const connecting = mcpServers.some(s => s && s.enabled !== false && s.status === 'connecting');
    if (connecting && isOpen(settingsModal) && settingsTab === 'mcp') {
      mcpPollTimer = setTimeout(() => { mcpPollTimer = 0; loadMcpServers(); }, 3000);
    }
  }

  async function loadMcpServers() {
    try {
      const data = await apiJson('GET', '/api/agent/mcp');
      mcpServers = Array.isArray(data.servers) ? data.servers.filter(s => s && s.id) : [];
      renderMcpServers();
    } catch (err) {
      mcpServerList.replaceChildren(emptyNote(`没能读取 MCP 服务器：${err.message}`, true));
    } finally {
      scheduleMcpPoll();
    }
  }

  function mcpStatusOf(s) {
    if (s.enabled === false) return 'disabled';
    return MCP_STATUS_TEXT[s.status] ? s.status : 'connecting';
  }

  function renderMcpServers() {
    // 轮询重绘时保留用户展开的「错误信息 / 工具列表」
    const openKeys = new Set(Array.from(mcpServerList.querySelectorAll('details[open]'))
      .map(d => `${d.closest('.mcp-card') ? d.closest('.mcp-card').dataset.id : ''}|${d.dataset.kind}`));
    if (mcpServers.length === 0) {
      mcpServerList.replaceChildren(emptyNote('还没有 MCP 服务器。点「添加服务器」或「导入 JSON」开始。'));
      return;
    }
    mcpServerList.replaceChildren(...mcpServers.map(s => mcpCard(s, openKeys)));
  }

  function mcpCard(s, openKeys) {
    const status = mcpStatusOf(s);
    const card = el('article', `mcp-card${status === 'error' ? ' is-error' : ''}`);
    card.dataset.id = s.id;
    card.dataset.status = status;
    const head = el('div', 'mcp-card-head');
    const name = el('span', 'mcp-name', s.name || s.id);
    name.title = String(s.name || s.id);
    head.appendChild(name);
    head.appendChild(el('span', 'badge', MCP_TRANSPORTS[s.transport] || String(s.transport || '')));
    const st = el('span', 'mcp-status');
    const dot = el('span', `status-dot ${status}`);
    dot.setAttribute('aria-hidden', 'true');
    st.appendChild(dot);
    st.appendChild(el('span', 'mcp-status-text', MCP_STATUS_TEXT[status]));
    head.appendChild(st);
    card.appendChild(head);

    const target = s.transport === 'stdio'
      ? [s.command || ''].concat(Array.isArray(s.args) ? s.args : []).join(' ').trim()
      : String(s.url || '');
    if (target) {
      const t = el('div', 'mcp-target', target);
      t.title = target;
      card.appendChild(t);
    }

    if (s.error && status !== 'disabled') {
      const d = el('details', 'mcp-details mcp-error');
      d.dataset.kind = 'error';
      d.appendChild(el('summary', '', '查看错误信息'));
      d.appendChild(el('pre', '', String(s.error)));
      if (openKeys && openKeys.has(`${s.id}|error`)) d.open = true;
      card.appendChild(d);
    }

    const tools = Array.isArray(s.tools) ? s.tools : [];
    if (tools.length > 0) {
      const d = el('details', 'mcp-details mcp-tools');
      d.dataset.kind = 'tools';
      d.appendChild(el('summary', '', `${tools.length} 个工具`));
      const ul = el('ul', 'mcp-tool-list');
      tools.forEach(tool => {
        const li = el('li');
        li.appendChild(el('span', 'mcp-tool-name', tool.name));
        li.appendChild(el('span', tool.read_only ? 'badge badge-ro' : 'badge badge-confirm', tool.read_only ? '只读' : '需确认'));
        if (tool.description) li.appendChild(el('span', 'mcp-tool-desc', firstLine(tool.description, 200)));
        ul.appendChild(li);
      });
      d.appendChild(ul);
      if (openKeys && openKeys.has(`${s.id}|tools`)) d.open = true;
      card.appendChild(d);
    } else if (status === 'connected') {
      card.appendChild(el('p', 'field-hint', '这个服务器没有提供工具。'));
    }

    const actions = el('div', 'mcp-actions');
    const enabled = s.enabled !== false;
    actions.appendChild(makeBtn(enabled ? '停用' : '启用', 'btn btn-sm mcp-toggle-btn', () => mcpAction(s, 'toggle')));
    const restart = makeBtn('重启', 'btn btn-sm mcp-restart-btn', () => mcpAction(s, 'restart'));
    restart.disabled = !enabled;
    actions.appendChild(restart);
    actions.appendChild(makeBtn('编辑', 'btn btn-sm mcp-edit-btn', () => openMcpForm(s)));
    actions.appendChild(makeBtn('删除', 'btn btn-sm btn-ghost-danger mcp-delete-btn', () => mcpAction(s, 'delete')));
    card.appendChild(actions);
    return card;
  }

  function replaceServer(updated) {
    if (!updated || !updated.id) return false;
    const i = mcpServers.findIndex(x => x.id === updated.id);
    if (i === -1) return false;
    mcpServers[i] = updated;
    return true;
  }

  async function mcpAction(s, action) {
    const label = s.name || s.id;
    if (action === 'delete') {
      const ok = await confirmDialog({
        title: '删除 MCP 服务器？',
        body: `「${label}」会被删除，它提供的工具也会一起移除。`,
        okText: '删除',
        danger: true
      });
      if (!ok) return;
    }
    const card = Array.from(mcpServerList.querySelectorAll('.mcp-card')).find(c => c.dataset.id === s.id);
    if (card) card.querySelectorAll('.mcp-actions button').forEach(b => { b.disabled = true; });
    try {
      if (action === 'delete') {
        await apiJson('DELETE', `/api/agent/mcp/${encSeg(s.id)}`);
        mcpServers = mcpServers.filter(x => x.id !== s.id);
        renderMcpServers();
        return;
      }
      if (action === 'restart' && card) {
        card.querySelector('.status-dot').className = 'status-dot connecting';
        card.querySelector('.mcp-status-text').textContent = MCP_STATUS_TEXT.connecting;
      }
      const updated = action === 'toggle'
        ? await apiJson('PUT', `/api/agent/mcp/${encSeg(s.id)}`, { enabled: s.enabled === false })
        : await apiJson('POST', `/api/agent/mcp/${encSeg(s.id)}/restart`);
      if (replaceServer(updated)) { renderMcpServers(); scheduleMcpPoll(); }
      else await loadMcpServers();
    } catch (err) {
      appendSettingsError(mcpServerList, `操作失败：${err.message}`);
      await loadMcpServers();
    }
  }

  function syncMcpTransportFields() {
    const stdio = mcpTransport.value === 'stdio';
    $('mcp-stdio-fields').hidden = !stdio;
    $('mcp-http-fields').hidden = stdio;
  }
  mcpTransport.addEventListener('change', syncMcpTransportFields);

  const pairsToText = (obj, sep) => Object.entries(obj && typeof obj === 'object' ? obj : {})
    .map(([k, v]) => `${k}${sep}${v}`).join('\n');

  function openMcpForm(server) {
    mcpEditingId = server ? server.id : null;
    $('mcp-form-title').textContent = server ? `编辑「${server.name || server.id}」` : '添加服务器';
    $('mcp-name').value = server ? (server.name || '') : '';
    mcpTransport.value = server && MCP_TRANSPORTS[server.transport] ? server.transport : 'stdio';
    $('mcp-command').value = server ? (server.command || '') : '';
    $('mcp-args').value = server && Array.isArray(server.args) ? server.args.join('\n') : '';
    $('mcp-env').value = server ? pairsToText(server.env, '=') : '';
    $('mcp-url').value = server ? (server.url || '') : '';
    $('mcp-headers').value = server ? pairsToText(server.headers, ': ') : '';
    const masked = server && [server.env, server.headers].some(o => o && Object.values(o).some(v => v === '***'));
    $('mcp-secret-hint').hidden = !masked;
    showStatusLine(mcpFormStatus, '', '');
    syncMcpTransportFields();
    mcpImportBox.hidden = true;
    mcpForm.hidden = false;
    mcpForm.scrollIntoView({ block: 'nearest' });
    $('mcp-name').focus();
  }
  function closeMcpForm() {
    mcpForm.hidden = true;
    mcpEditingId = null;
  }

  const splitLines = (text) => String(text || '').split(/\r?\n/).map(l => l.trim()).filter(Boolean);
  function parsePairs(text, sep) {
    const out = {};
    const bad = [];
    splitLines(text).forEach(line => {
      const i = line.indexOf(sep);
      const key = i > 0 ? line.slice(0, i).trim() : '';
      if (!key) { bad.push(line); return; }
      out[key] = line.slice(i + sep.length).trim();
    });
    return { out, bad };
  }

  async function saveMcpForm(e) {
    if (e) e.preventDefault();
    const name = $('mcp-name').value.trim();
    const transport = mcpTransport.value;
    if (!name) { showStatusLine(mcpFormStatus, 'error', '请填写名称。'); $('mcp-name').focus(); return; }
    const body = { name, transport, command: '', args: [], env: {}, url: '', headers: {} };
    if (transport === 'stdio') {
      body.command = $('mcp-command').value.trim();
      if (!body.command) { showStatusLine(mcpFormStatus, 'error', '请填写要运行的命令，例如 npx。'); $('mcp-command').focus(); return; }
      body.args = splitLines($('mcp-args').value);
      const env = parsePairs($('mcp-env').value, '=');
      if (env.bad.length) { showStatusLine(mcpFormStatus, 'error', `环境变量格式应为 KEY=VALUE：${env.bad[0]}`); $('mcp-env').focus(); return; }
      body.env = env.out;
    } else {
      body.url = $('mcp-url').value.trim();
      if (!/^https?:\/\//i.test(body.url)) { showStatusLine(mcpFormStatus, 'error', '地址需要以 http:// 或 https:// 开头。'); $('mcp-url').focus(); return; }
      const headers = parsePairs($('mcp-headers').value, ':');
      if (headers.bad.length) { showStatusLine(mcpFormStatus, 'error', `请求头格式应为 Key: Value：${headers.bad[0]}`); $('mcp-headers').focus(); return; }
      body.headers = headers.out;
    }
    const existing = mcpEditingId ? mcpServers.find(x => x.id === mcpEditingId) : null;
    body.enabled = existing ? existing.enabled !== false : true;
    const saveBtn = $('mcp-form-save');
    saveBtn.disabled = true;
    showStatusLine(mcpFormStatus, '', '正在保存并连接…');
    try {
      const saved = mcpEditingId
        ? await apiJson('PUT', `/api/agent/mcp/${encSeg(mcpEditingId)}`, body)
        : await apiJson('POST', '/api/agent/mcp', body);
      closeMcpForm();
      if (saved && saved.id && !replaceServer(saved)) mcpServers.push(saved);
      renderMcpServers();
      await loadMcpServers();
    } catch (err) {
      showStatusLine(mcpFormStatus, 'error', `保存失败：${err.message}`);
    } finally {
      saveBtn.disabled = false;
    }
  }
  mcpForm.addEventListener('submit', saveMcpForm);
  $('mcp-form-cancel').addEventListener('click', closeMcpForm);
  $('btn-mcp-add').addEventListener('click', () => openMcpForm(null));

  $('btn-mcp-import').addEventListener('click', () => {
    closeMcpForm();
    mcpImportBox.hidden = false;
    showStatusLine(mcpImportStatus, '', '');
    mcpImportJson.focus();
  });
  $('mcp-import-cancel').addEventListener('click', () => { mcpImportBox.hidden = true; });
  $('mcp-import-submit').addEventListener('click', async () => {
    const text = mcpImportJson.value.trim();
    let parsed;
    try { parsed = JSON.parse(text); } catch (e) {
      showStatusLine(mcpImportStatus, 'error', '这不是有效的 JSON，请检查括号和引号。');
      return;
    }
    if (!parsed || typeof parsed !== 'object' || !parsed.mcpServers || typeof parsed.mcpServers !== 'object') {
      showStatusLine(mcpImportStatus, 'error', '没有找到 "mcpServers" 字段。');
      return;
    }
    const submit = $('mcp-import-submit');
    submit.disabled = true;
    showStatusLine(mcpImportStatus, '', '正在导入…');
    try {
      const data = await apiJson('POST', '/api/agent/mcp/import', { json: text });
      const n = Array.isArray(data.added) ? data.added.length : 0;
      showStatusLine(mcpImportStatus, 'success', n ? `已导入 ${n} 个服务器。` : '没有新增服务器（可能已存在）。');
      mcpImportJson.value = '';
      await loadMcpServers();
      setTimeout(() => { if (!mcpImportJson.value) mcpImportBox.hidden = true; }, 1200);
    } catch (err) {
      showStatusLine(mcpImportStatus, 'error', `导入失败：${err.message}`);
    } finally {
      submit.disabled = false;
    }
  });

  // ---------- 技能 ----------
  const skillList = $('skill-list');
  const skillForm = $('skill-form');
  const skillFormStatus = $('skill-form-status');
  const skillImportFile = $('skill-import-file');
  const skillImportStatus = $('skill-import-status');

  async function loadSkills() {
    try {
      const data = await apiJson('GET', '/api/agent/skills');
      skills = Array.isArray(data.skills) ? data.skills.filter(s => s && s.slug) : [];
      renderSkills();
    } catch (err) {
      skillList.replaceChildren(emptyNote(`没能读取技能：${err.message}`, true));
    }
  }

  function renderSkills() {
    if (skills.length === 0) {
      skillList.replaceChildren(emptyNote('还没有技能。可以新建一个，或导入别人分享的 SKILL.md。'));
      return;
    }
    const rows = el('div', 'agent-group-rows');
    skills.forEach(s => {
      const row = el('div', 'agent-row skill-row');
      row.dataset.slug = s.slug;
      row.classList.toggle('disabled', s.enabled === false);
      const main = el('div', 'agent-row-main');
      const head = el('div', 'agent-row-title');
      head.appendChild(el('span', 'skill-name', s.name || s.slug));
      main.appendChild(head);
      const desc = el('div', 'agent-row-desc', firstLine(s.description) || '（没有说明）');
      desc.title = String(s.description || '');
      main.appendChild(desc);
      const ctrls = el('div', 'agent-row-ctrls');
      const btns = el('div', 'row-btns');
      btns.appendChild(makeBtn('编辑', 'btn btn-sm skill-edit-btn', () => editSkill(s)));
      btns.appendChild(makeBtn('删除', 'btn btn-sm btn-ghost-danger skill-delete-btn', () => deleteSkill(s)));
      ctrls.appendChild(btns);
      ctrls.appendChild(makeSwitch(s.enabled !== false, `启用技能 ${s.name || s.slug}`, async input => {
        input.disabled = true;
        try {
          await apiJson('PUT', `/api/agent/skills/${encSeg(s.slug)}`, { enabled: input.checked });
          s.enabled = input.checked;
          row.classList.toggle('disabled', !input.checked);
        } catch (err) {
          input.checked = !input.checked;
          appendSettingsError(skillList, `保存失败：${err.message}`);
        } finally {
          input.disabled = false;
        }
      }, 'skill-enabled'));
      row.appendChild(main);
      row.appendChild(ctrls);
      rows.appendChild(row);
    });
    skillList.replaceChildren(rows);
  }

  // 编辑器里只放正文；名称与说明由单独的输入框维护 (后端据此生成 frontmatter)
  function stripFrontmatter(text) {
    return String(text || '').replace(/^﻿?---\r?\n[\s\S]*?\r?\n---[^\S\r\n]*(?:\r?\n|$)/, '').replace(/^\s*\n/, '');
  }

  function openSkillForm(skill) {
    skillEditingSlug = skill ? skill.slug : null;
    $('skill-form-title').textContent = skill ? `编辑「${skill.name || skill.slug}」` : '新建技能';
    $('skill-name').value = skill ? (skill.name || '') : '';
    $('skill-description').value = skill ? (skill.description || '') : '';
    $('skill-content').value = skill ? stripFrontmatter(skill.content) : '';
    showStatusLine(skillFormStatus, '', '');
    skillForm.hidden = false;
    skillForm.scrollIntoView({ block: 'nearest' });
    $('skill-name').focus();
  }
  function closeSkillForm() {
    skillForm.hidden = true;
    skillEditingSlug = null;
  }

  async function editSkill(s) {
    try {
      const data = await apiJson('GET', `/api/agent/skills/${encSeg(s.slug)}`);
      openSkillForm(Object.assign({}, s, data, { slug: s.slug }));
    } catch (err) {
      appendSettingsError(skillList, `没能读取技能内容：${err.message}`);
    }
  }

  async function deleteSkill(s) {
    const ok = await confirmDialog({
      title: '删除技能？',
      body: `「${s.name || s.slug}」会被移到回收站，助手之后不会再用到它。`,
      okText: '删除',
      danger: true
    });
    if (!ok) return;
    try {
      await apiJson('DELETE', `/api/agent/skills/${encSeg(s.slug)}`);
      if (skillEditingSlug === s.slug) closeSkillForm();
      await loadSkills();
    } catch (err) {
      appendSettingsError(skillList, `删除失败：${err.message}`);
    }
  }

  skillForm.addEventListener('submit', async e => {
    e.preventDefault();
    const name = $('skill-name').value.trim();
    const description = $('skill-description').value.trim();
    const content = $('skill-content').value;
    if (!name) { showStatusLine(skillFormStatus, 'error', '请填写名称。'); $('skill-name').focus(); return; }
    if (!description) { showStatusLine(skillFormStatus, 'error', '请用一句话说明什么时候用这个技能。'); $('skill-description').focus(); return; }
    const saveBtn = $('skill-form-save');
    saveBtn.disabled = true;
    try {
      if (skillEditingSlug) await apiJson('PUT', `/api/agent/skills/${encSeg(skillEditingSlug)}`, { name, description, content });
      else await apiJson('POST', '/api/agent/skills', { name, description, content });
      closeSkillForm();
      await loadSkills();
    } catch (err) {
      showStatusLine(skillFormStatus, 'error', `保存失败：${err.message}`);
    } finally {
      saveBtn.disabled = false;
    }
  });
  $('skill-form-cancel').addEventListener('click', closeSkillForm);
  $('btn-skill-new').addEventListener('click', () => openSkillForm(null));
  // 编辑器中 Tab 插入两个空格
  $('skill-content').addEventListener('keydown', e => {
    if (e.key !== 'Tab' || e.shiftKey) return;
    e.preventDefault();
    const ta = e.target;
    const start = ta.selectionStart;
    ta.value = ta.value.slice(0, start) + '  ' + ta.value.slice(ta.selectionEnd);
    ta.selectionStart = ta.selectionEnd = start + 2;
  });

  $('btn-skill-import').addEventListener('click', () => skillImportFile.click());
  skillImportFile.addEventListener('change', async () => {
    const file = skillImportFile.files && skillImportFile.files[0];
    skillImportFile.value = '';
    if (!file) return;
    if (!/\.(md|zip)$/i.test(file.name)) { showStatusLine(skillImportStatus, 'error', '只支持 .md 或 .zip 文件。'); return; }
    if (file.size > 5 * 1024 * 1024) { showStatusLine(skillImportStatus, 'error', '文件太大（上限 5 MB）。'); return; }
    const fd = new FormData();
    fd.append('file', file);
    showStatusLine(skillImportStatus, '', `正在导入 ${file.name}…`);
    try {
      const data = await apiJson('POST', '/api/agent/skills/import', fd);
      const label = (data && (data.name || data.slug)) || file.name;
      showStatusLine(skillImportStatus, 'success', `已导入技能「${label}」。`);
      await loadSkills();
    } catch (err) {
      showStatusLine(skillImportStatus, 'error', `导入失败：${err.message}`);
    }
  });

  // ---------- 系统提示词 ----------
  const promptList = $('prompt-list');
  const promptForm = $('prompt-form');
  const promptFormStatus = $('prompt-form-status');
  const promptModeSelect = $('prompt-mode');

  function applyPromptsData(data) {
    promptsState = {
      prompts: Array.isArray(data.prompts) ? data.prompts.filter(p => p && p.id) : [],
      active_prompt_id: data.active_prompt_id || '',
      prompt_mode: data.prompt_mode === 'replace' ? 'replace' : 'append'
    };
    if (promptOverrideId && !promptsState.prompts.some(p => p.id === promptOverrideId)) promptOverrideId = null;
  }

  async function loadPrompts() {
    try {
      applyPromptsData(await apiJson('GET', '/api/agent/prompts'));
      renderPrompts();
      renderPromptMenu();
    } catch (err) {
      promptList.replaceChildren(emptyNote(`没能读取系统提示词：${err.message}`, true));
    }
  }
  // 页面加载时静默读取一次 (供输入框的预设选择器使用)
  async function refreshPromptsQuiet() {
    try {
      applyPromptsData(await apiJson('GET', '/api/agent/prompts'));
      renderPromptMenu();
    } catch (e) { /* 后端未提供该接口时隐藏选择器 */ }
  }

  function renderPrompts() {
    promptModeSelect.value = promptsState.prompt_mode;
    const prompts = promptsState.prompts;
    if (prompts.length === 0) {
      promptList.replaceChildren(emptyNote('还没有提示词预设。'));
      return;
    }
    const rows = el('div', 'agent-group-rows');
    prompts.forEach((p, i) => {
      const active = p.id === promptsState.active_prompt_id;
      const row = el('div', `agent-row prompt-row${active ? ' active' : ''}`);
      row.dataset.id = p.id;
      const radio = el('input');
      radio.type = 'radio';
      radio.name = 'active-prompt';
      radio.value = p.id;
      radio.id = `prompt-radio-${i}`;
      radio.checked = active;
      radio.addEventListener('change', () => { if (radio.checked) setActivePrompt(p.id, promptModeSelect.value); });
      const main = el('label', 'agent-row-main');
      main.htmlFor = radio.id;
      const head = el('div', 'agent-row-title');
      head.appendChild(el('span', 'prompt-name', p.name || p.id));
      if (p.builtin) head.appendChild(el('span', 'badge badge-builtin', '内置'));
      main.appendChild(head);
      const preview = firstLine(p.content);
      main.appendChild(el('div', 'agent-row-desc', preview || (p.builtin ? '只使用内置的能力说明' : '（空）')));
      row.appendChild(radio);
      row.appendChild(main);
      if (!p.builtin) {
        const ctrls = el('div', 'agent-row-ctrls');
        const btns = el('div', 'row-btns');
        btns.appendChild(makeBtn('编辑', 'btn btn-sm prompt-edit-btn', () => openPromptForm(p)));
        btns.appendChild(makeBtn('删除', 'btn btn-sm btn-ghost-danger prompt-delete-btn', () => deletePrompt(p)));
        ctrls.appendChild(btns);
        row.appendChild(ctrls);
      }
      rows.appendChild(row);
    });
    promptList.replaceChildren(rows);
  }

  async function setActivePrompt(id, mode) {
    try {
      await apiJson('POST', '/api/agent/prompts/active', { id, mode });
      promptsState.active_prompt_id = id;
      promptsState.prompt_mode = mode === 'replace' ? 'replace' : 'append';
      promptOverrideId = null;
      renderPrompts();
      renderPromptMenu();
      flashSaved(promptModeSelect);
    } catch (err) {
      appendSettingsError(promptList, `保存失败：${err.message}`);
      renderPrompts();
    }
  }
  promptModeSelect.addEventListener('change', () => {
    const active = promptsState.active_prompt_id || (promptsState.prompts[0] && promptsState.prompts[0].id) || '';
    setActivePrompt(active, promptModeSelect.value);
  });

  function openPromptForm(p) {
    promptEditingId = p ? p.id : null;
    $('prompt-form-title').textContent = p ? `编辑「${p.name || p.id}」` : '新建预设';
    $('prompt-name').value = p ? (p.name || '') : '';
    $('prompt-content').value = p ? (p.content || '') : '';
    showStatusLine(promptFormStatus, '', '');
    promptForm.hidden = false;
    promptForm.scrollIntoView({ block: 'nearest' });
    $('prompt-name').focus();
  }
  function closePromptForm() {
    promptForm.hidden = true;
    promptEditingId = null;
  }
  promptForm.addEventListener('submit', async e => {
    e.preventDefault();
    const name = $('prompt-name').value.trim();
    const content = $('prompt-content').value;
    if (!name) { showStatusLine(promptFormStatus, 'error', '请填写名称。'); $('prompt-name').focus(); return; }
    const saveBtn = $('prompt-form-save');
    saveBtn.disabled = true;
    try {
      if (promptEditingId) await apiJson('PUT', `/api/agent/prompts/${encSeg(promptEditingId)}`, { name, content });
      else await apiJson('POST', '/api/agent/prompts', { name, content });
      closePromptForm();
      await loadPrompts();
    } catch (err) {
      showStatusLine(promptFormStatus, 'error', `保存失败：${err.message}`);
    } finally {
      saveBtn.disabled = false;
    }
  });
  $('prompt-form-cancel').addEventListener('click', closePromptForm);
  $('btn-prompt-new').addEventListener('click', () => openPromptForm(null));

  async function deletePrompt(p) {
    const ok = await confirmDialog({
      title: '删除提示词预设？',
      body: `「${p.name || p.id}」会被删除。${p.id === promptsState.active_prompt_id ? '删除后将改用默认预设。' : ''}`,
      okText: '删除',
      danger: true
    });
    if (!ok) return;
    try {
      await apiJson('DELETE', `/api/agent/prompts/${encSeg(p.id)}`);
      if (promptEditingId === p.id) closePromptForm();
      await loadPrompts();
    } catch (err) {
      appendSettingsError(promptList, `删除失败：${err.message}`);
    }
  }

  // ---------- 输入框：🧰 工具开关 + 提示词预设 ----------
  const toolsToggleBtn = $('tools-toggle-btn');
  const promptChip = $('prompt-chip');
  const promptChipLabel = $('prompt-chip-label');
  const promptMenuSection = $('prompt-menu-section');
  const promptMenuList = $('prompt-menu-list');

  function setToolsEnabled(on, persist = true) {
    toolsEnabled = Boolean(on);
    if (persist) lsSet('toolsEnabled', toolsEnabled);
    toolsToggleBtn.classList.toggle('active', toolsEnabled);
    toolsToggleBtn.setAttribute('aria-pressed', String(toolsEnabled));
  }
  toolsToggleBtn.addEventListener('click', () => setToolsEnabled(!toolsEnabled));
  setToolsEnabled(toolsEnabled, false);

  function effectivePromptId() {
    return promptOverrideId || promptsState.active_prompt_id || '';
  }

  function renderPromptMenu() {
    const prompts = promptsState.prompts;
    const current = effectivePromptId();
    promptMenuSection.hidden = prompts.length < 2;
    promptMenuList.replaceChildren(...prompts.map(p => {
      const item = el('button', 'menu-item menu-radio');
      item.type = 'button';
      item.setAttribute('role', 'menuitemradio');
      item.setAttribute('aria-checked', String(p.id === current));
      item.dataset.promptId = p.id;
      item.appendChild(el('span', '', p.name || p.id));
      item.addEventListener('click', () => {
        promptOverrideId = p.id === promptsState.active_prompt_id ? null : p.id;
        renderPromptMenu();
      });
      return item;
    }));
    const cur = prompts.find(p => p.id === current);
    promptChip.hidden = !cur || Boolean(cur.builtin);
    promptChipLabel.textContent = cur ? (cur.name || cur.id) : '';
    promptChip.setAttribute('aria-label', cur ? `系统提示词：${cur.name || cur.id}（点击切换）` : '系统提示词');
  }
  promptChip.addEventListener('click', e => {
    e.stopPropagation();
    if (!attachMenu) return;
    attachMenu.open();
    const checked = promptMenuList.querySelector('[aria-checked="true"]');
    if (checked) checked.focus();
  });

  // ---------- 对话中的工具调用卡片 ----------
  const TOOL_STATUS_TEXT = {
    running: '运行中', pending_approval: '等待确认', done: '完成', denied: '已拒绝', error: '失败', cancelled: '已取消'
  };
  const TOOL_ARGS_MAX = 6000;
  const TOOL_RESULT_MAX = 4000;

  function toolStepsOf(aiRow) {
    const main = aiRow.querySelector('.message-main');
    let box = main.querySelector('.tool-steps');
    if (!box) {
      box = el('div', 'tool-steps');
      main.insertBefore(box, main.querySelector('.message-bubble'));
    }
    return box;
  }
  function findToolCard(aiRow, id) {
    return Array.from(aiRow.querySelectorAll('.tool-card')).find(c => c.dataset.callId === id) || null;
  }
  function prettyJson(value) {
    if (value === undefined || value === null || value === '') return '{}';
    let v = value;
    if (typeof value === 'string') {
      try { v = JSON.parse(value); } catch (e) { return value; }
    }
    try { return JSON.stringify(v, null, 2); } catch (e) { return String(value); }
  }
  function clip(text, max) {
    const s = String(text === undefined || text === null ? '' : text);
    return s.length > max ? { text: s.slice(0, max), more: s.length - max } : { text: s, more: 0 };
  }
  function toolSection(kind, summary, text, max) {
    const d = el('details', `tool-section tool-${kind}`);
    d.appendChild(el('summary', '', summary));
    const c = clip(text, max);
    d.appendChild(el('pre', '', c.text));
    if (c.more) d.appendChild(el('div', 'tool-trunc', `…已省略 ${c.more.toLocaleString()} 个字符`));
    return d;
  }
  function setToolStatus(card, status) {
    card.dataset.status = status;
    card.querySelector('.tool-status').textContent = TOOL_STATUS_TEXT[status] || status;
  }
  function setTypingHint(aiRow, text) {
    if (aiRow._rawText) return;
    const t = aiRow.querySelector('.message-bubble .typing');
    if (t) t.textContent = text;
  }

  function buildToolCard(id, name) {
    const card = el('div', 'tool-card');
    card.dataset.callId = id;
    card.setAttribute('role', 'group');
    card.setAttribute('aria-label', `工具调用：${name}`);
    const head = el('div', 'tool-card-head');
    const icon = el('span', 'tool-card-icon', '🔧');
    icon.setAttribute('aria-hidden', 'true');
    head.appendChild(icon);
    const title = el('span', 'tool-card-title', '调用 ');
    title.appendChild(el('span', 'tool-name', name));
    title.title = name;
    head.appendChild(title);
    const elapsed = el('span', 'tool-elapsed');
    elapsed.hidden = true;
    head.appendChild(elapsed);
    const status = el('span', 'tool-status');
    status.setAttribute('aria-live', 'polite');
    head.appendChild(status);
    card.appendChild(head);
    return card;
  }

  function handleToolCall(aiRow, tc) {
    if (!tc || typeof tc !== 'object') return;
    const id = String(tc.id || `call-${aiRow.querySelectorAll('.tool-card').length + 1}`);
    const name = String(tc.name || '工具');
    const nearBottom = isNearBottom();
    let card = findToolCard(aiRow, id);
    if (!card) {
      card = buildToolCard(id, name);
      card.appendChild(toolSection('args', '参数', prettyJson(tc.arguments), TOOL_ARGS_MAX));
      toolStepsOf(aiRow).appendChild(card);
      aiRow._toolCount = (aiRow._toolCount || 0) + 1;
    }
    if (card._final) return;
    if (tc.status === 'pending_approval') {
      setToolStatus(card, 'pending_approval');
      if (!card.querySelector('.tool-approval')) card.appendChild(approvalBox(card, id, tc));
      setTypingHint(aiRow, '等待你的确认…');
    } else {
      setToolStatus(card, 'running');
      setTypingHint(aiRow, '正在调用工具…');
    }
    if (nearBottom) scrollToBottom(); else checkScrollBottom();
  }

  function approvalBox(card, id, tc) {
    const box = el('div', 'tool-approval');
    box.appendChild(el('span', 'tool-approval-text', tc.read_only === false
      ? '这一步可能会修改文件或执行操作，需要你同意后才会运行。'
      : '需要你同意后才会运行。'));
    const allow = makeBtn('允许', 'btn btn-sm btn-primary tool-allow-btn', () => decideToolCall(card, id, 'allow', false));
    const always = makeBtn('始终允许', 'btn btn-sm tool-always-btn', () => decideToolCall(card, id, 'allow', true));
    const deny = makeBtn('拒绝', 'btn btn-sm tool-deny-btn', () => decideToolCall(card, id, 'deny', false));
    always.title = `以后调用「${tc.name || ''}」不再询问`;
    const btns = el('div', 'tool-approval-btns');
    btns.appendChild(allow);
    btns.appendChild(always);
    btns.appendChild(deny);
    box.appendChild(btns);
    return box;
  }

  async function decideToolCall(card, id, decision, remember) {
    const box = card.querySelector('.tool-approval');
    const buttons = box ? Array.from(box.querySelectorAll('button')) : [];
    buttons.forEach(b => { b.disabled = true; });
    const text = box && box.querySelector('.tool-approval-text');
    const prevErr = box && box.querySelector('.tool-approval-error');
    if (prevErr) prevErr.remove();
    card._pendingDecision = decision;   // tool_result 可能先于 approve 响应到达
    try {
      await apiJson('POST', '/api/agent/approve', { call_id: id, decision, remember });
      card._decided = true;
      if (box) box.classList.add('decided');
      if (decision === 'deny') {
        card._denied = true;
        setToolStatus(card, 'denied');
        if (text) text.textContent = '你拒绝了这一步，助手不会执行它。';
      } else {
        if (card.dataset.status === 'pending_approval') setToolStatus(card, 'running');
        if (text) text.textContent = remember ? '已允许（以后不再询问）。' : '已允许。';
      }
    } catch (err) {
      if (err.status === 404) {
        card._final = true;
        setToolStatus(card, 'cancelled');
        if (box) box.classList.add('decided');
        if (text) text.textContent = '这个请求已失效（可能已超时或对话已结束）。';
      } else {
        card._pendingDecision = null;
        buttons.forEach(b => { b.disabled = false; });
        if (box) box.appendChild(el('span', 'tool-approval-error', `没能提交：${err.message}`));
      }
    }
  }

  function formatElapsed(ms) {
    const n = Number(ms);
    if (!Number.isFinite(n) || n < 0) return '';
    return n >= 1000 ? `${(n / 1000).toFixed(1)} 秒` : `${Math.round(n)} ms`;
  }

  function handleToolResult(aiRow, tr) {
    if (!tr || typeof tr !== 'object') return;
    const id = String(tr.id || '');
    const nearBottom = isNearBottom();
    let card = id ? findToolCard(aiRow, id) : null;
    if (!card) {
      card = buildToolCard(id || `call-${Date.now()}`, String(tr.name || '工具'));
      toolStepsOf(aiRow).appendChild(card);
      aiRow._toolCount = (aiRow._toolCount || 0) + 1;
    }
    const ok = tr.ok !== false;
    card._final = true;
    if (card._pendingDecision === 'deny') card._denied = true;
    setToolStatus(card, card._denied ? 'denied' : (ok ? 'done' : 'error'));
    const box = card.querySelector('.tool-approval');
    if (box && !card._decided && !card._pendingDecision) {
      box.querySelectorAll('button').forEach(b => { b.disabled = true; });
      box.classList.add('decided');
      const t = box.querySelector('.tool-approval-text');
      if (t) t.textContent = ok ? '已执行。' : '没有执行。';
    }
    const elapsed = card.querySelector('.tool-elapsed');
    const et = formatElapsed(tr.elapsed_ms);
    elapsed.hidden = !et;
    elapsed.textContent = et;
    const old = card.querySelector('.tool-result');
    if (old) old.remove();
    const section = toolSection('result', ok ? '结果' : (card._denied ? '说明' : '错误信息'), tr.content, TOOL_RESULT_MAX);
    if (!ok && !card._denied) section.open = true;
    card.appendChild(section);
    setTypingHint(aiRow, '正在思考…');
    if (nearBottom) scrollToBottom(); else checkScrollBottom();
  }

  function appendToolNotice(aiRow, text) {
    if (!text) return;
    const nearBottom = isNearBottom();
    const note = el('p', 'inline-note', String(text));
    note.setAttribute('role', 'status');
    toolStepsOf(aiRow).appendChild(note);
    if (nearBottom) scrollToBottom(); else checkScrollBottom();
  }

  // 流结束 / 中止时，仍在等待确认的卡片不可再点
  function finalizeToolCards(aiRow, aborted) {
    aiRow.querySelectorAll('.tool-card').forEach(card => {
      const st = card.dataset.status;
      if (st !== 'pending_approval' && !(aborted && st === 'running')) return;
      card._final = true;
      setToolStatus(card, 'cancelled');
      const box = card.querySelector('.tool-approval');
      if (box) {
        box.querySelectorAll('button').forEach(b => { b.disabled = true; });
        box.classList.add('decided');
        const t = box.querySelector('.tool-approval-text');
        if (t && st === 'pending_approval') t.textContent = '对话已结束，这一步没有执行。';
      }
    });
  }

  // ======================================================================
  // 长期记忆与新对话
  // ======================================================================
  function hasConversation() {
    return chatMessages.querySelector('.message-row, .notice') !== null || fileList.children.length > 0;
  }

  function resetView() {
    if (isGenerating) {
      if (currentAbortController) currentAbortController.abort();
      setGeneratingState(false);
    }
    chatMessages.replaceChildren(emptyState);
    clearWorkspaceFiles();
    resetQuestionOutline();
    chatTitle.textContent = '新对话';
    chatInput.placeholder = DEFAULT_PLACEHOLDER;
    setThinking(thinkingDefault);
    lastCtxStats = null;
    checkScrollBottom();
  }

  async function newChat() {
    closeMobileSidebar();
    if (hasConversation()) {
      const ok = await confirmDialog({
        title: '开始新对话？',
        body: '当前对话和工作区文件会被清空，长期记忆不受影响。',
        okText: '开始新对话'
      });
      if (!ok) return;
    }
    try {
      const res = await fetch('/api/clear', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.status === 'ok') {
        resetView();
        fetchStatus();
        chatInput.focus();
      } else {
        appendSystemNotice('没能开始新对话，请稍后再试。', 'error');
      }
    } catch (err) {
      appendSystemNotice(`没能开始新对话：${err.message}`, 'error');
    }
  }
  btnNewChat.addEventListener('click', newChat);

  btnFlush.addEventListener('click', async () => {
    btnFlush.disabled = true;
    try {
      const res = await fetch('/api/flush', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.status === 'ok') {
        appendSystemNotice(data.flushed_chunks > 0
          ? `已把工作区文件存入长期记忆（新增 ${data.flushed_chunks} 条），现在共有 ${data.total_memory_count} 条记忆。`
          : `没有需要存入的新内容，现在共有 ${data.total_memory_count} 条记忆。`, 'success');
        fetchStatus();
      } else {
        throw new Error(uploadErrorDetail(data, res.status));
      }
    } catch (err) {
      appendSystemNotice(`存入记忆失败：${err.message}`, 'error');
    } finally {
      btnFlush.disabled = false;
    }
  });

  btnClearRag.addEventListener('click', async () => {
    const ok = await confirmDialog({
      title: '清空全部长期记忆？',
      body: '这会永久删除助手记住的所有内容，包括存入记忆的文件、网页和搜索资料，同时清空当前对话和工作区文件。此操作无法撤销。',
      okText: '清空记忆',
      danger: true
    });
    if (!ok) return;
    try {
      const res = await fetch('/api/clear_rag', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.status === 'ok') {
        resetView();
        appendSystemNotice('长期记忆已清空。');
        fetchStatus();
      } else {
        throw new Error(uploadErrorDetail(data, res.status));
      }
    } catch (err) {
      appendSystemNotice(`清空失败：${err.message}`, 'error');
    }
  });

  // ======================================================================
  // 模型选择 (列表来自 /api/status 的 available_models，不在前端硬编码)
  // ======================================================================
  let availableModels = [];
  let availableModelsSig = '';
  let currentModelId = '';

  function getModelProfile(modelId) {
    const id = String(modelId || '');
    const m = availableModels.find(x => x && x.id === id);
    const fullName = (m && m.name) || id.split('/').pop().replace(':latest', '');
    return {
      name: fullName.replace(/\s*\(.*$/, '') || fullName,
      fullName,
      tag: (m && m.tag) || '',
      speed: (m && m.speed) || '',
      vram: (m && m.vram) || ''
    };
  }

  function renderModelOptions(models) {
    if (!Array.isArray(models) || models.length === 0 || isSwitchingModel) return;
    availableModels = models.filter(m => m && m.id);
    const sig = availableModels.map(m => `${m.id}|${m.name}`).join('\n');
    if (sig === availableModelsSig) return;
    availableModelsSig = sig;
    modelSelect.replaceChildren(...availableModels.map(m => {
      const p = getModelProfile(m.id);
      const opt = el('option', '', p.name);
      opt.value = m.id;
      opt.title = [p.fullName, p.speed, p.vram].filter(Boolean).join(' · ');
      return opt;
    }));
    if (currentModelId) modelSelect.value = currentModelId;
  }

  function updateModelUI(modelId) {
    if (!modelId) return;
    currentModelId = modelId;
    if (modelSelect.value !== modelId) modelSelect.value = modelId;
    const p = getModelProfile(modelId);
    modelSelect.title = [p.fullName, p.speed, p.vram].filter(Boolean).join(' · ');
  }

  function setModelSwitching(switching) {
    isSwitchingModel = switching;
    modelSelect.disabled = switching;
    modelChip.classList.toggle('switching', switching);
    modelChipStatus.hidden = !switching;
    if (!isGenerating) sendBtn.disabled = switching;
    setConnection(switching ? 'switching' : 'ok');
  }

  modelSelect.addEventListener('change', async () => {
    const selectedModel = modelSelect.value;
    const previousModel = currentModelId;
    if (isSwitchingModel || selectedModel === previousModel) {
      modelSelect.value = previousModel;
      return;
    }
    const profile = getModelProfile(selectedModel);
    setModelSwitching(true);   // 成功前不修改当前模型
    appendSystemNotice(`正在切换到 **${profile.name}**，首次加载可能需要几十秒…`);

    let switched = false;
    try {
      const res = await fetch('/api/model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: selectedModel })
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.status === 'ok') {
        switched = true;
        isSwitchingModel = false;
        updateModelUI(data.current_model || selectedModel);
        appendSystemNotice(`已切换到 **${getModelProfile(data.current_model || selectedModel).name}**，可以继续提问了。`, 'success');
      } else {
        let detail = data && data.detail !== undefined ? data.detail : '';
        if (detail && typeof detail !== 'string') detail = JSON.stringify(detail);
        if (!detail) detail = res.status === 409 ? '另一个模型切换正在进行中，请稍后再试' : `HTTP ${res.status}`;
        appendSystemNotice(`模型切换没有成功：${detail}。已恢复为之前的模型。`, 'error');
      }
    } catch (err) {
      console.error('切换模型网络错误:', err);
      appendSystemNotice(`模型切换没有成功：${err.message}。已恢复为之前的模型。`, 'error');
    } finally {
      setModelSwitching(false);
      if (!switched) updateModelUI(previousModel);
      fetchStatus();
    }
  });

  // ======================================================================
  // 对话
  // ======================================================================
  function sendMessage(overrideText) {
    // 生成中点击按钮 = 停止生成
    if (isGenerating) {
      if (currentAbortController) currentAbortController.abort();
      setGeneratingState(false);
      return;
    }
    if (isSwitchingModel) return;
    const fromInput = typeof overrideText !== 'string';
    const text = (fromInput ? chatInput.value : overrideText).trim();
    if (!text) return;
    appendMessage('user', text);
    if (fromInput) {
      chatInput.value = '';
      autoGrow();
    }
    runChat(text);
  }

  const BACKEND_DOWN_RE = /connect|refused|unreachable|timed?\s*out|reset by peer|getaddrinfo|ECONN|502|503|504|bad gateway|service unavailable|无法连接|连接失败|拒绝/i;

  async function runChat(text) {
    const controller = new AbortController();
    currentAbortController = controller;
    setGeneratingState(true);

    const aiRow = appendMessage('ai', '', true);
    aiRow._question = text;
    let accumulatedText = '';   // 原始累计文本，停止生成时基于原文重新渲染
    let streamError = '';
    let aborted = false;

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify({
          message: text,
          stream: true,
          model: modelSelect.value || currentModelId || undefined,
          thinking: thinkingEnabled,
          web_search: webSearchEnabled,
          tools_enabled: toolsEnabled,
          prompt_id: effectivePromptId() || undefined
        })
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => '');
        const err = new Error(`HTTP ${res.status}`);
        err.status = res.status;
        err.detail = detail;
        throw err;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop();
        for (const part of parts) {
          const trimmed = part.trim();
          if (!trimmed.startsWith('data: ')) continue;
          let parsed;
          try { parsed = JSON.parse(trimmed.slice(6)); } catch (e) { continue; }   // 忽略残缺片段
          if (parsed.search_results) renderSources(aiRow, parsed.search_results);
          if (parsed.ctx_stats) updateLiveStats(aiRow, parsed.ctx_stats);
          if (parsed.tool_call) handleToolCall(aiRow, parsed.tool_call);
          if (parsed.tool_result) handleToolResult(aiRow, parsed.tool_result);
          if (parsed.notice) appendToolNotice(aiRow, parsed.notice);
          if (parsed.token) {
            accumulatedText += parsed.token;
            scheduleAiRender(aiRow, accumulatedText);   // rAF 批量渲染：每帧最多一次
          }
          if (parsed.done && accumulatedText) updateAiMessage(aiRow, accumulatedText);
          if (parsed.done && parsed.stats) {
            updateMetrics(parsed.stats);
            if (parsed.stats.active_model && !isSwitchingModel) updateModelUI(parsed.stats.active_model);
            showAiMessageMetrics(aiRow, parsed.stats);
          }
          if (parsed.error) {
            if (!accumulatedText.trim()) {
              streamError = String(parsed.error);
            } else {
              accumulatedText += `\n\n⚠️ ${parsed.error}`;
              updateAiMessage(aiRow, accumulatedText);
            }
          }
        }
      }

      if (accumulatedText) updateAiMessage(aiRow, accumulatedText);
      else if (streamError) showErrorCard(aiRow, BACKEND_DOWN_RE.test(streamError) ? 'backend' : 'other', streamError);
      else showErrorCard(aiRow, 'empty', '');
    } catch (err) {
      if (err.name === 'AbortError') {
        aborted = true;
        // 保留已生成的原始 Markdown (代码块 / 思考过程结构完整)
        updateAiMessage(aiRow, accumulatedText);
        const main = aiRow.querySelector('.message-main');
        if (!main.querySelector('.stopped-note')) main.insertBefore(el('p', 'stopped-note', '已停止生成'), aiRow.querySelector('.message-actions'));
      } else {
        console.warn('对话请求失败:', err);
        if (accumulatedText) updateAiMessage(aiRow, accumulatedText);
        const raw = err.status ? `HTTP ${err.status}${err.detail ? `\n${err.detail}` : ''}` : `${err.name}: ${err.message}`;
        const backendDown = err instanceof TypeError || [502, 503, 504].includes(err.status);
        showErrorCard(aiRow, backendDown ? 'backend' : 'other', raw);
      }
    } finally {
      if (aiRow._rafId) { cancelAnimationFrame(aiRow._rafId); aiRow._rafId = 0; }
      finalizeToolCards(aiRow, aborted);
      aiRow.dataset.state = aiRow._error ? 'error' : 'done';
      // 若用户已中止并开始了新一轮生成，不要重置新一轮的状态
      if (!currentAbortController || currentAbortController === controller) setGeneratingState(false);
      if (!isNarrow()) chatInput.focus();
    }
  }

  function showErrorCard(aiRow, kind, raw) {
    aiRow._error = true;
    const bubble = aiRow.querySelector('.message-bubble');
    if (!aiRow._rawText) bubble.replaceChildren();
    const titles = {
      backend: '模型还没准备好：请确认「本地大模型」窗口已启动，然后重试',
      empty: '这次没有收到回复，请再试一次。',
      other: '出了点问题，这次没能完成回答。'
    };
    const card = el('div', 'error-card');
    card.setAttribute('role', 'alert');
    card.appendChild(el('p', 'error-title', titles[kind] || titles.other));
    const retry = el('button', 'btn btn-sm retry-btn', '重试');
    retry.type = 'button';
    card.appendChild(retry);
    if (raw) {
      const details = el('details', 'error-details');
      details.appendChild(el('summary', '', '查看详细信息'));
      details.appendChild(el('pre', '', raw));
      card.appendChild(details);
    }
    const nearBottom = isNearBottom();
    aiRow.querySelector('.message-main').insertBefore(card, aiRow.querySelector('.message-actions'));
    if (nearBottom) scrollToBottom(); else checkScrollBottom();
  }

  function appendMessage(role, text, isTyping = false) {
    hideEmptyState();
    const row = el('div', `message-row ${role}`);
    row._rawText = text || '';

    if (role === 'user') {
      const qIndex = userQuestions.length + 1;
      row.id = `user-question-${qIndex}`;
      addQuestionToOutline(text, row.id, qIndex);
      if (qIndex === 1) chatTitle.textContent = text.length > 40 ? text.slice(0, 40) + '…' : text;
      const bubble = el('div', 'message-bubble');
      bubble.innerHTML = formatMarkdown(text);
      row.appendChild(bubble);
    } else {
      row.dataset.state = isTyping ? 'streaming' : 'done';
      const avatar = el('div', 'avatar');
      avatar.setAttribute('aria-hidden', 'true');
      avatar.innerHTML = ICONS.spark;
      const main = el('div', 'message-main');
      const bubble = el('div', 'message-bubble');
      if (isTyping) bubble.innerHTML = '<span class="typing">正在思考…</span>';
      else bubble.innerHTML = formatMarkdown(text);

      const statsId = `msg-stats-${++msgSeq}`;
      const stats = el('div', 'message-stats');
      stats.id = statsId;
      stats.hidden = true;
      stats.appendChild(el('span', 'gen-metrics-badge', '暂无统计信息'));

      const actions = el('div', 'message-actions');
      actions.innerHTML = `
        <button class="msg-action copy-msg-btn" type="button" aria-label="复制回答" title="复制">${ICONS.copy}</button>
        <button class="msg-action regen-btn" type="button" aria-label="重新生成" title="重新生成">${ICONS.regen}</button>
        <button class="msg-action msg-info-btn" type="button" aria-label="查看生成统计" title="生成统计" aria-expanded="false" aria-controls="${statsId}">${ICONS.info}</button>`;
      main.appendChild(bubble);
      main.appendChild(stats);
      main.appendChild(actions);
      row.appendChild(avatar);
      row.appendChild(main);
    }
    chatMessages.appendChild(row);
    scrollToBottom();
    return row;
  }

  // 立即渲染 (取消待执行的帧渲染)
  function updateAiMessage(row, text) {
    if (!row) return;
    if (row._rafId) { cancelAnimationFrame(row._rafId); row._rafId = 0; }
    row._rawText = text;
    renderAiBubble(row);
  }

  // 流式 token：每帧最多渲染一次，避免每个 token 都整段重排
  function scheduleAiRender(row, text) {
    row._rawText = text;
    if (row._rafId) return;
    row._rafId = requestAnimationFrame(() => {
      row._rafId = 0;
      renderAiBubble(row);
    });
  }

  function renderAiBubble(row) {
    const bubble = row.querySelector('.message-bubble');
    if (!bubble) return;
    const nearBottom = isNearBottom();   // 仅当用户已在底部附近时才自动滚动
    bubble.innerHTML = formatMarkdown(row._rawText || '');
    // 恢复用户手动设置的「思考过程」展开/收起状态
    const userStates = row._thinkOpenStates;
    if (userStates) {
      bubble.querySelectorAll('details.think-block-wrapper').forEach((d, i) => {
        if (userStates[i] !== undefined) d.open = userStates[i];
      });
    }
    if (nearBottom) scrollToBottom(); else checkScrollBottom();
  }

  // 联网参考来源：答案下方的小链接标签
  function renderSources(aiRow, results) {
    if (!aiRow || !Array.isArray(results) || results.length === 0) return;
    const main = aiRow.querySelector('.message-main');
    if (!main || main.querySelector('.sources-card')) return;
    const card = el('div', 'sources-card');
    card.appendChild(el('span', 'sources-label', '参考来源'));
    results.forEach((r, i) => {
      let domain = '';
      try {
        const u = new URL(r.url);
        if (u.protocol === 'http:' || u.protocol === 'https:') domain = u.hostname.replace(/^www\./, '');
      } catch (e) { domain = ''; }
      const a = el('a', 'source-link');
      a.setAttribute('href', safeUrl(r.url));
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
      a.title = String(r.title || domain || '');
      a.appendChild(el('span', 'source-index', i + 1));
      a.appendChild(el('span', 'source-title', r.title || domain || '来源'));
      if (domain) a.appendChild(el('span', 'source-domain', domain));
      card.appendChild(a);
    });
    const nearBottom = isNearBottom();
    main.insertBefore(card, main.querySelector('.message-stats'));
    if (nearBottom) scrollToBottom(); else checkScrollBottom();
  }

  // 生成统计 (默认隐藏，点 ⓘ 显示)
  function setStatsText(aiRow, text) {
    const badge = aiRow && aiRow.querySelector('.gen-metrics-badge');
    if (badge) badge.textContent = text;
  }
  function ctxSummary(total, maxCtx) {
    const pct = Math.min(100, Math.max(0, (total / maxCtx) * 100)).toFixed(1);
    return `上下文 ${total.toLocaleString()} / ${maxCtx.toLocaleString()} (${pct}%)`;
  }
  function updateLiveStats(aiRow, s) {
    lastCtxStats = s;
    const r = Number(s.reasoning_tokens || 0) || 0;
    const c = Number(s.content_tokens || 0) || 0;
    const total = Number(s.total_context_tokens || ((Number(s.prompt_tokens) || 0) + r + c)) || 0;
    const maxCtx = Number(s.server_n_ctx || 24576) || 24576;
    const phase = s.phase === 'reasoning' ? '思考中' : '生成中';
    setStatsText(aiRow, `${phase} · 思考 ${r} tokens · 正文 ${c} tokens · ${ctxSummary(total, maxCtx)}`);
  }
  function showAiMessageMetrics(aiRow, stats) {
    lastCtxStats = stats;
    const sec = Number(stats.elapsed_sec || 0) || 0;
    const spd = Number(stats.tokens_per_sec || stats.chars_per_sec || 0) || 0;
    const count = Number(stats.token_count || stats.char_count || 0) || 0;
    const r = Number(stats.reasoning_tokens || 0) || 0;
    const c = Number(stats.content_tokens || (count - r)) || 0;
    const total = Number(stats.total_context_tokens || (stats.prompt_tokens ? Number(stats.prompt_tokens) + count : count)) || 0;
    const maxCtx = Number(stats.server_n_ctx || 24576) || 24576;
    const tokens = r > 0 ? `思考 ${r} tokens · 正文 ${c} tokens` : `${count} tokens`;
    const toolCalls = Number(stats.tool_calls !== undefined ? stats.tool_calls : (aiRow._toolCount || 0)) || 0;
    const tools = toolCalls > 0 ? ` · 工具调用 ${toolCalls} 次` : '';
    setStatsText(aiRow, `用时 ${sec} 秒 · ${spd} tok/s · ${tokens} · ${ctxSummary(total, maxCtx)}${tools}`);
    renderDevInfo();
  }

  // 剪贴板复制 (Clipboard API + 兼容回退)
  function copyTextToClipboard(text, buttonEl, successText = '已复制') {
    if (!text) return;
    // 只在首次记录原始按钮内容，避免连续点击时把「已复制」状态当作原始内容
    if (!buttonEl.dataset.originalHtml) buttonEl.dataset.originalHtml = buttonEl.innerHTML;
    const originalHtml = buttonEl.dataset.originalHtml;
    const markSuccess = () => {
      if (buttonEl._copyResetTimer) clearTimeout(buttonEl._copyResetTimer);
      buttonEl.classList.add('copied');
      buttonEl.innerHTML = `${ICONS.check}<span>${escapeHtml(successText)}</span>`;
      buttonEl._copyResetTimer = setTimeout(() => {
        buttonEl._copyResetTimer = 0;
        buttonEl.classList.remove('copied');
        buttonEl.innerHTML = originalHtml;
      }, 1800);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(markSuccess).catch(() => { fallbackCopyText(text); markSuccess(); });
    } else {
      fallbackCopyText(text);
      markSuccess();
    }
  }
  function fallbackCopyText(text) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) { console.error('Fallback copy failed:', e); }
    document.body.removeChild(ta);
  }

  // 提取纯净最终回答 (去掉 <think>…</think> 或 [Reasoning]…[Answer] 思考过程)
  function getCleanAnswerText(raw) {
    if (!raw) return '';
    if (raw.includes('[Reasoning]') && raw.includes('[Answer]')) {
      return raw.split('[Answer]').slice(1).join('[Answer]').trim();
    }
    if (raw.includes('<think>') && raw.includes('</think>')) {
      const answerPart = raw.split('</think>').slice(1).join('</think>').trim();
      return answerPart || raw.replace(/<\/?think>/g, '').trim();
    }
    return raw.replace(/<\/?think>/g, '').replace(/\[Reasoning\]/g, '').replace(/\[Answer\]/g, '').trim();
  }

  // 事件委托：思考过程折叠状态、代码块复制 / 写回、消息操作
  chatMessages.addEventListener('click', e => {
    const thinkSummary = e.target.closest('summary.think-block-summary');
    if (thinkSummary) {
      const details = thinkSummary.closest('details.think-block-wrapper');
      const row = thinkSummary.closest('.message-row');
      const bubble = thinkSummary.closest('.message-bubble');
      if (details && row && bubble) {
        const idx = Array.from(bubble.querySelectorAll('details.think-block-wrapper')).indexOf(details);
        if (idx !== -1) {
          if (!row._thinkOpenStates) row._thinkOpenStates = [];
          row._thinkOpenStates[idx] = !details.open;   // click 默认行为在此之后发生
        }
      }
      return;
    }

    const writeBackBtn = e.target.closest('.write-back-btn');
    if (writeBackBtn) {
      const wrapper = writeBackBtn.closest('.code-block-wrapper');
      const codeEl = wrapper && wrapper.querySelector('pre code');
      const langEl = wrapper && wrapper.querySelector('.code-lang');
      if (codeEl) openWriteBackModal(codeEl.innerText, langEl ? langEl.innerText : '', writeBackBtn);
      return;
    }

    const copyCodeBtn = e.target.closest('.copy-code-btn');
    if (copyCodeBtn) {
      const codeEl = copyCodeBtn.closest('.code-block-wrapper').querySelector('pre code');
      if (codeEl) copyTextToClipboard(codeEl.innerText, copyCodeBtn, '已复制');
      return;
    }

    const row = e.target.closest('.message-row');
    if (e.target.closest('.copy-msg-btn')) {
      if (row && row._rawText) copyTextToClipboard(getCleanAnswerText(row._rawText), e.target.closest('.copy-msg-btn'), '已复制');
      return;
    }
    if (e.target.closest('.regen-btn')) {
      if (row && row._question && !isGenerating && !isSwitchingModel) sendMessage(row._question);
      return;
    }
    const infoBtn = e.target.closest('.msg-info-btn');
    if (infoBtn) {
      const stats = row && row.querySelector('.message-stats');
      if (stats) {
        stats.hidden = !stats.hidden;
        infoBtn.setAttribute('aria-expanded', String(!stats.hidden));
        infoBtn.classList.toggle('active', !stats.hidden);
      }
      return;
    }
    if (e.target.closest('.retry-btn')) {
      if (row && row._question && !isGenerating && !isSwitchingModel) {
        const q = row._question;
        row.remove();
        runChat(q);
      }
      return;
    }
    const link = e.target.closest('a.source-link');
    if (link && link.getAttribute('href') === '#') e.preventDefault();
  });

  // ======================================================================
  // Markdown (代码块、思考过程折叠、粗体、行内代码)
  // ======================================================================
  function formatMarkdownSub(str) {
    if (!str) return '';
    let text = str;

    // 1. 抽取并保护代码块 (连同结束围栏后的换行，避免代码块下方多出空行)
    const codeBlocks = [];
    text = text.replace(/```([a-zA-Z0-9_\-\+]*)\s*\n?([\s\S]*?)(?:```[^\S\n]*\n?|$)/g, (match, lang, code) => {
      const language = (lang || 'code').trim();
      const codeClean = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      const placeholder = `__TV_CODE_BLOCK_${codeBlocks.length}__`;
      codeBlocks.push(
        `<div class="code-block-wrapper">` +
          `<div class="code-block-header">` +
            `<span class="code-lang">${language}</span>` +
            `<div class="code-block-actions">` +
              `<button class="code-btn write-back-btn" type="button" title="把这段代码写回本地文件">${ICONS.save}<span>写回文件</span></button>` +
              `<button class="code-btn copy-code-btn" type="button" title="复制代码">${ICONS.copy}<span>复制</span></button>` +
            `</div>` +
          `</div>` +
          `<pre><code class="language-${language}">${codeClean.trim()}</code></pre>` +
        `</div>`
      );
      return placeholder;
    });

    // 2. HTML 转义
    let escaped = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    // 3. 基础格式
    escaped = escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    escaped = escaped.replace(/`([^`]+)`/g, '<code>$1</code>');
    escaped = escaped.replace(/\n/g, '<br>');

    // 4. 还原代码块 (函数替换值，避免 $$ / $& / $' 等特殊替换模式破坏代码内容)
    codeBlocks.forEach((blockHtml, i) => {
      escaped = escaped.replace(`__TV_CODE_BLOCK_${i}__`, () => blockHtml);
    });
    return escaped;
  }

  function thinkBlock(bodyHtml, finished) {
    return `<details class="think-block-wrapper"${finished ? '' : ' open'}>` +
      `<summary class="think-block-summary">` +
        `<span class="think-status-text">${finished ? '思考过程' : '正在思考…'}</span>` +
        `<span class="think-toggle-hint">${finished ? '点击展开' : ''}</span>` +
      `</summary>` +
      `<div class="think-content-body">${bodyHtml}</div>` +
    `</details>`;
  }

  // 支持 [Reasoning]…[Answer] 与 <think>…</think> 两种思考过程格式 (流式中 / 已完成)
  function formatMarkdown(str) {
    if (!str) return '';
    const markers = [['[Reasoning]', '[Answer]'], ['<think>', '</think>']];
    for (const [open, close] of markers) {
      const start = str.indexOf(open);
      if (start === -1) continue;
      const before = str.substring(0, start);
      const rest = str.substring(start + open.length);
      const end = rest.indexOf(close);
      if (end !== -1) {
        const body = rest.substring(0, end).trim();
        const answer = rest.substring(end + close.length).trim();
        return `${formatMarkdownSub(before)}${body ? thinkBlock(formatMarkdownSub(body), true) : ''}` +
          `<div class="think-final-answer">${formatMarkdownSub(answer)}</div>`;
      }
      return `${formatMarkdownSub(before)}${thinkBlock(formatMarkdownSub(rest), false)}`;
    }
    return formatMarkdownSub(str);
  }

  // ======================================================================
  // 滚动
  // ======================================================================
  function isNearBottom() {
    return chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight <= 80;
  }
  function checkScrollBottom() {
    const distance = chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight;
    scrollBottomBtn.classList.toggle('visible', distance > 140);
  }
  function scrollToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
    checkScrollBottom();
  }
  chatMessages.addEventListener('scroll', checkScrollBottom);
  scrollBottomBtn.addEventListener('click', () => {
    chatMessages.scrollTo({ top: chatMessages.scrollHeight, behavior: scrollBehavior() });
  });

  // ======================================================================
  // 状态轮询 (/api/status)
  // ======================================================================
  function updateMetrics(stats) {
    if (!stats) return;
    if (stats.memory_count !== undefined) memoryCountEl.textContent = String(stats.memory_count);
    lastStatus = Object.assign(lastStatus || {}, ['memory_count', 'session_turns', 'buffer_mb', 'max_buffer_gb', 'server_n_ctx']
      .reduce((acc, k) => { if (stats[k] !== undefined) acc[k] = stats[k]; return acc; }, {}));
  }

  let statusInFlight = false;
  async function fetchStatus() {
    if (statusInFlight) return;
    statusInFlight = true;
    try {
      const res = await fetch('/api/status');
      if (!res.ok) { setConnection('down'); return; }
      const data = await res.json();
      lastStatus = data;
      updateMetrics(data);
      // 切换进行中时不覆盖「切换中…」状态
      if (!isSwitchingModel) {
        renderModelOptions(data.available_models);
        updateModelUI(data.llm_model);
      }
      // 页面刷新后恢复工作区文件；后端返回空列表时同步清空，避免写回弹窗展示失效文件
      cachedActiveFiles = Array.isArray(data.active_files) ? data.active_files : [];
      syncWorkspaceFiles(cachedActiveFiles);
      if (!isSwitchingModel) setConnection('ok');
      if (data.kg) renderKgMini(data.kg);
      renderDevInfo();
    } catch (err) {
      setConnection('down');
    } finally {
      statusInFlight = false;
    }
  }

  // 初始化拉取一次，之后仅在页面可见且空闲时每 10 秒轻量轮询
  fetchStatus();
  setInterval(() => {
    if (document.visibilityState === 'visible' && !isGenerating && !isSwitchingModel) fetchStatus();
  }, 10000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && !isGenerating && !isSwitchingModel) fetchStatus();
  });

  // ======================================================================
  // 全局快捷键：Esc 关闭菜单 / 弹窗 / 抽屉；Ctrl+Shift+O 新对话；/ 聚焦输入框
  // ======================================================================
  function isEditable(node) {
    if (!node || node === document.body) return false;
    const tag = node.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || node.isContentEditable;
  }
  document.addEventListener('keydown', e => {
    if (e.key === 'Tab') { trapFocus(e); return; }
    if (e.key === 'Escape') {
      if (closeAllMenus(true)) return;
      const modal = topModal();
      if (modal) { closeModal(modal); return; }
      if (isOutlineOpen() && window.matchMedia && window.matchMedia('(max-width: 1099px)').matches) { setOutlineOpen(false); return; }
      closeMobileSidebar();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'o') {
      e.preventDefault();
      if (!topModal()) newChat();
      return;
    }
    if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !isEditable(e.target) && !topModal()) {
      e.preventDefault();
      chatInput.focus();
    }
  });

  refreshPromptsQuiet();
  openSettingsFromHash();
  autoGrow();
});
