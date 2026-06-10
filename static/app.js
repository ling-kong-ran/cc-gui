/**
 * CCB GUI - 前端主逻辑
 * 使用 SSE (Server-Sent Events) + fetch POST 替代 WebSocket
 */

// ─── 状态 ────────────────────────────────────────────────────
let clientId = null;
let eventSource = null;
let sessionActive = false;
let isResponding = false;
let currentAssistantEl = null;
let currentContent = [];
let streamBlocks = {};
let totalCost = 0;
let currentSessionId = null; // ccb 的 session UUID
const sessionGroupOpenState = new Map();
let connectionOnline = false;
let currentTurnContent = '';
let currentTurnHasAssistantOutput = false;
let currentTurnStartedAt = 0;
let currentTurnAttachmentCount = 0;
let lastFocusConfigReloadAt = 0;

// ─── DOM ─────────────────────────────────────────────────────
const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('message-input');
const btnSend = document.getElementById('btn-send');
const btnStop = document.getElementById('btn-stop');
const btnNewSession = document.getElementById('btn-new-session');
const modelSelect = document.getElementById('model-select');
const cwdInput = document.getElementById('cwd-input');
const connectionStatus = document.getElementById('connection-status');
const costDisplay = document.getElementById('cost-display');
const costValue = document.getElementById('cost-value');
const btnThemeToggle = document.getElementById('btn-theme-toggle');
const themeToggleText = document.getElementById('theme-toggle-text');
const languageSelect = document.getElementById('language-select');
const fontSizeRange = document.getElementById('font-size-range');
const fontSizeValue = document.getElementById('font-size-value');
const notificationsToggle = document.getElementById('notifications-toggle');
const notificationsRow = document.getElementById('notifications-row');
let currentLanguage = 'en';
let i18nMap = {};
let fontSizePercent = 100;
let notificationsEnabled = false;
let lastNotifyAt = 0;

// ─── 初始化 ──────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  initInterfaceSettings();
  initNotifications();
  await loadThemePreference();
  initNavigation();
  initSSE();
  initInput();
  initCliInstallModal();
  loadDefaultCwd();
  loadClis();
  loadModels();
  loadConfig();
  loadSessions();
  initFocusConfigReload();
});

async function loadDefaultCwd() {
  try {
    const resp = await fetch('/api/default-cwd');
    const data = await resp.json();
    if (data.cwd && !cwdInput.value.trim()) {
      cwdInput.value = data.cwd;
      scheduleSlashCommandReload();
      loadSessions();
    }
  } catch (e) { /* ignore */ }
}

function initTheme() {
  updateThemeToggle();
  btnThemeToggle.addEventListener('click', () => {
    const nextTheme = document.documentElement.classList.contains('light-theme') ? 'dark' : 'light';
    applyTheme(nextTheme);
  });
}

function initInterfaceSettings() {
  languageSelect?.addEventListener('change', () => {
    applyLanguage(languageSelect.value || 'en').then(() => {
      loadConfig();
      loadSessions();
    });
  });
  fontSizeRange?.addEventListener('input', () => {
    applyFontSize(Number(fontSizeRange.value || 100));
  });
}

function initNotifications() {
  if (!notificationsToggle) return;
  if (!("Notification" in window)) {
    notificationsEnabled = false;
    notificationsToggle.checked = false;
    notificationsToggle.disabled = true;
    if (notificationsRow) notificationsRow.title = t('notifyUnsupported');
    return;
  }

  notificationsToggle.addEventListener('change', async () => {
    if (!notificationsToggle.checked) {
      notificationsEnabled = false;
      await saveGuiSettings({ notifications_enabled: false });
      return;
    }

    const permission = await requestNotificationPermission();
    notificationsEnabled = permission === 'granted';
    notificationsToggle.checked = notificationsEnabled;
    await saveGuiSettings({ notifications_enabled: notificationsEnabled });
    if (!notificationsEnabled) {
      addSystemMsg(t('notifyPermissionDenied'), true);
    }
  });
}

async function requestNotificationPermission() {
  if (!("Notification" in window)) return 'unsupported';
  if (Notification.permission === 'granted' || Notification.permission === 'denied') {
    return Notification.permission;
  }
  try {
    return await Notification.requestPermission();
  } catch (e) {
    return Notification.permission || 'default';
  }
}

function applyNotificationPreference(enabled, persist = false) {
  const supported = "Notification" in window;
  notificationsEnabled = Boolean(enabled && supported && Notification.permission === 'granted');
  if (notificationsToggle) {
    notificationsToggle.checked = notificationsEnabled;
    notificationsToggle.disabled = !supported;
  }
  if (persist) saveGuiSettings({ notifications_enabled: notificationsEnabled });
}

function pageIsUnfocused() {
  return document.visibilityState === 'hidden' || !document.hasFocus();
}

function notifyComplete(kind, detail = {}) {
  if (!notificationsEnabled || !("Notification" in window) || Notification.permission !== 'granted' || !pageIsUnfocused()) {
    return;
  }

  const now = Date.now();
  if (now - lastNotifyAt < 1500) return;
  lastNotifyAt = now;

  const project = getProjectName(cwdInput.value.trim()) || t('appSubtitleShort');
  const model = detail.model || getDisplayModelName(modelSelect.value) || '';
  const duration = formatDuration(detail.durationMs || 0);
  const cost = formatUsd(detail.costUsd || 0);
  const prompt = summarizePrompt(detail.prompt || currentTurnContent || '');
  const meta = [model, duration, cost].filter(Boolean).join(' · ');

  let title = t('notifyTurnTitle', { project, model: model || t('model') });
  let body = [
    prompt ? t('notifyPromptLine', { prompt }) : t('notifyTurnBody', { project }),
    meta,
  ].filter(Boolean).join('\n');

  if (kind === 'subagent') {
    const agent = detail.agent || t('subagent');
    const task = summarizePrompt(detail.task || '');
    title = t('notifySubagentTitle', { agent });
    body = [task ? t('notifyTaskLine', { task }) : t('notifySubagentBody', { agent, task: project }), meta].filter(Boolean).join('\n');
  } else if (kind === 'process') {
    body = [t('notifyFallbackBody', { project }), meta].filter(Boolean).join('\n');
  }

  try {
    const notification = new Notification(title, { body, tag: `ccb-gui-${kind}`, renotify: true });
    notification.onclick = () => {
      try { window.focus(); } catch (e) { /* ignore */ }
      notification.close();
    };
    setTimeout(() => notification.close(), 8000);
  } catch (e) { /* ignore */ }
}

function summarizePrompt(text, maxLen = 90) {
  const clean = (text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return '';
  return clean.length > maxLen ? `${clean.slice(0, maxLen - 1)}…` : clean;
}

function formatDuration(ms) {
  const seconds = Math.round(Number(ms || 0) / 1000);
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  if (seconds < 60) return t('notifyDurationSeconds', { seconds });
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? t('notifyDurationMinutesSeconds', { minutes, seconds: rest }) : t('notifyDurationMinutes', { minutes });
}

function formatUsd(value) {
  const cost = Number(value || 0);
  if (!Number.isFinite(cost) || cost <= 0) return '';
  return t('notifyCost', { cost: cost.toFixed(4) });
}

function getProjectName(cwd) {
  if (!cwd) return '';
  return cwd.replace(/\\/g, '/').split('/').filter(Boolean).pop() || cwd;
}

function initFocusConfigReload() {
  window.addEventListener('focus', reloadConfigOnFocus);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      reloadConfigOnFocus();
    }
  });
}

function reloadConfigOnFocus() {
  const now = Date.now();
  if (now - lastFocusConfigReloadAt < 1500) return;
  lastFocusConfigReloadAt = now;
  reloadExternalConfig();
}

async function reloadExternalConfig() {
  await Promise.all([
    loadClis(),
    loadModels(),
    loadConfig(),
  ]);
  loadSlashCommands();
}

function applyTheme(theme, persist = true) {
  const isLight = theme === 'light';
  document.documentElement.classList.toggle('light-theme', isLight);
  const themeValue = isLight ? 'light' : 'dark';
  document.cookie = `ccb-theme=${encodeURIComponent(themeValue)}; Max-Age=31536000; Path=/; SameSite=Lax`;
  try {
    localStorage.setItem('ccb-theme', themeValue);
  } catch (e) { /* ignore */ }
  updateThemeToggle();
  if (persist) saveThemePreference(themeValue);
}

async function loadThemePreference() {
  try {
    const resp = await fetch('/api/gui-settings');
    const data = await resp.json();
    const language = data.language === 'zh' ? 'zh' : 'en';
    const size = normalizeFontSize(data.font_size_percent);

    if (data.theme === 'light' || data.theme === 'dark') {
      applyTheme(data.theme, false);
    } else {
      const currentTheme = document.documentElement.classList.contains('light-theme') ? 'light' : 'dark';
      saveGuiSettings({ theme: currentTheme });
    }

    applyFontSize(size, false);
    await applyLanguage(language, false);
    applyNotificationPreference(Boolean(data.notifications_enabled));

    if (data.language !== language || Number(data.font_size_percent) !== size) {
      saveGuiSettings({ language, font_size_percent: size });
    }
  } catch (e) {
    applyFontSize(100, false);
    await applyLanguage('en', false);
    applyNotificationPreference(false);
  }
}

