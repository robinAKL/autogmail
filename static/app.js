(function () {
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
        var oversized = files.filter(function (file) { return file.size > max; });
        if (oversized.length) {
          event.preventDefault();
          window.alert(oversized[0].name + " is larger than " + limitLabel + ". Split the file or upload a smaller one. Vercel cannot accept a request over 4.5 MB.");
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
          body.append("files", files[index], files[index].name);
          var controller = window.AbortController ? new AbortController() : null;
          var timer = controller ? window.setTimeout(function () { controller.abort(); }, 25000) : null;
          fetch(form.action, {
            method: "POST",
            body: body,
            headers: { Accept: "application/json" },
            redirect: "manual",
            signal: controller ? controller.signal : undefined,
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
            window.alert("Could not add " + name + (error && error.name === "AbortError" ? " (it took too long to read)." : ". Keep each file under " + limitLabel + "."));
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
})();
