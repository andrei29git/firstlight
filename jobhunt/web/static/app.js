// Small behaviours on top of htmx: sheets (dialogs), search filtering and error toasts.

document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-open]");
  if (opener) {
    document.getElementById(opener.dataset.open)?.showModal();
    return;
  }
  if (event.target.closest("[data-close]")) {
    event.target.closest("dialog")?.close();
    return;
  }
  // A click on the backdrop (the dialog element itself, outside its content) closes the sheet.
  if (event.target instanceof HTMLDialogElement) {
    const box = event.target.getBoundingClientRect();
    const inside = event.clientX >= box.left && event.clientX <= box.right &&
                   event.clientY >= box.top && event.clientY <= box.bottom;
    if (!inside) event.target.close();
  }
});

// The edit sheet is filled by htmx; open it once its content has arrived.
document.addEventListener("htmx:afterSwap", (event) => {
  const target = event.detail.target;
  if (target.id === "sheet" && !target.open) {
    target.showModal();
    target.querySelector("select, input, textarea")?.focus();
  }
});

// Toasts at the foot of the window: error messages, and "Hidden: ... · Undo" after hiding a role.
// The timer pauses while the pointer is over the toast.
let toastAction = null;

function hideToast() {
  const toast = document.getElementById("toast");
  toast.hidden = true;
  toastAction = null;
}

function showToast(text, action = null, ms = 6000) {
  const toast = document.getElementById("toast");
  const message = document.createElement("span");
  message.textContent = text;
  toast.replaceChildren(message);
  toastAction = action;
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "toast-action";
    button.textContent = action.label;
    button.addEventListener("click", () => { hideToast(); action.run(); });
    toast.append(button);
  }
  toast.hidden = false;
  const start = () => { clearTimeout(toast._timer); toast._timer = setTimeout(hideToast, ms); };
  toast.onmouseenter = () => clearTimeout(toast._timer);
  toast.onmouseleave = start;
  start();
}

// Errors from the server (validation messages) appear as a toast instead of failing silently.
document.addEventListener("htmx:responseError", (event) => {
  showToast(event.detail.xhr.responseText || "Something went wrong");
});

// Hiding a role (or showing it again, on the Hidden list) leaves a placeholder where its row was.
// Undo reverses the change and puts the row back into that placeholder.
document.addEventListener("job-visibility", (event) => {
  const { key, hidden, list_view: listView, layout, label } = event.detail;
  showToast(`${hidden ? "Hidden" : "Shown again"}: ${label}`, {
    label: "Undo",
    run: () => {
      const placeholder = [...document.querySelectorAll("tr[data-placeholder]")]
        .find((row) => row.dataset.placeholder === key);
      htmx.ajax("POST", "/job/mark", {
        target: placeholder || "#toast", swap: placeholder ? "outerHTML" : "none",
        values: { key, hidden: hidden ? "0" : "1", list_view: listView, layout: layout || "", undo: "1" },
      });
    },
  }, 8000);
});

// Jobs: "Marked as applied: Stripe, Software Engineer New Grad · Undo". The role is on the
// Applications page now; Undo puts it back (as it was: not applied, or To apply) in its place.
document.addEventListener("job-applied", (event) => {
  const { key, previous, list_view: listView, layout, label } = event.detail;
  showToast(`Marked as applied: ${label}`, {
    label: "Undo",
    run: () => {
      const placeholder = [...document.querySelectorAll("tr[data-placeholder]")]
        .find((row) => row.dataset.placeholder === key);
      htmx.ajax("POST", "/job/stage", {
        target: placeholder || "#toast", swap: placeholder ? "outerHTML" : "none",
        values: { key, stage: previous, list_view: listView, layout: layout || "", undo: "1" },
      });
    },
  }, 8000);
});