async function saveThemePreference(theme) {
  await saveGuiSettings({ theme });
}

async function saveGuiSettings(settings) {
  try {
    await fetch('/api/gui-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
    });
  } catch (e) { /* ignore */ }
}

function updateThemeToggle() {
  const isLight = document.documentElement.classList.contains('light-theme');
  themeToggleText.textContent = isLight ? t('switchToDark') : t('switchToLight');
  btnThemeToggle.setAttribute('aria-label', isLight ? t('switchToDarkTheme') : t('switchToLightTheme'));
  btnThemeToggle.title = isLight ? t('switchToDarkTheme') : t('switchToLightTheme');
}

async function applyLanguage(language, persist = true) {
  currentLanguage = language === 'zh' ? 'zh' : 'en';
  if (languageSelect) languageSelect.value = currentLanguage;
  document.documentElement.lang = currentLanguage === 'zh' ? 'zh-CN' : 'en';
  await loadLanguageMap(currentLanguage);
  document.title = t('pageTitle');
  renderLocalizedText();
  updateThemeToggle();
  updateConnectionText();
  updateUI();
  updateFilePickerCount();
  if (persist) saveGuiSettings({ language: currentLanguage });
}

async function loadLanguageMap(language) {
  try {
    const resp = await fetch(`/static/i18n/${language}.json`);
    if (!resp.ok) throw new Error(`missing locale: ${language}`);
    i18nMap = await resp.json();
  } catch (e) {
    if (language !== 'en') {
      currentLanguage = 'en';
      await loadLanguageMap('en');
    }
  }
}

function renderLocalizedText() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    if (el.id === 'topbar-model' && sessionActive) return;
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    el.title = t(el.dataset.i18nTitle);
  });
}

function t(key, vars = {}) {
  let text = i18nMap[key] || key;
  for (const [name, value] of Object.entries(vars)) {
    text = text.replaceAll(`{${name}}`, String(value));
  }
  return text;
}

function applyFontSize(value, persist = true) {
  fontSizePercent = normalizeFontSize(value);
  document.documentElement.style.setProperty('--ui-scale', String(fontSizePercent / 100));
  if (fontSizeRange) fontSizeRange.value = String(fontSizePercent);
  if (fontSizeValue) fontSizeValue.textContent = `${fontSizePercent}%`;
  if (persist) saveGuiSettings({ font_size_percent: fontSizePercent });
}

function normalizeFontSize(value) {
  const size = Number(value);
  if (!Number.isFinite(size)) return 100;
  return Math.min(125, Math.max(85, Math.round(size / 5) * 5));
}

async function loadClis() {
  const cliSelect = document.getElementById('cli-select');
  const guideBtn = document.getElementById('btn-cli-install-guide');
  try {
    const resp = await fetch('/api/clis');
    const data = await resp.json();
    const available = data.available || [];
    const current = data.current || '';
    if (data.install_command) cliInstallCommand = data.install_command;
    cliSelect.innerHTML = '';
    if (available.length === 0) {
      cliSelect.innerHTML = `<option value="">${esc(t('noCli'))}</option>`;
      if (guideBtn) guideBtn.style.display = '';
      // 首次检测不到 CLI 时自动弹出安装引导
      if (!cliInstallPromptShown) {
        cliInstallPromptShown = true;
        openCliInstallModal();
      }
      return;
    }
    if (guideBtn) guideBtn.style.display = 'none';
    for (const cli of available) {
      const opt = document.createElement('option');
      opt.value = cli.path;
      opt.textContent = `${cli.name}`;
      opt.title = cli.path;
      if (cli.path === current) opt.selected = true;
      cliSelect.appendChild(opt);
    }
    cliSelect.onchange = async () => {
      await fetch('/api/clis', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: cliSelect.value }),
      });
      addSystemMsg(t('cliSwitched', { path: cliSelect.value }));
      loadSlashCommands();
    };
  } catch (e) { /* ignore */ }
}

// ─── CLI 安装引导 ────────────────────────────────────────────
let cliInstallCommand = 'npm install -g @anthropic-ai/claude-code';
let cliInstallPromptShown = false;
let cliInstalling = false;

function openCliInstallModal() {
  const overlay = document.getElementById('cli-install-overlay');
  if (!overlay) return;
  const cmdEl = document.getElementById('cli-install-cmd');
  if (cmdEl) cmdEl.textContent = cliInstallCommand;
  setCliInstallStatus('', '');
  const output = document.getElementById('cli-install-output');
  if (output) { output.style.display = 'none'; output.textContent = ''; }
  overlay.style.display = '';
}

function closeCliInstallModal() {
  const overlay = document.getElementById('cli-install-overlay');
  if (overlay) overlay.style.display = 'none';
}

function setCliInstallStatus(text, kind) {
  const status = document.getElementById('cli-install-status');
  if (!status) return;
  if (!text) { status.style.display = 'none'; status.textContent = ''; return; }
  status.style.display = '';
  status.textContent = text;
  status.className = `cli-install-status${kind ? ' ' + kind : ''}`;
}

async function copyCliInstallCommand() {
  let copied = false;
  try {
    await navigator.clipboard.writeText(cliInstallCommand);
    copied = true;
  } catch (e) {
    // http 环境下 clipboard API 可能不可用，回退到 execCommand
    const ta = document.createElement('textarea');
    ta.value = cliInstallCommand;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { copied = document.execCommand('copy'); } catch (e2) { /* ignore */ }
    ta.remove();
  }
  setCliInstallStatus(copied ? t('cmdCopied') : t('cmdCopyFailed'), copied ? 'ok' : 'err');
}

async function runCliAutoInstall() {
  if (cliInstalling) return;
  cliInstalling = true;
  const runBtn = document.getElementById('cli-install-run');
  const output = document.getElementById('cli-install-output');
  if (runBtn) runBtn.disabled = true;
  setCliInstallStatus(t('cliInstalling'), '');
  try {
    const resp = await fetch('/api/install-cli', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const result = await resp.json();
    if (output && result.output) {
      output.style.display = '';
      output.textContent = result.output;
      output.scrollTop = output.scrollHeight;
    }
    if (result.ok) {
      setCliInstallStatus(t('cliInstallSuccess'), 'ok');
      await loadClis();
      addSystemMsg(t('cliInstallSuccess'));
      setTimeout(closeCliInstallModal, 1200);
    } else {
      const reasons = {
        npm_not_found: t('cliInstallNpmMissing'),
        install_in_progress: t('cliInstallInProgress'),
        install_timeout: t('cliInstallTimeout'),
        cli_not_detected_after_install: t('cliInstallNotDetected'),
      };
      setCliInstallStatus(reasons[result.error] || t('cliInstallFailed'), 'err');
    }
  } catch (e) {
    setCliInstallStatus(t('cliInstallFailed'), 'err');
  } finally {
    cliInstalling = false;
    if (runBtn) runBtn.disabled = false;
  }
}

function initCliInstallModal() {
  document.getElementById('btn-cli-install-guide')?.addEventListener('click', openCliInstallModal);
  document.getElementById('cli-install-close')?.addEventListener('click', closeCliInstallModal);
  document.getElementById('cli-install-copy')?.addEventListener('click', copyCliInstallCommand);
  document.getElementById('cli-install-run')?.addEventListener('click', runCliAutoInstall);
  document.getElementById('cli-install-overlay')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeCliInstallModal();
  });
}

async function loadModels() {
  const previousModel = modelSelect.value;
  try {
    const resp = await fetch('/api/models');
    const models = await resp.json();
    const availableModels = Array.isArray(models) ? models.filter(Boolean) : [];
    if (!availableModels.length) {
      modelSelect.innerHTML = '<option value="claude-sonnet-4-6">Sonnet 4.6</option>';
      scheduleSlashCommandReload();
      return;
    }
    modelSelect.innerHTML = availableModels.map((model, idx) => (
      `<option value="${esc(model)}" ${(previousModel ? model === previousModel : idx === 0) ? 'selected' : ''}>${esc(formatModelName(model))}</option>`
    )).join('');
    if (previousModel && !availableModels.includes(previousModel)) {
      modelSelect.value = availableModels[0] || '';
    }
    scheduleSlashCommandReload();
  } catch (e) {
    modelSelect.innerHTML = '<option value="claude-sonnet-4-6">Sonnet 4.6</option>';
    scheduleSlashCommandReload();
  }
}

// ─── 导航 ────────────────────────────────────────────────────
function initNavigation() {
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
      document.getElementById(`page-${btn.dataset.page}`).classList.add('active');
    });
  });
}

