(function () {
  // Uploads. The hosted site turns away any request over about 4.5 MB, so bigger files go from the browser
  // straight into R2 file storage first (data-direct), and the form then names them in hidden "stored" fields.
  // Without that, files too big are stopped here with a clear message instead of a bare error page.
  var total = parseInt(document.body.getAttribute("data-upload-total") || "0", 10);
  var fileMax = parseInt(document.body.getAttribute("data-attach-max") || "0", 10);
  var direct = document.body.getAttribute("data-direct") === "1";
  var zh = (document.documentElement.getAttribute("lang") || "").indexOf("zh") === 0;
  var mb = function (bytes) { return (bytes / 1000000).toFixed(1) + " MB"; };
  var chosen = function (form) {
    var list = [];
    form.querySelectorAll("input[type=file]").forEach(function (input) {
      Array.prototype.forEach.call(input.files || [], function (file) { list.push(file); });
    });
    return list;
  };
  var problemWith = function (list, alone) {
    var sum = 0;
    for (var i = 0; i < list.length; i++) {
      if (fileMax && list[i].size > fileMax) {
        return zh
          ? list[i].name + " 有 " + mb(list[i].size) + "，每个文件最多 " + mb(fileMax) + "。请压缩或拆分后再上传。"
          : list[i].name + " is " + mb(list[i].size) + ". Files can be up to " + mb(fileMax) + " each. Compress or split it, then try again.";
      }
      sum += list[i].size;
    }
    if (!direct && total && (alone ? Math.max.apply(null, list.map(function (f) { return f.size; }).concat([0])) : sum) > total) {
      return zh
        ? "这次上传共 " + mb(sum) + "，超过了网站一次能收的 " + mb(total) + "。请一次少传几个，或压缩后再传。"
        : "These files add up to " + mb(sum) + ", more than the site can take at once (" + mb(total) + "). Upload fewer at a time, or compress them.";
    }
    return "";
  };
  // Put one file straight into R2. Resolves to what the form names in a hidden "stored" field.
  var putDirect = window.putDirect = function (file) {
    return fetch("/files/direct", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ name: file.name, size: file.size, type: file.type }),
    }).then(function (response) {
      return response.json().then(function (ticket) {
        if (!response.ok || !ticket.ok) { throw new Error(ticket.error || file.name); }
        return fetch(ticket.url, { method: "PUT", body: file, headers: { "Content-Type": ticket.mime } }).then(function (put) {
          if (!put.ok) { throw new Error(file.name + " (" + put.status + ")"); }
          return JSON.stringify({ id: ticket.id, name: ticket.name, mime: ticket.mime });
        }, function () {
          throw new Error(zh
            ? "浏览器没能把 " + file.name + " 传到 R2。请在 Cloudflare R2 → 你的存储桶 → Settings → CORS policy 里允许 " + location.origin + " 使用 PUT。"
            : "The browser could not upload " + file.name + " to R2. In Cloudflare R2, open the bucket's Settings → CORS policy and allow PUT from " + location.origin + ".");
        });
      });
    });
  };
  var directLimit = function () { return direct && total; };
  document.addEventListener("submit", function (event) {
    var form = event.target;
    // The Knowledge form sends its files one by one itself (below).
    if (!form || form.getAttribute("enctype") !== "multipart/form-data" || form.hasAttribute("data-upload-max")) { return; }
    var list = chosen(form);
    if (!list.length) { return; }
    var problem = problemWith(list, false);
    var sum = list.reduce(function (acc, file) { return acc + file.size; }, 0);
    if (!problem && !(directLimit() && sum > total)) { return; }
    event.preventDefault();
    event.stopImmediatePropagation();
    if (problem) {
      form.querySelectorAll("input[type=file]").forEach(function (input) { input.value = ""; });
      window.alert(problem);
      return;
    }
    var submitter = event.submitter;
    if (submitter) {
      submitter.classList.add("busy");
      submitter.textContent = zh ? "正在上传…" : "Uploading…";
    }
    var inputs = Array.prototype.slice.call(form.querySelectorAll("input[type=file]"));
    var step = function (index) {
      if (index >= list.length) {
        inputs.forEach(function (input) { input.value = ""; });
        form.requestSubmit(submitter || undefined);
        return;
      }
      if (submitter) { submitter.textContent = (zh ? "正在上传 " : "Uploading ") + (index + 1) + " / " + list.length + "…"; }
      putDirect(list[index]).then(function (stored) {
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = "stored";
        hidden.value = stored;
        form.appendChild(hidden);
        step(index + 1);
      }).catch(function (error) {
        window.alert(error.message);
        window.location.reload();
      });
    };
    step(0);
  }, true);

  // Confirm before anything that sends or removes, and show that work is under way.
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var button = event.submitter || form.querySelector("button[type=submit]");
      var message = form.getAttribute("data-confirm");
      var skip = button && button.hasAttribute("data-skip-confirm");
      if (message && !skip && !window.confirm(message)) {
        event.preventDefault();
        return;
      }
      var busy = form.getAttribute("data-busy");
      if (button && busy) {
        // Disabling would drop the button's own name/value from the submission, so only mark it.
        window.setTimeout(function () {
          button.classList.add("busy");
          button.setAttribute("aria-busy", "true");
          button.textContent = busy;
        }, 0);
      }
    });
  });

  // The reading frame grows to the height of the letter inside it.
  document.querySelectorAll("iframe[data-autosize]").forEach(function (frame) {
    function fit() {
      try {
        var doc = frame.contentDocument;
        if (!doc || !doc.documentElement) { return; }
        var height = Math.max(doc.documentElement.scrollHeight, doc.body ? doc.body.scrollHeight : 0);
        frame.style.height = Math.min(Math.max(height, 160), 6000) + "px";
      } catch (error) {
        frame.style.height = "70vh";
      }
    }
    frame.addEventListener("load", function () {
      fit();
      try {
        var doc = frame.contentDocument;
        if (window.ResizeObserver && doc && doc.body) {
          new ResizeObserver(fit).observe(doc.body);
        }
        Array.prototype.forEach.call(doc.images || [], function (image) {
          image.addEventListener("load", fit);
        });
      } catch (error) { /* the frame stays at its fallback height */ }
      window.setTimeout(fit, 600);
    });
  });

  // Keep the opened letter in view in the list after a page load.
  var current = document.querySelector(".row[data-current]");
  var list = current && current.closest(".list");
  if (list) {
    var top = current.offsetTop - list.offsetTop;
    if (top < list.scrollTop || top + current.offsetHeight > list.scrollTop + list.clientHeight) {
      list.scrollTop = Math.max(0, top - 12);
    }
  }

  // Sending modes need an explicit yes; draft-only does not.
  var modeForm = document.querySelector("[data-mode-form]");
  if (modeForm) {
    var row = modeForm.querySelector("[data-confirm-row]");
    var sync = function () {
      var chosen = modeForm.querySelector("input[name=mode]:checked");
      if (row) { row.hidden = !chosen || chosen.getAttribute("data-sends") !== "1"; }
    };
    modeForm.addEventListener("change", sync);
    sync();
  }

  // File picker: show what was chosen, and accept a drop.
  document.querySelectorAll("[data-drop]").forEach(function (zone) {
    var input = zone.querySelector("input[type=file]");
    var label = zone.querySelector("[data-drop-label]");
    var form = zone.closest("form");
    var max = form ? parseInt(form.getAttribute("data-upload-max") || "0", 10) : 0;
    var limitLabel = form ? (form.getAttribute("data-upload-label") || "4 MB") : "4 MB";
    var show = function () {
      var files = input.files ? Array.prototype.slice.call(input.files) : [];
      if (!files.length) { return; }
      var oversized = files.filter(function (file) { return max && file.size > max; });
      if (oversized.length) {
        label.textContent = oversized[0].name + " is larger than " + limitLabel;
        input.setCustomValidity("Each file must be under " + limitLabel + " on this hosted site.");
      } else {
        input.setCustomValidity("");
        label.textContent = files.length === 1 ? files[0].name : files.length + " files chosen";
      }
    };
    input.addEventListener("change", show);
    if (form && max) {
      form.addEventListener("submit", function (event) {
        var files = input.files ? Array.prototype.slice.call(input.files) : [];
        var oversized = files.filter(function (file) { return file.size > (direct ? fileMax : max); });
        if (oversized.length) {
          event.preventDefault();
          window.alert(direct
            ? oversized[0].name + " is larger than " + mb(fileMax) + ". Split the file or upload a smaller one."
            : oversized[0].name + " is larger than " + limitLabel + ". Split the file or upload a smaller one. Vercel cannot accept a request over 4.5 MB.");
          return;
        }
        if (!files.length) { return; }
        event.preventDefault();
        event.stopImmediatePropagation();
        var button = form.querySelector("button[type=submit]");
        var send = function (index) {
          if (index >= files.length) {
            window.location.href = form.getAttribute("data-next") || "/knowledge#documents";
            return;
          }
          if (button) {
            button.classList.add("busy");
            button.textContent = "Saving " + (index + 1) + " of " + files.length + "…";
          }
          var body = new FormData();
          var role = form.querySelector("[name=role]");
          if (role) { body.append("role", role.value); }
          var controller = window.AbortController ? new AbortController() : null;
          var timer = null;
          var ready = direct && files[index].size > max
            ? putDirect(files[index]).then(function (stored) { body.append("stored", stored); })
            : Promise.resolve(body.append("files", files[index], files[index].name));
          ready.then(function () {
            timer = controller ? window.setTimeout(function () { controller.abort(); }, 55000) : null;
            return fetch(form.action, {
              method: "POST",
              body: body,
              headers: { Accept: "application/json" },
              redirect: "manual",
              signal: controller ? controller.signal : undefined,
            });
          }).then(function (response) {
            if (timer) { window.clearTimeout(timer); }
            return response.json().then(function (payload) {
              if (!response.ok || (payload && payload.ok === false)) {
                throw new Error((payload && payload.error) || files[index].name);
              }
              send(index + 1);
            });
          }).catch(function (error) {
            if (timer) { window.clearTimeout(timer); }
            var name = files[index].name;
            window.alert(error && error.name === "AbortError" ? "Could not add " + name + " (it took too long to read)." : (error && error.message && error.message !== name ? error.message : "Could not add " + name + "."));
            if (button) {
              button.classList.remove("busy");
              button.textContent = "Add to knowledge";
            }
          });
        };
        send(0);
      }, true);
    }
    ["dragenter", "dragover"].forEach(function (name) {
      zone.addEventListener(name, function () { zone.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (name) {
      zone.addEventListener(name, function () { zone.classList.remove("over"); });
    });
  });

  // Hover details for chart marks.
  var tip = document.getElementById("tip");
  if (tip) {
    document.querySelectorAll("[data-tip]").forEach(function (mark) {
      mark.addEventListener("mousemove", function (event) {
        tip.textContent = mark.getAttribute("data-tip");
        tip.hidden = false;
        var x = Math.min(event.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
        var y = Math.max(event.clientY - tip.offsetHeight - 12, 8);
        tip.style.left = x + "px";
        tip.style.top = y + "px";
      });
      mark.addEventListener("mouseleave", function () { tip.hidden = true; });
    });
  }

  // Settings page tabs driven by URL hash so post-save redirects stay on the active tab.
  var settingsTabs = document.querySelector("[data-settings-tabs]");
  if (settingsTabs) {
    var tabs = Array.prototype.slice.call(settingsTabs.querySelectorAll("[data-tab]"));
    var panels = Array.prototype.slice.call(document.querySelectorAll("[data-panel]"));
    var hashToTab = function (hash) {
      var key = (hash || "").replace(/^#/, "").toLowerCase();
      if (key === "replies" || key === "notifications" || key === "interrupt") { return "interrupt"; }
      if (key === "cleanup") { return "cleanup"; }
      if (key === "profile" || key === "voice") { return "voice"; }
      if (key === "mode" || key === "model" || key === "send" || key === "general") { return "send"; }
      return "send";
    };
    var activateTab = function (name) {
      tabs.forEach(function (tab) {
        var on = tab.getAttribute("data-tab") === name;
        tab.classList.toggle("on", on);
        tab.setAttribute("aria-selected", on ? "true" : "false");
      });
      panels.forEach(function (panel) {
        panel.hidden = panel.getAttribute("data-panel") !== name;
      });
    };
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function (event) {
        event.preventDefault();
        var href = tab.getAttribute("href");
        if (window.history && window.history.replaceState && href) {
          window.history.replaceState(null, "", href);
        } else if (href) {
          window.location.hash = href;
        }
        activateTab(tab.getAttribute("data-tab"));
      });
    });
    window.addEventListener("hashchange", function () {
      activateTab(hashToTab(window.location.hash));
    });
    activateTab(hashToTab(window.location.hash));
  }

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") { return; }
    var close = document.querySelector(".matter-drawer a[aria-label='Close storyline'], .matter-drawer-head a");
    if (close && close.href) {
      window.location.href = close.href;
    }
  });
})();