// "Marked as applied: Stripe, Software Engineer New Grad · Undo" on a stage list the row has left.
// Undo sets the stage back (Applied on is cleared again) and refills the row's placeholder.
document.addEventListener("application-moved", (event) => {
  const { id, label, stage, stage_label: stageLabel, back_to: backTo, list_stage: listStage,
          applied_on: appliedOn } = event.detail;
  const done = stage === "to_apply" ? "Back on Jobs" : stage === "applied" ? "Marked as applied" : `Moved to ${stageLabel}`;
  showToast(`${done}: ${label}`, {
    label: "Undo",
    run: () => {
      const placeholder = document.querySelector(`tr[data-placeholder="app-${id}"]`);
      htmx.ajax("POST", `/applications/${id}`, {
        target: placeholder || "#toast", swap: placeholder ? "outerHTML" : "none",
        values: { stage: backTo, applied_on: appliedOn, list_stage: listStage || "", undo: "1" },
      });
    },
  }, 8000);
});

// A wishlist change sets every star of that company on the page (the clicked row is redrawn by
// the server; other rows of the same company follow here).
document.addEventListener("wishlist-updated", (event) => {
  const { id, on } = event.detail;
  document.querySelectorAll("[data-wishlist-company]").forEach((star) => {
    if (star.dataset.wishlistCompany !== id) return;
    const name = star.dataset.companyName;
    star.classList.toggle("on", on);
    star.setAttribute("aria-pressed", String(on));
    const label = star.querySelector(".menu-label");
    if (label) label.textContent = on ? `Remove ${name} from the wishlist` : `Add ${name} to the wishlist`;
    else if (star.dataset.tip !== undefined) star.dataset.tip = on ? "Remove from the wishlist" : "Add to the wishlist";
    else star.title = on ? `Remove ${name} from the wishlist` : `Add ${name} to the wishlist`;
    const vals = JSON.parse(star.getAttribute("hx-vals"));
    vals.on = on ? "0" : "1";
    star.setAttribute("hx-vals", JSON.stringify(vals));
  });
});

// "Added to wishlist: Mistral · Undo" after a star, a sidebar removal or the Add sheet.
document.addEventListener("wishlist-changed", (event) => {
  const { name, on } = event.detail;
  showToast(`${on ? "Added to wishlist" : "Removed from wishlist"}: ${name}`, {
    label: "Undo",
    run: () => htmx.ajax("POST", "/wishlist/set", {
      target: "#toast", swap: "none", values: { name, on: on ? "0" : "1", undo: "1" },
    }),
  }, 8000);
});

// Blocking a company: its rows leave the page at once (Undo shows them again).
let unblockedHere = false;
document.addEventListener("company-block-updated", (event) => {
  const { id, on } = event.detail;
  const rows = [...document.querySelectorAll("tr[data-company-id]")].filter((r) => r.dataset.companyId === id);
  rows.forEach((row) => { row.hidden = on; });
  if (!on && !rows.length) unblockedHere = true;  // its roles were not on this page: reload to show them
  document.querySelectorAll("details.row-menu[open]").forEach((menu) => menu.removeAttribute("open"));
});

function blockToast(name, on, afterUndo = null) {
  showToast(`${on ? "Blocked" : "Unblocked"}: ${name}`, {
    label: "Undo",
    run: () => {
      const done = htmx.ajax("POST", "/blocked/set", {
        target: "#toast", swap: "none", values: { name, on: on ? "0" : "1", undo: "1" },
      });
      if (afterUndo) done.then(afterUndo);
    },
  }, 8000);
}

document.addEventListener("company-blocked", (event) => blockToast(event.detail.name, event.detail.on));

// Blocked from Job detail: back on the list, with the same Undo.
{
  const params = new URLSearchParams(location.search);
  const name = params.get("blocked_toast");
  if (name) {
    params.delete("blocked_toast");
    const rest = params.toString();
    history.replaceState(null, "", location.pathname + (rest ? `?${rest}` : ""));
    blockToast(name, true, () => location.reload());
  }
}

document.getElementById("sheet")?.addEventListener("close", () => {
  if (unblockedHere) location.reload();
});