// ─── SSE 连接 ────────────────────────────────────────────────
function initSSE() {
  // 生成唯一客户端 ID
  clientId = 'c_' + Math.random().toString(36).substring(2, 10);
  eventSource = new EventSource(`/sse?id=${clientId}`);

  eventSource.addEventListener('connected', (e) => {
    const data = JSON.parse(e.data);
    clientId = data.client_id;
    setConnectionStatus(true);
  });

  eventSource.addEventListener('session_started', (e) => {
    const data = JSON.parse(e.data);
    sessionActive = true;
    updateUI();
    const topbarModel = document.getElementById('topbar-model');
    const modelLabel = getDisplayModelName(data.model || '');
    if (topbarModel) topbarModel.textContent = modelLabel || t('noSession');
    addSystemMsg(modelLabel ? t('sessionStarted', { model: modelLabel }) : t('sessionStartedPlain'));
  });

  eventSource.addEventListener('session_stopped', (e) => {
    sessionActive = false;
    isResponding = false;
    updateUI();
    addSystemMsg(t('sessionStopped'));
  });

  eventSource.addEventListener('system', (e) => {
    const data = JSON.parse(e.data);
    if (data.subtype === 'init') {
      const modelLabel = getDisplayModelName(data.model || '');
      addSystemMsg(t('initStatus', {
        model: modelLabel || t('model'),
        tools: (data.tools || []).length,
        skills: (data.skills || []).length,
      }));
    }
  });

  eventSource.addEventListener('stream_event', (e) => {
    handleStreamEvent(JSON.parse(e.data));
  });

  eventSource.addEventListener('assistant', (e) => {
    handleAssistantFinal(JSON.parse(e.data));
  });

  eventSource.addEventListener('session_id_captured', (e) => {
    const data = JSON.parse(e.data);
    currentSessionId = data.session_id;
    openCurrentCwdSessionGroup();
    loadSessions();
  });

  eventSource.addEventListener('model_changed', (e) => {
    const data = JSON.parse(e.data);
    const modelLabel = getDisplayModelName(data.model || '');
    const topbarModel = document.getElementById('topbar-model');
    if (topbarModel && modelLabel) topbarModel.textContent = modelLabel;
    if (modelLabel) addSystemMsg(t('modelChanged', { model: modelLabel }));
  });

  eventSource.addEventListener('result', (e) => {
    handleResult(JSON.parse(e.data));
  });

  eventSource.addEventListener('tool_result', (e) => {
    const data = JSON.parse(e.data);
    finishTasks(data.tool_use_ids);
  });

  eventSource.addEventListener('process_ended', (e) => {
    // ccb 进程结束 —— 确保前端退出 responding 状态
    const data = JSON.parse(e.data || '{}');
    clearRunningTasks();
    cleanupUploadedFiles(uploadedFilesPendingCleanup);
    uploadedFilesPendingCleanup = [];
    if (isResponding) {
      const finishedTurn = currentTurnContent;
      const hadAssistantOutput = currentTurnHasAssistantOutput;
      const durationMs = Date.now() - currentTurnStartedAt;
      isResponding = false;
      currentTurnContent = '';
      currentTurnHasAssistantOutput = false;
      currentTurnStartedAt = 0;
      currentTurnAttachmentCount = 0;
      currentAssistantEl = null;
      notifyComplete('process', {
        prompt: finishedTurn,
        durationMs,
        model: getDisplayModelName(modelSelect.value),
      });
      updateUI();
      if (isSlashCommand(finishedTurn) && !hadAssistantOutput) {
        const command = getSlashCommandName(finishedTurn);
        if (Number(data.exit_code || 0) === 0) {
          addSystemMsg(t('commandCompleted', { command }));
        } else {
          addSystemMsg(t('commandEnded', { command }), true);
        }
      }
    }
  });

  eventSource.addEventListener('generation_interrupted', () => {
    isResponding = false;
    currentTurnContent = '';
    currentTurnHasAssistantOutput = false;
    currentAssistantEl = null;
    clearRunningTasks();
    cleanupUploadedFiles(uploadedFilesPendingCleanup);
    uploadedFilesPendingCleanup = [];
    updateUI();
    addSystemMsg(t('interrupted'));
  });

  eventSource.addEventListener('error', (e) => {
    if (e.data) {
      const data = JSON.parse(e.data);
      addSystemMsg(data.message || t('unknownError'), true);
      // 收到错误事件也要退出 responding 状态
      isResponding = false;
      currentTurnContent = '';
      currentTurnHasAssistantOutput = false;
      currentAssistantEl = null;
      cleanupUploadedFiles(uploadedFilesPendingCleanup);
      uploadedFilesPendingCleanup = [];
      updateUI();
    }
    if (eventSource.readyState === EventSource.CLOSED) {
      setConnectionStatus(false);
      setTimeout(initSSE, 3000);
    }
  });

  eventSource.onerror = () => {
    setConnectionStatus(false);
  };
}

function setConnectionStatus(connected) {
  connectionOnline = connected;
  const dot = connectionStatus.querySelector('.status-dot');
  dot.className = `status-dot ${connected ? 'online' : 'offline'}`;
  updateConnectionText();
  btnNewSession.style.opacity = connected ? '1' : '0.5';
}

function updateConnectionText() {
  const text = connectionStatus.querySelector('.status-text');
  if (text) text.textContent = connectionOnline ? t('connected') : t('connecting');
}

// ─── 发送 action ────────────────────────────────────────────
async function sendAction(action, extra = {}) {
  try {
    const resp = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: clientId, action, ...extra }),
    });
    return await resp.json();
  } catch (e) {
    addSystemMsg(t('requestFailed', { message: e.message }), true);
    return null;
  }
}

// ─── 流式事件处理 ────────────────────────────────────────────
function handleStreamEvent(data) {
  const evt = data.event;
  if (!evt) return;

  isResponding = true;
  currentTurnHasAssistantOutput = true;
  updateUI();

  switch (evt.type) {
    case 'message_start':
      if (!currentAssistantEl) {
        currentAssistantEl = createAssistantBubble();
        currentContent = [];
        streamBlocks = {};
      }
      break;

    case 'content_block_start': {
      const idx = evt.index;
      const blockType = evt.content_block?.type;
      streamBlocks[idx] = { type: blockType, text: '', thinking: '', input: '', name: evt.content_block?.name || '', id: evt.content_block?.id || '' };
      break;
    }

    case 'content_block_delta': {
      const block = streamBlocks[evt.index];
      if (!block) break;
      if (evt.delta?.type === 'text_delta') {
        block.text += evt.delta.text || '';
      } else if (evt.delta?.type === 'thinking_delta') {
        block.thinking += evt.delta.thinking || '';
      } else if (evt.delta?.type === 'input_json_delta') {
        block.input += evt.delta.partial_json || '';
      }
      scheduleRender();
      break;
    }

    case 'content_block_stop': {
      const finishedBlock = streamBlocks[evt.index];
      if (finishedBlock) {
        if (finishedBlock.type === 'thinking') {
          currentContent.push({ type: 'thinking', thinking: finishedBlock.thinking });
        } else if (finishedBlock.type === 'text') {
          currentContent.push({ type: 'text', text: finishedBlock.text });
        } else if (finishedBlock.type === 'tool_use') {
          let input = finishedBlock.input;
          try { input = JSON.parse(input); } catch(e) {}
          const toolBlock = { type: 'tool_use', name: finishedBlock.name, id: finishedBlock.id, input };
          currentContent.push(toolBlock);
          registerTaskBlocks([toolBlock]);
        }
        delete streamBlocks[evt.index];
      }
      scheduleRender();
      break;
    }
  }
}

let renderScheduled = false;
function scheduleRender() {
  if (renderScheduled) return;
  renderScheduled = true;
  requestAnimationFrame(() => {
    renderScheduled = false;
    renderCurrentState();
    scrollToBottom();
  });
}

function renderCurrentState() {
  if (!currentAssistantEl) return;
  const el = currentAssistantEl.querySelector('.msg-content');
  let html = '';

  for (const block of currentContent) {
    html += renderBlock(block);
  }

  for (const idx of Object.keys(streamBlocks).sort((a,b) => a-b)) {
    const block = streamBlocks[idx];
    if (block.type === 'thinking' && block.thinking) {
      html += renderBlock({ type: 'thinking', thinking: block.thinking });
    } else if (block.type === 'text' && block.text) {
      html += `<div class="text-block">${renderMd(block.text)}<span class="typing-cursor"></span></div>`;
    } else if (block.type === 'tool_use') {
      html += `<div class="tool-card">
        <div class="tool-header"><span class="tool-icon">&#9881;</span> ${esc(block.name || t('tool'))}</div>
        <div class="tool-body">${esc(block.input)}</div>
      </div>`;
    }
  }

  if (isResponding && !Object.values(streamBlocks).some(b => b.type === 'text') && currentContent.length === 0 && Object.keys(streamBlocks).length === 0) {
    html += '<span class="typing-cursor"></span>';
  }

  el.innerHTML = html;
}

