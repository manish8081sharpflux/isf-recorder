/*
 * Jomi web app (21 Oct 2026, phase 1) — reading in the browser. Same API and rules as the phone app.
 * Login (phone + WhatsApp OTP) · Previous recordings with filters · report (read / edit / finalize / copy /
 * share link / Hindi) · audio · transcript · Director's POV (admins). Recording stays in the phone app.
 * Plain JS, no build step. Every server text is escaped before it is shown.
 */
(function () {
  "use strict";

  // The page lives at <base>/web/ (e.g. /recorder/web/): the API is <base>/...
  const BASE = location.pathname.replace(/\/web(\/.*)?$/, "");
  const $app = document.getElementById("app");
  const store = {
    get token() { try { return localStorage.getItem("jomi_web_token"); } catch (e) { return null; } },
    set token(v) { try { v ? localStorage.setItem("jomi_web_token", v) : localStorage.removeItem("jomi_web_token"); } catch (e) {} },
    get user() { try { return JSON.parse(localStorage.getItem("jomi_web_user") || "null"); } catch (e) { return null; } },
    set user(v) { try { v ? localStorage.setItem("jomi_web_user", JSON.stringify(v)) : localStorage.removeItem("jomi_web_user"); } catch (e) {} },
  };
  const state = { config: null, list: { items: [], total: 0, page: 1, loading: false, key: null, scroll: 0 }, filters: loadFilters() };

  // ── helpers ────────────────────────────────────────────────────────────────
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const isAdmin = () => (store.user && store.user.role) === "admin";

  function toast(msg) {
    const t = document.getElementById("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, 2600);
  }

  async function api(method, path, body) {
    const res = await fetch(BASE + path, {
      method,
      headers: Object.assign({ "Content-Type": "application/json" }, store.token ? { Authorization: "Bearer " + store.token } : {}),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 401) { logout(); throw new Error("Please log in again"); }
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty */ }
    if (!res.ok) {
      const d = data && data.detail;
      const err = new Error(typeof d === "string" ? d : "Something went wrong (" + res.status + ")");
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function logout() {
    store.token = null; store.user = null; state.config = null;
    state.list = { items: [], total: 0, page: 1, loading: false, key: null, scroll: 0 };
    location.hash = "#/login"; render();
  }

  function loadFilters() {
    try { return Object.assign({ mode: "", balgruh: "", coach: "", date: "all" }, JSON.parse(sessionStorage.getItem("jomi_web_filters") || "{}")); }
    catch (e) { return { mode: "", balgruh: "", coach: "", date: "all" }; }
  }
  function saveFilters() { try { sessionStorage.setItem("jomi_web_filters", JSON.stringify(state.filters)); } catch (e) {} }

  const IST = "Asia/Kolkata";
  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString("en-IN", { timeZone: IST, day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
  }
  function istDay(offsetDays) {
    const d = new Date(Date.now() + offsetDays * 86400000);
    return d.toLocaleDateString("en-CA", { timeZone: IST });          // YYYY-MM-DD
  }
  function dateRange(key) {
    if (key === "today") return [istDay(0), istDay(0)];
    if (key === "7") return [istDay(-6), istDay(0)];
    if (key === "30") return [istDay(-29), istDay(0)];
    return [null, null];
  }
  function duration(rec) {
    const s = Math.round(Number(rec.voice_seconds || rec.duration_seconds || 0));
    if (!s) return "";
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? h + " h " + String(m).padStart(2, "0") + " min" : (m ? m + " min" : s + " sec");
  }
  function clock(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : String(m).padStart(2, "0")) + ":" + String(s).padStart(2, "0");
  }
  const names = (v) => Array.isArray(v) ? v.map((p) => (p && p.name) || p).filter(Boolean) : [];
  const modeLabel = (m) => ({ schedule: "Schedule", meeting: "Meeting" })[m] || "";
  function speakerName(s) {
    const m = /SPEAKER_?(\d+)/i.exec(s || "");
    return m ? "Speaker " + (Number(m[1]) + 1) : (s || "");
  }
  function statusPill(rec) {
    const st = rec.status, rs = rec.report_status;
    if (st === "error") return '<span class="status red">Failed</span>';
    if (st === "recording" || st === "uploading") return '<span class="status orange">Recording</span>';
    if (st === "processing" || rs === "generating") return '<span class="status orange">Processing</span>';
    if (rs === "final") return '<span class="status">Report final</span>';
    if (rs === "draft") return '<span class="status">Report ready</span>';
    if (rs === "error") return '<span class="status red">Report failed</span>';
    return '<span class="status">' + esc(st || "") + "</span>";
  }
  function peopleLine(rec) {
    if (rec.mode === "meeting") return names(rec.participants).join(", ");
    const coaches = names(rec.assistant_coaches);
    return [rec.balgruh_name, (coaches.length ? coaches : [rec.assistant_coach].filter(Boolean)).join(", ")].filter(Boolean).join(" · ");
  }

  // Markdown the report AI writes → HTML (escaped). Same rules as the shared-report page.
  function inline(t) {
    let x = esc(t);
    x = x.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/__(.+?)__/g, "<strong>$1</strong>");
    x = x.replace(/(^|[^*\w])\*(?![*\s])(.+?)(?<![*\s])\*(?![*\w])/g, "$1<em>$2</em>");
    return x;
  }
  function markdown(md) {
    const out = []; let para = []; let table = [];
    const flush = () => {
      if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; }
      if (table.length) {
        const rows = table.filter((r) => !/^\|?\s*:?-{2,}/.test(r.trim())).map((r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
        if (rows.length) {
          out.push('<div class="tbl"><table><thead><tr>' + rows[0].map((c) => "<th>" + inline(c) + "</th>").join("") +
            "</tr></thead><tbody>" + rows.slice(1).map((r) => "<tr>" + r.map((c) => "<td>" + inline(c) + "</td>").join("") + "</tr>").join("") + "</tbody></table></div>");
        }
        table = [];
      }
    };
    String(md || "").replace(/\r\n/g, "\n").split("\n").forEach((raw) => {
      const line = raw.replace(/\s+$/, ""), t = line.trim();
      const indent = Math.min(Math.floor((line.length - line.trimStart().length) / 2), 4);
      if (t.startsWith("|") && (t.match(/\|/g) || []).length >= 2) { if (para.length) flush(); table.push(t); return; }
      if (table.length) flush();
      let m;
      if (!t) flush();
      else if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) { flush(); out.push("<hr>"); }
      else if (t.startsWith("#")) { flush(); const lvl = Math.min(t.length - t.replace(/^#+/, "").length + 1, 4); out.push("<h" + lvl + ">" + inline(t.replace(/^#+/, "").trim().replace(/\*\*/g, "")) + "</h" + lvl + ">"); }
      else if ((t.length > 2 && t.startsWith("*") && t.endsWith("*") && !t.startsWith("**") && !t.startsWith("* ")) ||
               (t.startsWith("**") && t.endsWith("**") && t.split("**").length === 3 && t.length > 4)) { flush(); out.push("<h4>" + inline(t.replace(/^\*+|\*+$/g, "").trim()) + "</h4>"); }
      else if ((m = /^([-*+•])\s+(.*)$/.exec(t))) { flush(); out.push('<div class="li" style="margin-left:' + indent * 18 + 'px"><span class="dot">•</span><span>' + inline(m[2]) + "</span></div>"); }
      else if ((m = /^(\d+)[.)]\s+(.*)$/.exec(t))) { flush(); out.push('<div class="li" style="margin-left:' + indent * 18 + 'px"><span class="dot">' + m[1] + ".</span><span>" + inline(m[2]) + "</span></div>"); }
      else para.push(t);
    });
    flush();
    return out.join("\n");
  }

  async function copyText(text, ok) {
    try { await navigator.clipboard.writeText(text); toast(ok || "Copied"); }
    catch (e) {
      const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); toast(ok || "Copied"); } catch (e2) { toast("Could not copy"); }
      ta.remove();
    }
  }

  // ── layout ────────────────────────────────────────────────────────────────
  function shell(inner) {
    const u = store.user || {};
    const initials = (u.name || "?").trim().split(/\s+/).slice(0, 2).map((p) => p[0]).join("").toUpperCase();
    return '<header class="top"><div class="top-in"><div class="avatar">' + esc(initials) + '</div><div class="who"><b>' + esc(u.name || "") +
      '</b><span class="pill">' + esc((u.org_name || "ISF") + " · " + (isAdmin() ? "ADMIN" : "COACH")) + "</span></div>" +
      '<button id="logout">Log out</button></div></header><main>' + inner + "</main>";
  }
  function bindShell() { const b = document.getElementById("logout"); if (b) b.onclick = logout; }

  // ── login ─────────────────────────────────────────────────────────────────
  function renderLogin() {
    let phone = "";
    $app.innerHTML = '<div class="login card"><h1>Jomi</h1><div class="muted">Log in with the same mobile number as the Jomi app. ' +
      'The code comes on WhatsApp.</div><label for="phone">Mobile number</label><input id="phone" type="tel" placeholder="10-digit number" autocomplete="tel">' +
      '<button class="btn primary" id="send">Send WhatsApp code</button><div id="step2" hidden><label for="code">6-digit code</label>' +
      '<input id="code" type="text" inputmode="numeric" maxlength="6" autocomplete="one-time-code"><button class="btn primary" id="verify">Log in</button></div>' +
      '<div class="err" id="err"></div></div>';
    const err = (m) => { document.getElementById("err").textContent = m || ""; };
    document.getElementById("send").onclick = async (e) => {
      phone = document.getElementById("phone").value.trim(); err("");
      e.target.disabled = true;
      try {
        await api("POST", "/auth/request-otp", { phone });
        document.getElementById("step2").hidden = false; document.getElementById("code").focus();
        toast("Code sent on WhatsApp");
      } catch (x) { err(x.message); }
      e.target.disabled = false;
    };
    document.getElementById("verify").onclick = async (e) => {
      err(""); e.target.disabled = true;
      try {
        const r = await api("POST", "/auth/verify-otp", { phone, code: document.getElementById("code").value.trim() });
        store.token = r.token; store.user = r.user;
        location.hash = "#/"; render();
      } catch (x) { err(x.message); }
      e.target.disabled = false;
    };
  }

  // ── recordings list ───────────────────────────────────────────────────────
  async function ensureConfig() {
    if (!state.config) state.config = await api("GET", "/settings/recorder-config");
    if (!state.filters.mode) state.filters.mode = state.config.default_mode || "schedule";
    return state.config;
  }

  function listQuery(page) {
    const f = state.filters, q = new URLSearchParams({ page: String(page), limit: "24", mode: f.mode });
    if (f.mode === "schedule" && f.balgruh) q.set("balgruh", f.balgruh);
    if (f.coach) {
      const [name, uid] = f.coach.split("\u0001");
      if (isAdmin()) { q.set("coach", name); if (uid) q.set("coach_user_id", uid); }
      else q.set("assistant_coach", name);
    }
    const [from, to] = dateRange(f.date);
    if (from) { q.set("date_from", from); q.set("date_to", to); }
    return "/recordings/?" + q.toString();
  }

  async function loadList(reset) {
    const L = state.list;
    if (L.loading) return;
    L.key = JSON.stringify(state.filters);
    L.loading = true;
    const page = reset ? 1 : L.page + 1;
    try {
      const r = await api("GET", listQuery(page));
      L.items = reset ? (r.items || []) : L.items.concat(r.items || []);
      L.total = r.total || L.items.length; L.page = page;
    } catch (e) { toast(e.message); }
    L.loading = false;
    drawList();
  }

  async function renderList() {
    $app.innerHTML = shell('<div class="center muted"><span class="spinner"></span>Loading…</div>'); bindShell();
    let cfg;
    try { cfg = await ensureConfig(); } catch (e) { $app.innerHTML = shell('<div class="center err">' + esc(e.message) + "</div>"); bindShell(); return; }
    const f = state.filters;
    const opts = (list, sel, all) => '<option value="">' + all + "</option>" + list.map((o) =>
      '<option value="' + esc(o.v) + '"' + (o.v === sel ? " selected" : "") + ">" + esc(o.l) + "</option>").join("");
    const coachList = (cfg.coaches || []).map((c) => ({ v: c.name + "\u0001" + (c.user_id || ""), l: c.name }));
    const html =
      '<div class="tabs">' + (cfg.modes || ["schedule", "meeting"]).map((m) =>
        '<button data-mode="' + m + '" class="' + (m === f.mode ? "on" : "") + '">' + esc(modeLabel(m)) + "</button>").join("") + "</div>" +
      '<div class="filters">' +
      (f.mode === "schedule" ? '<select id="f-balgruh" aria-label="Balgruha">' + opts((cfg.balgruhs || []).map((b) => ({ v: b.name, l: b.name })), f.balgruh, "All Balgruha") + "</select>" : "") +
      '<select id="f-coach" aria-label="Coach">' + opts(coachList, f.coach, isAdmin() ? "All coaches" : "All assistant coaches") + "</select>" +
      '<select id="f-date" aria-label="Date">' + [["all", "Any date"], ["today", "Today"], ["7", "Last 7 days"], ["30", "Last 30 days"]].map(([v, l]) =>
        '<option value="' + v + '"' + (v === f.date ? " selected" : "") + ">" + l + "</option>").join("") + "</select></div>" +
      '<div class="row small muted" id="count"></div><div id="list" class="list"></div><div id="more"></div>';
    $app.innerHTML = shell(html); bindShell();
    document.querySelectorAll("[data-mode]").forEach((b) => b.onclick = () => {
      f.mode = b.dataset.mode; f.balgruh = ""; f.coach = ""; saveFilters(); renderList();
    });
    const on = (id, key) => { const el = document.getElementById(id); if (el) el.onchange = () => { f[key] = el.value; saveFilters(); loadList(true); }; };
    on("f-balgruh", "balgruh"); on("f-coach", "coach"); on("f-date", "date");
    // Back from a report: same list, same place (no reload); a filter change starts again
    const key = JSON.stringify(f);
    if (state.list.key === key && state.list.items.length) {
      drawList();
      requestAnimationFrame(() => window.scrollTo(0, state.list.scroll || 0));
    } else {
      state.list.key = key;
      loadList(true);
    }
  }

  function drawList() {
    const L = state.list, $l = document.getElementById("list");
    if (!$l) return;
    document.getElementById("count").textContent = L.total ? L.total + " recording" + (L.total === 1 ? "" : "s") : "";
    if (!L.items.length) { $l.innerHTML = '<div class="muted">' + (L.loading ? "Loading…" : "No recordings match these filters.") + "</div>"; }
    else $l.innerHTML = L.items.map((r) =>
      '<div class="card rec" data-id="' + esc(r.id) + '" tabindex="0"><div class="row"><span class="small muted">' +
      esc([fmtDate(r.started_at || r.created_at), modeLabel(r.mode)].filter(Boolean).join(" · ")) + "</span>" + statusPill(r) +
      "</div><h3>" + esc(r.title || "Untitled recording") + "</h3>" +
      (peopleLine(r) && peopleLine(r).toLowerCase() !== String(r.title || "").toLowerCase() ? '<div class="small muted">' + esc(peopleLine(r)) + "</div>" : "") +
      '<div class="row"><span class="owner">' + (isAdmin() && r.user_name ? "🎙️ " + esc(r.user_name) : "") + '</span><span class="small muted">' + esc(duration(r)) + "</span></div></div>").join("");
    $l.querySelectorAll(".rec").forEach((c) => {
      const go = () => { state.list.scroll = window.scrollY; location.hash = "#/r/" + c.dataset.id; };
      c.onclick = go; c.onkeydown = (e) => { if (e.key === "Enter") go(); };
    });
    document.getElementById("more").innerHTML = L.items.length < L.total
      ? '<button class="btn plain more" id="load-more">' + (L.loading ? "Loading…" : "Load older (" + L.items.length + " of " + L.total + ")") + "</button>" : "";
    const m = document.getElementById("load-more"); if (m) m.onclick = () => loadList(false);
  }

  // ── recording detail ──────────────────────────────────────────────────────
  async function renderDetail(id) {
    $app.innerHTML = shell('<div class="center muted"><span class="spinner"></span>Loading…</div>'); bindShell();
    let rec;
    try { rec = await api("GET", "/recordings/" + encodeURIComponent(id)); }
    catch (e) { $app.innerHTML = shell('<button class="linkbtn back" id="back">← Previous recordings</button><div class="card err">' + esc(e.message) + "</div>"); bindShell(); document.getElementById("back").onclick = () => history.back(); return; }
    const isCall = rec.source === "call";
    const tabs = [["report", isCall ? "Summary" : "Report"], ["transcript", "Transcript"]];
    if (isAdmin() && !isCall) tabs.push(["pov", "🎬 Director's POV"]);
    const details = [
      [fmtDate(rec.started_at || rec.created_at), modeLabel(rec.mode)].filter(Boolean).join(" · "),
      rec.balgruh_name ? "🏠 Balgruha: " + rec.balgruh_name : "",
      rec.user_name ? "🎙️ Recorded by: " + rec.user_name : "",
      rec.mode === "meeting" ? (names(rec.participants).length ? "👥 Participants: " + names(rec.participants).join(", ") : "")
        : (names(rec.assistant_coaches).length ? "👥 Assistant coaches: " + names(rec.assistant_coaches).join(", ") : ""),
    ].filter(Boolean);
    $app.innerHTML = shell(
      '<button class="linkbtn back" id="back">← Previous recordings</button>' +
      '<div class="card detail-head"><h2>' + esc(rec.title) + "</h2>" + details.map((d) => '<div class="meta">' + esc(d) + "</div>").join("") +
      (rec.has_audio ? '<audio id="audio" controls preload="none"></audio>' : '<div class="small muted" style="margin-top:8px">Audio is not available.</div>') +
      "</div>" +
      '<div class="subtabs">' + tabs.map(([k, l], i) => '<button data-tab="' + k + '" class="' + (i === 0 ? "on" : "") + '">' + l + "</button>").join("") + "</div>" +
      '<div id="tab" class="card"></div>');
    bindShell();
    document.getElementById("back").onclick = () => { if (history.length > 1) history.back(); else location.hash = "#/"; };
    if (rec.has_audio) setupAudio(id);
    const show = (k) => {
      document.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === k));
      if (k === "report") showReport(id, rec, details);
      else if (k === "transcript") showTranscript(rec);
      else showPov(id, rec);
    };
    document.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => show(b.dataset.tab));
    show("report");
  }

  async function setupAudio(id) {
    const a = document.getElementById("audio");
    try { const r = await api("POST", "/web/audio-link/" + encodeURIComponent(id)); a.src = BASE + r.path; }
    catch (e) { a.replaceWith(Object.assign(document.createElement("div"), { className: "small muted", textContent: "Audio: " + e.message })); }
  }

  function seekAudio(sec) {
    const a = document.getElementById("audio");
    if (!a) return;
    a.currentTime = sec; a.play().catch(() => {});
  }

  async function showReport(id, rec, details) {
    const $t = document.getElementById("tab");
    $t.innerHTML = '<span class="spinner"></span>Loading report…';
    let rep;
    try { rep = await api("GET", "/recordings/" + id + "/report"); }
    catch (e) {
      $t.innerHTML = '<div class="muted">' + (e.status === 404 ? "There is no report yet." : esc(e.message)) + "</div>"; return;
    }
    let lang = "en", hindi = null;
    const isCall = rec.source === "call";
    const draw = () => {
      const content = lang === "hi" && hindi ? hindi : rep.content;
      const final = rep.status === "final";
      $t.innerHTML =
        '<div class="row" style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">' +
        '<span class="small muted">' + esc([rep.template_title, final ? "Final" : rep.status === "draft" ? "Draft" : rep.status].filter(Boolean).join(" · ")) + "</span>" +
        (rep.content ? '<span class="lang"><button data-lang="en" class="' + (lang === "en" ? "on" : "") + '">English</button><button data-lang="hi" class="' + (lang === "hi" ? "on" : "") + '">हिंदी</button></span>' : "") +
        "</div>" +
        (rep.status === "generating" ? '<p class="muted"><span class="spinner"></span>The report is being written…</p>' :
          rep.status === "error" ? '<p class="err">' + esc(rep.error_message || "The report could not be written.") + "</p>" : "") +
        '<div class="md" id="md">' + (content ? markdown(content) : '<p class="muted">The report is empty.</p>') + "</div>" +
        '<div class="actions">' +
        (rep.can_edit && lang === "en" ? '<button class="btn" id="edit">✏️ Edit</button>' : "") +
        (rep.status === "draft" && rep.content ? '<button class="btn" id="finalize">✅ Finalize</button>' : "") +
        (final && isAdmin() ? '<button class="btn plain" id="reopen">Reopen as draft</button>' : "") +
        (content ? '<button class="btn plain" id="copy">Copy</button>' : "") +
        (content && !isCall ? '<button class="btn plain" id="share">🔗 Share link</button>' : "") +
        "</div>";
      $t.querySelectorAll("[data-lang]").forEach((b) => b.onclick = async () => {
        if (b.dataset.lang === "hi" && !hindi) {
          b.disabled = true; b.textContent = "…";
          try { hindi = (await api("POST", "/recordings/" + id + "/report/translate", { lang: "hi" })).content; }
          catch (e) { toast(e.message); b.disabled = false; b.textContent = "हिंदी"; return; }
        }
        lang = b.dataset.lang; draw();
      });
      const on = (bid, fn) => { const el = document.getElementById(bid); if (el) el.onclick = fn; };
      on("edit", () => editReport());
      on("finalize", async () => { try { rep = await api("POST", "/recordings/" + id + "/report/finalize"); toast("Report finalized"); draw(); } catch (e) { toast(e.message); } });
      on("reopen", async () => { try { rep = await api("POST", "/recordings/" + id + "/report/reopen"); toast("Reopened as draft"); draw(); } catch (e) { toast(e.message); } });
      on("copy", () => copyText([rec.title].concat(details).join("\n") + "\n\n" + content, "Report copied"));
      on("share", async () => {
        try {
          const r = await api("POST", "/recordings/" + id + "/report/share-link", { lang: lang === "hi" ? "hi" : null });
          const msg = "*" + rec.title + "*\n" + details.join("\n") + "\n\n📄 " + (lang === "hi" ? "पूरी रिपोर्ट (खोलने के लिए टैप करें):" : "Full report (tap to open):") + "\n" + r.url;
          if (navigator.share) { try { await navigator.share({ title: rec.title, text: msg }); return; } catch (e) { /* cancelled */ } }
          copyText(msg, "Link copied — paste it in WhatsApp");
        } catch (e) { toast(e.message); }
      });
    };
    const editReport = () => {
      $t.innerHTML = '<textarea class="editor" id="ed"></textarea><div class="actions"><button class="btn primary" id="save">Save</button>' +
        '<button class="btn plain" id="cancel">Cancel</button></div>';
      const ed = document.getElementById("ed"); ed.value = rep.content || ""; ed.focus();
      document.getElementById("cancel").onclick = draw;
      document.getElementById("save").onclick = async (e) => {
        e.target.disabled = true;
        try { rep = await api("PUT", "/recordings/" + id + "/report", { content: ed.value }); hindi = null; lang = "en"; toast("Saved"); draw(); }
        catch (x) { toast(x.message); e.target.disabled = false; }
      };
    };
    draw();
    if (rep.status === "generating") setTimeout(() => { if (document.getElementById("md")) showReport(id, rec, details); }, 5000);
  }

  function showTranscript(rec) {
    const $t = document.getElementById("tab");
    const segs = rec.segments || [];
    if (!segs.length) { $t.innerHTML = '<div class="muted">' + (rec.transcript ? esc(rec.transcript) : "There is no transcript yet.") + "</div>"; return; }
    $t.innerHTML = '<input type="text" id="q" placeholder="Search the transcript…"><div style="margin-top:8px" id="segs"></div>';
    const draw = (q) => {
      const ql = (q || "").trim().toLowerCase();
      const shown = ql ? segs.filter((s) => (s.text || "").toLowerCase().includes(ql) || speakerName(s.speaker).toLowerCase().includes(ql)) : segs;
      document.getElementById("segs").innerHTML = shown.length ? shown.map((s) =>
        '<div class="seg" data-t="' + Number(s.start_time || 0) + '"><span class="t">' + clock(s.start_time) + '</span><div><div class="s">' +
        esc(speakerName(s.speaker)) + "</div>" + esc(s.text) + "</div></div>").join("") : '<div class="muted">No match.</div>';
      document.querySelectorAll(".seg").forEach((el) => el.onclick = () => seekAudio(Number(el.dataset.t)));
    };
    draw("");
    document.getElementById("q").oninput = (e) => draw(e.target.value);
  }

  async function showPov(id, rec) {
    const $t = document.getElementById("tab");
    const draw = (p) => {
      if (!document.getElementById("tab")) return;
      if (p.status === "none" || p.status === "generating") {
        $t.innerHTML = '<p class="muted"><span class="spinner"></span>Writing the Director\'s POV… this takes about a minute.</p>';
        setTimeout(async () => {
          if (!document.querySelector('[data-tab="pov"].on')) return;
          try { draw(await api("GET", "/recordings/" + id + "/director-pov")); } catch (e) { $t.innerHTML = '<p class="err">' + esc(e.message) + "</p>"; }
        }, 3000);
        return;
      }
      $t.innerHTML = '<div class="small muted">Only admins can see this.' + (p.generated_by_name ? " Made by " + esc(p.generated_by_name) + (p.generated_at ? " · " + esc(fmtDate(p.generated_at)) : "") : "") + "</div>" +
        (p.status === "error" ? '<p class="err">' + esc(p.error_message || "It could not be made.") + "</p>" : '<div class="md">' + markdown(p.content) + "</div>") +
        '<div class="actions">' + (p.status === "ready" ? '<button class="btn plain" id="pov-copy">Copy</button>' : "") +
        '<button class="btn" id="pov-regen">Regenerate</button></div>';
      const c = document.getElementById("pov-copy"); if (c) c.onclick = () => copyText("Director's POV — " + rec.title + "\n\n" + p.content, "Director's POV copied");
      document.getElementById("pov-regen").onclick = async () => {
        if (!confirm("Make a new Director's POV? The current one is replaced.")) return;
        try { draw(await api("POST", "/recordings/" + id + "/director-pov?regenerate=true")); } catch (e) { toast(e.message); }
      };
    };
    $t.innerHTML = '<span class="spinner"></span>Loading…';
    try {
      let p = await api("GET", "/recordings/" + id + "/director-pov");
      if (p.status === "none") p = await api("POST", "/recordings/" + id + "/director-pov");
      draw(p);
    } catch (e) { $t.innerHTML = '<p class="err">' + esc(e.message) + "</p>"; }
  }

  // ── router ────────────────────────────────────────────────────────────────
  function render() {
    const h = location.hash || "#/";
    if (!store.token) { if (h !== "#/login") history.replaceState(null, "", "#/login"); renderLogin(); return; }
    const m = /^#\/r\/([0-9a-fA-F-]{36})$/.exec(h);
    if (m) renderDetail(m[1]);
    else { if (h !== "#/") history.replaceState(null, "", "#/"); renderList(); return; }
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", render);
  render();
})();
