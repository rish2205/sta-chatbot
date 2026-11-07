/* STA assistant widget: floating chat button -> welcome screen (asks name) -> chat.
 * Embed on any page:  <script src="http://YOUR-SERVER:8000/widget.js" defer></script>
 */
(() => {
  'use strict';
  if (window.__staWidgetLoaded) return;
  window.__staWidgetLoaded = true;

  const scriptEl = document.currentScript;
  const BASE = scriptEl && scriptEl.src ? new URL(scriptEl.src).origin : '';

  const ICONS = {
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-8.9 8.4 9 9 0 0 1-3.6-.8L3 21l1.9-5A8.4 8.4 0 1 1 21 11.5z"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    share: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></svg>',
    restart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg>',
    send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
    pdf: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></svg>',
  };



  // ------------------------------------------------------------------ helpers
  const esc = (s) =>
    String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const cleanName = (s) => (s || '').replace(/[^\p{L}\p{N}_ .'-]/gu, '').trim().slice(0, 40);
  const timeNow = () => new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: true });
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  };

  // Small, safe markdown renderer (input is escaped first; only https links are allowed).
  function inline(s) {
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    return s;
  }
  function md(src) {
    const lines = esc(src).split(/\r?\n/);
    const out = [];
    let list = null;
    let para = [];   
    const flushPara = () => {
      if (para.length) out.push('<p>' + inline(para.join('<br>')) + '</p>');
      para = [];
    };
    const flushList = () => {
      if (list) out.push(`<${list.t}>` + list.items.map((i) => '<li>' + inline(i) + '</li>').join('') + `</${list.t}>`);
      list = null;
    };
    for (const raw of lines) {
      const line = raw.trimEnd();
      let m;
      if (!line.trim()) { flushPara(); flushList(); continue; }
      if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) {
        flushPara();
        if (!list || list.t !== 'ul') { flushList(); list = { t: 'ul', items: [] }; }
        list.items.push(m[1]); continue;
      }
      if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
        flushPara();
        if (!list || list.t !== 'ol') { flushList(); list = { t: 'ol', items: [] }; }
        list.items.push(m[1]); continue;
      }
      if ((m = line.match(/^#{1,6}\s+(.*)$/))) { flushPara(); flushList(); out.push('<p><strong>' + inline(m[1]) + '</strong></p>'); continue; }
      flushList();
      para.push(line);
    }
    flushPara(); flushList();
    return out.join('');
  }

  // --------------------------------------------------------------------- init
  async function init() {
    let css, cfg;
    try {
      [css, cfg] = await Promise.all([
        fetch(BASE + '/widget.css', { cache: 'no-store' }).then((r) => r.text()),
        fetch(BASE + '/api/config', { cache: 'no-store' }).then((r) => r.json()),
      ]);
    } catch (e) {
      console.error('STA widget: could not load', e);
      return;
    }

    if (!document.getElementById('sta-widget-font')) {
      const link = el('link');
      link.id = 'sta-widget-font';
      link.rel = 'stylesheet';
      link.href = 'https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap';
      document.head.appendChild(link);
    }

    const host = el('div');
    host.id = 'sta-widget-host';
    host.style.cssText = 'position:fixed;right:0;bottom:0;z-index:2147483000;';
    document.body.appendChild(host);
    const shadow = host.attachShadow({ mode: 'open' });
    shadow.innerHTML = `
      <style></style>
      <div class="root">
        <section class="panel" role="dialog" aria-label="Chat assistant">
          <header class="head">
            <div class="avatar big"></div>
            <div class="who"><div class="title"></div><div class="sub"><i></i><span></span></div></div>
            <div class="actions" hidden>
              <button class="icon share" type="button" title="Copy link" aria-label="Copy link">${ICONS.share}</button>
              <button class="icon restart" type="button" title="Restart chat" aria-label="Restart chat">${ICONS.restart}</button>
            </div>
            <button class="icon close" type="button" aria-label="Close chat">${ICONS.close}</button>
          </header>
          <div class="welcome">
            <div class="card"><h3></h3><p>I can help you with your queries regarding</p><div class="grid"></div></div>
            <div class="label">Your name</div>
            <input class="field" type="text" maxlength="40" placeholder="Enter your name to start" autocomplete="given-name" />
            <div class="spacer"></div>
            <button class="start" type="button" disabled>Start chat</button>
          </div>
          <div class="chat" hidden>
            <div class="messages" aria-live="polite"></div>
            <div class="composer">
              <div class="inputbox">
                <input type="text" maxlength="500" placeholder="Type your query..." aria-label="Type your query" />
                <button class="send" type="button" aria-label="Send" disabled>${ICONS.send}</button>
                <span class="online" title="Online"></span>
              </div>
            </div>
            <div class="foot"></div>
          </div>
          <div class="toast" role="status"></div>
        </section>
        <button class="launcher" type="button" aria-label="Open chat">
          <span class="ico-chat">${ICONS.chat}</span><span class="ico-close">${ICONS.close}</span>
        </button>
      </div>`;
    shadow.querySelector('style').textContent = css;
    const $ = (s) => shadow.querySelector(s);
    const botName = cfg.botName || 'Assistant';
    const initial = botName.charAt(0).toUpperCase();
    const topicChips = cfg.topics.map((t) => ({ label: t.label, kind: 'topic', id: t.id }));
    const topicById = Object.fromEntries(cfg.topics.map((t) => [t.id, t]));

    // static texts
    $('.avatar.big').textContent = initial;
    $('.title').textContent = botName;
    $('.sub span').textContent = cfg.tagline;
    $('.card h3').textContent = `Hi there! I'm ${botName} \u{1F44B}`;
    cfg.welcomeList.forEach((t) => $('.grid').append(el('span', '', esc(t))));

    const panel = $('.panel'), launcher = $('.launcher');
    const welcome = $('.welcome'), chat = $('.chat'), messages = $('.messages');
    const nameInput = $('.field'), startBtn = $('.start');
    const askInput = $('.inputbox input'), sendBtn = $('.send');
    const actions = $('.actions'), toast = $('.toast');

    let name = '', history = [], busy = false, seenEl = null, seenTime = '', currentTopic = '';
    let lastUserEl = null, manualScroll = false;
    let folio = '';  // text, optional, not validated
    let otpSession = null;  // backend-issued verification session id (memory only); revoked on any change/reset
    const revokeOtp = () => {
      if (!otpSession) return;
      const session_id = otpSession; otpSession = null;
      fetch(BASE + '/api/folio/otp/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
        body: JSON.stringify({ session_id }) }).catch(() => {});
    };
    let company = null;  // selected {id, company_name, ISIN}; session state, optional, not sent to the chat API
    let companiesCache = null;

    // ---------------------------------------------------------------- open/close
    function setOpen(open) {
      panel.classList.toggle('open', open);
      launcher.classList.toggle('is-open', open);
      launcher.setAttribute('aria-label', open ? 'Close chat' : 'Open chat');
      if (open) setTimeout(() => (name ? askInput : nameInput).focus(), 200);
    }
    launcher.addEventListener('click', () => setOpen(!panel.classList.contains('open')));
    $('.icon.close').addEventListener('click', () => setOpen(false));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setOpen(false); });

    // ------------------------------------------------------------------ welcome
    nameInput.addEventListener('input', () => { startBtn.disabled = !cleanName(nameInput.value); });
    nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') startChat(); });
    startBtn.addEventListener('click', startChat);

    function startChat() {
      const n = cleanName(nameInput.value);
      if (!n) return;
      name = n;
      welcome.hidden = true;
      chat.hidden = false;
      actions.hidden = false;
      resetConversation();
      askInput.focus();
    }

    // -------------------------------------------------------------------- chat UI
    const scrollDown = () => requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
    // A wheel, touch or scrollbar drag by the reader means they are reading earlier messages:
    // later replies must not pull the view away until they send the next question.
    const markManual = () => { manualScroll = true; };
    messages.addEventListener('wheel', markManual, { passive: true });
    messages.addEventListener('touchmove', markManual, { passive: true });
    messages.addEventListener('pointerdown', (e) => {
      if (e.offsetX >= messages.clientWidth) markManual();  // pressed on the scrollbar
    });


    // Put the latest question at the top of the conversation with its answer directly below.
    // If both fit, the browser clamps to the bottom so both stay visible; long answers scroll.
    function showLatestExchange() {
      requestAnimationFrame(() => {
        if (manualScroll) return;
        if (!lastUserEl) { messages.scrollTop = 0; return; }
        const delta = lastUserEl.getBoundingClientRect().top - messages.getBoundingClientRect().top;
        messages.scrollTop += delta - 8;
      });
    }

    function settleUser() {
      if (seenEl) { seenEl.textContent = 'seen · ' + seenTime; seenEl = null; }
    }
    function addUser(text) {
      manualScroll = false;
      lastUserEl = el('div', 'ubub', esc(text));
      messages.append(lastUserEl);
      seenTime = timeNow();
      seenEl = el('div', 'ts right', 'sent · ' + seenTime);
      messages.append(seenEl);
      scrollDown();
    }


    function addBot(text, opts = {}) {
      settleUser();
      const wrap = el('div', 'bwrap');
      const av = el('div', 'avatar sm'); av.textContent = initial;
      const bubble = el('div', 'bubble', md(text));
      // Sources: only usable links, no blank bullets, no duplicate entries; no heading when empty.
      const seenSrc = new Set();
      const items = (opts.sources || [])
        .filter((s) => s && s.url && s.title && String(s.title).trim() &&
          (/^https:\/\//.test(s.url) || /^\/source-docs\//.test(s.url)))
        .filter((s) => { const k = s.url.toLowerCase(); if (seenSrc.has(k)) return false; seenSrc.add(k); return true; })
        .map((s) => `<li><a href="${esc(/^\//.test(s.url) ? BASE + s.url : s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a></li>`)
        .join('');
      if (items) {
        const d = el('details', 'srcs');
        d.innerHTML = `<summary>Sources</summary><ul>${items}</ul>`;
        bubble.append(d);
      }
      wrap.append(av, bubble);
      messages.append(wrap, el('div', 'ts', timeNow()));
      if (opts.chips) messages.append(chipsEl(opts.chips));
      showLatestExchange();
    }


    function addTyping() {
      const wrap = el('div', 'bwrap');
      const av = el('div', 'avatar sm'); av.textContent = initial;
      wrap.append(av, el('div', 'bubble', '<span class="dots"><span></span><span></span><span></span></span>'));
      messages.append(wrap);
      if (!manualScroll) scrollDown();
      return wrap;
    }


    function chipsEl(list) {
      const box = el('div', 'chips');
      list.forEach((c) => {
        const b = el('button', 'chip');
        b.type = 'button';
        b.textContent = c.label;
        b.addEventListener('click', () => { if (!busy) onChip(c); });
        box.append(b);
      });
      return box;
    }

    // ----------------------------------------------------------------- behaviour
    // Company list is fetched once per widget load (a failed load is retried on demand), then filtered locally.
    async function loadCompanies() {
      if (companiesCache) return companiesCache;
      const res = await fetch(BASE + '/api/companies', { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      companiesCache = Array.isArray(data.companies) ? data.companies : [];
      return companiesCache;
    }

    // Searchable combobox: only returned records can be selected; editing the text clears a stale selection.
    let comboSeq = 0;
    function companyPicker() {
      const uid = 'sta-co-' + (++comboSeq);
      const box = el('div', 'cobox');
      box.innerHTML = `<div class="label">Company name</div>
        <div class="combo">
          <input class="field co-input" type="text" role="combobox" aria-expanded="false" aria-autocomplete="list"
            aria-controls="${uid}" aria-label="Company name" autocomplete="off" spellcheck="false" placeholder="Loading companies..." disabled />
          <ul class="co-list" id="${uid}" role="listbox" hidden></ul>
          <div class="co-status" role="status" aria-live="polite"></div>
        </div>`;
      const input = box.querySelector('.co-input'), list = box.querySelector('.co-list'), status = box.querySelector('.co-status');
      let all = [], shown = [], active = -1;
      const display = (c) => `${c.ISIN} - ${c.company_name}`;

      // Folio number: plain text (kept as a string so leading zeros survive); shown only after a valid selection.
      const folioWrap = el('div', 'folio', `<div class="label">Folio number</div>
        <input class="field folio-input" type="text" maxlength="32" autocomplete="off" spellcheck="false" aria-label="Folio number" />
        <button type="button" class="folio-check" hidden>Check folio</button>
        <div class="folio-msg" role="status" aria-live="polite"></div>
        <div class="folio-pan" hidden>
          <div class="label">PAN</div>
          <input class="field pan-input" type="text" maxlength="12" autocomplete="off" autocapitalize="characters" spellcheck="false" aria-label="PAN" />
          <button type="button" class="folio-check pan-verify">Verify PAN</button>
        </div>
        <div class="folio-otp" hidden>
          <div class="otp-test" hidden>the code was sent to the configured mail address.</div>
          <div class="otp-to" role="status"></div>
          <div class="label">Email OTP</div>
          <input class="field otp-input" type="text" inputmode="numeric" maxlength="6" autocomplete="one-time-code" spellcheck="false" aria-label="Email OTP" />
          <div class="otp-timer" role="timer" aria-live="off"></div>
          <button type="button" class="folio-check otp-verify">Verify OTP</button>
          <button type="button" class="folio-check otp-regen" hidden>Regenerate OTP</button>
        </div>
        <div class="folio-result" hidden></div>`);
      folioWrap.hidden = true;
      const folioInput = folioWrap.querySelector('.folio-input');  
      box.append(folioWrap);
      const checkBtn = folioWrap.querySelector('.folio-check'), folioMsg = folioWrap.querySelector('.folio-msg');
      const resultBox = folioWrap.querySelector('.folio-result');

      checkBtn.hidden = !cfg.folioLookup;  // lookup exists only in the explicit local test mode
      let lookupSeq = 0;
      const panBox = folioWrap.querySelector('.folio-pan'), panInput = folioWrap.querySelector('.pan-input'), panBtn = folioWrap.querySelector('.pan-verify');
      const otpBox = folioWrap.querySelector('.folio-otp'), otpInput = folioWrap.querySelector('.otp-input');
      const otpVerifyBtn = folioWrap.querySelector('.otp-verify'), otpRegenBtn = folioWrap.querySelector('.otp-regen');
      const otpTimer = folioWrap.querySelector('.otp-timer'), otpTo = folioWrap.querySelector('.otp-to'), otpTest = folioWrap.querySelector('.otp-test');
      let otpTick = null, otpExpiryMs = 0, otpOffset = 0, otpDead = false;
      function clearOtp() {
        clearInterval(otpTick); otpTick = null; revokeOtp();
        otpBox.hidden = true; otpInput.value = ''; otpTimer.textContent = ''; otpDead = false;
      }
      function otpExpired() {  // attempts used up or time out: only Regenerate remains
        otpDead = true; otpVerifyBtn.disabled = true; otpInput.disabled = true; otpRegenBtn.hidden = false;
      }
      function startOtp(d) {
        clearInterval(otpTick);
        otpSession = d.session_id; otpDead = false;
        otpOffset = Date.parse(d.server_time) - Date.now();  // same expires_at as the backend, corrected for clock skew
        otpExpiryMs = Date.parse(d.expires_at);
        otpTo.textContent = 'Code sent to ' + d.masked_email;
        otpTest.hidden = d.test_mode !== true;
        otpInput.value = ''; otpInput.disabled = false; otpVerifyBtn.disabled = false; otpRegenBtn.hidden = true;
        otpBox.hidden = false; panBtn.disabled = true; panInput.readOnly = true; otpInput.focus();
        const tick = () => {
          if (!otpBox.isConnected) { clearInterval(otpTick); return; }
          const left = Math.ceil((otpExpiryMs - (Date.now() + otpOffset)) / 1000);
          if (left <= 0) {
            clearInterval(otpTick); otpTick = null; otpExpired();
            otpTimer.textContent = otpInput.value.trim()
              ? 'OTP expired. Please request a new OTP.'
              : 'OTP expired. Didn’t receive it? Request a new OTP, or contact GNSA if your registered email needs updating.';
            return;
          }
          if (!otpDead) otpTimer.textContent = 'Code expires in ' + Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0');
        };
        tick(); otpTick = setInterval(tick, 250);
      }
      panInput.addEventListener('input', () => { if (otpSession) clearOtp(); });
      otpVerifyBtn.addEventListener('click', async () => {
        const otp = otpInput.value.trim();
        if (!otpSession) return;
        if (!/^[0-9]{6}$/.test(otp)) return say('Enter the 6-digit code.', true);
        const seq = lookupSeq; otpVerifyBtn.disabled = true; say('Verifying...');
        try {
          const res = await fetch(BASE + '/api/folio/otp/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store',
            body: JSON.stringify({ session_id: otpSession, otp }) });
          const data = await res.json().catch(() => ({}));
          if (seq !== lookupSeq) return;
          if (res.status === 403 && data.verified === false) {
            if (data.status === 'incorrect') { say('Incorrect code. Attempts left: ' + data.attempts_left + '.', true); otpInput.value = ''; }
            else if (data.status === 'exhausted') { say('Too many incorrect attempts. Please regenerate the code.', true); otpExpired(); }
            else if (data.status === 'expired') { say('OTP expired. Please request a new OTP.', true); clearInterval(otpTick); otpTick = null; otpTimer.textContent = 'OTP expired. Please request a new OTP.'; otpExpired(); }
            else { say('This code can no longer be used. Please regenerate it.', true); otpExpired(); }
            return;
          }
          if (!res.ok) { say(typeof data.detail === 'string' ? data.detail : 'Verification failed. Please try again.', true); return; }
          clearInterval(otpTick); otpTick = null; otpTimer.textContent = '';  // verified: stop the timer, no expiry reminder
          showDetails(data);
        } catch (e) {
          if (seq === lookupSeq) say('Could not reach the server. Please try again.', true);
        } finally {
          if (seq === lookupSeq && !otpDead && !resultBox.children.length) otpVerifyBtn.disabled = false;
        }
      });
      otpRegenBtn.addEventListener('click', async () => {
        if (!otpSession) return;
        const seq = lookupSeq; otpRegenBtn.disabled = true; say('Sending a new code...');
        try {
          const res = await fetch(BASE + '/api/folio/otp/regenerate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store',
            body: JSON.stringify({ session_id: otpSession }) });
          const data = await res.json().catch(() => ({}));
          if (seq !== lookupSeq) return;
          if (!res.ok || !data.session_id) { say(typeof data.detail === 'string' ? data.detail : 'Could not send a new code. Please try again.', true); return; }
          say('A new code has been sent.'); startOtp(data);
        } catch (e) {
          if (seq === lookupSeq) say('Could not reach the server. Please try again.', true);
        } finally { otpRegenBtn.disabled = false; }
      });
      // Details exist in the DOM only after a verified response with usable fields.
      function showDetails(data) {
        const nm = typeof data.shareholder_name === 'string' ? data.shareholder_name.trim() : '';
        const joint = Array.isArray(data.joint_holder_names) ? data.joint_holder_names.filter((n) => typeof n === 'string' && n.trim()) : [];
        if (data.verified !== true || !nm || !Number.isInteger(data.shares) || data.shares < 0) { say('Verification failed. Please try again.', true); return; }
        const row = (label, value) => { const l = el('div', 'label', label); const i = el('input', 'field'); i.type = 'text'; i.readOnly = true; i.value = value; i.setAttribute('aria-label', label); resultBox.append(l, i); };
        resultBox.replaceChildren();
        row('Shareholder name', nm);
        joint.forEach((n, k) => row(joint.length > 1 ? 'Joint holder ' + (k + 1) + ' name' : 'Joint holder name', n.trim()));
        row('Number of shares', String(data.shares));
        clearInterval(otpTick); otpTick = null; otpBox.hidden = true; panBox.hidden = true; panInput.value = '';
        say('Email verified.'); resultBox.hidden = false;
      }
      // Clears PAN, verification state and holder details (in the DOM only: nothing is stored in the browser).
      const clearResult = () => { lookupSeq++; clearOtp(); resultBox.hidden = true; resultBox.replaceChildren(); panBox.hidden = true; panInput.value = ''; panInput.readOnly = false; panBtn.disabled = false; folioMsg.textContent = ''; folioMsg.className = 'folio-msg'; checkBtn.disabled = false; };
      const clearCompany = () => {
        company = null;
        folio = '';
        folioInput.value = '';
        folioWrap.hidden = true;
        clearResult();
      };

      
      folioInput.addEventListener('input', () => { folio = folioInput.value; clearResult(); });
      const say = (t, err) => { folioMsg.textContent = t; folioMsg.className = 'folio-msg' + (err ? ' err' : ''); };
      checkBtn.addEventListener('click', async () => {
        const no = folioInput.value.trim();  // sent as text: leading zeros are kept
        if (!company) return say('Select a company first.', true);
        if (!no) return say('Enter a folio number.', true);
        const seq = ++lookupSeq;
        resultBox.hidden = true; resultBox.replaceChildren(); panBox.hidden = true; panInput.value = ''; checkBtn.disabled = true; say('Checking...');
        try {
          const res = await fetch(BASE + '/api/folio/lookup', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ comp_gid: company.id, folio_no: no }),
          });
          const data = await res.json().catch(() => ({}));
          if (seq !== lookupSeq) return;  // company or folio changed meanwhile
          if (res.status === 404) { say('Folio does not exist for the selected company.', true); return; }
          if (!res.ok) { say(typeof data.detail === 'string' ? data.detail : 'Folio lookup failed. Please try again.', true); return; }
          if (data.kyc_status === 'NOT_A_SHAREHOLDER') { say('You are not a shareholder of this company.', true); return; }
          if (data.kyc_status === 'KYC_PENDING') { say('Your KYC is pending. Please complete your KYC.', true); return; }
          if (data.kyc_status !== 'KYC_COMPLIANT') { say('Folio lookup failed. Please try again.', true); return; }
          say('You are KYC compliant. Please enter your PAN.');  // no holder details exist in this response
          panBox.hidden = false; panInput.focus();
        } catch (e) {
          if (seq === lookupSeq) say('Could not reach the server. Please try again.', true);
        } finally {
          if (seq === lookupSeq) checkBtn.disabled = false;
        }
      });


      panBtn.addEventListener('click', async () => {
        const pan = panInput.value.trim().toUpperCase();
        const no = folioInput.value.trim();
        if (!company || !no) return;
        if (!pan) return say('Enter your PAN.', true);
        if (!/^[A-Z]{5}[0-9]{4}[A-Z]$/.test(pan)) return say('Enter a valid 10-character PAN (for example ABCDE1234F).', true);
        const seq = ++lookupSeq;
        resultBox.hidden = true; resultBox.replaceChildren(); panBtn.disabled = true; say('Verifying...');
        try {
          const res = await fetch(BASE + '/api/folio/verify-pan', {  // PAN travels in the POST body only
            method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store',
            body: JSON.stringify({ comp_gid: company.id, folio_no: no, pan }),
          });
          const data = await res.json().catch(() => ({}));
          if (seq !== lookupSeq) return;  // company, folio or PAN state changed meanwhile
          if (res.status === 403 && data.verified === false) { say('PAN does not match this folio. Please check and try again.', true); return; }
          if (!res.ok) { say(typeof data.detail === 'string' ? data.detail : 'Verification failed. Please try again.', true); return; }
          if (data.otp_sent === true && data.session_id && data.expires_at && data.server_time) { say('PAN verified. Enter the code sent to your email.'); startOtp(data); return; }
          if (data.otp_sent === false && typeof data.detail === 'string') { say(data.detail, true); return; }
          panBox.hidden = true; panInput.value = '';
          say(data.kyc_status === 'KYC_PENDING' ? 'Your KYC is pending. Please complete your KYC.'
            : data.kyc_status === 'NOT_A_SHAREHOLDER' ? 'You are not a shareholder of this company.'
            : 'Verification failed. Please try again.', true);
        } catch (e) {
          if (seq === lookupSeq) say('Could not reach the server. Please try again.', true);
        } finally {
          if (seq === lookupSeq && !otpSession) panBtn.disabled = false;
        }
      });


      const setStatus = (html) => { status.innerHTML = html || ''; };
      const close = () => { list.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); active = -1; };
      function setActive(i) {
        active = i;
        list.querySelectorAll('li').forEach((li, k) => li.classList.toggle('on', k === i));
        const li = list.children[i];
        if (li) { input.setAttribute('aria-activedescendant', li.id); li.scrollIntoView({ block: 'nearest' }); }
        else input.removeAttribute('aria-activedescendant');
      }


      function render() {
        const q = input.value.trim().toLowerCase();
        shown = q ? all.filter((c) => (String(c.company_name) + ' ' + String(c.ISIN) + ' ' + display(c)).toLowerCase().includes(q)) : all;
        list.innerHTML = '';
        shown.forEach((c, i) => {
          const li = el('li', '', esc(display(c)));
          li.id = `${uid}-${i}`; li.setAttribute('role', 'option');
          li.addEventListener('mousedown', (e) => { e.preventDefault(); choose(c); });
          list.append(li);
        });
        setStatus(shown.length ? '' : '<span class="co-note">No matching company</span>');
        list.hidden = !shown.length;
        input.setAttribute('aria-expanded', String(!list.hidden));
        active = -1; input.removeAttribute('aria-activedescendant');
      }


      function choose(c) {
        if (!company || company.id !== c.id) { folio = ''; folioInput.value = ''; clearResult(); }  // new company: clear folio and result
        company = { id: c.id, company_name: c.company_name, ISIN: c.ISIN };
        input.value = display(c);
        folioWrap.hidden = false;
        close(); setStatus('');
      }


      async function load() {
        input.disabled = true; input.placeholder = 'Loading companies...';
        setStatus('<span class="co-note">Loading companies...</span>');
        try {
          all = await loadCompanies();
          input.placeholder = all.length ? 'Search company name (optional)' : 'No companies available';
          input.disabled = !all.length;
          setStatus(all.length ? '' : '<span class="co-note">No companies available</span>');
        } catch (e) {
          input.placeholder = 'Company list unavailable';
          setStatus('<span class="co-err">Could not load companies.</span> <button type="button" class="co-retry">Retry</button>');
          status.querySelector('.co-retry').addEventListener('click', load);
        }
      }


      input.addEventListener('focus', () => { if (all.length) render(); });
      input.addEventListener('click', () => { if (all.length && list.hidden) render(); });
      input.addEventListener('blur', close);
      input.addEventListener('input', () => {
        if (company && input.value !== display(company)) clearCompany();  // no stale selection
        render();
      });


      input.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          if (list.hidden) render();
          if (!shown.length) return;
          const d = e.key === 'ArrowDown' ? 1 : -1;
          setActive(active < 0 ? (d > 0 ? 0 : shown.length - 1) : (active + d + shown.length) % shown.length);
        } else if (e.key === 'Enter') {
          if (active >= 0 && !list.hidden) { e.preventDefault(); choose(shown[active]); }
        } else if (e.key === 'Escape' && !list.hidden) {
          e.stopPropagation(); close();
        }
      });
      load();
      return box;
    }

    function showWelcomeMessage() {
      // Order: welcome message, Company name picker, then the existing menu items.
      addBot(`Welcome **${name}** to **${botName}**, your e-assistant. Get started by selecting a query below or typing your own.`);
      messages.append(companyPicker(), chipsEl(topicChips));
      showLatestExchange();
    }
    function resetConversation() {
      messages.innerHTML = '';
      revokeOtp();
      company = null; folio = '';
      history = []; seenEl = null; currentTopic = ''; lastUserEl = null; manualScroll = false;
      showWelcomeMessage();
    }
    function onChip(c) {
      if (c.kind === 'question') return ask(c.label, true);
      addUser(c.label);
      if (c.kind === 'menu') return addBot('What else can I help you with?', { chips: topicChips });
      const topic = topicById[c.id];
      currentTopic = c.id;  // sent with questions so the server can route by category
      const qs = topic.questions.map((q) => ({ label: q, kind: 'question' }));
      addBot(`Here are some common questions about **${topic.label}**. Pick one or type your own.`,
        { chips: [...qs, { label: 'Main menu', kind: 'menu' }] });
    }
    async function ask(text, fresh = false) {
      text = text.trim();
      if (!text || busy) return;
      addUser(text);
      history.push({ role: 'user', content: text });
      busy = true; syncSend();
      const typing = addTyping();
      // Menu questions are self-contained, so they are sent with no earlier context.
      // Typed follow-ups get the last two turns, trimmed, so long answers never block a request.
      const context = fresh
        ? []
        : history.slice(0, -1).slice(-4).map((m) => ({ role: m.role, content: m.content.slice(0, 800) }));
      let data;
      try {
        const res = await fetch(BASE + '/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question: text, history: context, name, topic: currentTopic || null }),
        });
        if (res.status === 429) data = { text: 'You are sending messages too quickly. Please wait a moment and try again.', kind: 'error' };
        else if (!res.ok) throw new Error('HTTP ' + res.status);
        else data = await res.json();
      } catch (e) {
        data = { text: 'Sorry, I could not reach the assistant. Please try again.', kind: 'error' };
      }
      typing.remove();
      busy = false; syncSend();
      addBot(data.text, { sources: data.sources, chips: [{ label: 'Main menu', kind: 'menu' }] });
      history.push({ role: 'assistant', content: data.text });
    }

    // ------------------------------------------------------------------ composer
    function syncSend() { sendBtn.disabled = busy || !askInput.value.trim(); }
    function submit() {
      const t = askInput.value;
      if (!t.trim() || busy) return;
      askInput.value = ''; syncSend();
      ask(t);
    }
    askInput.addEventListener('input', syncSend);
    askInput.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); submit(); } });
    sendBtn.addEventListener('click', submit);

    // ---------------------------------------------------------- header actions
    function showToast(msg) {
      toast.textContent = msg; toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 1800);
    }
    $('.restart').addEventListener('click', () => { if (!busy) resetConversation(); });
    $('.share').addEventListener('click', async () => {
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(location.href);
        } else {
          const ta = document.createElement('textarea');
          ta.value = location.href; ta.style.cssText = 'position:fixed;opacity:0';
          document.body.appendChild(ta); ta.select();
          const ok = document.execCommand('copy');
          ta.remove();
          if (!ok) throw new Error('copy failed');
        }
        showToast('Link copied');
      } catch (e) { showToast('Could not copy the link'); }
    });

    // Expose a tiny API for the host page / tests.
    window.staWidget = { open: () => setOpen(true), close: () => setOpen(false), getCompany: () => company, getFolio: () => folio };
  }

  if (document.body) init();
  else document.addEventListener('DOMContentLoaded', init);
})();