function renderBlock(block) {
  if (block.type === 'thinking' && block.thinking) {
    const preview = block.thinking.replace(/\n/g, ' ').substring(0, 100);
    return `<div class="thinking-block">
      <div class="thinking-header" onclick="this.parentElement.classList.toggle('open')">
        <span class="thinking-arrow">&#9654;</span>
        <span class="thinking-label">${esc(t('thinking'))}</span>
        <span class="thinking-preview">${esc(preview)}</span>
      </div>
      <div class="thinking-content">${esc(block.thinking)}</div>
    </div>`;
  } else if (block.type === 'text' && block.text) {
    return `<div class="text-block">${renderMd(block.text)}</div>`;
  } else if (block.type === 'tool_use') {
    const input = typeof block.input === 'string' ? block.input : JSON.stringify(block.input, null, 2);
    const isRunningTask = block.name === 'Task' && block.id && runningTasks.has(block.id);
    const runningBadge = isRunningTask
      ? `<span class="tool-running-badge"><span class="agent-spinner"></span>${esc(t('running'))}</span>`
      : '';
    return `<div class="tool-card${isRunningTask ? ' tool-card-running' : ''}">
      <div class="tool-header"><span class="tool-icon">&#9881;</span> ${esc(block.name || t('tool'))}${runningBadge}</div>
      <div class="tool-body">${esc(input)}</div>
    </div>`;
  }
  return '';
}

function handleAssistantFinal(data) {
  // subagent 的 assistant 消息带 parent_tool_use_id，只更新状态栏，不混入主消息流
  if (data.parent_tool_use_id) {
    updateTaskActivity(data.parent_tool_use_id, data.message);
    return;
  }

  // ccb 的 assistant 事件带增量消息（partial messages）
  isResponding = true;
  currentTurnHasAssistantOutput = true;
  updateUI();

  if (!currentAssistantEl) {
    currentAssistantEl = createAssistantBubble();
    currentContent = [];
  }

  const message = data.message;
  if (!message || !message.content) return;

  currentContent = [];
  for (const block of message.content) {
    if (block.type === 'thinking' && block.thinking) {
      currentContent.push({ type: 'thinking', thinking: block.thinking });
    } else if (block.type === 'text' && block.text) {
      currentContent.push({ type: 'text', text: block.text });
    } else if (block.type === 'tool_use') {
      currentContent.push({ type: 'tool_use', name: block.name, id: block.id, input: block.input });
    }
  }
  registerTaskBlocks(currentContent);

  streamBlocks = {};
  renderCurrentState();
  scrollToBottom();
}

// ─── Subagent 运行状态跟踪 ───────────────────────────────────
// tool_use_id -> {type, desc, last}
const runningTasks = new Map();
// 已结束的 Task id（partial assistant 事件会重复携带同一 tool_use 块，避免重新标记为运行中）
const finishedTaskIds = new Set();

function registerTaskBlocks(content) {
  let changed = false;
  for (const block of content) {
    if (block.type !== 'tool_use' || block.name !== 'Task' || !block.id) continue;
    if (finishedTaskIds.has(block.id)) continue;
    let input = block.input;
    if (typeof input === 'string') { try { input = JSON.parse(input); } catch (e) { input = {}; } }
    if (!input || typeof input !== 'object') input = {};
    const existing = runningTasks.get(block.id) || {};
    runningTasks.set(block.id, {
      type: input.subagent_type || existing.type || '',
      desc: input.description || existing.desc || '',
      last: existing.last || '',
    });
    changed = true;
  }
  if (changed) renderAgentStatus();
}

function updateTaskActivity(parentToolUseId, message) {
  if (!parentToolUseId || finishedTaskIds.has(parentToolUseId)) return;
  // 会话恢复到一半时可能没见过对应 Task 块，此处兜底注册
  const entry = runningTasks.get(parentToolUseId) || { type: '', desc: '', last: '' };
  const content = message?.content;
  if (Array.isArray(content)) {
    for (const block of content) {
      if (block.type === 'text' && block.text) {
        entry.last = block.text.replace(/\s+/g, ' ').trim().slice(-60);
      } else if (block.type === 'tool_use' && block.name) {
        entry.last = `> ${block.name}`;
      }
    }
  }
  runningTasks.set(parentToolUseId, entry);
  renderAgentStatus();
}

function finishTasks(ids) {
  let changed = false;
  let completedTask = null;
  for (const id of ids || []) {
    const taskInfo = runningTasks.get(id);
    if (taskInfo && !completedTask) completedTask = taskInfo;
    finishedTaskIds.add(id);
    if (runningTasks.delete(id)) changed = true;
  }
  if (changed) {
    notifyComplete('subagent', {
      agent: completedTask?.type || t('subagent'),
      task: completedTask?.last || completedTask?.desc || '',
      model: getDisplayModelName(modelSelect.value),
    });
    renderAgentStatus();
    if (currentAssistantEl) scheduleRender();
  }
}

function clearRunningTasks() {
  if (runningTasks.size) {
    runningTasks.clear();
    renderAgentStatus();
  }
  finishedTaskIds.clear();
}

function renderAgentStatus() {
  const bar = document.getElementById('agent-status-bar');
  if (!bar) return;
  if (runningTasks.size === 0) {
    bar.style.display = 'none';
    bar.innerHTML = '';
    return;
  }
  bar.style.display = '';
  let html = `<span class="agent-status-title">${esc(t('agentsRunning', { count: runningTasks.size }))}</span>`;
  for (const [id, info] of runningTasks) {
    const label = info.type || t('subagent');
    const detail = info.last || info.desc || '';
    html += `<span class="agent-chip" title="${esc(info.desc || '')}">` +
      `<span class="agent-spinner"></span>${esc(label)}` +
      `${detail ? `<span class="agent-chip-detail">${esc(detail.substring(0, 40))}</span>` : ''}` +
      `</span>`;
  }
  bar.innerHTML = html;
}

function handleResult(data) {
  const finishedTurn = currentTurnContent;
  const hadAssistantOutput = currentTurnHasAssistantOutput;
  const turnCost = Number(data.total_cost_usd || 0);
  const persistedCost = Number(data.session_total_cost_usd || 0);
  isResponding = false;
  currentAssistantEl = null;
  currentContent = [];
  streamBlocks = {};
  clearRunningTasks();
  notifyComplete('turn', {
    prompt: finishedTurn,
    durationMs: Date.now() - currentTurnStartedAt,
    costUsd: turnCost,
    model: getDisplayModelName(data.model || modelSelect.value),
  });
  currentTurnContent = '';
  currentTurnHasAssistantOutput = false;
  currentTurnStartedAt = 0;
  currentTurnAttachmentCount = 0;
  cleanupUploadedFiles(uploadedFilesPendingCleanup);
  uploadedFilesPendingCleanup = [];
  updateUI();

  if (Number.isFinite(persistedCost) && persistedCost > 0) {
    totalCost = persistedCost;
    renderCost();
  } else if (Number.isFinite(turnCost) && turnCost > 0) {
    totalCost += turnCost;
    renderCost();
  }

  if (data.is_error && data.errors) {
    data.errors.forEach(e => addSystemMsg(e, true));
  } else if (isSlashCommand(finishedTurn) && !hadAssistantOutput) {
    addSystemMsg(t('commandCompleted', { command: getSlashCommandName(finishedTurn) }));
  }
}

// ─── UI 组件 ─────────────────────────────────────────────────
function createAssistantBubble() {
  const el = document.createElement('div');
  el.className = 'message assistant';
  el.innerHTML = `
    <div class="avatar assistant-avatar">C</div>
    <div class="msg-bubble"><div class="msg-content"></div></div>
  `;
  messagesEl.appendChild(el);
  return el;
}

function addUserMessage(text) {
  const el = document.createElement('div');
  el.className = 'message user';
  el.innerHTML = `
    <div class="avatar user-avatar">U</div>
    <div class="msg-bubble"><div class="msg-content">${esc(text)}</div></div>
  `;
  messagesEl.appendChild(el);
  scrollToBottom();
}

function addSystemMsg(text, isError) {
  const el = document.createElement('div');
  el.className = `system-msg${isError ? ' error' : ''}`;
  el.textContent = text;
  messagesEl.appendChild(el);
  scrollToBottom();
}

// ─── 输入 ────────────────────────────────────────────────────
const btnAttach = document.getElementById('btn-attach');
const fileInput = document.getElementById('file-input');
const attachmentsBar = document.getElementById('attachments-bar');
const slashCommandPanel = document.getElementById('slash-command-panel');
const inputWrapper = document.querySelector('.input-wrapper');
let attachedFiles = []; // [{name, path, isImage, uploaded}]
let uploadedFilesPendingCleanup = []; // 本轮已发送、等待回合结束后删除的上传缓存文件
let slashCommands = [];
let slashCommandMatches = [];
let slashCommandIndex = 0;
let slashCommandLoadTimer = null;
let inputDragDepth = 0;