// Row menus and More filters close when clicking elsewhere, and on Escape, which returns focus to
// their button (2026-11-19).
const POPOVERS = "details.row-menu[open], details.more-filters[open]";
document.addEventListener("click", (event) => {
  document.querySelectorAll(POPOVERS).forEach((menu) => {
    if (!menu.contains(event.target)) menu.removeAttribute("open");
  });
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || document.querySelector("dialog[open]")) return;
  const open = [...document.querySelectorAll(POPOVERS)];
  if (!open.length) return;
  const focused = open.find((menu) => menu.contains(document.activeElement)) || open[open.length - 1];
  open.forEach((menu) => menu.removeAttribute("open"));
  event.preventDefault();
  event.stopPropagation();
  focused.querySelector("summary").focus();
}, true);

// Cmd+Z (Ctrl+Z) runs the toast's Undo while it is showing.
document.addEventListener("keydown", (event) => {
  const typing = event.target.closest("input, textarea, [contenteditable]");
  if (toastAction && !typing && event.key === "z" && (event.metaKey || event.ctrlKey) && !event.shiftKey) {
    event.preventDefault();
    const action = toastAction;
    hideToast();
    action.run();
  }
});

// Search filters rows on the current page as you type. "/" focuses the search field.
function applySearch() {
  const input = document.getElementById("search");
  if (!input) return;
  const query = input.value.trim().toLowerCase();
  let anyVisible = false;
  document.querySelectorAll("[data-group]").forEach((group) => {
    let visibleInGroup = 0;
    group.querySelectorAll("tr.row").forEach((row) => {
      const match = !query || row.textContent.toLowerCase().includes(query);
      row.hidden = !match;
      if (match) visibleInGroup += 1;
    });
    const hasRows = group.querySelector("tr.row") !== null;
    group.hidden = Boolean(query) && hasRows && visibleInGroup === 0;
    if (visibleInGroup) anyVisible = true;
  });
  const none = document.querySelector(".no-matches");
  if (none) none.hidden = !query || anyVisible;
}

document.addEventListener("input", (event) => {
  if (event.target.id === "search") applySearch();
});

document.addEventListener("htmx:afterSettle", applySearch);

document.addEventListener("keydown", (event) => {
  const typing = event.target.closest("input, textarea, select, [contenteditable]");
  if (event.key === "?" && !typing && !document.querySelector("dialog[open]")) {
    event.preventDefault();
    document.getElementById("shortcuts")?.showModal();
    return;
  }
  if (event.key === "/" && !typing && !document.querySelector("dialog[open]")) {
    event.preventDefault();
    document.getElementById("search")?.focus();
  }
});

// Job rows open the job's page, unless the click was on one of the row's own controls.
document.addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-href]");
  if (row && !event.target.closest("select, button, a, input, label, details")) location.href = row.dataset.href;
});

// Enter in a filter form's text field submits it: with two text fields (search and company) and no
// submit button, the browser would not (2026-11-19).
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.isComposing) return;
  const field = event.target.closest && event.target.closest("form[data-autosubmit] input[type=search], form[data-autosubmit] input[type=text], form[data-autosubmit] input:not([type])");
  if (!field) return;
  event.preventDefault();
  field.form.requestSubmit();
});

// Filter forms submit as soon as a filter changes (search submits on Enter).
document.addEventListener("change", (event) => {
  const form = event.target.closest("form[data-autosubmit]");
  if (form && event.target.name !== "q") form.requestSubmit();
});

// --- Going back from Job detail to exactly where the role was opened ---------------------------
// Each role link carries its list page (view, filters, search, page). List pages remember their
// scroll position; coming back (by the back link or the browser's Back) restores it.
const LIST_PAGES = ["/jobs", "/applications"];
const here = () => location.pathname + location.search;
const storage = {
  get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* private mode */ } },
  remove(key) { try { sessionStorage.removeItem(key); } catch { /* private mode */ } },
};

