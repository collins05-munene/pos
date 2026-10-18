(function () {
  "use strict";
  var root = document.getElementById("helpdesk");
  if (!root) return;
  var API = root.getAttribute("data-api");
  var TENANT_BASE = root.getAttribute("data-tenant-base");
  var LANDING = root.getAttribute("data-landing");
  var CH = JSON.parse(document.getElementById("hd-choices").textContent);
  var CSRF = root.querySelector("[name=csrfmiddlewaretoken]").value;

  function h(tag, props, kids) {
    var n = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      var v = props[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    });
    (kids || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return n;
  }
  function $(id) { return document.getElementById(id); }
  function clear(n) { n.replaceChildren(); return n; }
  function fill(select, pairs, current) {
    pairs.forEach(function (p) {
      var o = h("option", { value: p[0], text: p[1] });
      if (String(current) === String(p[0])) o.selected = true;
      select.appendChild(o);
    });
    return select;
  }
  function fmt(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString("en-KE", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
  }
  function ago(iso) {
    if (!iso) return "";
    var s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }
  function dur(sec) {
    if (sec === null || sec === undefined) return "--";
    if (sec < 3600) return Math.max(1, Math.round(sec / 60)) + " min";
    if (sec < 172800) return (sec / 3600).toFixed(1).replace(/\.0$/, "") + " h";
    return Math.round(sec / 86400) + " days";
  }
  function size(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB"; }
  function qs(o) {
    return Object.keys(o).filter(function (k) { return o[k] !== "" && o[k] !== null; })
      .map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(o[k]); }).join("&");
  }
  function form(o) {
    var fd = new FormData();
    Object.keys(o).forEach(function (k) {
      var v = o[k];
      if (v === true) fd.append(k, "on");
      else if (v !== false && v !== null && v !== undefined) fd.append(k, String(v));
    });
    return fd;
  }
  function api(path, o) {
    o = o || {};
    var init = { method: o.method || "GET", credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } };
    if (init.method !== "GET") { init.headers["X-CSRFToken"] = CSRF; init.body = o.body; }
    return fetch(API + path, init).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw { status: r.status, data: d };
        return d;
      });
    });
  }
  var flashTimer;
  function flash(msg, bad) {
    var f = $("hd-flash");
    f.textContent = msg; f.className = "hd-flash" + (bad ? " bad" : ""); f.hidden = false;
    clearTimeout(flashTimer); flashTimer = setTimeout(function () { f.hidden = true; }, 5000);
  }
  function failText(e) {
    if (e && e.data && e.data.errors) {
      return Object.keys(e.data.errors).map(function (k) { return e.data.errors[k].join(" "); }).join(" ");
    }
    if (e && e.status === 403) return "You don't have access to the help desk.";
    return "Something went wrong. Please try again.";
  }

  function showErrors(container, e) {
    container.querySelectorAll(".hd-error").forEach(function (n) { n.remove(); });
    var errors = (e && e.data && e.data.errors) || null;
    if (!errors) { flash(failText(e), true); return; }
    Object.keys(errors).forEach(function (k) {
      var input = container.querySelector('[name="' + k + '"]');
      var msg = h("p", { class: "hd-error", role: "alert", text: errors[k].join(" ") });
      if (input && input.parentNode) input.parentNode.appendChild(msg); else container.insertBefore(msg, container.firstChild);
    });
  }
  function field(label, input, hint) {
    return h("div", { class: "hd-field" }, [h("label", { text: label }), input, hint ? h("p", { class: "hd-hint", text: hint }) : null]);
  }
  function badge(text, cls) { return h("span", { class: "hd-badge " + (cls || ""), text: text }); }
  function pager(box, d, go) {
    clear(box);
    if (d.pages <= 1) return;
    if (d.page > 1) box.appendChild(h("button", { type: "button", class: "hd-btn", text: "Previous", onclick: function () { go(d.page - 1); } }));
    box.appendChild(h("span", { class: "hd-meta", text: "Page " + d.page + " of " + d.pages }));
    if (d.page < d.pages) box.appendChild(h("button", { type: "button", class: "hd-btn", text: "Next", onclick: function () { go(d.page + 1); } }));
  }
  function emptyRow(tbody, cols, text) {
    clear(tbody).appendChild(h("tr", { class: "empty-row" }, [h("td", { colspan: cols, text: text })]));
  }
  function debounce(fn, ms) { var t; return function () { clearTimeout(t); t = setTimeout(fn, ms); }; }

  var drawer = $("hd-drawer"), backdrop = $("hd-backdrop"), lastFocus = null, onClose = null;
  function openDrawer(title, node, closeCb) {
    lastFocus = document.activeElement; onClose = closeCb || null;
    $("hd-drawer-title").textContent = title;
    clear($("hd-drawer-body")).appendChild(node);
    drawer.hidden = false; backdrop.hidden = false;
    $("hd-close").focus();
  }
  function setDrawer(node) { clear($("hd-drawer-body")).appendChild(node); }
  function closeDrawer() {
    if (drawer.hidden) return;
    drawer.hidden = true; backdrop.hidden = true;
    clear($("hd-drawer-body"));
    if (onClose) onClose();
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  $("hd-close").addEventListener("click", closeDrawer);
  backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeDrawer(); });

  function loadSummary() {
    api("summary/").then(function (d) {
      var set = function (k, v) { var n = root.querySelector('[data-kpi="' + k + '"]'); if (n) n.textContent = v; };
      set("open", d.open); set("unassigned", d.unassigned); set("urgent", d.urgent);
      set("reply", dur(d.median_first_reply_seconds));
      root.querySelectorAll("[data-count]").forEach(function (n) {
        var v = d[n.getAttribute("data-count")];
        n.textContent = v ? String(v) : "";
        n.title = n.getAttribute("data-count") === "draft_articles" ? v + " draft(s)" : v + " to review";
      });
      $("refreshed-at").textContent = "Updated " + new Date().toLocaleTimeString("en-KE", { hour: "numeric", minute: "2-digit" });
    }).catch(function (e) { flash(failText(e), true); });
  }

  var loaded = {};
  var loaders = { tickets: loadTickets, articles: loadArticles, features: loadFeatures };
  function show(name) {
    if (!loaders[name]) name = "tickets";
    root.querySelectorAll("[data-tab]").forEach(function (b) {
      var on = b.getAttribute("data-tab") === name;
      b.setAttribute("aria-selected", on ? "true" : "false"); b.tabIndex = on ? 0 : -1;
    });
    ["tickets", "articles", "features"].forEach(function (n) { $("panel-" + n).hidden = n !== name; });
    if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
    loaders[name]();
  }
  root.querySelectorAll("[data-tab]").forEach(function (b) {
    b.addEventListener("click", function () { show(b.getAttribute("data-tab")); });
  });

  var T = { view: "active", priority: "", kind: "", q: "", page: 1 };
  fill($("t-priority"), CH.priority); fill($("t-kind"), CH.ticket_kind);
  $("t-view").addEventListener("change", function () { T.view = this.value; T.page = 1; loadTickets(); });
  $("t-priority").addEventListener("change", function () { T.priority = this.value; T.page = 1; loadTickets(); });
  $("t-kind").addEventListener("change", function () { T.kind = this.value; T.page = 1; loadTickets(); });
  $("t-search").addEventListener("input", debounce(function () { T.q = $("t-search").value.trim(); T.page = 1; loadTickets(); }, 300));

  function loadTickets() {
    api("tickets/?" + qs(T)).then(function (d) {
      var tbody = $("t-rows");
      if (!d.results.length) emptyRow(tbody, 7, "Nothing in this queue.");
      else {
        clear(tbody);
        d.results.forEach(function (t) {
          var open = function () { openTicket(t.number); };
          tbody.appendChild(h("tr", { class: "hd-row", tabindex: "0", role: "button", onclick: open,
            onkeydown: function (e) { if (e.key === "Enter") open(); } }, [
            h("td", { text: t.reference }), h("td", { text: t.tenant }), h("td", { text: t.subject }),
            h("td", {}, [badge(t.priority_label, "hd-p-" + t.priority.toLowerCase())]),
            h("td", {}, [badge(t.status_label, "hd-s-" + t.status.toLowerCase())]),
            h("td", { text: t.agent || "Unassigned" }), h("td", { text: ago(t.updated_at) })]));
        });
      }
      pager($("t-pager"), d, function (p) { T.page = p; loadTickets(); });
    }).catch(function (e) { flash(failText(e), true); });
  }

  function files(list) {
    if (!list || !list.length) return null;
    return h("ul", { class: "hd-files" }, list.map(function (a) {
      if (a.is_image) return h("li", {}, [h("a", { href: a.url, target: "_blank", rel: "noopener" }, [h("img", { src: a.url, alt: a.name, loading: "lazy" })])]);
      if (a.is_video) return h("li", {}, [h("video", { controls: true, preload: "metadata", src: a.url })]);
      return h("li", {}, [h("a", { href: a.url, text: a.name + " (" + size(a.size) + ")" })]);
    }));
  }

  function openTicket(number) {
    api("tickets/" + number + "/").then(function (t) {
      openDrawer(t.reference, renderTicket(t), function () { loadTickets(); loadSummary(); });
    }).catch(function (e) { flash(failText(e), true); });
  }
  function refreshTicket(t, msg) { setDrawer(renderTicket(t)); flash(msg); loadTickets(); loadSummary(); }

  function renderTicket(t) {
    var box = h("div", { class: "hd-ticket" });
    box.appendChild(h("p", { class: "hd-ref", text: t.reference + "  " + t.kind_label }));
    box.appendChild(h("h2", { class: "hd-subject", text: t.subject }));
    box.appendChild(h("div", { class: "hd-badges" }, [
      badge(t.status_label, "hd-s-" + t.status.toLowerCase()),
      badge("Priority: " + t.priority_label, "hd-p-" + t.priority.toLowerCase()),
      badge("Customer severity: " + t.severity_label),
      t.category_label ? badge(t.category_label) : null]));

    var c = t.customer;
    var grid = h("dl", { class: "hd-grid" });
    [["Shop", c.shop], ["Raised by", c.name], ["Email", c.email || c.shop_email || "-"], ["Shop phone", c.shop_phone || "-"],
     ["Opened", fmt(t.created_at)], ["First reply", t.first_response_at ? fmt(t.first_response_at) : "Not yet"]].forEach(function (r) {
      grid.appendChild(h("dt", { text: r[0] })); grid.appendChild(h("dd", { text: r[1] }));
    });
    box.appendChild(grid);
    var actions = h("div", { class: "hd-actions" });
    if (c.whatsapp) actions.appendChild(h("a", { class: "hd-btn", href: c.whatsapp, target: "_blank", rel: "noopener", text: "WhatsApp the shop" }));
    actions.appendChild(h("button", { type: "button", class: "hd-btn", text: "Open shop in support session", onclick: function () {
      if (!confirm("Start a support session as " + c.shop + "? This is logged.")) return;
      fetch(TENANT_BASE + t.tenant_id + "/support-start/", { method: "POST", credentials: "same-origin",
        headers: { "X-CSRFToken": CSRF, "X-Requested-With": "XMLHttpRequest" }, body: form({ note: "Support session for " + t.reference }) })
        .then(function (r) { if (!r.ok) throw r; location.href = LANDING; })
        .catch(function () { flash("Could not start the support session.", true); });
    } }));
    box.appendChild(actions);

    // Manage
    var m = h("form", { class: "hd-panel", novalidate: true });
    var status = fill(h("select", { name: "status" }), CH.ticket_status, t.status);
    var priority = fill(h("select", { name: "priority" }), CH.priority, t.priority);
    var agent = fill(h("select", { name: "assigned_to" }), [["", "Unassigned"]].concat(CH.agents.map(function (a) { return [a.id, a.name]; })), t.assigned_to || "");
    var summary = h("textarea", { name: "resolution_summary", rows: "3" }); summary.value = t.resolution_summary || "";
    m.appendChild(h("h3", { text: "Manage" }));
    m.appendChild(h("div", { class: "hd-two" }, [field("Status", status), field("Priority", priority)]));
    m.appendChild(field("Assigned to", agent));
    m.appendChild(field("Resolution summary", summary, "Shown to the customer when the ticket is resolved or closed."));
    m.appendChild(h("div", { class: "hd-actions" }, [
      h("button", { type: "submit", class: "hd-primary", text: "Save changes" }),
      t.assigned_to === CH.me ? null : h("button", { type: "button", class: "hd-btn", text: "Assign to me", onclick: function () {
        api("tickets/" + t.number + "/assign-me/", { method: "POST", body: form({}) })
          .then(function (d) { refreshTicket(d.ticket, "Assigned to you."); }).catch(function (e) { showErrors(m, e); });
      } })]));
    m.addEventListener("submit", function (e) {
      e.preventDefault();
      api("tickets/" + t.number + "/update/", { method: "POST", body: form({
        status: status.value, priority: priority.value, assigned_to: agent.value, resolution_summary: summary.value }) })
        .then(function (d) { refreshTicket(d.ticket, "Ticket updated."); }).catch(function (err) { showErrors(m, err); });
    });
    box.appendChild(m);

    // Thread
    box.appendChild(h("h3", { text: "Conversation" }));
    var thread = h("div", { class: "hd-thread" });
    var first = h("article", { class: "hd-msg" }, [
      h("header", {}, [c.name, h("time", { text: fmt(t.created_at) })]),
      h("p", { text: t.report.description })]);
    if (t.report.steps) { first.appendChild(h("h3", { text: "Steps to reproduce" })); first.appendChild(h("p", { text: t.report.steps })); }
    var ff = files(t.report.attachments); if (ff) first.appendChild(ff);
    thread.appendChild(first);
    t.messages.forEach(function (msg) {
      var cls = msg.is_internal ? "hd-msg internal" : msg.is_staff_reply ? "hd-msg support" : "hd-msg";
      var art = h("article", { class: cls }, [
        h("header", {}, [(msg.is_internal ? "Internal note, customers can't see this. " : "") + msg.author, h("time", { text: fmt(msg.created_at) })]),
        h("p", { text: msg.body })]);
      var mf = files(msg.attachments); if (mf) art.appendChild(mf);
      thread.appendChild(art);
    });
    box.appendChild(thread);

    // Reply
    var r = h("form", { class: "hd-panel", novalidate: true });
    var body = h("textarea", { name: "body", rows: "5", "aria-label": "Reply" });
    var file = h("input", { type: "file", name: "attachments", multiple: true, accept: "image/*,video/*,.pdf,.txt,.csv" });
    var internal = h("input", { type: "checkbox", name: "is_internal", id: "hd-internal" });
    var send = h("button", { type: "submit", class: "hd-primary", text: "Send reply" });
    internal.addEventListener("change", function () { send.textContent = internal.checked ? "Add internal note" : "Send reply"; });
    r.appendChild(field("Reply to customer", body));
    r.appendChild(field("Attach files", file));
    r.appendChild(h("label", { class: "hd-check", for: "hd-internal" }, [internal, "Internal note: only your team sees this"]));
    r.appendChild(send);
    r.addEventListener("submit", function (e) {
      e.preventDefault();
      var fd = form({ body: body.value, is_internal: internal.checked });
      Array.prototype.forEach.call(file.files, function (f) { fd.append("attachments", f); });
      send.disabled = true;
      api("tickets/" + t.number + "/reply/", { method: "POST", body: fd })
        .then(function (d) { refreshTicket(d.ticket, internal.checked ? "Internal note added." : "Reply sent."); })
        .catch(function (err) { send.disabled = false; showErrors(r, err); });
    });
    box.appendChild(r);

    // Environment + history
    var env = Object.keys(t.environment || {});
    if (env.length) {
      box.appendChild(h("h3", { text: "Environment" }));
      var eg = h("dl", { class: "hd-grid" });
      env.forEach(function (k) { eg.appendChild(h("dt", { text: k })); eg.appendChild(h("dd", { text: String(t.environment[k]) })); });
      box.appendChild(eg);
    }
    box.appendChild(h("h3", { text: "History" }));
    box.appendChild(h("ol", { class: "hd-history" }, t.events.map(function (ev) {
      return h("li", {}, [ev.detail + (ev.internal ? " (internal)" : ""), h("time", { text: fmt(ev.created_at) })]);
    })));
    return box;
  }

  var A = { status: "", kind: "", topic: "", q: "", page: 1 };
  fill($("a-kind"), CH.article_kind); fill($("a-topic"), CH.topic);
  $("a-status").addEventListener("change", function () { A.status = this.value; A.page = 1; loadArticles(); });
  $("a-kind").addEventListener("change", function () { A.kind = this.value; A.page = 1; loadArticles(); });
  $("a-topic").addEventListener("change", function () { A.topic = this.value; A.page = 1; loadArticles(); });
  $("a-search").addEventListener("input", debounce(function () { A.q = $("a-search").value.trim(); A.page = 1; loadArticles(); }, 300));
  $("a-new").addEventListener("click", function () { openArticle(null); });

  function loadArticles() {
    api("articles/?" + qs(A)).then(function (d) {
      var tbody = $("a-rows");
      if (!d.results.length) emptyRow(tbody, 6, "No articles yet. Use New article to write the first one.");
      else {
        clear(tbody);
        d.results.forEach(function (a) {
          var open = function () { openArticle(a.id); };
          tbody.appendChild(h("tr", { class: "hd-row", tabindex: "0", role: "button", onclick: open,
            onkeydown: function (e) { if (e.key === "Enter") open(); } }, [
            h("td", { text: a.title + (a.is_featured ? "  (featured)" : "") }), h("td", { text: a.kind_label }), h("td", { text: a.topic_label }),
            h("td", {}, [badge(a.is_published ? "Published" : "Draft", a.is_published ? "hd-on" : "")]),
            h("td", { text: ago(a.updated_at) }), h("td", { text: "Edit" })]));
        });
      }
      pager($("a-pager"), d, function (p) { A.page = p; loadArticles(); });
    }).catch(function (e) { flash(failText(e), true); });
  }

  function openArticle(id) {
    if (id === null) return renderArticleEditor({ id: null, title: "", slug: "", topic: CH.topic[0][0], kind: "GUIDE", summary: "", body: "", video_url: "", keywords: "", is_published: false, is_featured: false, sort_order: 100 }, true);
    api("articles/" + id + "/").then(function (a) { renderArticleEditor(a, true); }).catch(function (e) { flash(failText(e), true); });
  }

  var VIDEO_RE = /^https:\/\/(?:www\.)?(?:youtube\.com\/(?:watch\?v=|embed\/)|youtu\.be\/)[\w-]{11}|^https:\/\/(?:player\.)?vimeo\.com\/(?:video\/)?\d+/;

  function insertAt(ta, before, after, placeholder) {
    var s = ta.selectionStart, e = ta.selectionEnd, sel = ta.value.slice(s, e) || placeholder || "";
    ta.value = ta.value.slice(0, s) + before + sel + after + ta.value.slice(e);
    ta.focus(); ta.selectionStart = s + before.length; ta.selectionEnd = s + before.length + sel.length;
    ta.dispatchEvent(new Event("input"));
  }

  function renderArticleEditor(a, open) {
    var f = h("form", { class: "hd-editor", novalidate: true });
    var inp = function (name, val, type) { var i = h("input", { type: type || "text", name: name }); i.value = val; return i; };
    var title = inp("title", a.title), slug = inp("slug", a.slug);
    var topic = fill(h("select", { name: "topic" }), CH.topic, a.topic);
    var kind = fill(h("select", { name: "kind" }), CH.article_kind, a.kind);
    var summary = h("textarea", { name: "summary", rows: "2", maxlength: "300" }); summary.value = a.summary;
    var video = inp("video_url", a.video_url, "url"); video.placeholder = "https://www.youtube.com/watch?v=...";
    var videoNote = h("p", { class: "hd-hint" });
    var body = h("textarea", { name: "body", rows: "12" }); body.value = a.body;
    var keywords = inp("keywords", a.keywords);
    var order = inp("sort_order", a.sort_order, "number");
    var featured = h("input", { type: "checkbox", name: "is_featured" }); featured.checked = a.is_featured;
    var preview = h("iframe", { class: "hd-preview", sandbox: "", title: "Preview" });

    function checkVideo() {
      var v = video.value.trim();
      videoNote.textContent = !v ? "Upload to YouTube (unlisted works) or Vimeo, then paste the link." : VIDEO_RE.test(v) ? "Video link recognised." : "This doesn't look like a YouTube or Vimeo link.";
    }
    var renderPreview = debounce(function () {
      preview.srcdoc = '<style>body{font:15px/1.55 system-ui,sans-serif;padding:14px;color:#0f172a}ol,ul{padding-left:22px}</style>' + body.value;
    }, 250);
    body.addEventListener("input", renderPreview); video.addEventListener("input", checkVideo);
    checkVideo(); renderPreview();

    var tb = h("div", { class: "hd-toolbar" }, [
      ["Heading", function () { insertAt(body, "<h2>", "</h2>", "Heading"); }],
      ["Bold", function () { insertAt(body, "<strong>", "</strong>", "text"); }],
      ["Steps", function () { insertAt(body, "<ol>\n  <li>", "</li>\n  <li>Next step</li>\n</ol>", "First step"); }],
      ["Bullets", function () { insertAt(body, "<ul>\n  <li>", "</li>\n  <li>Another point</li>\n</ul>", "First point"); }],
      ["Link", function () {
        var url = prompt("Link address (https://... or /support/help/...)");
        if (url && (/^https:\/\//.test(url) || url.charAt(0) === "/")) insertAt(body, '<a href="' + url.replace(/"/g, "") + '">', "</a>", "link text");
      }]
    ].map(function (b) { return h("button", { type: "button", text: b[0], onclick: b[1] }); }));

    f.appendChild(field("Title", title));
    f.appendChild(h("div", { class: "hd-two" }, [field("Type", kind), field("Topic", topic)]));
    f.appendChild(field("Summary", summary, "One or two sentences shown in search results."));
    f.appendChild(field("Video link", video)); f.appendChild(videoNote);
    f.appendChild(h("div", { class: "hd-field" }, [h("label", { text: "Article text (HTML)" }), tb, body,
      h("p", { class: "hd-hint", text: "Use Steps for how-to guides. Customers see exactly what the preview shows." })]));
    f.appendChild(h("div", { class: "hd-field" }, [h("label", { text: "Preview" }), preview]));
    f.appendChild(field("Search keywords", keywords, "Words customers might type that aren't in the title, e.g. till, cashup, mpesa."));
    f.appendChild(h("div", { class: "hd-two" }, [field("Web address ending", slug, "Leave blank to create it from the title."), field("Sort order", order, "Lower numbers appear first.")]));
    f.appendChild(h("label", { class: "hd-check" }, [featured, "Show under Popular guides on the support home page"]));

    var save = function (publish) {
      var data = { title: title.value, slug: slug.value, topic: topic.value, kind: kind.value, summary: summary.value,
        body: body.value, video_url: video.value.trim(), keywords: keywords.value, sort_order: order.value,
        is_featured: featured.checked, is_published: publish };
      if (a.id) data.id = a.id;
      api("articles/save/", { method: "POST", body: form(data) }).then(function (d) {
        flash(publish ? "Published." : "Saved.");
        loadArticles(); loadSummary(); renderArticleEditor(d.article, false);
      }).catch(function (e) { showErrors(f, e); });
    };
    var bar = h("div", { class: "hd-actions" });
    if (a.is_published) {
      bar.appendChild(h("button", { type: "button", class: "hd-primary", text: "Save changes", onclick: function () { save(true); } }));
      bar.appendChild(h("button", { type: "button", class: "hd-btn", text: "Unpublish", onclick: function () { save(false); } }));
      if (a.public_url) bar.appendChild(h("a", { class: "hd-btn", href: a.public_url, target: "_blank", rel: "noopener", text: "View as customer" }));
    } else {
      bar.appendChild(h("button", { type: "button", class: "hd-btn", text: "Save draft", onclick: function () { save(false); } }));
      bar.appendChild(h("button", { type: "button", class: "hd-primary", text: "Publish", onclick: function () { save(true); } }));
    }
    if (a.id) bar.appendChild(h("button", { type: "button", class: "hd-btn danger", text: "Delete", onclick: function () {
      if (!confirm("Delete this article permanently?")) return;
      api("articles/" + a.id + "/delete/", { method: "POST", body: form({}) })
        .then(function () { closeDrawer(); flash("Article deleted."); loadArticles(); loadSummary(); })
        .catch(function (e) { flash(failText(e), true); });
    } }));
    f.appendChild(bar);
    f.addEventListener("submit", function (e) { e.preventDefault(); });

    var heading = a.id ? "Edit article" : "New article";
    if (open) openDrawer(heading, f); else { setDrawer(f); }
  }

  var F = { status: "", q: "", page: 1 };
  fill($("f-status"), CH.feature_status);
  $("f-status").addEventListener("change", function () { F.status = this.value; F.page = 1; loadFeatures(); });
  $("f-search").addEventListener("input", debounce(function () { F.q = $("f-search").value.trim(); F.page = 1; loadFeatures(); }, 300));

  function loadFeatures() {
    api("features/?" + qs(F)).then(function (d) {
      var tbody = $("f-rows");
      if (!d.results.length) emptyRow(tbody, 6, "No feature requests match.");
      else {
        clear(tbody);
        d.results.forEach(function (x) {
          var open = function () { openFeature(x); };
          tbody.appendChild(h("tr", { class: "hd-row", tabindex: "0", role: "button", onclick: open,
            onkeydown: function (e) { if (e.key === "Enter") open(); } }, [
            h("td", { class: "num", text: String(x.votes) }), h("td", { text: x.title + (x.is_visible ? "" : "  (hidden)") }),
            h("td", { text: x.module_label }), h("td", {}, [badge(x.status_label, "hd-f-" + x.status.toLowerCase())]),
            h("td", { text: x.shop || "-" }), h("td", { text: ago(x.created_at) })]));
        });
      }
      pager($("f-pager"), d, function (p) { F.page = p; loadFeatures(); });
    }).catch(function (e) { flash(failText(e), true); });
  }

  function openFeature(x) {
    var f = h("form", { novalidate: true });
    f.appendChild(h("h2", { class: "hd-subject", text: x.title }));
    f.appendChild(h("p", { class: "hd-meta", text: x.votes + " vote" + (x.votes === 1 ? "" : "s") + ". From " + (x.shop || "unknown") + (x.submitted_by ? " (" + x.submitted_by + ")" : "") + (x.ticket ? ". Ticket TKT-" + x.ticket : "") }));
    f.appendChild(h("p", { text: x.description, style: "white-space:pre-wrap" }));
    var status = fill(h("select", { name: "status" }), CH.feature_status, x.status);
    var note = h("input", { type: "text", name: "status_note", maxlength: "200" }); note.value = x.status_note;
    var rel = h("textarea", { name: "release_notes", rows: "4" }); rel.value = x.release_notes;
    var vis = h("input", { type: "checkbox", name: "is_visible" }); vis.checked = x.is_visible;
    f.appendChild(h("div", { class: "hd-panel" }, [
      field("Status", status),
      field("Public note", note, "Shown next to the status, e.g. Planned for the December release."),
      field("Release notes", rel, "Required when setting Released. Emailed to everyone who voted, once."),
      h("label", { class: "hd-check" }, [vis, "Visible to customers (untick to hide spam or duplicates)"]),
      h("button", { type: "submit", class: "hd-primary", text: "Save" })]));
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      if (status.value === "RELEASED" && x.status !== "RELEASED" && !confirm("Setting Released emails everyone who voted. Continue?")) return;
      api("features/" + x.id + "/update/", { method: "POST", body: form({ status: status.value, status_note: note.value, release_notes: rel.value, is_visible: vis.checked }) })
        .then(function (d) { closeDrawer(); flash("Feature request updated."); loadFeatures(); loadSummary(); })
        .catch(function (err) { showErrors(f, err); });
    });
    openDrawer("Feature request", f);
  }

  loadSummary();
  setInterval(function () { if (!document.hidden) loadSummary(); }, 60000);
  show((location.hash || "#tickets").slice(1));
})();
