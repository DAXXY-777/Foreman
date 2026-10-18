/* Foreman live crew board. Reads the graph through /api/* and draws it. Nothing here can change data. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const INK = "#1E2A5A", SOFT = "#55608F";
  const COL = { tang: "#FF9A3D", coral: "#FF6F6F", mint: "#4FCB9E", lilac: "#A58BFF", blue: "#4AA3FF", hat: "#FFC93C" };
  const STATUS_FILL = { open: "#FFFFFF", claimed: COL.tang, submitted: COL.blue, approved: COL.mint };
  const WORKER_HATS = ["#FF9A3D", "#4FCB9E", "#4AA3FF", "#FF6F6F", "#A58BFF"];
  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const S = {
    repo: null, run: null, data: null, mem: [], sel: null, hi: null, tab: "feed",
    layers: { memory: true, code: false, decisions: false, results: false },
    seen: null, pulses: new Map(), nodes: new Map(), links: new Map(), sig: "", session: null,
    fitted: false, graph: null, agentPos: new Map(), timer: null,
  };

  /* ---------- small helpers ---------- */
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  const q = (extra = "") => `?repo=${encodeURIComponent(S.repo || "")}&run=${encodeURIComponent(S.run || "")}${extra}`;
  async function get(path) {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).detail || msg; } catch (e) { /* keep status text */ }
      throw new Error(msg);
    }
    return r.json();
  }
  const taskNum = (id) => parseInt(String(id).replace(/\D+/g, ""), 10) || 0;
  const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
  function alpha(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
  function niceName(a) {
    if (a.role === "worker") return "Worker " + (String(a.id).match(/\d+$/) || [a.id])[0];
    return a.role ? a.role[0].toUpperCase() + a.role.slice(1) : a.id;
  }

  /* ---------- the crew's faces ---------- */
  function avatarSVG(role, i = 0) {
    const hat = WORKER_HATS[i % WORKER_HATS.length];
    const face = "#FFF1D6", st = `stroke="${INK}" stroke-width="3" stroke-linejoin="round"`;
    const head = `<circle cx="32" cy="39" r="19" fill="${face}" ${st}/>`;
    const eyes = `<circle cx="25.5" cy="41" r="2.6" fill="${INK}"/><circle cx="38.5" cy="41" r="2.6" fill="${INK}"/>`;
    const smile = `<path d="M27 47.5q5 5 10 0" stroke="${INK}" stroke-width="3" fill="none" stroke-linecap="round"/>`;
    const blush = `<circle cx="20.5" cy="47" r="3" fill="#FF9DA8" opacity=".75"/><circle cx="43.5" cy="47" r="3" fill="#FF9DA8" opacity=".75"/>`;
    let top = "", mid = "";
    if (role === "foreman") {
      top = `<path d="M12 32a20 19 0 0 1 40 0z" fill="${COL.hat}" ${st}/><rect x="8" y="31" width="48" height="7" rx="3.5" fill="${COL.hat}" ${st}/>
             <path d="M32 18l2.3 4.7 5.1.7-3.7 3.6.9 5.1-4.6-2.4-4.6 2.4.9-5.1-3.7-3.6 5.1-.7z" fill="#fff" stroke="${INK}" stroke-width="1.6" stroke-linejoin="round"/>`;
    } else if (role === "worker") {
      top = `<path d="M13 32a19 17 0 0 1 38 0z" fill="${hat}" ${st}/><rect x="9" y="31" width="46" height="7" rx="3.5" fill="${hat}" ${st}/>
             <circle cx="32" cy="20" r="4.2" fill="#FFF7C2" ${st.replace('stroke-width="3"', 'stroke-width="2.4"')}/>`;
    } else if (role === "reviewer") {
      top = `<path d="M13 31a19 15 0 0 1 38 0z" fill="${COL.blue}" ${st}/><path d="M44 31h13" stroke="${INK}" stroke-width="3" stroke-linecap="round"/>`;
      mid = `<circle cx="25.5" cy="41" r="6.3" fill="#fff" fill-opacity=".7" stroke="${INK}" stroke-width="2.4"/><circle cx="38.5" cy="41" r="6.3" fill="#fff" fill-opacity=".7" stroke="${INK}" stroke-width="2.4"/><path d="M31.8 41h.4" stroke="${INK}" stroke-width="2.4"/>`;
    } else if (role === "historian") {
      top = `<ellipse cx="31" cy="26" rx="21" ry="9.5" fill="${COL.lilac}" ${st}/><circle cx="33" cy="15.5" r="3.2" fill="${COL.lilac}" ${st.replace('stroke-width="3"', 'stroke-width="2.4"')}/>`;
    } else {
      top = `<path d="M13 32a19 15 0 0 1 38 0z" fill="#C9D2F0" ${st}/>`;
    }
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">${head}${blush}${eyes}${mid}${smile}${top}</svg>`;
  }
  const avatarImgs = new Map();
  function avatarImg(role, i) {
    const k = role + i;
    if (!avatarImgs.has(k)) {
      const im = new Image();
      im.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(avatarSVG(role, i));
      avatarImgs.set(k, im);
    }
    return avatarImgs.get(k);
  }

  /* ---------- tabs, pickers, layers ---------- */
  function setTab(t) {
    S.tab = t;
    document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === t)));
    ["feed", "lessons", "chat"].forEach((n) => { $("#panel-" + n).hidden = n !== t; });
    if (t === "chat") loadSessions();
    render();
  }
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  document.querySelectorAll("[data-layer]").forEach((c) => c.addEventListener("change", () => {
    S.layers[c.dataset.layer] = c.checked;
    S.sig = "";
    syncGraph();
    setTimeout(() => S.graph && S.graph.zoomToFit(500, 50), 700);
  }));
  $("#whyClose").addEventListener("click", clearWhy);

  async function loadRepos() {
    const r = await get("/api/repos");
    const sel = $("#repoPick");
    sel.replaceChildren(...r.repos.map((n) => { const o = el("option", "", n); o.value = n; return o; }));
    S.repo = new URLSearchParams(location.search).get("repo") || r.default || r.repos[0];
    sel.value = S.repo;
    sel.onchange = () => { S.repo = sel.value; S.run = null; resetView(); loadRuns(); };
    await loadRuns();
  }
  async function loadRuns() {
    const r = await get("/api/runs?repo=" + encodeURIComponent(S.repo));
    const sel = $("#runPick");
    sel.replaceChildren(...r.runs.map((x) => { const o = el("option", "", `${x.run} (${x.tasks} tickets)`); o.value = x.run; return o; }));
    const want = new URLSearchParams(location.search).get("run");
    S.run = (want && r.runs.some((x) => x.run === want)) ? want : (r.runs[0] && r.runs[0].run) || null;
    if (S.run) sel.value = S.run;
    sel.onchange = () => { S.run = sel.value; resetView(); tick(true); };
  }
  function resetView() {
    S.sel = null; S.hi = null; S.seen = null; S.data = null; S.nodes.clear(); S.links.clear(); S.sig = ""; S.fitted = false;
    S.session = null; clearWhy(true);
    if (S.graph) S.graph.graphData({ nodes: [], links: [] });
  }

  /* ---------- polling ---------- */
  function setLive(ok, msg) {
    const l = $("#live");
    l.className = "live " + (ok ? "on" : "off");
    $("#liveText").textContent = ok ? "live" : "waiting";
    const box = $("#errorbox");
    if (ok) { box.hidden = true; return; }
    box.hidden = false;
    box.replaceChildren(el("p", "", "Can't read the graph right now."), el("p", "", msg || "Is the database running?"));
  }
  async function tick(now) {
    clearTimeout(S.timer);
    try {
      if (!S.repo) await loadRepos();
      if (S.run) {
        const [state, mem] = await Promise.all([get("/api/state" + q()), get("/api/memories?repo=" + encodeURIComponent(S.repo))]);
        S.data = state; S.mem = mem.memories;
        setLive(true);
        render();
        if (S.tab === "chat") refreshChat();
      } else {
        S.data = null; setLive(true); render();
        await loadRuns();
      }
    } catch (e) {
      setLive(false, e.message);
    }
    S.timer = setTimeout(tick, 1500);
  }

  /* ---------- rendering ---------- */
  function render() {
    const d = S.data;
    $("#empty").hidden = !!(d && d.nodes.some((n) => n.type === "Task"));
    renderMetrics(d);
    renderRoster(d);
    renderBoard(d);
    syncGraph();
    if (S.tab === "feed") renderFeed(d);
    if (S.tab === "lessons") renderLessons();
  }

  function renderMetrics(d) {
    const m = d ? d.metrics : null;
    const refused = m ? Object.values(m.claims_refused || {}).reduce((a, b) => a + b, 0) : 0;
    $("#signNumber").textContent = m ? m.double_claims : 0;
    $("#signFoot").textContent = `in ${m ? m.claims_attempted : 0} claim attempts`;
    $("#sign").classList.toggle("bad", !!(m && m.double_claims > 0));
    const pills = [
      ["c1", m ? m.claims_granted : 0, "claims granted"],
      ["c2", refused, "refused, kept apart"],
      ["c3", m ? m.collisions : 0, "collisions"],
      ["c4", m ? m.reworks : 0, "retries after review"],
      ["c5", m ? m.handoffs_recovered : 0, "handoffs picked up"],
    ];
    $("#pills").replaceChildren(...pills.map(([c, n, label]) => {
      const p = el("span", "pill " + c);
      p.append(el("b", "", String(n)), document.createTextNode(label));
      return p;
    }));
  }

  function agentsOf(d) {
    const order = { foreman: 0, worker: 1, reviewer: 2, historian: 3 };
    return d.nodes.filter((n) => n.type === "Agent")
      .sort((a, b) => (order[a.role] ?? 9) - (order[b.role] ?? 9) || taskNum(a.id) - taskNum(b.id) || a.id.localeCompare(b.id));
  }
  const tasksOf = (d) => d.nodes.filter((n) => n.type === "Task").sort((a, b) => taskNum(a.id) - taskNum(b.id));

  function renderRoster(d) {
    const ul = $("#roster");
    if (!d) { ul.replaceChildren(); return; }
    const tasks = tasksOf(d), agents = agentsOf(d);
    const withLesson = new Set(d.edges.filter((e) => e.type === "LEARNED_FROM").map((e) => e.to));
    let wi = 0;
    ul.replaceChildren(...agents.map((a) => {
      const li = el("li", "agent");
      const idx = a.role === "worker" ? wi++ : 0;
      li.insertAdjacentHTML("afterbegin", avatarSVG(a.role, idx));
      const box = el("div");
      box.append(el("div", "agent-name", niceName(a)));
      let doing = "";
      if (a.role === "worker") {
        const mine = tasks.find((t) => t.claimed_by === a.id && t.status === "claimed");
        const wait = tasks.find((t) => t.claimed_by === a.id && t.status === "submitted");
        doing = mine ? `Working on ${mine.id}` : wait ? `${wait.id} sent for review` : tasks.every((t) => t.status === "approved") ? "All done" : "Looking for a job";
        li.classList.toggle("busy", !!mine);
      } else if (a.role === "reviewer") {
        const t = tasks.find((x) => x.status === "submitted");
        doing = t ? `Checking ${t.id}` : "Waiting for a patch";
        li.classList.toggle("busy", !!t);
      } else if (a.role === "historian") {
        const t = tasks.find((x) => x.status === "approved" && !withLesson.has(x.id));
        doing = t ? `Writing up ${t.id}` : `${d.nodes.filter((n) => n.type === "Memory").length} lessons saved`;
        li.classList.toggle("busy", !!t);
      } else {
        doing = `${tasks.length} jobs planned, overlaps kept apart`;
      }
      box.append(el("div", "agent-doing", doing));
      li.append(box);
      return li;
    }));
  }

  function renderBoard(d) {
    const ul = $("#board");
    if (!d) { ul.replaceChildren(); return; }
    const byId = new Map(d.nodes.filter((n) => n.type === "Agent").map((a) => [a.id, a]));
    ul.replaceChildren(...tasksOf(d).map((t) => {
      const li = el("li");
      const b = el("button", "job");
      b.setAttribute("aria-pressed", String(S.sel === t.id));
      b.append(el("span", "job-id", t.id), el("span", "job-title", t.label));
      const meta = el("span", "job-meta");
      meta.append(el("span", "chip " + t.status, t.status));
      if (t.attempts > 0 && t.status !== "approved") meta.append(el("span", "chip rework", `retry ${t.attempts}`));
      if (t.claimed_by && byId.get(t.claimed_by)) meta.append(el("span", "", niceName(byId.get(t.claimed_by))));
      b.append(meta);
      b.addEventListener("click", () => selectTask(S.sel === t.id ? null : t.id));
      li.append(b);
      return li;
    }));
  }

  function selectTask(id) {
    S.sel = id;
    S.sig = "";
    if (!id) clearWhy();
    render();
    if (S.tab === "chat") loadSessions();
  }

  const ICON = { claim: "🙋", footprint: "👣", order: "📋", release: "↩️", expire: "⏰", review: "🔍", result: "📦", handoff: "📝", retire: "🗑️" };
  const ASKABLE = new Set(["claim", "footprint", "order", "release", "expire", "review"]);
  function renderFeed(d) {
    const ol = $("#feed");
    if (!d) { ol.replaceChildren(); return; }
    let evs = d.events.slice().sort((a, b) => (a.ts < b.ts ? 1 : -1));
    if (S.sel) evs = evs.filter((e) => e.task === S.sel);
    evs = evs.slice(0, 150);
    const first = S.seen === null;
    if (first) S.seen = new Set();
    if (!evs.length) {
      ol.replaceChildren(el("li", "hint", S.sel ? `Nothing recorded on ${S.sel} yet.` : "Nothing has happened yet."));
      return;
    }
    ol.replaceChildren(...evs.map((e) => {
      const fresh = !first && !S.seen.has(e.id);
      const li = el("li", "ev" + (fresh ? " new" : ""));
      const verdict = e.type === "review" ? (/approved/.test(e.summary) ? " review-yes" : " review-no") : "";
      li.className += verdict;
      li.append(el("span", "ico", ICON[e.type] || "•"));
      const mid = el("div");
      mid.append(el("div", "what", e.summary.charAt(0).toUpperCase() + e.summary.slice(1)), el("div", "when", clock(e.ts)));
      li.append(mid);
      if (ASKABLE.has(e.type)) {
        const b = el("button", "ask", "Why?");
        b.setAttribute("aria-label", "Why did this happen? " + e.summary);
        b.addEventListener("click", () => explain(e.id));
        li.append(b);
      }
      if (fresh && e.task) S.pulses.set(e.task, performance.now());
      return li;
    }));
    evs.forEach((e) => S.seen.add(e.id));
    d.events.forEach((e) => S.seen.add(e.id));
  }

  function renderLessons() {
    const ul = $("#lessons");
    if (!S.mem.length) {
      ul.replaceChildren(el("li", "hint", "No lessons yet. The Historian writes them after a ticket is approved, and workers see them on similar tickets."));
      return;
    }
    ul.replaceChildren(...S.mem.map((m) => {
      const li = el("li", "lesson" + (m.status === "retired" ? " retired" : ""));
      const head = el("div");
      head.append(el("span", "chip " + m.kind, m.kind));
      if (m.status === "retired") head.append(document.createTextNode(" "), el("span", "chip rework", "retired"));
      li.append(head, el("p", "", m.summary));
      const total = m.uses || 0, ok = m.successes || 0;
      const bar = el("div", "bar");
      const fill = el("i");
      fill.style.width = total ? Math.round((ok / total) * 100) + "%" : "0%";
      bar.append(fill);
      li.append(bar, el("small", "", total ? `Helped ${ok} of ${total} times it was used` : "Not used yet"));
      if (m.files.length) li.append(el("small", "", "About " + m.files.join(", ")));
      if (m.task) li.append(el("small", "", "Learned from " + m.task));
      if (m.status === "retired") li.append(el("small", "", `Retired: ${m.retired_reason || "outdated"}${m.superseded_by ? `, replaced by ${m.superseded_by}` : ""}`));
      return li;
    }));
  }

  /* ---------- explain ---------- */
  async function explain(id) {
    try {
      const r = await get(`/api/explain/${encodeURIComponent(id)}` + q());
      const nodes = new Set(r.path.nodes.map((n) => n.id));
      const edges = new Set(r.path.edges.map((e) => e.from + "|" + e.to));
      S.hi = { nodes, edges, extra: r.path.edges };
      $("#whyText").textContent = r.summary;
      $("#why").hidden = false;
      S.sig = "";
      syncGraph();
      setTimeout(() => fitTo((n) => nodes.has(n.id)), 500);
    } catch (e) {
      $("#whyText").textContent = "Couldn't load that explanation: " + e.message;
      $("#why").hidden = false;
    }
  }
  function fitTo(filter) {
    if (!S.graph) return;
    S.graph.zoomToFit(500, 70, filter);
    setTimeout(() => { if (S.graph.zoom() > 1.5) S.graph.zoom(1.5, 300); }, 560);
  }
  function clearWhy(quiet) {
    S.hi = null;
    $("#why").hidden = true;
    if (!quiet) { S.sig = ""; syncGraph(); setTimeout(() => fitTo(), 500); }
  }

  /* ---------- chat history ---------- */
  async function loadSessions() {
    const hint = $("#chatHint"), box = $("#sessions");
    if (!S.sel) { hint.hidden = false; box.replaceChildren(); $("#chat").replaceChildren(); S.session = null; return; }
    hint.hidden = true;
    try {
      const r = await get("/api/sessions" + q("&task=" + encodeURIComponent(S.sel)));
      if (!r.sessions.length) {
        box.replaceChildren(el("p", "hint", `No conversations on ${S.sel} yet.`));
        $("#chat").replaceChildren(); S.session = null; return;
      }
      if (!r.sessions.some((s) => s.id === S.session)) S.session = r.sessions[0].id;
      box.replaceChildren(...r.sessions.map((s) => {
        const b = el("button", "", `${s.agent} (${s.role}), ${s.messages} messages`);
        b.setAttribute("aria-pressed", String(s.id === S.session));
        b.addEventListener("click", () => { S.session = s.id; loadSessions(); });
        return b;
      }));
      refreshChat();
    } catch (e) { box.replaceChildren(el("p", "hint", e.message)); }
  }
  async function refreshChat() {
    if (!S.session) return;
    try {
      const r = await get(`/api/session/${encodeURIComponent(S.session)}` + q());
      const ol = $("#chat");
      const atEnd = $("#panel-chat").scrollTop + $("#panel-chat").clientHeight >= $("#panel-chat").scrollHeight - 40;
      ol.replaceChildren(...r.messages.map((m) => {
        const li = el("li", "msg " + m.role);
        if (m.role === "tool") {
          li.textContent = `${m.result_ok === false ? "✗" : "✓"} ${m.text}`;
          if (m.result_ok === false) li.classList.add("bad");
        } else if (m.role === "agent") {
          if (m.text) li.append(document.createTextNode(m.text));
          m.calls.forEach((c) => {
            const arg = c.arguments ? Object.values(c.arguments).map((v) => String(typeof v === "object" ? JSON.stringify(v) : v).slice(0, 34)).join(", ") : "";
            li.append(el("span", "call", `${c.name}(${arg})`));
          });
          if (!m.text && !m.calls.length) li.textContent = "…";
        } else {
          li.textContent = m.text;
        }
        return li;
      }));
      if (atEnd) $("#panel-chat").scrollTop = $("#panel-chat").scrollHeight;
    } catch (e) { /* the next poll will retry */ }
  }

  /* ---------- the graph ---------- */
  function visible(n, d) {
    const inHi = S.hi && S.hi.nodes.has(n.id);
    if (inHi) return true;
    switch (n.type) {
      case "Agent": case "Task": return true;
      case "Memory": return S.layers.memory;
      case "Function": return S.layers.code || (S.sel && S.touchSel && S.touchSel.has(n.id));
      case "Decision": return S.layers.decisions;
      case "Result": return S.layers.results;
      default: return false;
    }
  }

  function agentSlots(agents) {
    const w = agents.filter((a) => a.role === "worker"), pos = new Map();
    const narrow = $("#canvas").clientWidth < 700;
    const X = narrow ? 190 : 330, step = narrow ? 80 : 95;
    agents.forEach((a) => {
      if (a.role === "foreman") pos.set(a.id, [-X, -170]);
      else if (a.role === "worker") pos.set(a.id, [-X, -60 + w.indexOf(a) * step - Math.max(0, w.length - 2) * 30]);
      else if (a.role === "reviewer") pos.set(a.id, [X, -90]);
      else if (a.role === "historian") pos.set(a.id, [X, 70]);
    });
    return pos;
  }

  function syncGraph() {
    if (!S.graph) return;
    const d = S.data;
    if (!d) return;
    S.touchSel = S.sel ? new Set(d.edges.filter((e) => e.type === "TOUCHES" && e.from === S.sel).map((e) => e.to)) : null;
    const agents = agentsOf(d), slots = agentSlots(agents);
    let wi = 0;
    const wanted = new Map();
    d.nodes.forEach((n) => { if (visible(n, d)) wanted.set(n.id, n); });
    // nodes: keep the same object so positions survive each refresh
    for (const id of [...S.nodes.keys()]) if (!wanted.has(id)) S.nodes.delete(id);
    wanted.forEach((n, id) => {
      let o = S.nodes.get(id);
      if (!o) { o = { id, x: (Math.random() - 0.5) * 200, y: (Math.random() - 0.5) * 160 }; S.nodes.set(id, o); }
      Object.assign(o, n);
      if (n.type === "Agent") {
        const p = slots.get(id);
        if (p) { o.fx = p[0]; o.fy = p[1]; }
        o.hatIndex = n.role === "worker" ? agents.filter((a) => a.role === "worker").indexOf(n) : 0;
      }
    });
    // links
    const keyOf = (e) => `${e.from}|${e.to}|${e.type}`;
    const want = new Map();
    d.edges.forEach((e) => { if (wanted.has(e.from) && wanted.has(e.to)) want.set(keyOf(e), e); });
    if (S.hi) S.hi.extra.forEach((e) => { if (wanted.has(e.from) && wanted.has(e.to)) want.set(keyOf(e), e); });
    for (const k of [...S.links.keys()]) if (!want.has(k)) S.links.delete(k);
    want.forEach((e, k) => { if (!S.links.has(k)) S.links.set(k, { source: e.from, target: e.to, type: e.type, key: k }); });
    const sig = [...S.nodes.keys()].sort().join(",") + "#" + [...S.links.keys()].sort().join(",");
    if (sig !== S.sig) {
      S.sig = sig;
      S.graph.graphData({ nodes: [...S.nodes.values()], links: [...S.links.values()] });
      if (!S.fitted && S.nodes.size > 3) { S.fitted = true; setTimeout(() => S.graph.zoomToFit(500, 60), 900); }
    }
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x, y, w, h, r); else ctx.rect(x, y, w, h);
  }
  const linkId = (v) => (typeof v === "object" ? v.id : v);
  const inHiNode = (id) => !S.hi || S.hi.nodes.has(id);
  const inHiLink = (l) => {
    if (!S.hi) return true;
    const a = linkId(l.source), b = linkId(l.target);
    return S.hi.edges.has(a + "|" + b) || S.hi.edges.has(b + "|" + a);
  };

  function paintNode(n, ctx, k) {
    const dim = S.hi && !S.hi.nodes.has(n.id);
    ctx.save();
    ctx.globalAlpha = dim ? 0.16 : 1;
    const pulse = S.pulses.get(n.id);
    if (pulse && !REDUCED) {
      const t = (performance.now() - pulse) / 1100;
      if (t < 1) {
        ctx.beginPath(); ctx.arc(n.x, n.y, 18 + t * 30, 0, 7);
        ctx.strokeStyle = alpha(COL.hat, 1 - t); ctx.lineWidth = 4; ctx.stroke();
      } else S.pulses.delete(n.id);
    }
    if (n.type === "Agent") {
      const r = 21;
      const sel = S.sel && S.data && S.data.nodes.some((t) => t.id === S.sel && t.claimed_by === n.id);
      ctx.beginPath(); ctx.arc(n.x, n.y, r + 3, 0, 7); ctx.fillStyle = sel ? COL.hat : "#fff"; ctx.fill();
      ctx.lineWidth = 2.5; ctx.strokeStyle = INK; ctx.stroke();
      const im = avatarImg(n.role, n.hatIndex || 0);
      if (im.complete) ctx.drawImage(im, n.x - r, n.y - r, r * 2, r * 2);
      ctx.font = `600 ${12}px Fredoka, sans-serif`; ctx.textAlign = "center"; ctx.fillStyle = INK;
      ctx.fillText(niceName(n), n.x, n.y + r + 17);
    } else if (n.type === "Task") {
      const w = 62, h = 30, x = n.x - w / 2, y = n.y - h / 2;
      const rework = n.status === "open" && n.attempts > 0;
      const selected = S.sel === n.id;
      if (selected) { roundRect(ctx, x - 5, y - 5, w + 10, h + 10, 14); ctx.fillStyle = COL.hat; ctx.fill(); }
      roundRect(ctx, x, y, w, h, 10);
      ctx.fillStyle = rework ? "#FFD9D9" : STATUS_FILL[n.status] || "#fff"; ctx.fill();
      ctx.lineWidth = 2.5; ctx.strokeStyle = INK; ctx.stroke();
      ctx.fillStyle = INK; ctx.font = "600 14px Fredoka, sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(n.id, n.x, n.y + 1);
      if (n.attempts > 0 && n.status !== "approved") {
        ctx.beginPath(); ctx.arc(x + w - 2, y + 1, 8, 0, 7); ctx.fillStyle = COL.coral; ctx.fill(); ctx.lineWidth = 2; ctx.stroke();
        ctx.fillStyle = INK; ctx.font = "700 10px Fredoka, sans-serif"; ctx.fillText("↻" + n.attempts, x + w - 2, y + 2);
      }
      ctx.textBaseline = "alphabetic";
    } else if (n.type === "Memory") {
      const retired = n.status === "retired";
      roundRect(ctx, n.x - 12, n.y - 12, 24, 24, 7);
      ctx.fillStyle = retired ? "#D8DCEB" : COL.lilac; ctx.fill(); ctx.lineWidth = 2.2; ctx.strokeStyle = INK; ctx.stroke();
      ctx.fillStyle = INK; ctx.font = "700 13px Fredoka, sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText((n.kind || "m")[0].toUpperCase(), n.x, n.y + 1);
      if (retired) { ctx.beginPath(); ctx.moveTo(n.x - 12, n.y + 12); ctx.lineTo(n.x + 12, n.y - 12); ctx.stroke(); }
      ctx.textBaseline = "alphabetic";
    } else if (n.type === "Function") {
      ctx.beginPath(); ctx.arc(n.x, n.y, 5, 0, 7); ctx.fillStyle = "#fff"; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = INK; ctx.stroke();
      if (S.hi || k > 1.2) {
        ctx.fillStyle = INK; ctx.font = "700 10px Atkinson Hyperlegible, sans-serif"; ctx.textAlign = "center";
        ctx.fillText(String(n.id).split(".").slice(-2).join("."), n.x, n.y - 9);
      }
    } else if (n.type === "Decision") {
      ctx.beginPath(); ctx.moveTo(n.x, n.y - 8); ctx.lineTo(n.x + 8, n.y); ctx.lineTo(n.x, n.y + 8); ctx.lineTo(n.x - 8, n.y); ctx.closePath();
      ctx.fillStyle = n.kind === "review" ? COL.mint : n.kind === "claim" ? COL.tang : COL.hat; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = INK; ctx.stroke();
      if (S.hi && S.hi.nodes.has(n.id)) {
        ctx.fillStyle = INK; ctx.font = "700 11px Atkinson Hyperlegible, sans-serif"; ctx.textAlign = "center";
        ctx.fillText(n.kind, n.x, n.y - 13);
      }
    } else {
      roundRect(ctx, n.x - 7, n.y - 9, 14, 18, 3); ctx.fillStyle = "#fff"; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = INK; ctx.stroke();
    }
    ctx.restore();
  }
  function paintArea(n, color, ctx) {
    ctx.fillStyle = color;
    const r = n.type === "Agent" ? 26 : n.type === "Task" ? 34 : 14;
    ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, 7); ctx.fill();
  }

  const LINK_STYLE = {
    CLAIMED: [COL.tang, 3.5, null], CONFLICTS_WITH: [COL.coral, 2.5, [6, 5]], RECALLED: [COL.lilac, 2, null],
    LEARNED_FROM: [COL.lilac, 1.6, null], TOUCHES: ["#9AA6D6", 1, null], ABOUT: ["#9AA6D6", 1.2, null],
    MADE_BY: ["#9AA6D6", 1.2, null], FOR: ["#9AA6D6", 1.2, null],
  };
  function initGraph() {
    const host = $("#canvas");
    const g = ForceGraph()(host)
      .backgroundColor("rgba(0,0,0,0)")
      .nodeId("id")
      .nodeRelSize(6)
      .autoPauseRedraw(false)
      .d3VelocityDecay(0.32)
      .cooldownTime(60000)
      .nodeCanvasObject(paintNode)
      .nodePointerAreaPaint(paintArea)
      .nodeLabel(() => "")
      .linkColor((l) => {
        const [c] = LINK_STYLE[l.type] || ["#9AA6D6"];
        return inHiLink(l) ? c : alpha(c.length === 7 ? c : "#9AA6D6", 0.08);
      })
      .linkWidth((l) => {
        const w = (LINK_STYLE[l.type] || [0, 1])[1];
        return S.hi && inHiLink(l) ? w + 1.8 : w;
      })
      .linkLineDash((l) => (LINK_STYLE[l.type] || [0, 0, null])[2])
      .linkDirectionalParticles((l) => (S.hi && inHiLink(l) && !REDUCED ? 4 : 0))
      .linkDirectionalParticleWidth(5)
      .linkDirectionalParticleSpeed(0.011)
      .linkDirectionalParticleColor(() => COL.hat)
      .linkCanvasObjectMode(() => "after")
      .linkCanvasObject((l, ctx) => {
        if (!S.hi || !inHiLink(l) || typeof l.source !== "object" || l.type === "TOUCHES") return;
        const x = (l.source.x + l.target.x) / 2, y = (l.source.y + l.target.y) / 2;
        const label = l.type.replace("_", " ").toLowerCase();
        ctx.font = "700 10px Atkinson Hyperlegible, sans-serif"; ctx.textAlign = "center";
        const w = ctx.measureText(label).width + 8;
        roundRect(ctx, x - w / 2, y - 8, w, 16, 8); ctx.fillStyle = "#fff"; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = INK; ctx.stroke();
        ctx.fillStyle = INK; ctx.textBaseline = "middle"; ctx.fillText(label, x, y + 0.5); ctx.textBaseline = "alphabetic";
      })
      .onNodeClick((n) => {
        if (n.type === "Task") selectTask(S.sel === n.id ? null : n.id);
        else if (n.type === "Decision") explain(n.id);
      })
      .onBackgroundClick(() => { if (S.sel) selectTask(null); });
    g.d3Force("charge").strength(-230);
    g.d3Force("link").distance((l) => (l.type === "CLAIMED" ? 120 : l.type === "CONFLICTS_WITH" ? 130 : l.type === "TOUCHES" ? 45 : 70)).strength(0.5);
    g.d3Force("apart", (a) => {
      const ts = [...S.nodes.values()].filter((n) => n.type === "Task");
      for (let i = 0; i < ts.length; i++) for (let j = i + 1; j < ts.length; j++) {
        const A = ts[i], B = ts[j], dx = B.x - A.x, dy = B.y - A.y;
        if (Math.abs(dx) < 74 && Math.abs(dy) < 38) {
          const px = (74 - Math.abs(dx)) * 0.06 * a * Math.sign(dx || Math.random() - 0.5), py = (38 - Math.abs(dy)) * 0.12 * a * Math.sign(dy || Math.random() - 0.5);
          B.vx += px; A.vx -= px; B.vy += py; A.vy -= py;
        }
      }
    });
    g.d3Force("pull", (a) => {
      S.nodes.forEach((n) => {
        if (n.type === "Task" && n.fx == null) { n.vx -= n.x * 0.035 * a; n.vy -= n.y * 0.035 * a; }
        if (n.type === "Memory" && n.fx == null) { n.vx += (260 - n.x) * 0.02 * a; n.vy += (170 - n.y) * 0.02 * a; }
      });
    });
    const fit = () => { const r = host.getBoundingClientRect(); g.width(r.width).height(r.height); };
    new ResizeObserver(fit).observe(host);
    fit();
    S.graph = g;
  }

  /* ---------- go ---------- */
  initGraph();
  setTab("feed");
  tick();
})();