if (LIST_PAGES.includes(location.pathname)) {
  addEventListener("pagehide", () => storage.set(`scroll:${here()}`, String(scrollY)));
  const navigation = performance.getEntriesByType("navigation")[0];
  const returning = navigation?.type === "back_forward" || storage.get("restore-scroll") === here();
  storage.remove("restore-scroll");
  const saved = Number(storage.get(`scroll:${here()}`));
  if (returning && saved) requestAnimationFrame(() => scrollTo(0, saved));
}

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-back]");
  if (!link || event.metaKey || event.ctrlKey || event.shiftKey) return;
  const target = new URL(link.href);
  // The list is the page before this one: the browser's own Back restores it as it was.
  if (document.referrer === target.href && history.length > 1) {
    event.preventDefault();
    history.back();
    return;
  }
  storage.set("restore-scroll", target.pathname + target.search);
});

// Actions on Job detail (rescore, star, hide, notes, mark as applied) replace the page in the
// history instead of adding entries, so the browser's Back still leads to the list.
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-replace]");
  if (!form || event.defaultPrevented) return;
  event.preventDefault();
  try {
    const response = await fetch(form.action, { method: "POST", body: new FormData(form) });
    if (!response.ok) throw new Error(await response.text());
    location.replace(response.url);
  } catch (error) {
    form.querySelectorAll("button[disabled]").forEach((b) => { b.disabled = false; });
    showToast(error.message || "Something went wrong");
  }
});

// The Companies page redraws its lists after a wishlist or block change (the toast offers Undo).
if (location.pathname === "/companies") {
  const redraw = () => htmx.ajax("GET", location.pathname + location.search, { target: "#companies-body", select: "#companies-body", swap: "outerHTML" });
  document.addEventListener("wishlist-updated", redraw);
  document.addEventListener("company-block-updated", redraw);
}

// --- Jobs: select several roles; move with the keyboard -----------------------------------------
// Each row has a checkbox (x selects the row under the keyboard cursor); the bar acts on all the
// selected roles at once, with one Undo. j and k move, Enter opens the role.
if (location.pathname === "/jobs") {
  const rows = () => [...document.querySelectorAll("table.jobs tbody tr.row.job:not([hidden])")];
  const selected = () => rows().filter((r) => r.querySelector(".row-select")?.checked);
  const bar = document.querySelector(".bulk-bar");
  const refreshBar = () => {
    const chosen = selected();
    rows().forEach((r) => r.classList.toggle("is-selected", Boolean(r.querySelector(".row-select")?.checked)));
    if (!bar) return;
    bar.hidden = chosen.length === 0;
    bar.querySelector(".bulk-count").textContent = `${chosen.length} selected`;
  };
  document.addEventListener("change", (event) => {
    if (event.target.matches(".select-page")) {
      rows().forEach((r) => { const box = r.querySelector(".row-select"); if (box) box.checked = event.target.checked; });
    }
    if (event.target.matches(".row-select, .select-page")) refreshBar();
  });

  const send = (action, keys, extra = {}) => htmx.ajax("POST", "/jobs/bulk", {
    target: "#toast", swap: "none",
    values: { action, keys, list_view: new URLSearchParams(location.search).get("view") || "scored", ...extra },
  });
  bar?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-bulk]");
    if (!button) return;
    const chosen = selected();
    if (button.dataset.bulk === "clear") {
      chosen.forEach((r) => { r.querySelector(".row-select").checked = false; });
      document.querySelector(".select-page").checked = false;
      refreshBar();
      return;
    }
    if (!chosen.length) return;
    if (button.dataset.bulk === "block") {
      const wished = [...new Set(chosen.filter((r) => r.dataset.wishlist === "1").map((r) => r.dataset.company))];
      if (wished.length && !confirm(`${wished.join(", ")} ${wished.length === 1 ? "is" : "are"} on your wishlist. ` +
                                    "Block anyway? They leave the wishlist (Undo puts them back).")) return;
    }
    send(button.dataset.bulk, chosen.map((r) => r.dataset.key));
  });

  document.addEventListener("jobs-bulk", (event) => {
    const { action, undo, keys, previous, companies, names } = event.detail;
    const affected = action === "block"
      ? rows().concat([...document.querySelectorAll("tr.row.job[hidden]")]).filter((r) => companies.includes(r.dataset.companyId))
      : [...document.querySelectorAll("tr.row.job")].filter((r) => keys.includes(r.dataset.key));
    affected.forEach((r) => {
      r.hidden = !undo;
      const box = r.querySelector(".row-select");
      if (box) box.checked = false;
    });
    document.querySelector(".select-page") && (document.querySelector(".select-page").checked = false);
    refreshBar();
    if (undo) return;
    const what = action === "hide" ? `Hidden: ${keys.length} role${keys.length === 1 ? "" : "s"}`
      : action === "applied" ? `Marked as applied: ${keys.length} role${keys.length === 1 ? "" : "s"}`
        : `Blocked: ${names.join(", ")}`;
    showToast(what, {
      label: "Undo",
      run: () => send(action, keys, { undo: "1", previous: JSON.stringify(previous) }),
    }, 8000);
  });

  let cursor = -1;
  const moveTo = (index) => {
    const list = rows();
    if (!list.length) return;
    cursor = Math.max(0, Math.min(list.length - 1, index));
    list.forEach((r, i) => r.classList.toggle("kb-focus", i === cursor));
    list[cursor].scrollIntoView({ block: "nearest" });
  };
  document.addEventListener("keydown", (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.target.closest("input, textarea, select, [contenteditable]") || document.querySelector("dialog[open]")) return;
    const list = rows();
    if (event.key === "j") { event.preventDefault(); moveTo(cursor + 1); }
    else if (event.key === "k") { event.preventDefault(); moveTo(cursor - 1); }
    else if (event.key === "Enter" && list[cursor]) { event.preventDefault(); location.href = list[cursor].dataset.href; }
    else if (event.key === "x" && list[cursor]) {
      event.preventDefault();
      const box = list[cursor].querySelector(".row-select");
      box.checked = !box.checked;
      refreshBar();
    }
  });
}

