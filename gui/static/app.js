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

// ─── 初始化 ──────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initNavigation();
  initSSE();
  initInput();
  loadDefaultCwd();
  loadClis();
  loadConfig();
  loadSessions();
});

async function loadDefaultCwd() {
  try {
    const resp = await fetch('/api/default-cwd');
    const data = await resp.json();
    if (data.cwd && !cwdInput.value.trim()) {
      cwdInput.value = data.cwd;
    }
  } catch (e) { /* ignore */ }
}

async function loadClis() {
  const cliSelect = document.getElementById('cli-select');
  try {
    const resp = await fetch('/api/clis');
    const data = await resp.json();
    const available = data.available || [];
    const current = data.current || '';
    cliSelect.innerHTML = '';
    if (available.length === 0) {
      cliSelect.innerHTML = '<option value="">未检测到可用 CLI</option>';
      return;
    }
    for (const cli of available) {
      const opt = document.createElement('option');
      opt.value = cli.path;
      opt.textContent = `${cli.name}`;
      opt.title = cli.path;
      if (cli.path === current) opt.selected = true;
      cliSelect.appendChild(opt);
    }
    cliSelect.addEventListener('change', async () => {
      await fetch('/api/clis', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: cliSelect.value }),
      });
      addSystemMsg(`已切换 CLI: ${cliSelect.value}`);
    });
  } catch (e) { /* ignore */ }
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
    addSystemMsg(`会话已启动 · ${data.model}`);
  });

  eventSource.addEventListener('session_stopped', (e) => {
    sessionActive = false;
    isResponding = false;
    updateUI();
    addSystemMsg('会话已停止');
  });

  eventSource.addEventListener('system', (e) => {
    const data = JSON.parse(e.data);
    if (data.subtype === 'init') {
      addSystemMsg(`${data.model} · ${(data.tools||[]).length} tools · ${(data.skills||[]).length} skills`);
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
    loadSessions();
  });

  eventSource.addEventListener('result', (e) => {
    handleResult(JSON.parse(e.data));
  });

  eventSource.addEventListener('process_ended', (e) => {
    // ccb 进程结束 —— 确保前端退出 responding 状态
    if (isResponding) {
      isResponding = false;
      currentAssistantEl = null;
      updateUI();
    }
  });

  eventSource.addEventListener('error', (e) => {
    if (e.data) {
      const data = JSON.parse(e.data);
      addSystemMsg(data.message || '未知错误', true);
      // 收到错误事件也要退出 responding 状态
      isResponding = false;
      currentAssistantEl = null;
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
  const dot = connectionStatus.querySelector('.status-dot');
  const text = connectionStatus.querySelector('.status-text');
  dot.className = `status-dot ${connected ? 'online' : 'offline'}`;
  text.textContent = connected ? '已连接' : '连接中...';
  btnNewSession.style.opacity = connected ? '1' : '0.5';
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
    addSystemMsg('请求失败: ' + e.message, true);
    return null;
  }
}

// ─── 流式事件处理 ────────────────────────────────────────────
function handleStreamEvent(data) {
  const evt = data.event;
  if (!evt) return;

  isResponding = true;
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
          currentContent.push({ type: 'tool_use', name: finishedBlock.name, id: finishedBlock.id, input });
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
        <div class="tool-header"><span class="tool-icon">&#9881;</span> ${esc(block.name || 'Tool')}</div>
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
        <span class="thinking-label">Thinking</span>
        <span class="thinking-preview">${esc(preview)}</span>
      </div>
      <div class="thinking-content">${esc(block.thinking)}</div>
    </div>`;
  } else if (block.type === 'text' && block.text) {
    return `<div class="text-block">${renderMd(block.text)}</div>`;
  } else if (block.type === 'tool_use') {
    const input = typeof block.input === 'string' ? block.input : JSON.stringify(block.input, null, 2);
    return `<div class="tool-card">
      <div class="tool-header"><span class="tool-icon">&#9881;</span> ${esc(block.name || 'Tool')}</div>
      <div class="tool-body">${esc(input)}</div>
    </div>`;
  }
  return '';
}

function handleAssistantFinal(data) {
  // ccb 的 assistant 事件带增量消息（partial messages）
  isResponding = true;
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

  streamBlocks = {};
  renderCurrentState();
  scrollToBottom();
}

function handleResult(data) {
  isResponding = false;
  currentAssistantEl = null;
  currentContent = [];
  streamBlocks = {};
  updateUI();

  if (data.total_cost_usd) {
    totalCost = data.total_cost_usd;
    costDisplay.style.display = 'block';
    costValue.textContent = totalCost.toFixed(4);
  }

  if (data.is_error && data.errors) {
    data.errors.forEach(e => addSystemMsg(e, true));
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
let attachedFiles = []; // [{name, path, isImage}]

function initInput() {
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  inputEl.addEventListener('input', () => {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
  });

  // 粘贴图片
  inputEl.addEventListener('paste', (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const blob = item.getAsFile();
        if (blob) uploadFile(blob);
        break;
      }
    }
  });

  btnSend.addEventListener('click', sendMessage);
  btnStop.addEventListener('click', () => sendAction('stop'));
  btnNewSession.addEventListener('click', startNewSession);

  // 附件按钮
  btnAttach.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    for (const file of fileInput.files) {
      uploadFile(file);
    }
    fileInput.value = '';
  });
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
        attachedFiles.push({ name: file.name, path, isImage });
      }
      renderAttachments();
    }
  } catch (e) {
    addSystemMsg('文件上传失败: ' + e.message, true);
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
      attachedFiles.splice(parseInt(btn.dataset.idx), 1);
      renderAttachments();
    });
  });
}

function sendMessage() {
  let content = inputEl.value.trim();
  if ((!content && attachedFiles.length === 0) || !sessionActive || isResponding) return;

  // 注入文件路径
  if (attachedFiles.length > 0) {
    const filePaths = attachedFiles.map(f => `- ${f.path}`).join('\n');
    const prefix = `请查看以下附件文件:\n${filePaths}\n\n`;
    content = prefix + content;
    attachedFiles = [];
    renderAttachments();
  }

  addUserMessage(content);
  sendAction('send_message', { content });
  inputEl.value = '';
  inputEl.style.height = 'auto';
}

function startNewSession() {
  if (!clientId) {
    addSystemMsg('未连接到服务器', true);
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
    costDisplay.style.display = 'none';
    addSystemMsg('会话已停止，可修改工作目录和模型后点击「+ 新建会话」');
    return;
  }

  messagesEl.innerHTML = '';
  currentAssistantEl = null;
  currentContent = [];
  streamBlocks = {};
  totalCost = 0;
  currentSessionId = null;
  costDisplay.style.display = 'none';

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
  btnNewSession.textContent = sessionActive ? '重新开始' : '+ 新建会话';
  // 会话活跃时禁用配置修改
  cwdInput.disabled = sessionActive;
  btnBrowse.disabled = sessionActive;
  btnBrowse.style.opacity = sessionActive ? '0.4' : '1';
  const cliSelect = document.getElementById('cli-select');
  if (cliSelect) cliSelect.disabled = sessionActive;
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
    console.error('Config load failed:', e);
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
    addSystemMsg('环境变量已保存');
  };
}

function renderSkills(skills) {
  const el = document.getElementById('skills-list');
  if (!skills.length) {
    el.innerHTML = '<p class="empty-state">无 Skills</p>';
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
    el.innerHTML = '<p class="empty-state">无 Agents</p>';
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
    console.error('Load sessions failed:', e);
  }
}

function renderSessionList(sessions) {
  const el = document.getElementById('session-list');
  if (!sessions || !sessions.length) {
    el.innerHTML = '<div class="session-empty">暂无历史会话</div>';
    return;
  }

  el.innerHTML = sessions.map(s => {
    const isActive = s.session_id === currentSessionId;
    const title = s.title || '新会话';
    const time = formatTime(s.updated_at);
    return `<div class="session-item${isActive ? ' active' : ''}" data-sid="${esc(s.session_id)}" data-cwd="${esc(s.cwd)}" data-model="${esc(s.model)}">
      <div class="session-item-main">
        <div class="session-item-title">${esc(title)}</div>
        <div class="session-item-meta">${esc(s.model.replace('claude-',''))} · ${esc(time)}</div>
      </div>
      <button class="session-item-delete" title="删除">&times;</button>
    </div>`;
  }).join('');

  el.querySelectorAll('.session-item').forEach(item => {
    item.addEventListener('click', (e) => {
      if (e.target.classList.contains('session-item-delete')) return;
      resumeSession(item.dataset.sid, item.dataset.cwd, item.dataset.model);
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

async function resumeSession(sessionId, cwd, model) {
  if (!clientId) {
    addSystemMsg('未连接到服务器', true);
    return;
  }

  // 清空当前消息区
  messagesEl.innerHTML = '';
  currentAssistantEl = null;
  currentContent = [];
  streamBlocks = {};
  currentSessionId = sessionId;

  // 设置 UI
  if (cwd) cwdInput.value = cwd;
  if (model) {
    for (const opt of modelSelect.options) {
      if (opt.value === model) {
        modelSelect.value = model;
        break;
      }
    }
  }

  addSystemMsg('正在恢复会话...');

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
    console.error('Load history failed:', e);
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
    addSystemMsg(`会话已恢复 · 发送消息继续对话`);
  } else {
    addSystemMsg('恢复失败: ' + (result?.error || '未知错误'), true);
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
            <div class="tool-header"><span class="tool-icon">&#9881;</span> ${esc(block.name || 'Tool')}</div>
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
    if (d.toDateString() === now.toDateString()) {
      return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    }
    return d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
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
  pickerList.innerHTML = '<div class="picker-empty">加载中...</div>';

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
      pickerList.innerHTML = '<div class="picker-empty">此目录下无子文件夹</div>';
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
    pickerList.innerHTML = `<div class="picker-empty">请求失败: ${esc(e.message)}</div>`;
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