function initInput() {
  inputEl.addEventListener('keydown', (e) => {
    if (handleSlashCommandKeydown(e)) return;

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  inputEl.addEventListener('input', () => {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
    updateSlashCommandPanel();
  });

  // 粘贴图片
  inputEl.addEventListener('paste', (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const blob = item.getAsFile();
        if (blob) uploadFiles([blob]);
        break;
      }
    }
  });

  btnSend.addEventListener('click', sendMessage);
  btnStop.addEventListener('click', () => sendAction('interrupt'));
  btnNewSession.addEventListener('click', startNewSession);
  document.getElementById('welcome-new-session')?.addEventListener('click', startNewSession);
  modelSelect.addEventListener('change', loadSlashCommands);
  cwdInput.addEventListener('change', loadSlashCommands);
  cwdInput.addEventListener('change', () => {
    openCurrentCwdSessionGroup();
    loadSessions();
  });
  cwdInput.addEventListener('blur', loadSlashCommands);

  // 附件按钮 —— 打开自定义文件选择器
  btnAttach.addEventListener('click', () => openFilePicker());
  fileInput.addEventListener('change', () => {
    uploadFiles(fileInput.files);
    fileInput.value = '';
  });
  initInputFileDrop();

  document.addEventListener('click', (e) => {
    if (!slashCommandPanel.contains(e.target) && e.target !== inputEl) {
      closeSlashCommandPanel();
    }
  });

  loadSlashCommands();
}

function initInputFileDrop() {
  if (!inputWrapper) return;

  inputWrapper.addEventListener('dragenter', (e) => {
    if (!dragEventHasFiles(e)) return;
    e.preventDefault();
    inputDragDepth += 1;
    inputWrapper.classList.add('drag-over');
  });

  inputWrapper.addEventListener('dragover', (e) => {
    if (!dragEventHasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });

  inputWrapper.addEventListener('dragleave', (e) => {
    if (!dragEventHasFiles(e)) return;
    e.preventDefault();
    inputDragDepth = Math.max(0, inputDragDepth - 1);
    if (inputDragDepth === 0) {
      inputWrapper.classList.remove('drag-over');
    }
  });

  inputWrapper.addEventListener('drop', (e) => {
    if (!dragEventHasFiles(e)) return;
    e.preventDefault();
    inputDragDepth = 0;
    inputWrapper.classList.remove('drag-over');
    uploadFiles(e.dataTransfer.files);
  });
}

function dragEventHasFiles(e) {
  return Array.from(e.dataTransfer?.types || []).includes('Files');
}

function uploadFiles(files) {
  Array.from(files || []).forEach((file) => {
    if (file) uploadFile(file);
  });
}

function scheduleSlashCommandReload() {
  clearTimeout(slashCommandLoadTimer);
  slashCommandLoadTimer = setTimeout(loadSlashCommands, 150);
}

async function loadSlashCommands() {
  const params = new URLSearchParams();
  if (modelSelect.value) params.set('model', modelSelect.value);
  if (cwdInput.value.trim()) params.set('cwd', cwdInput.value.trim());

  try {
    const resp = await fetch(`/api/slash-commands?${params.toString()}`);
    const data = await resp.json();
    const commands = Array.isArray(data) ? data : (data.commands || []);
    const seen = new Set();
    slashCommands = commands
      .filter(cmd => cmd.name && !seen.has(cmd.name) && seen.add(cmd.name))
      .sort((a, b) => a.name.localeCompare(b.name));
  } catch (e) {
    slashCommands = [];
  }

  updateSlashCommandPanel();
}

function getSlashQuery() {
  const value = inputEl.value;
  const cursor = inputEl.selectionStart || 0;
  if (!value.startsWith('/') || cursor !== value.length || value.includes('\n') || /\s/.test(value)) {
    return null;
  }
  return value.slice(1).toLowerCase();
}

function updateSlashCommandPanel() {
  const query = getSlashQuery();
  if (query === null) {
    closeSlashCommandPanel();
    return;
  }

  slashCommandMatches = slashCommands.filter(cmd => (
    cmd.name.slice(1).toLowerCase().includes(query) ||
    (cmd.description || '').toLowerCase().includes(query)
  )).slice(0, 10);
  slashCommandIndex = Math.min(slashCommandIndex, Math.max(slashCommandMatches.length - 1, 0));

  if (!slashCommandMatches.length) {
    slashCommandPanel.innerHTML = `<div class="slash-command-empty">${esc(t('noCommandMatches'))}</div>`;
    slashCommandPanel.style.display = 'block';
    return;
  }

  slashCommandPanel.innerHTML = slashCommandMatches.map((cmd, idx) => `
    <button type="button" class="slash-command-item${idx === slashCommandIndex ? ' active' : ''}" data-idx="${idx}">
      <span class="slash-command-name">${esc(cmd.name)}</span>
      <span class="slash-command-desc">${esc(cmd.description || '')}</span>
    </button>
  `).join('');
  slashCommandPanel.style.display = 'block';

  slashCommandPanel.querySelectorAll('.slash-command-item').forEach(btn => {
    btn.addEventListener('mouseenter', () => {
      slashCommandIndex = Number(btn.dataset.idx || 0);
      renderSlashCommandActiveState();
    });
    btn.addEventListener('click', () => {
      selectSlashCommand(Number(btn.dataset.idx || 0));
    });
  });
}

function renderSlashCommandActiveState() {
  slashCommandPanel.querySelectorAll('.slash-command-item').forEach((item, idx) => {
    item.classList.toggle('active', idx === slashCommandIndex);
  });
}

function handleSlashCommandKeydown(e) {
  if (slashCommandPanel.style.display === 'none') return false;
  if (!slashCommandMatches.length && e.key !== 'Escape') return false;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    slashCommandIndex = (slashCommandIndex + 1) % slashCommandMatches.length;
    renderSlashCommandActiveState();
    return true;
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault();
    slashCommandIndex = (slashCommandIndex - 1 + slashCommandMatches.length) % slashCommandMatches.length;
    renderSlashCommandActiveState();
    return true;
  }
  if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
    e.preventDefault();
    selectSlashCommand(slashCommandIndex);
    return true;
  }
  if (e.key === 'Escape') {
    e.preventDefault();
    closeSlashCommandPanel();
    return true;
  }
  return false;
}

function selectSlashCommand(index) {
  const cmd = slashCommandMatches[index];
  if (!cmd) return;
  inputEl.value = `${cmd.name} `;
  inputEl.focus();
  inputEl.selectionStart = inputEl.selectionEnd = inputEl.value.length;
  closeSlashCommandPanel();
}

function closeSlashCommandPanel() {
  slashCommandPanel.style.display = 'none';
  slashCommandMatches = [];
  slashCommandIndex = 0;
}

function cleanupUploadedFiles(files) {
  const paths = (files || []).filter(f => f && f.uploaded && f.path).map(f => f.path);
  if (!paths.length) return;
  fetch('/api/upload/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths }),
    keepalive: true,
  }).catch(() => {});
}

async function uploadFile(file) {
  const formData = new FormData();
  formData.append('cwd', cwdInput.value.trim() || '');
  formData.append('file', file);
  try {
    const resp = await fetch('/api/upload', { method: 'POST', body: formData });
    const data = await resp.json();
    if (data.files && data.files.length > 0) {
      for (const path of data.files) {
        const isImage = /\.(png|jpg|jpeg|gif|webp|bmp|svg)$/i.test(path);
        attachedFiles.push({ name: file.name, path, isImage, uploaded: true });
      }
      renderAttachments();
    }
  } catch (e) {
    addSystemMsg(t('uploadFailed', { message: e.message }), true);
  }
}

function renderAttachments() {
  if (attachedFiles.length === 0) {
    attachmentsBar.style.display = 'none';
    attachmentsBar.innerHTML = '';
    return;
  }
  attachmentsBar.style.display = 'flex';
  attachmentsBar.innerHTML = attachedFiles.map((f, i) => `
    <div class="attachment-item">
      ${f.isImage ? `<img src="/api/file?path=${encodeURIComponent(f.path)}" class="attachment-thumb">` : '<span class="attachment-icon">&#128196;</span>'}
      <span class="attachment-name">${esc(f.name)}</span>
      <button class="attachment-remove" data-idx="${i}">&times;</button>
    </div>
  `).join('');
  attachmentsBar.querySelectorAll('.attachment-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.idx);
      const [removed] = attachedFiles.splice(idx, 1);
      cleanupUploadedFiles([removed]);
      renderAttachments();
    });
  });
}

