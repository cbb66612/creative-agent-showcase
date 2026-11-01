(function () {
  var chat = document.getElementById('chat');
  if (!chat) return;
  var form = document.getElementById('agent-form');
  var input = document.getElementById('agent-input');
  var sid = chat.getAttribute('data-sid');
  var sendBtn = document.getElementById('agent-send');
  var busy = false;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function scrollDown() {
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
  }

  function addUser(text) {
    var wrap = el('div', 'msg me');
    wrap.appendChild(el('div', 'bubble', text));
    chat.appendChild(wrap);
    scrollDown();
  }

  function addTyping() {
    var wrap = el('div', 'msg bot typing');
    var b = el('div', 'bubble');
    b.appendChild(el('span', 'dotw'));
    b.appendChild(el('span', 'muted', ' 正在查库…'));
    wrap.appendChild(b);
    chat.appendChild(wrap);
    scrollDown();
    return wrap;
  }

  function stepsBlock(steps, elapsed) {
    if (!steps || !steps.length) return null;
    var box = el('details', 'steps');
    var sum = el('summary', '', '用了 ' + steps.length + ' 步工具 · ' + (elapsed ? (elapsed / 1000).toFixed(1) + ' 秒' : ''));
    box.appendChild(sum);
    var ol = el('ol', 'steplist');
    steps.forEach(function (s) {
      var li = el('li');
      li.appendChild(el('span', 'toolname', s.tool));
      if (s.args && Object.keys(s.args).length) {
        li.appendChild(el('code', 'args', JSON.stringify(s.args)));
      }
      li.appendChild(el('span', 'res' + (s.ok ? '' : ' bad'), s.summary || ''));
      ol.appendChild(li);
    });
    box.appendChild(ol);
    return box;
  }

  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function inlineHtml(line) {
    var h = escHtml(line);
    h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
    h = h.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
    h = h.replace(/\[(\d+)\]/g, '<b class="cite">[$1]</b>');
    return h;
  }

  function renderAnswer(res) {
    var wrap = el('div', 'msg bot');
    var body = el('div', 'bubble');

    var head = el('div', 'bothead');
    head.appendChild(el('span', 'mark', '剧'));
    head.appendChild(el('span', 'name', '灵感引擎 Agent'));
    if (res.mode === 'agent') head.appendChild(el('span', 'pill ok', '工具调用'));
    body.appendChild(head);

    var txt = el('div', 'answer');
    (res.answer || '').split('\n').forEach(function (line) {
      if (!line.trim()) { txt.appendChild(el('div', 'gap')); return; }
      var p = el('div', 'line');
      var bullet = /^\s*[-*]\s+/.test(line);
      var numbered = /^\s*\d+[.)]\s+/.test(line);
      if (bullet) p.className = 'line li';
      p.innerHTML = (bullet ? '• ' : '') + inlineHtml(line.replace(/^\s*[-*]\s+/, ''));
      if (numbered) p.className = 'line li-n';
      txt.appendChild(p);
    });
    body.appendChild(txt);

    var sb = stepsBlock(res.steps, res.elapsed_ms);
    if (sb) body.appendChild(sb);

    if (res.sources && res.sources.length) {
      var src = el('div', 'srcs');
      src.appendChild(el('span', 'muted', '引用来源'));
      res.sources.slice(0, 8).forEach(function (s) {
        var a = el('a', 'srcchip', '[' + s.id + '] ' + s.title);
        a.href = '/r/' + s.id;
        a.title = s.title;
        src.appendChild(a);
      });
      body.appendChild(src);
    }
    wrap.appendChild(body);
    chat.appendChild(wrap);
    scrollDown();
  }

  function renderError(text, hint) {
    var wrap = el('div', 'msg bot');
    var b = el('div', 'bubble errb');
    b.appendChild(el('div', '', text));
    if (hint) b.appendChild(el('div', 'muted', hint));
    wrap.appendChild(b);
    chat.appendChild(wrap);
    scrollDown();
  }

  function send(q) {
    q = (q || '').trim();
    if (!q || busy) return;
    busy = true;
    sendBtn.disabled = true;
    input.value = '';
    input.style.height = 'auto';
    addUser(q);
    var typing = addTyping();
    fetch('/api/agent/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ q: q, sid: sid })
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, data: j }; });
    }).then(function (out) {
      typing.remove();
      var d = out.data || {};
      if (!out.ok || !d.ok) {
        renderError(d.error || '出错了', d.mode === 'no-key' ? '配好 Key 再回来：右上角「AI 设置」' : '');
      } else {
        if (d.sid) { sid = d.sid; chat.setAttribute('data-sid', sid); }
        renderAnswer(d);
      }
    }).catch(function (e) {
      typing.remove();
      renderError('网络或服务器出错：' + e, '看下那个黑窗口是不是还开着。');
    }).then(function () {
      busy = false;
      sendBtn.disabled = false;
      input.focus();
    });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    send(input.value);
  });

  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send(input.value);
    }
  });

  input.addEventListener('input', function () {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
  });

  document.querySelectorAll('[data-ask]').forEach(function (b) {
    b.addEventListener('click', function () { send(b.getAttribute('data-ask')); });
  });

  input.focus();
})();
