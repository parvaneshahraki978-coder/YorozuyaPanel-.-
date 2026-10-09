/* YorozuyaPanel — منطق SPA پنل ادمین (JS خام، بدون وابستگی خارجی) */
(() => {
  'use strict';

  // ------------------------------------------------------------ کمک‌ها
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (value) =>
    String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const pad = (n) => String(n).padStart(2, '0');
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const dateInputValue = (iso) => {
    const d = iso ? new Date(iso) : null;
    return d && !Number.isNaN(d.getTime()) ? ymd(d) : '';
  };
  // پایان همان روز به وقت محلی مرورگر (تا تاریخ روی تابلو با تاریخ واردشده یکی باشه)
  const endOfDayIso = (value) => {
    const [y, m, d] = value.split('-').map(Number);
    return new Date(y, m - 1, d, 23, 59, 59).toISOString();
  };
  const GB = 1024 ** 3;
  const nf = new Intl.NumberFormat('fa-IR');
  const fmtDay = new Intl.DateTimeFormat('fa-IR-u-ca-persian', { year: 'numeric', month: 'long', day: 'numeric' });
  const rtf = new Intl.RelativeTimeFormat('fa', { numeric: 'auto' });
  const STATUS = { active: 'فعال', expired: 'منقضی', disabled: 'غیرفعال', limit_reached: 'حجم تمام' };
  const PROTO = { vless: 'VLESS', vmess: 'VMess', trojan: 'Trojan', ss: 'Shadowsocks' };
  const TABS = ['dashboard', 'users', 'nodes', 'tools', 'settings'];
  const NODE_TEXT_FIELDS = [
    'name', 'address', 'port', 'protocol', 'network', 'security', 'sni', 'path', 'host', 'fp', 'alpn',
    'reality_pbk', 'reality_sid', 'reality_spx', 'flow', 'ss_method', 'ss_password',
  ];
  const JOKES = [
    'بدون بکاپ مثل بستنی بدون قاشقه؛ می‌شه خوردش، ولی خیلی بد می‌گذره. بکاپ بگیر! 🍨',
    'نود آفلاین شد؟ اول مطمئن شو سرور خودت روشنه. گاهی فقط خودِ ما قهر کردیم 😅',
    'کاربر منقضی حذف نمی‌شه؛ فقط می‌ره گوشه‌ی تابلو و یه چرت کوچولو می‌زنه 💤',
    'ترافیک مثل کیک خامه‌ای زود تموم می‌شه. ریست ترافیک رو یادت نره 🍰',
    'من بیشتر وقتم رو بالای تابلو می‌گذرونم، ولی کانفیگ‌ها رو درست نگه می‌دارم. قول 🍓',
    'یه نود خوب مثل یه دوست خوبه: پیدا کردنش سخته، ولی وقتی پیداش کردی ولش نکن ⚡',
    'شیر توت‌فرنگی بدون نی؟ مثل پنل بدون لینک اشتراک؛ هیچ‌کدوم جواب نمی‌ده 🥛',
    'قانون اول: هیچ کاربری بی‌دلیل حذف نمی‌شه. قانون دوم: اگه حذف شد، بکاپت کجاست؟ 📋',
    'آی‌پی سرور عوض شد؟ نگران نباش؛ دوباره چک کن. دنیای پروکسی همیشه یه کم شلوغه 🌍',
    'رمز ضعیف مثل برچسب کاغذیِ روی تابلوئه؛ یه نسیم می‌بره. رمز قوی بذار 🔐',
  ];

  const state = { users: [], nodes: [], tab: '', jokeIndex: -1 };

  const fmtBytes = (bytes) => {
    const n = Number(bytes) || 0;
    if (n >= GB) return `${nf.format(Math.round((n / GB) * 100) / 100)} گیگ`;
    if (n >= 1024 ** 2) return `${nf.format(Math.round((n / 1024 ** 2) * 10) / 10)} مگ`;
    return `${nf.format(n)} بایت`;
  };
  const round2 = (n) => Math.round((Number(n) || 0) * 100) / 100;
  const fmtDate = (iso) => {
    const d = iso ? new Date(iso) : null;
    return d && !Number.isNaN(d.getTime()) ? fmtDay.format(d) : '—';
  };
  const daysText = (days) => {
    if (days === null || days === undefined) return 'بدون انقضا';
    if (days < 0) return 'منقضی شده';
    if (days === 0) return 'امروز تمام می‌شه';
    return `${nf.format(days)} روز مونده`;
  };
  const timeAgo = (iso) => {
    if (!iso) return 'هنوز چک نشده';
    const sec = (new Date(iso).getTime() - Date.now()) / 1000;
    for (const [unit, size] of [['day', 86400], ['hour', 3600], ['minute', 60]]) {
      if (Math.abs(sec) >= size) return rtf.format(Math.round(sec / size), unit);
    }
    return rtf.format(Math.round(sec), 'second');
  };
  const flagOf = (code) =>
    /^[A-Za-z]{2}$/.test(code || '')
      ? [...code.toUpperCase()].map((c) => String.fromCodePoint(0x1f1e6 + c.charCodeAt(0) - 65)).join('')
      : '🌐';
  const findUser = (id) => state.users.find((u) => u.id === id);
  const findNode = (id) => state.nodes.find((n) => n.id === id);
  const need = (item, what) => {
    if (!item) throw new Error(`${what} پیدا نشد؛ صفحه را تازه کن`);
    return item;
  };

  async function api(path, { method = 'GET', body, form } = {}) {
    const init = { method, credentials: 'same-origin', headers: {} };
    if (form) {
      init.body = form;
    } else if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch {
      throw new Error('ارتباط با سرور قطع شد');
    }
    if (res.status === 401) {
      location.replace('/login');
      throw new Error('نیاز به ورود دارید');
    }
    const isJson = (res.headers.get('content-type') || '').includes('application/json');
    const data = isJson ? await res.json().catch(() => null) : await res.text();
    if (!res.ok) {
      const detail = data && typeof data === 'object' ? data.detail : '';
      throw new Error(typeof detail === 'string' && detail ? detail : `خطای ${res.status}`);
    }
    return data;
  }

  // هر handler را امن می‌کند: خطاها به‌صورت toast نشون داده می‌شن
  const guard = (fn) => (event) => {
    try {
      const out = fn(event);
      if (out && typeof out.catch === 'function') out.catch((err) => toast(err.message, 'err'));
    } catch (err) {
      toast(err.message, 'err');
    }
  };

  function toast(message, kind = 'info') {
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.textContent = message;
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), 3800);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = Object.assign(document.createElement('textarea'), { value: text });
      ta.style.cssText = 'position:fixed;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    toast('کپی شد ✅', 'ok');
  }

  // ------------------------------------------------------------ مودال‌ها
  function openModal(id) {
    const modal = $(`#${id}`);
    modal.hidden = false;
    const focusable = modal.querySelector('input:not([type="hidden"]), select, textarea, button');
    if (focusable) focusable.focus();
  }
  const closeModal = (id) => {
    $(`#${id}`).hidden = true;
  };
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') $$('.modal:not([hidden])').forEach((m) => { m.hidden = true; });
  });
  document.addEventListener('click', (e) => {
    if (e.target.classList && e.target.classList.contains('modal')) e.target.hidden = true;
  });

  let pendingConfirm = null;
  function askConfirm({ title, text, okText = 'تأیید', danger = false, onOk }) {
    $('#confirm-title').textContent = title;
    $('#confirm-text').textContent = text;
    const ok = $('#confirm-ok');
    ok.textContent = okText;
    ok.className = `btn ${danger ? 'danger' : 'primary'}`;
    pendingConfirm = onOk;
    openModal('modal-confirm');
  }
  $('#confirm-ok').addEventListener('click', async () => {
    const run = pendingConfirm;
    pendingConfirm = null;
    closeModal('modal-confirm');
    if (!run) return;
    try {
      await run();
    } catch (err) {
      toast(err.message, 'err');
    }
  });

  // ------------------------------------------------------------ داده و رندر
  async function loadUsers() {
    state.users = await api('/api/users');
  }
  async function loadNodes() {
    state.nodes = await api('/api/nodes');
  }
  async function loadAll() {
    await Promise.all([loadUsers(), loadNodes()]);
  }
  async function refreshAll() {
    await loadAll();
    render();
  }
  function render() {
    if (state.tab === 'dashboard') renderDashboard();
    if (state.tab === 'users') renderUsers();
    if (state.tab === 'nodes') renderNodes();
  }

  function showTab() {
    const hash = location.hash.replace('#', '');
    state.tab = TABS.includes(hash) ? hash : 'dashboard';
    $$('.tab-btn').forEach((a) => a.classList.toggle('active', a.dataset.tab === state.tab));
    $$('.tab').forEach((sec) => { sec.hidden = sec.id !== `tab-${state.tab}`; });
    if (state.tab === 'dashboard') loadServerInfo().catch((err) => toast(err.message, 'err'));
    if (state.tab === 'settings') loadSettings().catch((err) => toast(err.message, 'err'));
    render();
  }

  // ---- داشبورد
  function pickJoke() {
    let i = Math.floor(Math.random() * JOKES.length);
    if (i === state.jokeIndex) i = (i + 1) % JOKES.length;
    state.jokeIndex = i;
    $('#joke-text').textContent = JOKES[i];
  }

  function renderDashboard() {
    const count = (status) => state.users.filter((u) => u.status === status).length;
    const used = state.users.reduce((sum, u) => sum + (u.used_bytes || 0), 0);
    const online = state.nodes.filter((n) => n.online === true).length;
    const cards = [
      ['👥', state.users.length, 'کاربران', '#ff4f7e'],
      ['✅', count('active'), 'فعال', '#3f9d62'],
      ['⏳', count('expired'), 'منقضی', '#cf3b47'],
      ['⛔', count('disabled') + count('limit_reached'), 'غیرفعال / حجم تمام', '#f2b441'],
      ['🛰', state.nodes.length, 'نود', '#3b8fd9'],
      ['🟢', online, 'نود آنلاین', '#3f9d62'],
      ['📦', fmtBytes(used), 'ترافیک مصرفی کل', '#9a6a3f'],
    ];
    $('#stats').innerHTML = cards
      .map(([icon, value, label, color]) => `
        <div class="stat" style="--c:${color}">
          <div class="num">${icon} ${typeof value === 'number' ? nf.format(value) : esc(value)}</div>
          <div class="lbl">${esc(label)}</div>
        </div>`)
      .join('');
  }

  async function loadServerInfo(refresh = false) {
    const card = $('#server-card');
    card.innerHTML = '<h2>🌍 آی‌پی و کشور سرور</h2><p class="muted">در حال گرفتن اطلاعات…</p>';
    const info = await api(`/api/server-info${refresh ? '?refresh=true' : ''}`);
    const head = '<h2>🌍 آی‌پی و کشور سرور</h2>';
    const refreshBtn = '<button class="btn small ghost" type="button" data-server-refresh>🔄 به‌روزرسانی</button>';
    if (!info.ip) {
      card.innerHTML = `${head}<div class="err-box">${esc(info.error || 'آی‌پی سرور پیدا نشد')}</div>${refreshBtn}`;
      return;
    }
    const where = [info.country, info.city].filter(Boolean).join(' · ') || 'کشور نامشخص';
    card.innerHTML = `
      ${head}
      <div class="copy-row" style="align-items:center">
        <span style="font-size:44px;line-height:1">${esc(flagOf(info.country_code))}</span>
        <div>
          <div class="ltr" style="font-size:20px;font-weight:800">${esc(info.ip)}</div>
          <div class="muted">${esc(where)}</div>
          <div class="muted">${esc(info.isp || info.org || '')}</div>
        </div>
      </div>
      ${info.error ? `<p class="muted">⚠️ ${esc(info.error)}</p>` : ''}
      <div style="margin-top:10px">${refreshBtn}</div>`;
  }

  // ---- کاربران
  function userCard(u) {
    const pct = u.usage_percent;
    const barClass = pct === null ? '' : pct >= 100 ? 'full' : pct >= 80 ? 'warn' : '';
    const bar = pct === null ? '' : `<div class="bar ${barClass}" title="${pct}%"><span style="width:${Math.min(pct, 100)}%"></span></div>`;
    const traffic = u.limit_bytes > 0 ? `${fmtBytes(u.used_bytes)} از ${fmtBytes(u.limit_bytes)}` : `${fmtBytes(u.used_bytes)} · نامحدود`;
    const expiry = u.expires_at ? `${fmtDate(u.expires_at)} · ${daysText(u.days_left)}` : 'بدون تاریخ انقضا';
    const toggle = u.status === 'expired'
      ? `<button class="btn small" type="button" data-action="edit-user" data-id="${esc(u.id)}">📅 تمدید</button>`
      : `<button class="btn small ghost" type="button" data-action="toggle-user" data-id="${esc(u.id)}">${u.enabled ? '⏸ غیرفعال' : '▶️ فعال'}</button>`;
    return `
      <article class="item ${esc(u.status)}">
        <div class="item-head">
          <div>
            <div class="item-title">${esc(u.username)}</div>
            ${u.note ? `<div class="note">${esc(u.note)}</div>` : ''}
          </div>
          <span class="badge ${esc(u.status)}">${esc(STATUS[u.status] || u.status)}</span>
        </div>
        <div class="item-meta"><span>📦 ${esc(traffic)}</span><span>⏳ ${esc(expiry)}</span></div>
        ${bar}
        <div class="item-actions">
          <button class="btn small primary" type="button" data-action="links" data-id="${esc(u.id)}">🔗 لینک‌ها</button>
          <button class="btn small" type="button" data-action="edit-user" data-id="${esc(u.id)}">✏️ ویرایش</button>
          ${toggle}
          <button class="btn small ghost" type="button" data-action="reset-traffic" data-id="${esc(u.id)}">♻️ ریست ترافیک</button>
          <button class="btn small danger" type="button" data-action="delete-user" data-id="${esc(u.id)}">🗑 حذف</button>
        </div>
      </article>`;
  }

  function renderUsers() {
    const q = $('#user-search').value.trim().toLowerCase();
    const filter = $('#user-filter').value;
    const list = state.users
      .filter((u) => {
        const hit = !q || u.username.toLowerCase().includes(q) || (u.note || '').toLowerCase().includes(q);
        return hit && (filter === 'all' || u.status === filter);
      })
      .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
    $('#user-count').textContent = `${nf.format(list.length)} از ${nf.format(state.users.length)}`;
    $('#user-list').innerHTML = list.length
      ? list.map(userCard).join('')
      : `<div class="empty"><span class="big">🍡</span>${state.users.length
        ? 'کاربری با این فیلتر پیدا نشد'
        : 'هنوز کاربری نساختی. اولین مشتری رو اضافه کن!'}</div>`;
  }

  function syncExpiryHint() {
    const value = $('#f-expires').value;
    if (!value) {
      $('#expires-hint').textContent = 'بدون تاریخ انقضا (نامحدود)';
      return;
    }
    const [y, m, d] = value.split('-').map(Number);
    $('#expires-hint').textContent = `تاریخ شمسی: ${fmtDay.format(new Date(y, m - 1, d, 12))}`;
  }

  function openUserModal(user = null) {
    const form = $('#user-form');
    form.reset();
    $('#user-modal-title').textContent = user ? `✏️ ویرایش ${user.username}` : '➕ کاربر جدید';
    form.elements.id.value = user ? user.id : '';
    form.elements.username.value = user ? user.username : '';
    form.elements.limit_gb.value = user ? String(user.limit_gb) : '0';
    form.elements.used_gb.value = user ? String(round2(user.used_bytes / GB)) : '0';
    $('#used-field').hidden = !user;
    form.elements.expires.value = user ? dateInputValue(user.expires_at) : '';
    form.elements.enabled.checked = user ? Boolean(user.enabled) : true;
    form.elements.note.value = user ? user.note || '' : '';
    // برای اینکه ذخیره، مقادیر دست‌نخورده را دوباره نفرستد (گرد کردن نکنه)
    form.dataset.autoDisabled = user && user.auto_disabled ? '1' : '';
    form.dataset.origUsed = form.elements.used_gb.value;
    form.dataset.origExpires = user ? dateInputValue(user.expires_at) : '';
    form.dataset.origIso = user ? user.expires_at || '' : '';
    syncExpiryHint();
    openModal('modal-user');
  }

  async function saveUser(e) {
    e.preventDefault();
    const form = e.currentTarget;
    const id = form.elements.id.value;
    const dateValue = form.elements.expires.value;
    const body = {
      username: form.elements.username.value.trim(),
      limit_gb: Number(form.elements.limit_gb.value) || 0,
      expires_at: dateValue === form.dataset.origExpires ? form.dataset.origIso : dateValue ? endOfDayIso(dateValue) : '',
      enabled: form.elements.enabled.checked,
      note: form.elements.note.value.trim(),
    };
    if (id && form.elements.used_gb.value !== form.dataset.origUsed) {
      body.used_gb = Number(form.elements.used_gb.value) || 0;
    }
    if (id) await api(`/api/users/${id}`, { method: 'PUT', body });
    else await api('/api/users', { method: 'POST', body });
    closeModal('modal-user');
    toast('ذخیره شد 🍓', 'ok');
    await refreshAll();
  }

  async function toggleUser(id) {
    const u = need(findUser(id), 'کاربر');
    await api(`/api/users/${id}`, { method: 'PUT', body: { enabled: !u.enabled } });
    toast(u.enabled ? 'کاربر غیرفعال شد' : 'کاربر فعال شد ✅', 'ok');
    await refreshAll();
  }

  function confirmResetTraffic(id) {
    const u = need(findUser(id), 'کاربر');
    askConfirm({
      title: 'ریست ترافیک؟',
      text: `مصرف «${u.username}» صفر می‌شه.`,
      okText: 'ریست کن',
      onOk: async () => {
        await api(`/api/users/${id}/reset-traffic`, { method: 'POST' });
        toast('ترافیک ریست شد ♻️', 'ok');
        await refreshAll();
      },
    });
  }

  function confirmDeleteUser(id) {
    const u = need(findUser(id), 'کاربر');
    askConfirm({
      title: 'حذف کاربر؟',
      text: `«${u.username}» و لینک اشتراکش برای همیشه حذف می‌شه. (کاربر منقضی را می‌شه فقط غیرفعال نگه داشت.)`,
      okText: 'حذف کن',
      danger: true,
      onOk: async () => {
        await api(`/api/users/${id}`, { method: 'DELETE' });
        toast('حذف شد', 'ok');
        await refreshAll();
      },
    });
  }

  function confirmRotate(id) {
    askConfirm({
      title: 'لینک اشتراک عوض شه؟',
      text: 'لینک قبلی دیگه کار نمی‌کنه و باید لینک جدید رو به کاربر بدی.',
      okText: 'تعویض کن',
      danger: true,
      onOk: async () => {
        await api(`/api/users/${id}/rotate-sub`, { method: 'POST' });
        toast('لینک جدید ساخته شد 🔁', 'ok');
        await refreshAll();
        await openLinks(id);
      },
    });
  }

  async function openLinks(id) {
    const data = await api(`/api/users/${id}/links`);
    $('#links-title').textContent = `🔗 لینک‌های ${data.username}`;
    $('#links-body').innerHTML = `
      <div class="field">
        <label>لینک اشتراک (Subscription)</label>
        <div class="copy-row">
          <input class="ltr" type="text" readonly value="${esc(data.sub_url)}">
          <button class="btn" type="button" data-copy="${esc(data.sub_url)}">📋 کپی</button>
        </div>
      </div>
      <div class="qr-row">
        <div class="qr-box"><img src="${esc(data.qr_url)}" alt="QR کد اشتراک" width="200" height="200"></div>
        <div>
          <p class="muted">QR رو با اپ اسکن کن، یا از دکمه‌های زیر مستقیم ایمپورت کن.</p>
          <div class="modal-actions">
            <a class="btn ghost" href="${esc(data.page_url)}" target="_blank" rel="noopener">🌐 صفحه اشتراک</a>
            <button class="btn ghost" type="button" data-action="rotate-sub" data-id="${esc(id)}">🔁 تعویض لینک</button>
          </div>
        </div>
      </div>
      <h3>ایمپورت مستقیم</h3>
      <div class="app-grid">
        ${data.apps.map((a) => `<a class="app-btn" href="${esc(a.url)}">${esc(a.name)}<small>${esc(a.platform)}</small></a>`).join('')}
      </div>
      <h3>لینک تکی نودها (${nf.format(data.links.length)})</h3>
      <ul class="link-list">
        ${data.links.length
          ? data.links
            .map((l) => `<li><span>🍓 ${esc(l.name)} <small>${esc(PROTO[l.protocol] || l.protocol)}</small></span>
              <button class="btn small" type="button" data-copy="${esc(l.link)}">📋 کپی</button></li>`)
            .join('')
          : '<li class="muted">هنوز نودی ثبت نشده</li>'}
      </ul>`;
    openModal('modal-links');
  }

  // ---- نودها
  function nodeCard(n) {
    const dot = n.online === true ? 'on' : n.online === false ? 'off' : '';
    let statusText = 'چک نشده';
    if (n.online === true) statusText = `آنلاین · ${nf.format(n.latency_ms ?? 0)} ms`;
    if (n.online === false) statusText = `آفلاین · ${n.last_error || 'بدون پاسخ'}`;
    const where = n.country ? `${flagOf(n.country_code)} ${n.country}` : '🌐 کشور نامشخص';
    return `
      <article class="item">
        <div class="item-head">
          <div>
            <div class="item-title"><span class="dot ${dot}"></span>${esc(n.name)}</div>
            <div class="note ltr">${esc(n.address)}:${esc(n.port)}</div>
          </div>
          <span class="badge proto">${esc(PROTO[n.protocol] || n.protocol)}</span>
        </div>
        <div class="item-meta">
          <span>🔀 ${esc(n.network)}</span>
          <span>🔒 ${esc(n.security)}</span>
          <span>${esc(where)}</span>
        </div>
        <div class="item-meta"><span>${esc(statusText)}</span><span>⏱ ${esc(timeAgo(n.last_check))}</span></div>
        <div class="item-actions">
          <button class="btn small primary" type="button" data-action="check-node" data-id="${esc(n.id)}">⚡ چک</button>
          <button class="btn small" type="button" data-action="edit-node" data-id="${esc(n.id)}">✏️ ویرایش</button>
          <button class="btn small danger" type="button" data-action="delete-node" data-id="${esc(n.id)}">🗑 حذف</button>
        </div>
      </article>`;
  }

  function renderNodes() {
    $('#node-list').innerHTML = state.nodes.length
      ? state.nodes.map(nodeCard).join('')
      : '<div class="empty"><span class="big">🛰</span>هنوز نودی ثبت نشده. اول یه نود اضافه کن.</div>';
  }

  function syncNodeForm() {
    const form = $('#node-form');
    const values = {
      protocol: form.elements.protocol.value,
      network: form.elements.network.value,
      security: form.elements.security.value,
    };
    // REALITY فقط برای VLESS و Trojan
    const reality = form.elements.security.querySelector('option[value="reality"]');
    reality.disabled = values.protocol === 'vmess' || values.protocol === 'ss';
    if (reality.disabled && values.security === 'reality') {
      form.elements.security.value = 'none';
      values.security = 'none';
    }
    $$('[data-show]', form).forEach((el) => {
      el.hidden = !el.dataset.show.split(';').every((cond) => {
        const [key, list] = cond.split(':');
        return list.split(',').includes(values[key]);
      });
    });
  }

  function openNodeModal(node = null) {
    const form = $('#node-form');
    form.reset();
    $('#node-modal-title').textContent = node ? `✏️ ویرایش ${node.name}` : '➕ نود جدید';
    const defaults = { port: 443, protocol: 'vless', network: 'tcp', security: 'reality', fp: 'chrome' };
    form.elements.id.value = node ? node.id : '';
    for (const key of NODE_TEXT_FIELDS) {
      const source = node ? node[key] : defaults[key];
      form.elements[key].value = source === null || source === undefined ? '' : String(source);
    }
    syncNodeForm();
    openModal('modal-node');
  }

  async function saveNode(e) {
    e.preventDefault();
    const form = e.currentTarget;
    const id = form.elements.id.value;
    const body = {};
    for (const key of NODE_TEXT_FIELDS) body[key] = form.elements[key].value.trim();
    body.port = Number(body.port) || 0;
    // فیلدهای نامربوط با پروتکل انتخابی رو خنثی می‌کنیم
    if (body.protocol === 'ss') {
      body.network = 'tcp';
      body.security = 'none';
    }
    if (body.protocol === 'vmess' && body.security === 'reality') body.security = 'none';
    if (id) await api(`/api/nodes/${id}`, { method: 'PUT', body });
    else await api('/api/nodes', { method: 'POST', body });
    closeModal('modal-node');
    toast('نود ذخیره شد 🛰', 'ok');
    await refreshAll();
  }

  function replaceNode(node) {
    const i = state.nodes.findIndex((n) => n.id === node.id);
    if (i >= 0) state.nodes[i] = node;
    else state.nodes.push(node);
  }

  async function checkNode(id) {
    const node = await api(`/api/nodes/${id}/check`, { method: 'POST' });
    replaceNode(node);
    render();
    toast(
      node.online ? `آنلاین ✅ (${nf.format(node.latency_ms)} ms)` : `آفلاین ❌ ${node.last_error || ''}`,
      node.online ? 'ok' : 'err',
    );
  }

  async function checkAll() {
    const btn = $('#btn-check-all');
    btn.disabled = true;
    btn.textContent = '⏳ در حال چک…';
    try {
      const res = await api('/api/nodes/check-all', { method: 'POST' });
      await loadNodes();
      render();
      const online = res.nodes.filter((n) => n.online).length;
      toast(`${nf.format(res.checked)} نود چک شد · ${nf.format(online)} آنلاین`, 'ok');
    } finally {
      btn.disabled = false;
      btn.textContent = '⚡ چک همه';
    }
  }

  function confirmDeleteNode(id) {
    const n = need(findNode(id), 'نود');
    askConfirm({
      title: 'حذف نود؟',
      text: `«${n.name}» از همه لینک‌های اشتراک حذف می‌شه.`,
      okText: 'حذف کن',
      danger: true,
      onOk: async () => {
        await api(`/api/nodes/${id}`, { method: 'DELETE' });
        toast('نود حذف شد', 'ok');
        await refreshAll();
      },
    });
  }

  // ---- ابزار
  async function lookup(e) {
    e.preventDefault();
    const query = $('#lookup-input').value.trim();
    const box = $('#lookup-result');
    if (!query) return;
    box.innerHTML = '<p class="muted">در حال جست‌وجو…</p>';
    try {
      const r = await api('/api/tools/lookup', { method: 'POST', body: { query } });
      box.innerHTML = `
        <div class="lookup-card">
          <span class="flag">${esc(r.flag || '🌐')}</span>
          <dl>
            <dt>آی‌پی</dt><dd class="ltr">${esc(r.ip)}</dd>
            <dt>کشور</dt><dd>${esc(r.country || 'نامشخص')}${r.country_code ? ` (${esc(r.country_code)})` : ''}</dd>
            <dt>شهر</dt><dd>${esc(r.city || '—')}</dd>
            <dt>ISP</dt><dd>${esc(r.isp || '—')}</dd>
            <dt>سازمان</dt><dd>${esc(r.org || '—')}</dd>
          </dl>
        </div>`;
    } catch (err) {
      box.innerHTML = `<div class="err-box">${esc(err.message)}</div>`;
    }
  }

  // ---- تنظیمات، بکاپ، ریستور
  async function loadSettings() {
    const s = await api('/api/settings');
    $('#sub-base-input').value = s.sub_base_url || '';
    $('#effective-base').textContent = s.effective_base_url;
  }

  async function saveSettings(e) {
    e.preventDefault();
    await api('/api/settings', { method: 'PUT', body: { sub_base_url: $('#sub-base-input').value.trim() } });
    toast('ذخیره شد ✅', 'ok');
    await loadSettings();
  }

  function restoreFromFile(e) {
    e.preventDefault();
    const file = $('#restore-file').files[0];
    if (!file) {
      toast('اول یه فایل بکاپ انتخاب کن', 'err');
      return;
    }
    askConfirm({
      title: 'ریستور بکاپ؟',
      text: 'کاربران و نودهای فعلی با محتوای این فایل جایگزین می‌شن. ادامه می‌دی؟',
      okText: 'ریستور کن',
      danger: true,
      onOk: async () => {
        const form = new FormData();
        form.append('file', file);
        const res = await api('/api/restore', { method: 'POST', form });
        $('#restore-file').value = '';
        toast(`ریستور شد: ${nf.format(res.users)} کاربر، ${nf.format(res.nodes)} نود`, 'ok');
        await refreshAll();
        await loadSettings();
      },
    });
  }

  async function logout() {
    try {
      await api('/api/logout', { method: 'POST' });
    } finally {
      location.replace('/login');
    }
  }

  // ------------------------------------------------------------ رویدادها
  async function runAction(action, id) {
    switch (action) {
      case 'links': return openLinks(id);
      case 'edit-user': return openUserModal(need(findUser(id), 'کاربر'));
      case 'toggle-user': return toggleUser(id);
      case 'reset-traffic': return confirmResetTraffic(id);
      case 'delete-user': return confirmDeleteUser(id);
      case 'rotate-sub': return confirmRotate(id);
      case 'check-node': return checkNode(id);
      case 'edit-node': return openNodeModal(need(findNode(id), 'نود'));
      case 'delete-node': return confirmDeleteNode(id);
      default: return undefined;
    }
  }

  document.addEventListener('click', (e) => {
    const closer = e.target.closest('[data-close]');
    if (closer) {
      closer.closest('.modal').hidden = true;
      return;
    }
    const copy = e.target.closest('[data-copy]');
    if (copy) {
      copyText(copy.dataset.copy);
      return;
    }
    if (e.target.closest('[data-server-refresh]')) {
      loadServerInfo(true).catch((err) => toast(err.message, 'err'));
      return;
    }
    const btn = e.target.closest('[data-action]');
    if (btn) {
      runAction(btn.dataset.action, btn.dataset.id).catch((err) => toast(err.message, 'err'));
    }
  });

  $('#btn-new-user').addEventListener('click', () => openUserModal(null));
  $('#quick-new-user').addEventListener('click', () => openUserModal(null));
  $('#btn-new-node').addEventListener('click', () => openNodeModal(null));
  $('#btn-check-all').addEventListener('click', guard(checkAll));
  $('#user-search').addEventListener('input', renderUsers);
  $('#user-filter').addEventListener('change', renderUsers);
  $('#joke-next').addEventListener('click', pickJoke);
  $('#user-form').addEventListener('submit', guard(saveUser));
  $('#node-form').addEventListener('submit', guard(saveNode));
  $('#node-form').addEventListener('change', syncNodeForm);
  $('#f-expires').addEventListener('change', () => {
    syncExpiryHint();
    const form = $('#user-form');
    const value = $('#f-expires').value;
    // اگر کاربرِ منقضی تاریخش رو به آینده تمدید کرد، فعال‌سازی خودکار تیک می‌خوره
    if (form.dataset.autoDisabled === '1' && (!value || value >= ymd(new Date()))) {
      form.elements.enabled.checked = true;
    }
  });
  $('#expires-chips').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-days]');
    if (!chip) return;
    const days = Number(chip.dataset.days);
    const input = $('#f-expires');
    if (days === 0) {
      input.value = '';
    } else {
      // تمدید از روی تاریخ فعلی (اگه هنوز آینده باشه) وگرنه از امروز
      const today = ymd(new Date());
      const base = input.value && input.value > today ? new Date(`${input.value}T12:00:00`) : new Date();
      base.setDate(base.getDate() + days);
      input.value = ymd(base);
    }
    input.dispatchEvent(new Event('change'));
  });
  $('#lookup-form').addEventListener('submit', guard(lookup));
  $('#settings-form').addEventListener('submit', guard(saveSettings));
  $('#restore-form').addEventListener('submit', guard(restoreFromFile));
  $('#logout-btn').addEventListener('click', guard(logout));

  async function boot() {
    try {
      await api('/api/me');
      await loadAll();
    } catch (err) {
      // 401 قبلاً به /login هدایت کرده؛ بقیه خطاها (مثلاً قطع شبکه) نشون داده می‌شن
      if (location.pathname === '/panel') toast(err.message, 'err');
      return;
    }
    pickJoke();
    window.addEventListener('hashchange', showTab);
    showTab();
    setInterval(() => {
      if (!document.hidden) refreshAll().catch(() => {});
    }, 60000);
  }

  boot();
})();