function sendMessage() {
  let content = inputEl.value.trim();
  if ((!content && attachedFiles.length === 0) || !sessionActive || isResponding) return;
  const originalContent = content;
  const attachmentCount = attachedFiles.length;

  // 注入文件路径。上传缓存文件只需要保留到本轮消息发出，之后异步删除以节省磁盘。
  let sentUploadedFiles = [];
  if (attachedFiles.length > 0) {
    const filesForThisTurn = attachedFiles.slice();
    sentUploadedFiles = filesForThisTurn.filter(f => f.uploaded);
    const filePaths = filesForThisTurn.map(f => `- ${f.path}`).join('\n');
    const prefix = `${t('attachmentIntro')}\n${filePaths}\n\n`;
    content = prefix + content;
    uploadedFilesPendingCleanup.push(...sentUploadedFiles);
    attachedFiles = [];
    renderAttachments();
  }

  addUserMessage(content);
  currentTurnContent = originalContent || (attachmentCount ? t('notifyAttachmentPrompt', { count: attachmentCount }) : '');
  currentTurnAttachmentCount = attachmentCount;
  currentTurnStartedAt = Date.now();
  currentTurnHasAssistantOutput = false;
  isResponding = true;
  updateUI();
  if (isSlashCommand(originalContent)) {
    addSystemMsg(t('commandRunning', { command: getSlashCommandName(originalContent) }));
  }
  sendAction('send_message', { content, model: modelSelect.value });
  inputEl.value = '';
  inputEl.style.height = 'auto';
}

function isSlashCommand(content) {
  return /^\/[^\s]+/.test((content || '').trim());
}

function getSlashCommandName(content) {
  const match = (content || '').trim().match(/^\/[^\s]+/);
  return match ? match[0] : '';
}

function startNewSession() {
  if (!clientId) {
    addSystemMsg(t('notConnected'), true);
    return;
  }

  // 如果当前有活跃会话，先停止并解锁 UI，让用户可以修改配置
  if (sessionActive) {
    sendAction('stop');
    sessionActive = false;
    isResponding = false;
    updateUI();
    messagesEl.innerHTML = '';
    currentAssistantEl = null;
    currentContent = [];
    streamBlocks = {};
    totalCost = 0;
    currentSessionId = null;
    renderCost();
    addSystemMsg(t('stoppedEditable'));
    return;
  }

  messagesEl.innerHTML = '';
  currentAssistantEl = null;
  currentContent = [];
  streamBlocks = {};
  totalCost = 0;
  currentSessionId = null;
  renderCost();

  openCurrentCwdSessionGroup();
  sendAction('new_session', {
    model: modelSelect.value,
    cwd: cwdInput.value.trim() || null,
    skip_permissions: document.getElementById('skip-permissions').checked,
  });
  loadSessions();
}

function updateUI() {
  btnSend.disabled = !sessionActive || isResponding;
  btnStop.classList.toggle('visible', isResponding);
  btnNewSession.innerHTML = `<span class="btn-prefix">&gt;</span> ${sessionActive ? t('restartSession') : t('newSession')}`;
  // 会话活跃时禁用配置修改（CLI 和模型可随时切换，下一条消息生效）
  cwdInput.disabled = sessionActive;
  btnBrowse.disabled = sessionActive;
  btnBrowse.style.opacity = sessionActive ? '0.4' : '1';
  const cliSelect = document.getElementById('cli-select');
  if (cliSelect) cliSelect.disabled = false;
  if (modelSelect) modelSelect.disabled = false;
  const skipPermissions = document.getElementById('skip-permissions');
  if (skipPermissions) skipPermissions.disabled = sessionActive;
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  });
}

// ─── 配置页 ──────────────────────────────────────────────────
async function loadConfig() {
  try {
    const env = await (await fetch('/api/env')).json();
    renderEnvEditor(env);
    const skills = await (await fetch('/api/skills')).json();
    renderSkills(skills);
    const agents = await (await fetch('/api/agents')).json();
    renderAgents(agents);
  } catch (e) {
    console.error('配置加载失败:', e);
  }
}

function renderEnvEditor(env) {
  const container = document.getElementById('env-fields');
  container.innerHTML = Object.entries(env).map(([k, v]) => `
    <div class="env-row">
      <input class="env-key" value="${esc(k)}" readonly>
      <input class="env-val" value="${esc(v)}">
    </div>
  `).join('');

  document.getElementById('btn-save-env').onclick = async () => {
    const newEnv = {};
    container.querySelectorAll('.env-row').forEach(row => {
      const key = row.querySelector('.env-key').value;
      const val = row.querySelector('.env-val').value;
      if (key) newEnv[key] = val;
    });
    await fetch('/api/env', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(newEnv),
    });
    addSystemMsg(t('envSaved'));
  };
}

function renderSkills(skills) {
  const el = document.getElementById('skills-list');
  if (!skills.length) {
    el.innerHTML = `<p class="empty-state">${esc(t('noSkills'))}</p>`;
    return;
  }
  el.innerHTML = skills.map(s => `
    <div class="skill-item">
      <span class="skill-name">/${s.name}</span>
      <span class="skill-desc">${esc(s.description)}</span>
    </div>
  `).join('');
}

function renderAgents(agents) {
  const el = document.getElementById('agents-list');
  if (!agents.length) {
    el.innerHTML = `<p class="empty-state">${esc(t('noAgents'))}</p>`;
    return;
  }
  el.innerHTML = agents.map(a => `
    <div class="agent-item">
      <span class="agent-name">${esc(a.name)}</span>
      <span class="agent-desc">${esc(a.description)}</span>
    </div>
  `).join('');
}

// ─── 会话管理 ─────────────────────────────────────────────────
async function loadSessions() {
  try {
    const sessions = await (await fetch('/api/sessions')).json();
    renderSessionList(sessions);
  } catch (e) {
    console.error('历史会话加载失败:', e);
  }
}

function renderSessionList(sessions) {
  const el = document.getElementById('session-list');
  if (!sessions || !sessions.length) {
    el.innerHTML = `<div class="session-empty">${esc(t('noHistory'))}</div>`;
    return;
  }

  const groups = groupSessionsByCwd(sessions);

  el.innerHTML = groups.map(group => {
    const forcedOpen = group.sessions.some(s => s.session_id === currentSessionId);
    const savedOpen = sessionGroupOpenState.get(group.key);
    const defaultOpen = isCurrentCwd(group.cwd) || groups.length === 1;
    const isOpen = forcedOpen || (savedOpen === undefined ? defaultOpen : savedOpen);
    const latestTime = formatTime(group.latest);
    const groupCost = group.sessions.reduce((sum, s) => sum + Number(s.total_cost_usd || 0), 0);
    const sessionsHtml = group.sessions.map(s => renderSessionItem(s)).join('');

    return `<div class="session-group${isOpen ? ' open' : ' collapsed'}" data-group-key="${esc(group.key)}">
      <button type="button" class="session-group-header" aria-expanded="${isOpen ? 'true' : 'false'}">
        <span class="session-group-chevron">${isOpen ? '▾' : '▸'}</span>
        <span class="session-group-main">
          <span class="session-group-title">${esc(group.name)}</span>
        <span class="session-group-path">${esc(group.cwd || t('unsetCwd'))}</span>
      </span>
      <span class="session-group-meta">${esc(t('itemCount', { count: group.sessions.length }))} · ${esc(latestTime)}${groupCost > 0 ? ` · $${groupCost.toFixed(4)}` : ''}</span>
      </button>
      <div class="session-group-body" ${isOpen ? '' : 'hidden'}>
        ${sessionsHtml}
      </div>
    </div>`;
  }).join('');

  el.querySelectorAll('.session-group-header').forEach(header => {
    header.addEventListener('click', () => {
      const groupEl = header.closest('.session-group');
      const key = groupEl?.dataset.groupKey || '';
      if (!key) return;
      const body = groupEl.querySelector('.session-group-body');
      const isOpen = !body.hasAttribute('hidden');
      body.toggleAttribute('hidden', isOpen);
      groupEl.classList.toggle('open', !isOpen);
      groupEl.classList.toggle('collapsed', isOpen);
      header.setAttribute('aria-expanded', String(!isOpen));
      const chevron = header.querySelector('.session-group-chevron');
      if (chevron) chevron.textContent = isOpen ? '▸' : '▾';
      sessionGroupOpenState.set(key, !isOpen);
    });
  });

  el.querySelectorAll('.session-item').forEach(item => {
    item.addEventListener('click', (e) => {
      if (e.target.classList.contains('session-item-delete')) return;
      resumeSession(item.dataset.sid, item.dataset.cwd, item.dataset.model, Number(item.dataset.cost || 0));
    });
    item.querySelector('.session-item-delete').addEventListener('click', async (e) => {
      e.stopPropagation();
      await fetch('/api/sessions/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: item.dataset.sid }),
      });
      loadSessions();
    });
  });
}

