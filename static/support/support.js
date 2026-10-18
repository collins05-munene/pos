(function () {
  "use strict";

  document.querySelectorAll("[data-support-form]").forEach(function (form) {
    // 1. Capture browser details the server can't see. The server adds the
    //    user agent and app version itself, so this is only supplementary.
    var envField = form.querySelector('input[name="client_env"]');
    if (envField) {
      var from = "";
      try {
        var ref = new URL(document.referrer);
        if (ref.origin === location.origin) from = ref.pathname; // path only, never the query string
      } catch (e) {}
      var tz = "";
      try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) {}
      envField.value = JSON.stringify({
        screen: screen.width + "x" + screen.height,
        viewport: innerWidth + "x" + innerHeight,
        pixel_ratio: window.devicePixelRatio || 1,
        language: navigator.language || "",
        timezone: tz,
        online: navigator.onLine,
        touch: navigator.maxTouchPoints > 0,
        platform: (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "",
        page: from
      });
    }

    // 2. Show relevant help (or similar requests) while the customer types.
    var box = form.querySelector("[data-suggestions]");
    var url = form.getAttribute("data-suggest-url");
    var scope = form.getAttribute("data-suggest-scope") || "articles";
    var sources = form.querySelectorAll("[data-suggest-source]");
    if (box && url && sources.length) {
      var timer = null, last = "";
      var render = function (items) {
        box.replaceChildren();
        if (!items.length) { box.hidden = true; return; }
        var title = document.createElement("p");
        title.textContent = scope === "features"
          ? "Already requested? A vote counts more than a duplicate."
          : "These articles might fix it already";
        var list = document.createElement("ul");
        items.forEach(function (item) {
          var li = document.createElement("li");
          var a = document.createElement("a");
          a.href = item.url; a.target = "_blank"; a.rel = "noopener";
          a.textContent = item.title;
          var note = document.createElement("span");
          note.textContent = item.note || "";
          li.appendChild(a); li.appendChild(note); list.appendChild(li);
        });
        box.appendChild(title); box.appendChild(list); box.hidden = false;
      };
      var run = function () {
        var q = Array.prototype.map.call(sources, function (s) { return s.value; }).join(" ").trim();
        if (q.length < 8 || q === last) return;
        last = q;
        fetch(url + "?scope=" + encodeURIComponent(scope) + "&q=" + encodeURIComponent(q), {
          credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" }
        })
          .then(function (r) { return r.ok ? r.json() : { items: [] }; })
          .then(function (d) { render(d.items || []); })
          .catch(function () {});
      };
      sources.forEach(function (s) {
        s.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(run, 450); });
      });
    }

    // 3. Friendly file check. The server enforces the real limits.
    var input = form.querySelector('input[type="file"]');
    var note = form.querySelector("[data-file-note]");
    if (input && note) {
      input.addEventListener("change", function () {
        var files = Array.prototype.slice.call(input.files || []);
        if (!files.length) { note.textContent = ""; return; }
        var total = files.reduce(function (n, f) { return n + f.size; }, 0);
        note.textContent = files.length + " file" + (files.length > 1 ? "s" : "") +
          " selected (" + (total / 1048576).toFixed(1) + " MB)";
      });
    }
  });
})();
