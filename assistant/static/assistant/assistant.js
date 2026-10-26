
(function () {
    'use strict';

    var root = document.getElementById('assistantRoot');
    if (!root) return;

    var fab = document.getElementById('assistantFab');
    var panel = document.getElementById('assistantPanel');
    var list = document.getElementById('assistantMessages');
    var chips = document.getElementById('assistantChips');
    var form = document.getElementById('assistantForm');
    var input = document.getElementById('assistantInput');
    var sendBtn = document.getElementById('assistantSend');
    var clearBtn = document.getElementById('assistantClear');
    var closeBtn = document.getElementById('assistantClose');

    var URL_CHAT = root.dataset.url;
    var CSRF = root.dataset.csrf;
    var KEY = 'assistant:v1:' + (root.dataset.user || '0');   
    var MAX_SEND = 12, MAX_KEEP = 20;
    var history = [];
    var busy = false;

    function load() {
        try {
            var saved = JSON.parse(sessionStorage.getItem(KEY) || '[]');
            if (Array.isArray(saved)) {
                history = saved.filter(function (m) {
                    return m && (m.role === 'user' || m.role === 'assistant') && typeof m.text === 'string';
                }).slice(-MAX_KEEP);
            }
        } catch (e) { history = []; }
    }
    function save() {
        try { sessionStorage.setItem(KEY, JSON.stringify(history.slice(-MAX_KEEP))); } catch (e) { /* storage unavailable */ }
    }

    function el(tag, cls) { var n = document.createElement(tag); if (cls) n.className = cls; return n; }

    function inline(node, text) {
        text.split(/(\*\*[^*]+\*\*)/g).forEach(function (part) {
            if (!part) return;
            if (/^\*\*[^*]+\*\*$/.test(part)) {
                var s = document.createElement('strong');
                s.textContent = part.slice(2, -2);
                node.appendChild(s);
            } else {
                node.appendChild(document.createTextNode(part.replace(/\*\*/g, '')));
            }
        });
    }

    function renderRich(container, text) {
        var listEl = null, listType = null, para = null;
        text.replace(/\r/g, '').split('\n').forEach(function (line) {
            var bullet = line.match(/^\s*[-*\u2022]\s+(.*)$/);
            var numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
            if (bullet || numbered) {
                var type = bullet ? 'ul' : 'ol';
                if (listType !== type) { listEl = document.createElement(type); container.appendChild(listEl); listType = type; }
                var li = document.createElement('li');
                inline(li, (bullet || numbered)[1]);
                listEl.appendChild(li);
                para = null;
                return;
            }
            listEl = null; listType = null;
            if (!line.trim()) { para = null; return; }
            if (!para) { para = document.createElement('p'); container.appendChild(para); }
            else { para.appendChild(document.createElement('br')); }
            inline(para, line);
        });
    }

    function scrollDown() { list.scrollTop = list.scrollHeight; }

    function addMessage(role, text, extra) {
        var row = el('div', 'assistant-msg assistant-msg-' + (role === 'user' ? 'user' : 'bot') + (extra ? ' ' + extra : ''));
        var bubble = el('div', 'assistant-bubble');
        if (role === 'user') { bubble.textContent = text; } else { renderRich(bubble, text); }
        row.appendChild(bubble);
        list.appendChild(row);
        scrollDown();
        return row;
    }

    function showTyping() {
        var row = el('div', 'assistant-msg assistant-msg-bot assistant-typing');
        row.setAttribute('aria-label', 'Assistant is typing');
        var bubble = el('div', 'assistant-bubble');
        for (var i = 0; i < 3; i++) bubble.appendChild(document.createElement('i'));
        row.appendChild(bubble);
        list.appendChild(row);
        scrollDown();
        return row;
    }

    function setBusy(state) {
        busy = state;
        sendBtn.disabled = state;
        input.disabled = state;
    }

    function updateChips() { chips.hidden = history.length > 0; }

    function autoGrow() {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    }

    function fallbackError(status) {
        if (status === 401) return 'Your session has expired. Please sign in again.';
        if (status === 403) return 'Your session may have expired. Please refresh the page and try again.';
        if (status === 429) return 'Too many messages. Please wait a moment and try again.';
        return 'Something went wrong. Please try again.';
    }

    function ask(text) {
        text = (text || '').trim();
        if (!text || busy) return;

        var prior = history.slice(-MAX_SEND);
        addMessage('user', text);
        history.push({ role: 'user', text: text });
        save();
        updateChips();
        input.value = '';
        autoGrow();
        setBusy(true);
        var typing = showTyping();

        var ctl = new AbortController();
        var timer = setTimeout(function () { ctl.abort(); }, 40000);

        fetch(URL_CHAT, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': CSRF,
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: JSON.stringify({ message: text, history: prior, page: window.location.pathname }),
            signal: ctl.signal
        })
            .then(function (res) {
                return res.json().catch(function () { return {}; }).then(function (data) {
                    return { ok: res.ok, status: res.status, data: data };
                });
            })
            .then(function (r) {
                typing.remove();
                if (r.ok && r.data.reply) {
                    addMessage('bot', r.data.reply);
                    history.push({ role: 'assistant', text: r.data.reply });
                } else {
                    addMessage('bot', r.data.error || fallbackError(r.status), 'assistant-msg-error');
                    history.pop();                 
                    input.value = text;           
                    autoGrow();
                }
            })
            .catch(function (err) {
                typing.remove();
                addMessage('bot', err && err.name === 'AbortError'
                    ? 'That took too long. Please try again.'
                    : 'Couldn\u2019t reach the assistant. Check your internet connection and try again.',
                    'assistant-msg-error');
                history.pop();
                input.value = text;
                autoGrow();
            })
            .finally(function () {
                clearTimeout(timer);
                save();
                updateChips();
                setBusy(false);
                if (!panel.hidden) input.focus();
            });
    }

    function openPanel() {
        panel.hidden = false;
        fab.setAttribute('aria-expanded', 'true');
        fab.setAttribute('aria-label', 'Close help assistant');
        scrollDown();
        input.focus();
    }
    function closePanel() {
        panel.hidden = true;
        fab.setAttribute('aria-expanded', 'false');
        fab.setAttribute('aria-label', 'Open help assistant');
        fab.focus();
    }

    fab.addEventListener('click', function () { panel.hidden ? openPanel() : closePanel(); });
    closeBtn.addEventListener('click', closePanel);
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !panel.hidden) closePanel();
    });

    clearBtn.addEventListener('click', function () {
        history = [];
        save();
        Array.prototype.slice.call(list.querySelectorAll('.assistant-msg:not(.assistant-greeting)'))
            .forEach(function (n) { n.remove(); });
        updateChips();
        input.focus();
    });

    chips.addEventListener('click', function (e) {
        var b = e.target.closest('button[data-q]');
        if (b) ask(b.dataset.q);
    });

    form.addEventListener('submit', function (e) { e.preventDefault(); ask(input.value); });
    input.addEventListener('input', autoGrow);
    input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(input.value); }
    });

    load();
    history.forEach(function (m) { addMessage(m.role, m.text); });
    updateChips();
}());