function renderSessionItem(s) {
  const isActive = s.session_id === currentSessionId;
  const title = s.title || t('newChat');
  const time = formatTime(s.updated_at);
  const savedCost = Number(s.total_cost_usd || 0);
  const modelLabel = getDisplayModelName(s.model || '', false);
  return `<div class="session-item${isActive ? ' active' : ''}" data-sid="${esc(s.session_id)}" data-cwd="${esc(s.cwd)}" data-model="${esc(s.model)}" data-cost="${esc(savedCost)}">
    <div class="session-item-main">
      <div class="session-item-title">${esc(title)}</div>
      <div class="session-item-meta">${modelLabel ? `${esc(modelLabel)} · ` : ''}${esc(time)}${savedCost > 0 ? ` · $${savedCost.toFixed(4)}` : ''}</div>
    </div>
    <button class="session-item-delete" title="${esc(t('delete'))}">&times;</button>
  </div>`;
}

function groupSessionsByCwd(sessions) {
  const map = new Map();
  for (const session of sessions) {
    const cwd = (session.cwd || '').trim();
    const key = normalizeCwdKey(cwd);
    if (!map.has(key)) {
      map.set(key, {
        key,
        cwd,
        name: getProjectName(cwd),
        latest: session.updated_at || '',
        sessions: [],
      });
    }
    const group = map.get(key);
    group.sessions.push(session);
    if ((session.updated_at || '') > (group.latest || '')) {
      group.latest = session.updated_at || '';
    }
  }
  return [...map.values()].sort((a, b) => (b.latest || '').localeCompare(a.latest || ''));
}

function normalizeCwdKey(cwd) {
  const value = (cwd || '').trim().replace(/\\/g, '/').replace(/\/+$/, '');
  return value ? value.toLowerCase() : '__no_cwd__';
}

function isCurrentCwd(cwd) {
  const current = cwdInput.value.trim();
  if (!current || !cwd) return false;
  return normalizeCwdKey(current) === normalizeCwdKey(cwd);
}

function openCurrentCwdSessionGroup() {
  const current = cwdInput.value.trim();
  if (!current) return;
  sessionGroupOpenState.set(normalizeCwdKey(current), true);
}

function getProjectName(cwd) {
  if (!cwd) return t('unsetCwd');
  const normalized = cwd.replace(/[\\\/]+$/, '');
  const parts = normalized.split(/[\\\/]+/).filter(Boolean);
  return parts[parts.length - 1] || normalized || t('unsetCwd');
}

async function resumeSession(sessionId, cwd, model, savedCost = 0) {
  if (!clientId) {
    addSystemMsg(t('notConnected'), true);
    return;
  }

  // 清空当前消息区
  messagesEl.innerHTML = '';
  currentAssistantEl = null;
  currentContent = [];
  streamBlocks = {};
  currentSessionId = sessionId;
  totalCost = Number.isFinite(savedCost) ? savedCost : 0;
  renderCost();

  // 设置 UI
  if (cwd) cwdInput.value = cwd;
  openCurrentCwdSessionGroup();
  if (model && hasModelOption(model)) {
    modelSelect.value = model;
  }

  addSystemMsg(t('restoring'));

  // 加载历史消息
  try {
    const resp = await fetch('/api/sessions/history', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, cwd: cwd }),
    });
    const history = await resp.json();
    if (history && history.length > 0) {
      renderHistory(history);
    }
  } catch(e) {
    console.error('历史消息加载失败:', e);
  }

  const result = await sendAction('resume_session', {
    session_id: sessionId,
    model: model || modelSelect.value,
    cwd: cwd || cwdInput.value.trim() || null,
    skip_permissions: document.getElementById('skip-permissions').checked,
  });

  if (result && result.ok) {
    sessionActive = true;
    updateUI();
    addSystemMsg(t('restored'));
  } else {
    addSystemMsg(t('restoreFailed', { message: result?.error || t('unknownError') }), true);
  }
  loadSessions();
}

function renderHistory(history) {
  for (const msg of history) {
    if (msg.role === 'user') {
      addUserMessage(msg.text);
    } else if (msg.role === 'assistant') {
      const el = createAssistantBubble();
      const contentEl = el.querySelector('.msg-content');
      let html = '';
      for (const block of (msg.blocks || [])) {
        if (block.type === 'text') {
          html += `<div class="text-block">${renderMd(block.text)}</div>`;
        } else if (block.type === 'tool_use') {
          const input = typeof block.input === 'string' ? block.input : JSON.stringify(block.input, null, 2);
          html += `<div class="tool-card">
            <div class="tool-header"><span class="tool-icon">&#9881;</span> ${esc(block.name || t('tool'))}</div>
            <div class="tool-body">${esc(input.length > 200 ? input.substring(0, 200) + '...' : input)}</div>
          </div>`;
        }
      }
      contentEl.innerHTML = html;
    }
  }
  scrollToBottom();
}

function formatTime(isoStr) {
  if (!isoStr) return '';
  try {
    const d = new Date(isoStr);
    const now = new Date();
    const locale = currentLanguage === 'zh' ? 'zh-CN' : 'en-US';
    if (d.toDateString() === now.toDateString()) {
      return d.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
    }
    return d.toLocaleDateString(locale, { month: 'numeric', day: 'numeric' });
  } catch(e) {
    return isoStr.substring(5, 16);
  }
}

// ─── Markdown 渲染 ──────────────────────────────────────────
function renderMd(text) {
  if (!text) return '';

  let html = esc(text);

  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return `<pre><code class="lang-${lang}">${code}</code></pre>`;
  });
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
  html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');
  html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
  html = html.replace(/^---$/gm, '<hr>');
  html = html.replace(/\n\n/g, '</p><p>');
  html = `<p>${html}</p>`;
  html = html.replace(/\n/g, '<br>');
  html = html.replace(/<p><\/p>/g, '');
  html = html.replace(/<p>(<h[1-4]>)/g, '$1');
  html = html.replace(/(<\/h[1-4]>)<\/p>/g, '$1');
  html = html.replace(/<p>(<pre>)/g, '$1');
  html = html.replace(/(<\/pre>)<\/p>/g, '$1');
  html = html.replace(/<p>(<ul>)/g, '$1');
  html = html.replace(/(<\/ul>)<\/p>/g, '$1');
  html = html.replace(/<p>(<blockquote>)/g, '$1');
  html = html.replace(/(<\/blockquote>)<\/p>/g, '$1');
  html = html.replace(/<p>(<hr>)<\/p>/g, '$1');
  return html;
}

