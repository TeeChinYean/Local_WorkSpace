import re
import sys

with open("app/static/app.js", "r", encoding="utf-8") as f:
    code = f.read()

# 1. Update fetchStatus to also fetch chats
new_fetch_status = """
    try {
      const chatRes = await fetch('/api/chats');
      if (chatRes.ok) {
        const chatData = await chatRes.json();
        renderChatList(chatData.chats, chatData.current_chat_id);
      }
    } catch(e) {}
"""
code = code.replace("const res = await fetch('/api/status');", "const res = await fetch('/api/status');\n" + new_fetch_status)

# 2. Add chat UI rendering and actions
new_chat_logic = """
  // ==== Chat List Logic ====
  const chatList = $('chat-list');
  const chatListEmpty = $('chat-list-empty');

  function renderChatList(chats, currentId) {
    if (!chatList) return;
    chatList.replaceChildren();
    if (!chats || Object.keys(chats).length === 0) {
      chatListEmpty.hidden = false;
      return;
    }
    chatListEmpty.hidden = true;
    for (const chat of chats) {
      const li = document.createElement('li');
      li.className = 'file-item';
      if (chat.id === currentId) li.classList.add('active');
      
      const a = document.createElement('a');
      a.className = 'file-name';
      a.textContent = chat.title || '新对话';
      a.onclick = async (e) => {
        e.preventDefault();
        if (chat.id === currentId) return;
        try {
          const res = await fetch('/api/chats/switch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({chat_id: chat.id})
          });
          if (res.ok) { location.reload(); }
        } catch(e) {}
      };
      
      const delBtn = document.createElement('button');
      delBtn.className = 'icon-btn remove-btn';
      delBtn.title = '删除';
      delBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
      delBtn.onclick = async (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (!await confirmDialog({title: '删除历史对话？', body: '此操作不可恢复。', okText: '删除'})) return;
        try {
          const res = await fetch('/api/chats/' + chat.id, {method: 'DELETE'});
          if (res.ok) { location.reload(); }
        } catch(e) {}
      };
      
      li.appendChild(a);
      li.appendChild(delBtn);
      chatList.appendChild(li);
    }
  }
  // ==== End Chat List ====
"""
code = code.replace("const fileList = $('file-list');", new_chat_logic + "\n  const fileList = $('file-list');")

# 3. Modify newChat logic
old_new_chat = """
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
"""
new_new_chat = """
  async function newChat() {
    closeMobileSidebar();
    try {
      const res = await fetch('/api/chats/new', { method: 'POST' });
      if (res.ok) { location.reload(); }
      return;
"""
code = code.replace(old_new_chat, new_new_chat)

with open("app/static/app.js", "w", encoding="utf-8") as f:
    f.write(code)

print("Patched app.js successfully")