// Copy to the clipboard: a fixed text (data-copy) or what an element holds now (data-copy-from,
// so an answer edited in place is copied as edited).
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy], [data-copy-from]");
  if (!button) return;
  let text = button.dataset.copy;
  if (button.dataset.copyFrom) {
    const source = document.querySelector(button.dataset.copyFrom);
    text = source ? ("value" in source ? source.value : source.textContent) : "";
  }
  navigator.clipboard.writeText(text || "").then(
    () => showToast(button.dataset.copyNote || "Copied: run it in the project folder"),
    () => showToast("Could not copy: select the text instead"));
});

// Appearance: System (follow macOS), Light or Dark, remembered in this browser. The head script
// applies it before the first paint; T cycles through the three.
const THEMES = ["system", "light", "dark"];
function currentTheme() {
  return document.documentElement.dataset.theme || "system";
}
function setTheme(theme) {
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try { theme === "system" ? localStorage.removeItem("jobhunt-theme") : localStorage.setItem("jobhunt-theme", theme); } catch (e) {}
  markTheme();
}
function markTheme() {
  document.querySelectorAll("[data-theme-choice]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.themeChoice === currentTheme())));
}
document.addEventListener("click", (event) => {
  const choice = event.target.closest("[data-theme-choice]");
  if (choice) setTheme(choice.dataset.themeChoice);
});
document.addEventListener("keydown", (event) => {
  const typing = event.target.closest("input, textarea, select, [contenteditable]");
  if ((event.key === "t" || event.key === "T") && !typing && !event.metaKey && !event.ctrlKey && !event.altKey
      && !document.querySelector("dialog[open]")) {
    const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
    setTheme(next);
    showToast(`Appearance: ${next[0].toUpperCase()}${next.slice(1)}`);
  }
});
document.addEventListener("DOMContentLoaded", markTheme);
document.addEventListener("htmx:afterSettle", markTheme);


// Chat (2026-10-29): Enter sends, Shift+Enter adds a new line (and nothing is sent while an
// input method is composing a character).
document.addEventListener("keydown", (event) => {
  const box = event.target.closest?.("textarea[data-enter-sends]");
  if (!box || event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  if (box.value.trim() && !box.disabled) box.form.requestSubmit();
});

// The cover letter's PDF: posted with the letter as it is now, then saved as a file; when it
// cannot be made (placeholders left, more than one page), the reason shows as a toast.
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-download]");
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector("button[type=submit]");
  const label = button?.textContent;
  if (button) { button.disabled = true; button.textContent = "Making the PDF…"; }
  try {
    const response = await fetch(form.action, { method: "POST", body: new FormData(form) });
    if (!response.ok) {
      showToast(await response.text());
      return;
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename\*=UTF-8''([^;]+)/);
    const name = match ? decodeURIComponent(match[1]) : "Cover letter.pdf";
    const url = URL.createObjectURL(await response.blob());
    const link = Object.assign(document.createElement("a"), { href: url, download: name });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    showToast(`Saved ${name}`);
  } catch (error) {
    showToast(`Could not make the PDF: ${error}`);
  } finally {
    if (button) { button.disabled = false; button.textContent = label; }
  }
});