function esc(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ─── 目录选择器 ──────────────────────────────────────────────
const pickerOverlay = document.getElementById('dir-picker-overlay');
const pickerList = document.getElementById('picker-list');
const pickerCurrentPath = document.getElementById('picker-current-path');
const pickerUp = document.getElementById('picker-up');
const pickerClose = document.getElementById('picker-close');
const pickerSelect = document.getElementById('picker-select');
const btnBrowse = document.getElementById('btn-browse');
let pickerCurrentDir = '/';

btnBrowse.addEventListener('click', () => {
  if (!sessionActive) openPicker();
});
pickerClose.addEventListener('click', closePicker);
pickerOverlay.addEventListener('click', (e) => {
  if (e.target === pickerOverlay) closePicker();
});
pickerUp.addEventListener('click', () => {
  navigatePicker(pickerCurrentDir === '/' ? '/' : getParentPath(pickerCurrentDir));
});
pickerSelect.addEventListener('click', () => {
  cwdInput.value = pickerCurrentDir;
  openCurrentCwdSessionGroup();
  loadSessions();
  loadSlashCommands();
  closePicker();
});

function openPicker() {
  pickerOverlay.style.display = 'flex';
  navigatePicker(cwdInput.value || '/');
}

function closePicker() {
  pickerOverlay.style.display = 'none';
}

async function navigatePicker(path) {
  pickerCurrentDir = path;
  pickerCurrentPath.textContent = path || '/';
  pickerList.innerHTML = `<div class="picker-empty">${esc(t('pickerLoading'))}</div>`;

  try {
    const resp = await fetch('/api/browse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    const data = await resp.json();

    if (data.error) {
      pickerList.innerHTML = `<div class="picker-empty">${esc(data.error)}</div>`;
      return;
    }

    pickerCurrentDir = data.current || path;
    pickerCurrentPath.textContent = pickerCurrentDir;

    if (!data.items || data.items.length === 0) {
      pickerList.innerHTML = `<div class="picker-empty">${esc(t('emptyDirFolders'))}</div>`;
      return;
    }

    pickerList.innerHTML = data.items.map(item => `
      <div class="picker-item ${item.type === 'drive' ? 'drive' : ''}" data-path="${esc(item.path)}">
        <span class="picker-item-icon">${item.type === 'drive' ? '&#128423;' : '&#128193;'}</span>
        <span class="picker-item-name">${esc(item.name)}</span>
      </div>
    `).join('');

    pickerList.querySelectorAll('.picker-item').forEach(el => {
      el.addEventListener('dblclick', () => navigatePicker(el.dataset.path));
      el.addEventListener('click', () => {
        pickerList.querySelectorAll('.picker-item').forEach(i => i.classList.remove('selected'));
        el.classList.add('selected');
        pickerCurrentDir = el.dataset.path;
        pickerCurrentPath.textContent = pickerCurrentDir;
      });
    });
  } catch (e) {
    pickerList.innerHTML = `<div class="picker-empty">${esc(t('requestFailed', { message: e.message }))}</div>`;
  }
}

function getParentPath(p) {
  if (!p || p === '/') return '/';
  const parts = p.replace(/\\/g, '/').split('/').filter(Boolean);
  if (parts.length <= 1) return '/';
  parts.pop();
  if (parts.length === 1 && parts[0].endsWith(':')) return parts[0] + '/';
  return parts.join('/');
}

// ─── 文件选择器 ──────────────────────────────────────────────
const filePickerOverlay = document.getElementById('file-picker-overlay');
const filePickerList = document.getElementById('file-picker-list');
const filePickerCurrentPath = document.getElementById('file-picker-current-path');
const filePickerUp = document.getElementById('file-picker-up');
const filePickerClose = document.getElementById('file-picker-close');
const filePickerConfirm = document.getElementById('file-picker-confirm');
const filePickerSelectedCount = document.getElementById('file-picker-selected-count');
const filePickerSearch = document.getElementById('file-picker-search');

let filePickerCurrentDir = '/';
let filePickerSelected = new Map(); // path -> name
let filePickerItems = [];
let filePickerSearchTimer = null;
let filePickerSearchSeq = 0;

filePickerClose.addEventListener('click', closeFilePicker);
filePickerOverlay.addEventListener('click', (e) => {
  if (e.target === filePickerOverlay) closeFilePicker();
});
filePickerUp.addEventListener('click', () => {
  navigateFilePicker(getParentPath(filePickerCurrentDir));
});
filePickerConfirm.addEventListener('click', confirmFileSelection);
filePickerSearch.addEventListener('input', handleFilePickerSearchInput);

function openFilePicker() {
  filePickerSelected.clear();
  filePickerSearch.value = '';
  updateFilePickerCount();
  filePickerOverlay.style.display = 'flex';
  // 默认打开当前 CWD，没有就用根目录
  navigateFilePicker(cwdInput.value.trim() || '/');
}

function closeFilePicker() {
  filePickerOverlay.style.display = 'none';
}

function updateFilePickerCount() {
  filePickerSelectedCount.textContent = t('selectedFiles', { count: filePickerSelected.size });
  filePickerConfirm.disabled = filePickerSelected.size === 0;
}

async function navigateFilePicker(path) {
  filePickerCurrentDir = path;
  filePickerCurrentPath.textContent = path || '/';
  filePickerItems = [];
  filePickerSearch.value = '';
  filePickerSearchSeq += 1;
  filePickerList.innerHTML = `<div class="picker-empty">${esc(t('pickerLoading'))}</div>`;

  try {
    const resp = await fetch('/api/browse-files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    const data = await resp.json();

    if (data.error) {
      filePickerList.innerHTML = `<div class="picker-empty">${esc(data.error)}</div>`;
      return;
    }

    filePickerCurrentDir = data.current || path;
    filePickerCurrentPath.textContent = filePickerCurrentDir;

    filePickerItems = data.items || [];

    if (filePickerItems.length === 0) {
      filePickerList.innerHTML = `<div class="picker-empty">${esc(t('emptyDir'))}</div>`;
      return;
    }

    renderFilePickerItems(filePickerItems);
  } catch (e) {
    filePickerList.innerHTML = `<div class="picker-empty">${esc(t('requestFailed', { message: e.message }))}</div>`;
  }
}

function hasModelOption(model) {
  if (!model) return;
  for (const opt of modelSelect.options) {
    if (opt.value === model) return true;
  }
  return false;
}

function renderCost() {
  costDisplay.style.display = totalCost > 0 ? 'block' : 'none';
  costValue.textContent = totalCost.toFixed(4);
}

function formatModelName(model) {
  model = (model || '').trim();
  if (!model) return '';
  const names = {
    'claude-opus-4-6': 'Opus 4.6',
    'claude-sonnet-4-6': 'Sonnet 4.6',
    'claude-haiku-4-6': 'Haiku 4.6',
  };
  return names[model] || model.replace(/^claude-/, '');
}

function isDisplayableModel(model) {
  const value = (model || '').trim();
  return Boolean(value && !/^<[^>]+>$/.test(value));
}

function getDisplayModelName(model, allowSelectedFallback = true) {
  if (isDisplayableModel(model)) return formatModelName(model);
  const selected = allowSelectedFallback ? modelSelect?.value : '';
  return isDisplayableModel(selected) ? formatModelName(selected) : '';
}

function handleFilePickerSearchInput() {
  window.clearTimeout(filePickerSearchTimer);
  const keyword = filePickerSearch.value.trim();

  if (!keyword) {
    renderFilePickerItems(filePickerItems);
    return;
  }

  filePickerSearchTimer = window.setTimeout(() => {
    searchFilePicker(keyword);
  }, 250);
}

async function searchFilePicker(keyword) {
  const seq = ++filePickerSearchSeq;
  filePickerList.innerHTML = `<div class="picker-empty">${esc(t('searchLoading'))}</div>`;

  try {
    const resp = await fetch('/api/search-files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: filePickerCurrentDir, query: keyword }),
    });
    const data = await resp.json();
    if (seq !== filePickerSearchSeq || filePickerSearch.value.trim() !== keyword) return;

    if (data.error) {
      filePickerList.innerHTML = `<div class="picker-empty">${esc(data.error)}</div>`;
      return;
    }

    renderFilePickerItems(data.items || [], {
      emptyText: t('noMatches'),
      truncated: data.truncated,
    });
  } catch (e) {
    if (seq === filePickerSearchSeq) {
      filePickerList.innerHTML = `<div class="picker-empty">${esc(t('searchFailed', { message: e.message }))}</div>`;
    }
  }
}

function renderFilePickerItems(items, options = {}) {
  const keyword = filePickerSearch.value.trim().toLowerCase();
  const filteredItems = keyword && items === filePickerItems
    ? items.filter(item => `${item.name} ${item.path}`.toLowerCase().includes(keyword))
    : items;

  if (filteredItems.length === 0) {
    filePickerList.innerHTML = `<div class="picker-empty">${esc(options.emptyText || (keyword ? t('noMatches') : t('emptyDir')))}</div>`;
    return;
  }

  filePickerList.innerHTML = `${options.truncated ? `<div class="picker-empty compact">${esc(t('tooManyResults'))}</div>` : ''}${filteredItems.map(item => {
    const isDir = item.type === 'dir' || item.type === 'drive';
    const icon = item.type === 'drive' ? '&#128423;' : isDir ? '&#128193;' : getFileIcon(item.name);
    const isSelected = filePickerSelected.has(item.path);
    const displayName = item.display || item.name;
    return `<div class="picker-item file-picker-item ${item.type === 'drive' ? 'drive' : ''} ${isSelected ? 'selected' : ''}"
        data-path="${esc(item.path)}" data-type="${esc(item.type)}" data-name="${esc(displayName)}">
      <span class="picker-item-icon">${icon}</span>
      <span class="picker-item-name">${esc(displayName)}</span>
      ${!isDir && isSelected ? '<span class="picker-check">✓</span>' : ''}
    </div>`;
  }).join('')}`;

  filePickerList.querySelectorAll('.file-picker-item').forEach(el => {
    el.addEventListener('click', () => {
      const type = el.dataset.type;
      const itemPath = el.dataset.path;
      const itemName = el.dataset.name;

      if (type === 'dir' || type === 'drive') {
        navigateFilePicker(itemPath);
        return;
      }

      if (filePickerSelected.has(itemPath)) {
        filePickerSelected.delete(itemPath);
      } else {
        filePickerSelected.set(itemPath, itemName);
      }
      updateFilePickerCount();
      renderFilePickerItems(filePickerSearch.value.trim() ? filteredItems : filePickerItems);
    });
  });
}

function getFileIcon(name) {
  const ext = name.split('.').pop().toLowerCase();
  const imageExts = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'];
  const codeExts = ['js', 'ts', 'py', 'go', 'rs', 'java', 'c', 'cpp', 'h', 'cs', 'rb', 'php', 'sh', 'bat'];
  const docExts = ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'];
  const textExts = ['txt', 'md', 'log', 'csv', 'json', 'yaml', 'yml', 'toml', 'xml', 'html', 'css'];
  if (imageExts.includes(ext)) return '&#128444;';
  if (codeExts.includes(ext)) return '&#128196;';
  if (docExts.includes(ext)) return '&#128209;';
  if (textExts.includes(ext)) return '&#128196;';
  return '&#128196;';
}

function confirmFileSelection() {
  if (filePickerSelected.size === 0) return;

  for (const [filePath, fileName] of filePickerSelected) {
    // 直接引用本地路径，不上传，不做缩略图（避免安全限制）
    attachedFiles.push({ name: fileName, path: filePath, isImage: false, uploaded: false });
  }

  renderAttachments();
  closeFilePicker();
}