// Date fields show "7 Oct 2026" (2026-11-18): the native date input sits invisibly on the text,
// opens its picker on a click or Enter, and the text follows what is picked.
function longDate(value) {
  if (!value) return "Not set";
  const d = new Date(value + "T00:00:00");
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}
document.addEventListener("click", (event) => {
  const input = event.target.closest("input[data-date-pick]");
  if (input && input.showPicker) { try { input.showPicker(); } catch (e) { /* not allowed here: the field still takes typing */ } }
});
document.addEventListener("keydown", (event) => {
  const input = event.target.closest && event.target.closest("input[data-date-pick]");
  if (input && (event.key === "Enter" || event.key === " ") && input.showPicker) { event.preventDefault(); try { input.showPicker(); } catch (e) { /* ignore */ } }
});
document.addEventListener("input", (event) => {
  const input = event.target.closest && event.target.closest("input[data-date-pick]");
  if (input) input.nextElementSibling.textContent = longDate(input.value);
});

// Sidebar groups that remember whether they are open (the wishlist, 2026-11-18). A group opened by
// the page itself (a wishlist company's view) stays open without changing what is remembered.
function restoreGroups(root = document) {
  root.querySelectorAll("details[data-remember]").forEach((group) => {
    let saved = null;
    try { saved = localStorage.getItem("jobhunt-" + group.dataset.remember); } catch (e) {}
    if (saved === "open") group.open = true;
    if (group.dataset.bound) return;
    group.dataset.bound = "1";
    group.querySelector("summary").addEventListener("click", () => {
      // Runs before the toggle: the new state is the opposite of the current one.
      try { localStorage.setItem("jobhunt-" + group.dataset.remember, group.open ? "closed" : "open"); } catch (e) {}
    });
  });
}
document.addEventListener("DOMContentLoaded", () => restoreGroups());
document.addEventListener("htmx:afterSettle", () => restoreGroups());

// Notes that save as you type: say when they were saved, or that they were not.
document.addEventListener("htmx:afterRequest", (event) => {
  const form = event.detail.elt.closest && event.detail.elt.closest("form[data-autosave]");
  if (!form) return;
  const status = form.querySelector(".autosave-status");
  if (!status) return;
  if (event.detail.successful) {
    const now = new Date();
    status.textContent = "Saved " + now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    status.classList.remove("error-text");
  } else {
    status.textContent = "Not saved: the app did not answer. Your text is still here.";
    status.classList.add("error-text");
  }
});

// Company set-ups (2026-11-18): once one starts, the sidebar watches for it; when it finishes with
// the sheet closed, a notice says so and opens it again.
document.addEventListener("setup-started", () => {
  if (document.querySelector("[hx-get='/companies/setup/watch']")) return;
  const watch = document.createElement("div");
  watch.hidden = true;
  watch.setAttribute("hx-get", "/companies/setup/watch");
  watch.setAttribute("hx-trigger", "every 3s");
  watch.setAttribute("hx-swap", "none");
  document.body.append(watch);
  htmx.process(watch);
});
document.addEventListener("setup-finished", (event) => {
  (event.detail.items || []).forEach((item) => {
    showToast(item.message, {
      label: "Review",
      run: () => htmx.ajax("GET", "/companies/setup?id=" + encodeURIComponent(item.id), { target: "#sheet", swap: "innerHTML" }),
    }, 12000);
  });
});

// Notes grow with their text (2026-11-19), where the browser cannot size a field to its content.
if (!(window.CSS && CSS.supports("field-sizing", "content"))) {
  const grow = (area) => {
    area.style.height = "auto";
    const cap = 0.75 * window.innerHeight;
    area.style.height = Math.min(area.scrollHeight + 2, cap) + "px";
  };
  document.addEventListener("input", (event) => { if (event.target.matches("textarea[data-autogrow]")) grow(event.target); });
  const growAll = () => document.querySelectorAll("textarea[data-autogrow]").forEach(grow);
  document.addEventListener("DOMContentLoaded", growAll);
  document.addEventListener("htmx:afterSettle", growAll);
}

// More filters opens under its button, left edges aligned, unless it would run past the window's
// edge; then it ends at the button's right edge instead.
document.addEventListener("toggle", (event) => {
  const menu = event.target;
  if (!(menu instanceof HTMLDetailsElement) || !menu.classList.contains("more-filters") || !menu.open) return;
  const panel = menu.querySelector(".more-panel");
  menu.classList.remove("align-end");
  if (panel.getBoundingClientRect().right > document.documentElement.clientWidth - 16) menu.classList.add("align-end");
}, true);
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("details.more-filters[open]").forEach((menu) => menu.dispatchEvent(new Event("toggle")));
});

// The Company filter lists its companies on a click, like the dropdowns beside it.
document.addEventListener("click", (event) => {
  const input = event.target.closest && event.target.closest("input[data-picker]");
  if (input && input.showPicker) { try { input.showPicker(); } catch (e) { /* not supported for this field */ } }
});

// The sidebar's list fades at an edge it can scroll past (2026-11-19), so a cut-off name reads as
// "more below" rather than as a fault.
function sidebarFades() {
  const list = document.querySelector(".source-list");
  if (!list) return;
  const update = () => {
    list.classList.toggle("fade-top", list.scrollTop > 1);
    list.classList.toggle("fade-bottom", list.scrollTop + list.clientHeight < list.scrollHeight - 1);
  };
  if (!list.dataset.fades) {
    list.dataset.fades = "1";
    list.addEventListener("scroll", update, { passive: true });
    new ResizeObserver(update).observe(list);
    list.addEventListener("toggle", update, true);
  }
  update();
}
document.addEventListener("DOMContentLoaded", sidebarFades);
document.addEventListener("htmx:afterSettle", sidebarFades);

// Job detail's sidebar scrolls on its own only when it does not fit the window (2026-11-19).
function sideFits() {
  const side = document.querySelector(".job-side");
  if (!side) return;
  // scrollHeight is the content's own height whether or not the box is capped, so this is stable.
  const tall = side.scrollHeight > window.innerHeight - 32;
  if (side.classList.contains("tall") !== tall) side.classList.toggle("tall", tall);
}
document.addEventListener("DOMContentLoaded", () => {
  const side = document.querySelector(".job-side");
  if (!side) return;
  sideFits();
  new ResizeObserver(sideFits).observe(side);
  window.addEventListener("resize", sideFits);
});

// A note shown in place by its button (the demo's Rescore, 2026-11-19).
document.addEventListener("click", (event) => {
  const button = event.target.closest && event.target.closest("[data-reveal]");
  if (!button) return;
  const note = document.querySelector(button.dataset.reveal);
  if (note) { note.hidden = false; button.setAttribute("aria-expanded", "true"); }
});
