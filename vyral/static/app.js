"use strict";

const view = document.getElementById("view");
const notice = document.getElementById("notice");
const projectTitle = document.getElementById("project-title");
const crumbSeparator = document.getElementById("crumb-separator");
const projectNav = document.getElementById("project-nav");
const stepsNav = document.getElementById("steps");
const projectsLink = document.getElementById("projects-link");
const settingsLink = document.getElementById("settings-link");
const STEPS = ["Product", "Scripts", "Visuals", "Voice & render", "Videos"];
const MAX_VARIANTS = 10;

const state = { project: null, step: 0, job: null, selected: new Set(), voiceMode: "ai", voice: "af_heart", speed: 1 };

// Builds elements with textContent only, so pasted reviews and AI text can't inject HTML.
function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node && key !== "list") node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

async function api(path, options = {}) {
  const init = { ...options };
  if (options.json !== undefined) {
    init.body = JSON.stringify(options.json);
    init.headers = { "Content-Type": "application/json" };
  }
  const response = await fetch(path, init);
  if (response.status === 401 && !path.startsWith("/api/auth/")) {
    // The session ended, so go back to the sign-in screen.
    state.me = { hosted: true, user: null };
    renderSignIn();
    throw new Error("Please sign in.");
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch { /* keep the generic message */ }
    throw new Error(detail);
  }
  return response;
}
const apiJson = async (path, options) => (await api(path, options)).json();

// A built-in dialog, because embedded browsers can silently block confirm().
function askConfirm(message, actionLabel) {
  return new Promise((resolve) => {
    const dialog = h("dialog", { class: "confirm" });
    const finish = (answer) => { dialog.remove(); resolve(answer); };
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); finish(false); });
    const cancel = h("button", { class: "btn", type: "button", onclick: () => finish(false) }, "Cancel");
    dialog.append(
      h("p", {}, message),
      h("div", { class: "row confirm-actions" }, cancel,
        h("button", { class: "btn danger", type: "button", onclick: () => finish(true) }, actionLabel)),
    );
    document.body.append(dialog);
    dialog.showModal();
    cancel.focus();
  });
}

// Stops the browser streaming videos, because Windows can't delete a file in use.
function releaseVideos() {
  document.querySelectorAll("video").forEach((video) => {
    video.pause();
    video.removeAttribute("src");
    video.load();
  });
}

function showError(error) {
  notice.textContent = error.message || String(error);
  notice.hidden = false;
}
function clearError() { notice.hidden = true; }
function guarded(work) {
  return async (event) => {
    clearError();
    try { await work(event); } catch (error) { showError(error); }
  };
}

const pid = () => state.project.id;
let saveQueue = Promise.resolve();

/* Home */
/* Accounts */
async function loadMe() {
  state.me = await apiJson("/api/auth/me");
  const signedOut = state.me.hosted && !state.me.user;
  document.body.classList.toggle("signed-out", signedOut);
  document.querySelector(".badge").hidden = state.me.hosted;
  document.querySelector(".sidebar-foot").textContent = state.me.user ? state.me.user.email : "Ad studio on your PC";
  return state.me;
}

function renderSignIn(mode = "signin", message = "") {
  document.body.classList.add("signed-out");
  state.project = null;
  updateChrome("", "");
  hideProjectNav();
  const titles = { signin: "Sign in to Vyral", signup: "Create your account", forgot: "Reset your password", recovery: "Choose a new password" };
  const email = h("input", { type: "email", autocomplete: "email", required: true });
  const password = h("input", {
    type: "password", required: true, minLength: mode === "signin" ? 1 : 8,
    autocomplete: mode === "signin" ? "current-password" : "new-password",
  });
  const submit = h("button", { class: "btn primary", type: "submit" },
    { signin: "Sign in", signup: "Create account", forgot: "Send reset link", recovery: "Save password" }[mode]);
  const form = h("form", {
    onsubmit: guarded(async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        if (mode === "signin") {
          await apiJson("/api/auth/login", { method: "POST", json: { email: email.value, password: password.value } });
          await loadMe();
          location.hash = "";
          await route();
        } else if (mode === "signup") {
          const result = await apiJson("/api/auth/signup", { method: "POST", json: { email: email.value, password: password.value } });
          renderSignIn("signin", result.message);
        } else if (mode === "forgot") {
          const result = await apiJson("/api/auth/forgot", { method: "POST", json: { email: email.value } });
          renderSignIn("signin", result.message);
        } else {
          await apiJson("/api/auth/password", { method: "POST", json: { password: password.value } });
          await loadMe();
          location.hash = "";
          await route();
        }
      } finally {
        submit.disabled = false;
      }
    }),
  },
  mode === "recovery" ? "" : h("label", { class: "field" }, "Email", email),
  mode === "forgot" ? "" : h("label", { class: "field" }, mode === "signin" ? "Password" : "Password (at least 8 characters)", password),
  submit);
  const link = (text, next) => h("button", { class: "link-button", type: "button", onclick: () => { clearError(); renderSignIn(next); } }, text);
  view.replaceChildren(h("div", { class: "auth-card card" },
    h("h1", {}, titles[mode]),
    h("p", { class: "sub" }, mode === "signup" ? "Your projects stay private to your account." : "Create. Test. Scale."),
    message ? h("p", { class: "auth-message", role: "status" }, message) : "",
    form,
    h("div", { class: "auth-links" },
      mode === "signin" ? link("Create an account", "signup") : "",
      mode === "signin" ? link("Forgot your password?", "forgot") : "",
      mode === "signup" || mode === "forgot" ? link("Back to sign in", "signin") : "")));
}

// Email links return with session details in the address, which are moved into secure cookies.
async function handleEmailLink() {
  const params = new URLSearchParams(location.hash.slice(1));
  const clearAddress = () => history.replaceState(null, "", location.pathname);
  if (params.get("error_code") || params.get("error")) {
    clearAddress();
    await loadMe();
    if (!state.me.user) renderSignIn("signin", "That link has expired or was already used. Please request a new one.");
    return true;
  }
  if (!params.get("access_token")) return false;
  const tokens = { access_token: params.get("access_token"), refresh_token: params.get("refresh_token") || "" };
  const type = params.get("type");
  clearAddress();
  await apiJson("/api/auth/session", { method: "POST", json: tokens });
  await loadMe();
  if (type === "recovery") renderSignIn("recovery");
  else await route();
  return true;
}

function accountCard(me) {
  const { usage, limits } = me;
  const deletePassword = h("input", { type: "password", autocomplete: "current-password", "aria-label": "Current password" });
  const newPassword = h("input", { type: "password", autocomplete: "new-password", minLength: 8, "aria-label": "New password" });
  const line = (label, used, total) => h("div", { class: "usage-line" },
    h("div", { class: "row between" }, h("span", {}, label), h("span", { class: "small mono" }, `${used} of ${total}`)),
    h("div", { class: "progress" }, h("div", { style: `width:${Math.min(100, total ? (used / total) * 100 : 100)}%` })));
  return h("div", {},
    h("div", { class: "card" }, h("h2", {}, "Account"),
      h("div", { class: "row between" },
        h("span", {}, me.user.email),
        h("button", {
          class: "btn",
          onclick: guarded(async () => {
            await apiJson("/api/auth/logout", { method: "POST" });
            state.me = { hosted: true, user: null };
            location.hash = "";
            renderSignIn();
          }),
        }, "Sign out"))),
    h("div", { class: "card" }, h("h2", {}, "This month's usage"),
      line("Videos", usage.videos, limits.videos),
      line("AI requests", usage.ai_requests, limits.ai_requests),
      h("p", { class: "small", style: "margin:12px 0 0" }, `Up to ${limits.projects} projects, and ${limits.upload_mb} MB per uploaded file.`)),
    h("div", { class: "card" }, h("h2", {}, "Change password"),
      h("form", {
        class: "row",
        onsubmit: guarded(async (event) => {
          event.preventDefault();
          await apiJson("/api/auth/password", { method: "POST", json: { password: newPassword.value } });
          newPassword.value = "";
          await askConfirm("Your password has been changed.", "OK");
        }),
      }, h("div", { style: "flex:1;min-width:220px" }, newPassword), h("button", { class: "btn", type: "submit" }, "Save password"))),
    h("div", { class: "card" }, h("h2", {}, "Delete account"),
      h("p", { class: "small" }, "This permanently deletes your account, projects, photos, clips and videos. It can't be undone."),
      h("form", {
        class: "row",
        onsubmit: guarded(async (event) => {
          event.preventDefault();
          if (!deletePassword.value) throw new Error("Enter your password to confirm.");
          if (!await askConfirm("Delete your account and everything in it? This can't be undone.", "Delete account")) return;
          await apiJson("/api/account", { method: "DELETE", json: { password: deletePassword.value } });
          state.me = { hosted: true, user: null };
          location.hash = "";
          renderSignIn("signin", "Your account and data have been deleted.");
        }),
      }, h("div", { style: "flex:1;min-width:220px" }, deletePassword), h("button", { class: "btn danger", type: "submit" }, "Delete my account"))));
}

async function renderHome() {
  state.project = null;
  updateChrome("", "projects");
  hideProjectNav();
  const [projects, status] = await Promise.all([apiJson("/api/projects"), apiJson("/api/status")]);
  const nameInput = h("input", { type: "text", placeholder: "Product name", "aria-label": "Product name", maxLength: 120 });
  const create = guarded(async (event) => {
    event.preventDefault();
    if (!nameInput.value.trim()) throw new Error("Enter a product name.");
    const project = await apiJson("/api/projects", { method: "POST", json: { name: nameInput.value } });
    location.hash = project.id;
  });
  const labels = { ffmpeg: "FFmpeg", ffprobe: "FFprobe", claude: "Claude Code", voice_model: "AI voice model" };
  const missing = Object.keys(labels).filter((key) => !status[key]);
  view.replaceChildren(
    h("h1", {}, "Your ad projects"),
    h("p", { class: "sub" }, "Turn one product and your photos or phone footage into captioned vertical ad variations."),
    missing.length ? h("div", { class: "notice" }, `Missing: ${missing.map((key) => labels[key]).join(", ")}. See the README.`) : "",
    h("div", { class: "card" },
      h("form", { class: "row", onsubmit: create },
        h("div", { style: "flex:1;min-width:220px" }, nameInput),
        h("button", { class: "btn primary", type: "submit" }, "New project"))),
    h("div", { class: "card" },
      projects.length ? projects.map((item) => h("div", { class: "project-link" },
        h("a", { href: `#${item.id}` }, item.name),
        h("span", { class: "small" }, `${item.videos} video${item.videos === 1 ? "" : "s"}`)))
      : h("p", { class: "small", style: "margin:0" }, "No projects yet. Create your first one above.")),
  );
}

/* Project shell */
function renderProject() {
  updateChrome(state.project.name, "projects");
  stepsNav.replaceChildren(...STEPS.map((name, index) =>
    h("button", {
      class: `side-link${index === state.step ? " active" : ""}`,
      "aria-current": index === state.step ? "step" : "false",
      onclick: () => { state.step = index; clearError(); renderProject(); },
    }, h("span", { class: "num" }, `0${index + 1}`), name)));
  projectNav.hidden = false;
  const body = [stepProduct, stepScripts, stepVisuals, stepVoice, stepVideos][state.step]();
  view.replaceChildren(jobBanner(), body);
}

// Keeps the breadcrumb and highlighted navigation in step with the current page.
function updateChrome(title, section) {
  projectTitle.textContent = title;
  crumbSeparator.hidden = !title;
  projectsLink.classList.toggle("active", section === "projects");
  settingsLink.classList.toggle("active", section === "settings");
  if (section === "settings") settingsLink.setAttribute("aria-current", "page");
  else settingsLink.removeAttribute("aria-current");
}

function hideProjectNav() {
  projectNav.hidden = true;
  stepsNav.replaceChildren();
}

function setUpThemeToggle() {
  const toggle = document.getElementById("theme-toggle");
  const show = () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    toggle.setAttribute("aria-label", `Switch to ${next} mode`);
    toggle.title = `Switch to ${next} mode`;
  };
  toggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("vyral-theme", next); } catch { /* It still applies until the page reloads. */ }
    show();
  });
  show();
}

function jobBanner() {
  const job = state.job;
  if (!job || job.status === "done") return "";
  if (job.status === "failed") {
    const action = job.kind === "generate"
      ? h("button", { class: "btn", onclick: () => { state.job = null; state.step = 1; renderProject(); } }, "Use another AI or write your own")
      : job.kind === "plan" ? h("button", { class: "btn", onclick: autoPlan }, "Plan automatically instead") : null;
    return h("div", { class: "notice job-failed" }, h("span", {}, job.error || "The job failed."), action);
  }
  const percent = job.total ? Math.round((job.done / job.total) * 100) : 8;
  return h("div", { class: "status", role: "status" },
    h("strong", {}, job.message),
    h("div", { class: "progress" }, h("div", { style: `width:${percent}%` })));
}

const autoPlan = guarded(async () => {
  state.project = await apiJson(`/api/projects/${pid()}/auto-plan`, { method: "POST" });
  state.job = null;
  renderProject();
});

const jobRunning = () => state.job && (state.job.status === "queued" || state.job.status === "running");

async function followJob(job, onDone) {
  state.job = job;
  renderProject();
  while (jobRunning()) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const projectId = pid();
    state.job = await apiJson(`/api/jobs/${job.id}`);
    state.project = await apiJson(`/api/projects/${projectId}`);
    renderJobOnly();
  }
  if (state.job.status === "done" && onDone) onDone();
  renderProject();
}

// Polling only swaps the banner, so it can't wipe text the user is typing.
function renderJobOnly() {
  const current = view.querySelector(".status");
  if (current && jobRunning()) current.replaceWith(jobBanner());
}

/* Step 1 */
function stepProduct() {
  const product = state.project.product;
  const fields = {};
  const field = (key, label, hint, multiline) => {
    const control = multiline
      ? h("textarea", { value: product[key] || "", rows: multiline })
      : h("input", { type: "text", value: product[key] || "" });
    fields[key] = control;
    return h("label", { class: "field" }, label, hint ? h("span", { class: "hint" }, hint) : null, control);
  };
  const values = () => Object.fromEntries(Object.entries(fields).map(([key, control]) => [key, control.value]));
  const save = async () => {
    state.project = await apiJson(`/api/projects/${pid()}/product`, { method: "PUT", json: values() });
    projectTitle.textContent = state.project.name;
  };
  const hasCopy = state.project.scripts.length > 0;
  return h("div", {},
    h("h1", {}, "Tell Vyral about the product"),
    h("p", { class: "sub" }, "The AI only uses what you enter here, so accurate details make better and safer ads."),
    h("div", { class: "card" },
      h("div", { class: "grid2" }, field("name", "Product name"), field("price", "Price")),
      field("description", "Description", "What it is, what it does and how it's used.", 4),
      field("audience", "Who it's for"),
      field("facts", "Verified facts", "Things you can prove, such as ingredients, specs, certifications or guarantees.", 3),
      field("reviews", "Real customer reviews (optional)", "Paste genuine reviews only. They reveal real pain points and the words customers use.", 5),
      field("link", "Product link (optional)", "For your reference. Vyral doesn't read this page, so paste the details above."),
      h("div", { class: "row" },
        h("button", { class: "btn", onclick: guarded(save) }, "Save"),
        h("button", {
          class: "btn primary",
          disabled: jobRunning(),
          onclick: guarded(async () => {
            if (hasCopy && !await askConfirm("This replaces your current hooks and scripts, including edits and approvals.", "Rewrite scripts")) return;
            await save();
            const job = await apiJson(`/api/projects/${pid()}/generate`, { method: "POST" });
            state.selected.clear();
            await followJob(job, () => { state.step = 1; });
          }),
        }, hasCopy ? "Save and rewrite scripts" : "Save and write scripts"),
        h("button", {
          class: "btn",
          onclick: guarded(async () => {
            await save();
            state.step = 1;
            renderProject();
          }),
        }, "Save and write them another way"),
        writerNote())),
    h("button", {
      class: "btn quiet",
      onclick: guarded(async () => {
        if (!await askConfirm(`Delete "${state.project.name}" and all its clips and videos? This can't be undone.`, "Delete project")) return;
        await api(`/api/projects/${pid()}`, { method: "DELETE" });
        location.hash = "";
      }),
    }, "Delete project"),
  );
}

/* Step 2 */
function stepScripts() {
  const { brief, hooks, scripts } = state.project;
  if (!scripts.length) {
    return h("div", {},
      h("h1", {}, "Get your scripts"),
      h("p", { class: "sub" }, "Write them with AI from the Product step, paste from another AI, or write your own."),
      h("div", { class: "card" }, h("h2", {}, "Write your own"),
        h("p", { class: "small" }, "Start with one hook and one script. You can add more afterwards."),
        h("button", {
          class: "btn",
          onclick: guarded(() => saveNewCopy({ hooks: [blankHook(hooks)], scripts: [blankScript(scripts)] })),
        }, "Start writing")),
      otherAiPanel(true));
  }
  // Saves run in order so quick clicks can't let an older save overwrite a newer one.
  const saveCopy = guarded(() => {
    const projectId = pid();
    saveQueue = saveQueue.catch(() => {}).then(async () => {
      const saved = await apiJson(`/api/projects/${projectId}/copy`, { method: "PUT", json: { hooks, scripts } });
      if (state.project && state.project.id === projectId) state.project = saved;
    });
    return saveQueue;
  });
  const approvedCount = (items) => items.filter((item) => item.approved).length;
  const counter = h("span", { class: "small" });
  const updateCounter = () => { counter.textContent = `${approvedCount(scripts)} scripts and ${approvedCount(hooks)} hooks approved`; };
  updateCounter();
  const approve = (item, label) => h("input", {
    type: "checkbox", checked: item.approved, "aria-label": `Approve ${label}`,
    onchange: (event) => {
      clearError();
      if (event.target.checked && !(item.text ?? item.body).trim()) {
        event.target.checked = false;
        showError(new Error(`Add the wording for ${label} before approving it.`));
        return;
      }
      item.approved = event.target.checked;
      updateCounter();
      saveCopy();
    },
  });
  // Waits for edits already saving, so adding or removing can't overwrite them.
  const changeItems = (next) => guarded(async () => {
    await saveQueue.catch(() => {});
    await saveNewCopy({ hooks, scripts, ...next() });
  });
  const removeButton = (label, next) => h("button", {
    class: "btn quiet", "aria-label": `Remove ${label}`, onclick: changeItems(next),
  }, "Remove");
  const editor = (item, key, label, rows) => h(rows ? "textarea" : "input", {
    ...(rows ? { rows } : { type: "text" }),
    value: item[key], "aria-label": label,
    onchange: (event) => { item[key] = event.target.value; saveCopy(); },
  });
  return h("div", {},
    h("div", { class: "row between" },
      h("div", {}, h("h1", {}, "Review and approve"),
        h("p", { class: "sub" }, "Edit anything, then tick what you're happy with. Only approved items can become videos.")),
      counter),
    state.project.copy_source ? h("p", { class: "small" }, `Written by ${state.project.copy_source}.`) : null,
    brief ? h("div", { class: "card" },
      h("h2", {}, "Brief"), h("p", {}, brief.summary),
      h("div", { class: "grid2" },
        listBlock("Pain points", brief.pain_points),
        listBlock("Selling angles", brief.angles),
        listBlock("Missing information", brief.missing_info))) : null,
    h("div", { class: "card" }, h("h2", {}, "Scripts"),
      scripts.map((script) => h("div", { class: "item" },
        approve(script, script.id),
        h("div", {},
          h("div", { class: "row between" },
            h("div", { class: "row" }, h("strong", {}, script.id), script.angle ? h("span", { class: "tag" }, script.angle) : null),
            scripts.length > 1 ? removeButton(script.id, () => ({ scripts: scripts.filter((item) => item !== script) })) : null),
          editor(script, "body", `${script.id} script`, 4),
          editor(script, "cta", `${script.id} call to action`),
          script.flags.length ? h("div", { class: "flags" }, h("strong", {}, "Verify before publishing"),
            h("ul", {}, script.flags.map((flag) => h("li", {}, flag)))) : null))),
      h("button", { class: "btn", onclick: changeItems(() => ({ scripts: [...scripts, blankScript(scripts)] })) }, "Add script")),
    h("div", { class: "card" }, h("h2", {}, "Hooks"),
      hooks.map((hook) => h("div", { class: "item" },
        approve(hook, hook.id),
        h("div", {},
          h("div", { class: "row between" },
            h("div", { class: "row" }, h("strong", {}, hook.id), hook.angle ? h("span", { class: "tag" }, hook.angle) : null),
            hooks.length > 1 ? removeButton(hook.id, () => ({ hooks: hooks.filter((item) => item !== hook) })) : null),
          editor(hook, "text", `${hook.id} hook`)))),
      h("button", { class: "btn", onclick: changeItems(() => ({ hooks: [...hooks, blankHook(hooks)] })) }, "Add hook")),
    otherAiPanel(false),
    h("button", { class: "btn primary", onclick: () => { state.step = 2; renderProject(); } }, "Next: add visuals"),
  );
}

const nextNumber = (items) => Math.max(0, ...items.map((item) => Number(item.id.slice(1)) || 0)) + 1;
const blankHook = (hooks) => ({ id: `H${nextNumber(hooks)}`, text: "", angle: "", approved: false });
const blankScript = (scripts) => ({ id: `S${nextNumber(scripts)}`, angle: "", body: "", cta: "", flags: [], approved: false });

async function saveNewCopy(copy) {
  state.project = await apiJson(`/api/projects/${pid()}/copy`, { method: "PUT", json: copy });
  state.job = null;
  renderProject();
}

function otherAiPanel(open) {
  const promptBox = h("textarea", { rows: 6, readOnly: true, hidden: true, "aria-label": "Prompt for another AI" });
  const copied = h("span", { class: "small", role: "status" });
  const answer = h("textarea", { rows: 6, placeholder: "Paste the AI's whole reply here", "aria-label": "Answer from another AI" });
  return h("details", { class: "card other-ai", open },
    h("summary", {}, h("strong", {}, "Use another AI (no Claude needed)")),
    h("ol", {},
      h("li", {}, "Copy the prompt. It contains your product details and Vyral's honesty rules."),
      h("li", {}, "Paste it into ChatGPT, Gemini or another AI assistant."),
      h("li", {}, "Paste the whole reply below, then check every claim yourself.")),
    h("div", { class: "row" },
      h("button", {
        class: "btn",
        onclick: guarded(async () => {
          const { prompt } = await apiJson(`/api/projects/${pid()}/script-prompt`);
          promptBox.value = prompt;
          promptBox.hidden = false;
          promptBox.select();
          try {
            await navigator.clipboard.writeText(prompt);
            copied.textContent = "Copied.";
          } catch {
            // Embedded browsers can block the modern clipboard, so try the older way.
            copied.textContent = document.execCommand("copy")
              ? "Copied."
              : "Couldn't copy automatically. Press Ctrl+C to copy the selected text.";
          }
        }),
      }, "Copy prompt"), copied),
    promptBox,
    h("label", { class: "field", style: "margin-top:12px" }, "AI's reply", answer),
    h("button", {
      class: "btn primary",
      onclick: guarded(async () => {
        if (!answer.value.trim()) throw new Error("Paste the AI's reply first.");
        if (state.project.scripts.length && !await askConfirm("This replaces your current hooks and scripts, including edits and approvals.", "Replace scripts")) return;
        state.project = await apiJson(`/api/projects/${pid()}/import-copy`, { method: "POST", json: { text: answer.value } });
        state.selected.clear();
        state.job = null;
        renderProject();
      }),
    }, "Add these scripts"));
}

// Says which AI will write the scripts, filled in once settings have loaded.
function writerNote() {
  const note = h("span", { class: "small" });
  apiJson("/api/settings").then((values) => {
    if (values.hosted && values.has_groq_key === undefined) {
      note.textContent = "Uses one of your monthly AI requests. ";
      return;
    }
    if (values.script_provider === "groq") {
      note.textContent = `Writes with Groq (${values.groq_model}). Product details are sent to Groq. `;
    } else if (values.script_provider === "claude_api") {
      note.textContent = `Writes with the Claude API (${values.anthropic_model}), which is billed to your API key. `;
    } else {
      note.textContent = `Writes with your Claude plan${values.has_groq_key && values.groq_fallback ? ", then Groq if the limit is reached" : ""}. `;
    }
    note.append(h("a", { href: "#settings" }, "Change"));
  }).catch(() => { note.textContent = ""; });
  return note;
}

// Keys from the server's environment, or on any hosted server, can't be edited here.
const keyIsManaged = (values, source) => values.hosted || source === "env";

function anthropicCard(values, save) {
  const managed = keyIsManaged(values, values.anthropic_key_source);
  const keyInput = h("input", { type: "password", autocomplete: "off", placeholder: "sk-ant-...", "aria-label": "Anthropic API key" });
  const saveKey = h("button", { class: "btn primary", type: "submit" }, "Save key");
  const keyForm = h("form", {
    class: "row",
    onsubmit: guarded(async (event) => {
      event.preventDefault();
      if (!keyInput.value.trim()) throw new Error("Paste your Anthropic API key first.");
      saveKey.disabled = true;
      saveKey.textContent = "Checking key...";
      try {
        await apiJson("/api/settings/anthropic-key", { method: "PUT", json: { key: keyInput.value } });
        keyInput.value = "";
        await renderSettings();
      } finally {
        saveKey.disabled = false;
        saveKey.textContent = "Save key";
      }
    }),
  }, h("div", { style: "flex:1;min-width:240px" }, keyInput), saveKey);

  const model = h("select", {
    "aria-label": "Claude model",
    onchange: (event) => save(() => ({ anthropic_model: event.target.value }))(),
  },
  h("option", { value: "claude-opus-5-5" }, "Opus 5.5 ($4 in / $20 out per million tokens)"),
  h("option", { value: "claude-sonnet-5-5" }, "Sonnet 5.5 ($2 in / $10 out per million tokens)"),
  h("option", { value: "claude-haiku-5-5" }, "Haiku 5.5 ($0.10 in / $0.50 out per million tokens)"));
  model.value = values.anthropic_model;

  const budget = h("input", {
    type: "number", min: "0", step: "1", value: values.anthropic_monthly_budget_usd, "aria-label": "Monthly budget in US dollars",
    onchange: (event) => {
      const amount = Number(event.target.value);
      if (!Number.isFinite(amount) || amount < 0) {
        showError(new Error("Enter a budget of 0 or more."));
        return;
      }
      save(() => ({ anthropic_monthly_budget_usd: amount }))();
    },
  });

  return h("div", { class: "card" },
    h("h2", {}, "Claude API"),
    values.has_anthropic_key
      ? h("div", { class: "row between key-status" },
        h("span", { class: "check-ok" }, managed ? "Key set on the server" : `Key saved, ending in ${values.anthropic_key_hint}`),
        managed ? null : h("button", {
          class: "btn quiet",
          onclick: guarded(async () => {
            if (!await askConfirm("Remove your Anthropic API key from this PC?", "Remove key")) return;
            await apiJson("/api/settings/anthropic-key", { method: "DELETE" });
            await renderSettings();
          }),
        }, "Remove key"))
      : managed
        ? h("p", { class: "small" }, "Set ANTHROPIC_API_KEY in the server's environment to use the Claude API.")
        : h("p", { class: "small" }, "Pay per use, separate from your Claude plan. Create a key at ",
          h("a", { href: "https://console.anthropic.com/settings/keys", target: "_blank", rel: "noreferrer" }, "the Anthropic Console"),
          ", then paste it here. Vyral tests it before saving."),
    managed ? null : keyForm,
    managed ? null : h("p", { class: "small", style: "margin:10px 0 0" }, "Your key is stored on this PC in data/settings.json, without encryption. You can use the .env file instead."),
    h("label", { class: "field", style: "margin:14px 0 0" }, "Model", model),
    h("label", { class: "field", style: "margin:14px 0 0" }, "Monthly budget (US dollars)",
      h("span", { class: "hint" }, "Vyral stops Claude API requests once its estimate reaches this. Also set a limit in the Anthropic Console, because that's the real bill."), budget),
    h("p", { class: "small", style: "margin:10px 0 0" },
      `Estimated this month: $${values.anthropic_spent_usd.toFixed(2)} of $${values.anthropic_monthly_budget_usd.toFixed(2)}, from ${values.anthropic_requests} request${values.anthropic_requests === 1 ? "" : "s"}.`));
}

async function renderSettings() {
  state.project = null;
  updateChrome("Settings", "settings");
  hideProjectNav();
  const me = await loadMe();
  if (me.hosted && !me.is_admin) {
    view.replaceChildren(h("h1", {}, "Settings"), h("p", { class: "sub" }, "Manage your account."), accountCard(me));
    return;
  }
  const values = await apiJson("/api/settings");
  const groqManaged = keyIsManaged(values, values.groq_key_source);

  const save = (changes) => guarded(async () => {
    const current = await apiJson("/api/settings");
    await apiJson("/api/settings", {
      method: "PUT",
      json: {
        script_provider: current.script_provider,
        groq_model: current.groq_model,
        groq_fallback: current.groq_fallback,
        ...changes(),
      },
    });
    await renderSettings();
  });

  const provider = h("select", {
    "aria-label": "Script writer",
    onchange: (event) => save(() => ({ script_provider: event.target.value }))(),
  }, values.hosted ? null : h("option", { value: "claude" }, "Claude plan (personal use only)"),
  h("option", { value: "claude_api", disabled: !values.has_anthropic_key }, values.has_anthropic_key ? "Claude API (billed to your key)" : "Claude API (add a key first)"),
  h("option", { value: "groq", disabled: !values.has_groq_key }, values.has_groq_key ? "Groq" : "Groq (add a key first)"));
  provider.value = values.script_provider;

  const keyInput = h("input", {
    type: "password", autocomplete: "off", placeholder: "gsk_...", "aria-label": "Groq API key",
  });
  const keyForm = h("form", {
    class: "row",
    onsubmit: guarded(async (event) => {
      event.preventDefault();
      if (!keyInput.value.trim()) throw new Error("Paste your Groq API key first.");
      saveKey.disabled = true;
      saveKey.textContent = "Checking key...";
      try {
        await apiJson("/api/settings/groq-key", { method: "PUT", json: { key: keyInput.value } });
        keyInput.value = "";
        await renderSettings();
      } finally {
        saveKey.disabled = false;
        saveKey.textContent = "Save key";
      }
    }),
  });
  const saveKey = h("button", { class: "btn primary", type: "submit" }, "Save key");
  keyForm.append(h("div", { style: "flex:1;min-width:240px" }, keyInput), saveKey);

  const model = h("select", {
    "aria-label": "Groq model",
    onchange: (event) => save(() => ({ groq_model: event.target.value }))(),
  }, h("option", { value: values.groq_model }, values.groq_model));
  if (values.has_groq_key) {
    apiJson("/api/settings/groq-models").then(({ models }) => {
      const names = models.includes(values.groq_model) ? models : [values.groq_model, ...models];
      model.replaceChildren(...names.map((name) => h("option", { value: name }, name)));
      model.value = values.groq_model;
    }).catch(showError);
  }

  view.replaceChildren(
    h("h1", {}, "Settings"),
    h("p", { class: "sub" }, "Choose which AI writes your brief, hooks and scripts."),
    me.hosted ? accountCard(me) : "",
    h("div", { class: "card" },
      h("label", { class: "field" }, "Script writer", provider),
      h("label", { class: "check" },
        h("input", {
          type: "checkbox", checked: values.groq_fallback, disabled: !values.has_groq_key,
          onchange: (event) => save(() => ({ groq_fallback: event.target.checked }))(),
        }),
        "Use Groq automatically if a Claude limit or budget is reached"),
      h("p", { class: "small", style: "margin:10px 0 0" }, "Scene planning with photos uses your Claude API key if one is saved, otherwise your Claude plan. Without Claude, choose Plan automatically.")),
    anthropicCard(values, save),
    h("div", { class: "card" },
      h("h2", {}, "Groq"),
      values.has_groq_key
        ? h("div", { class: "row between key-status" },
          h("span", { class: "check-ok" }, groqManaged ? "Key set on the server" : `Key saved, ending in ${values.groq_key_hint}`),
          groqManaged ? null : h("button", {
            class: "btn quiet",
            onclick: guarded(async () => {
              if (!await askConfirm("Remove your Groq key from this PC? Vyral will go back to using Claude.", "Remove key")) return;
              await apiJson("/api/settings/groq-key", { method: "DELETE" });
              await renderSettings();
            }),
          }, "Remove key"))
        : groqManaged
          ? h("p", { class: "small" }, "Set GROQ_API_KEY in the server's environment to use Groq.")
          : h("p", { class: "small" }, "Create a free key at ",
            h("a", { href: "https://console.groq.com/keys", target: "_blank", rel: "noreferrer" }, "console.groq.com/keys"),
            ", then paste it here. Vyral tests it before saving."),
      groqManaged ? null : keyForm,
      groqManaged ? null : h("p", { class: "small", style: "margin:10px 0 0" }, "Your key is stored on this PC in data/settings.json, without encryption. It isn't added to your projects. You can use the .env file instead."),
      values.has_groq_key ? h("label", { class: "field", style: "margin:14px 0 0" }, "Model", model) : null,
      h("p", { class: "small", style: "margin:12px 0 0" }, "When Groq writes, your product details and reviews are sent to Groq. Limits depend on your account. ",
        h("a", { href: "https://console.groq.com/docs/your-data", target: "_blank", rel: "noreferrer" }, "Groq's data policy"))),
  );
}

function listBlock(title, items) {
  if (!items || !items.length) return null;
  return h("div", {}, h("h3", {}, title), h("ul", {}, items.map((item) => h("li", {}, item))));
}

/* Step 3 */
function stepVisuals() {
  const { visual_mode: mode, brand_color: brandColor } = state.project;
  const saveVisuals = (changes) => guarded(async () => {
    state.project = await apiJson(`/api/projects/${pid()}/visuals`, {
      method: "PUT",
      json: { visual_mode: state.project.visual_mode, brand_color: state.project.brand_color, ...changes },
    });
    renderProject();
  });
  const option = (value, title, detail) => h("button", {
    class: `choice${mode === value ? " active" : ""}`, "aria-pressed": String(mode === value),
    onclick: saveVisuals({ visual_mode: value }),
  }, h("strong", {}, title), h("span", { class: "small" }, detail));
  return h("div", {},
    h("h1", {}, "Choose your visuals"),
    h("p", { class: "sub" }, "Make animated ads from product photos, or use your own phone footage."),
    h("div", { class: "choices" },
      option("photos", "Product photos", "No filming needed. Vyral animates your photos."),
      option("clips", "Video clips", "Use footage you filmed yourself.")),
    mode === "photos" ? photoPanel(brandColor, saveVisuals) : stepFootage(),
  );
}

function photoPanel(brandColor, saveVisuals) {
  const { photos } = state.project;
  const input = h("input", {
    type: "file", multiple: true, accept: "image/*,.heic,.heif",
    onchange: guarded(async (event) => {
      const files = [...event.target.files];
      if (!files.length) return;
      const form = new FormData();
      files.forEach((file) => form.append("files", file));
      state.job = { status: "running", message: `Uploading ${files.length} photo${files.length === 1 ? "" : "s"}`, done: 0, total: 0 };
      renderProject();
      try {
        const result = await apiJson(`/api/projects/${pid()}/photos`, { method: "POST", body: form });
        state.project = result.project;
        state.job = null;
        renderProject();
        if (result.rejected.length) showError(new Error(`These files could not be read as photos: ${result.rejected.join(", ")}`));
      } catch (error) {
        state.job = null;
        renderProject();
        throw error;
      }
    }),
  });
  const colorInput = h("input", {
    type: "color", value: brandColor || "#f2703a", "aria-label": "Brand color",
    onchange: (event) => saveVisuals({ brand_color: event.target.value })(),
  });
  return h("div", {},
    h("div", { class: "card" },
      h("label", { class: "dropzone" }, h("strong", {}, "Choose product photos"),
        h("div", { class: "small" }, "Aim for 3–6 clear, well-lit photos: front, close-up, packaging, and in use. JPG, PNG, WEBP or HEIC."), input)),
    photos.length ? h("div", { class: "card" },
      h("h2", {}, `${photos.length} photo${photos.length === 1 ? "" : "s"}`),
      h("div", { class: "photos" }, photos.map((photo) => h("div", { class: "photo" },
        h("img", { src: photoUrl(photo.id), alt: photo.name }),
        h("div", { class: "small photo-name" }, photo.name),
        photo.width < 800 && photo.height < 800 ? h("div", { class: "flags" }, "Low resolution, so it may look blurry.") : null,
        h("button", {
          class: "btn quiet", "aria-label": `Remove ${photo.name}`,
          onclick: guarded(async () => {
            state.project = await apiJson(`/api/projects/${pid()}/photos/${photo.id}`, { method: "DELETE" });
            renderProject();
          }),
        }, "Remove"))))) : null,
    h("div", { class: "card" }, h("h2", {}, "Brand color"),
      h("div", { class: "row" }, colorInput,
        h("span", { class: "small" }, brandColor ? `Using ${brandColor}.` : "Currently picked automatically from your first photo."),
        brandColor ? h("button", { class: "btn quiet", onclick: saveVisuals({ brand_color: null }) }, "Use automatic") : null)),
    photos.length ? h("button", { class: "btn primary", onclick: () => { state.step = 3; renderProject(); } }, "Next: plan and render") : null,
  );
}

const photoUrl = (id) => `/api/projects/${pid()}/photos/${id}`;

function stepFootage() {
  const { clips } = state.project;
  const input = h("input", {
    type: "file", multiple: true, accept: "video/*",
    onchange: guarded(async (event) => {
      const files = [...event.target.files];
      if (!files.length) return;
      const form = new FormData();
      files.forEach((file) => form.append("files", file));
      state.job = { status: "running", message: `Uploading ${files.length} clip${files.length === 1 ? "" : "s"}`, done: 0, total: 0 };
      renderProject();
      try {
        const result = await apiJson(`/api/projects/${pid()}/clips`, { method: "POST", body: form });
        state.project = result.project;
        state.job = null;
        renderProject();
        if (result.rejected.length) showError(new Error(`These files could not be read as video: ${result.rejected.join(", ")}`));
      } catch (error) {
        state.job = null;
        renderProject();
        throw error;
      }
    }),
  });
  const total = clips.reduce((sum, clip) => sum + clip.duration, 0);
  return h("div", {},
    h("p", { class: "sub" }, "Aim for 10–15 clips of 3–10 seconds: the product, close-ups, it being used, and the result."),
    h("div", { class: "card" },
      h("label", { class: "dropzone" }, h("strong", {}, "Choose video clips"),
        h("div", { class: "small" }, "MP4, MOV, M4V, WEBM, MKV or AVI. Clips play in this order."), input)),
    clips.length ? h("div", { class: "card" },
      h("h2", {}, `${clips.length} clip${clips.length === 1 ? "" : "s"}, ${total.toFixed(1)} seconds total`),
      h("table", {}, h("tbody", {}, clips.map((clip) => h("tr", {},
        h("td", {}, clip.name), h("td", {}, `${clip.duration.toFixed(1)}s`),
        h("td", { style: "text-align:right" }, h("button", {
          class: "btn quiet", "aria-label": `Remove ${clip.name}`,
          onclick: guarded(async () => {
            state.project = await apiJson(`/api/projects/${pid()}/clips/${clip.id}`, { method: "DELETE" });
            renderProject();
          }),
        }, "Remove"))))))) : null,
    clips.length ? h("button", { class: "btn primary", onclick: () => { state.step = 3; renderProject(); } }, "Next: voice and render") : null,
  );
}

/* Step 4 */
function stepVoice() {
  const { hooks, scripts, clips, photos, recordings, visual_mode: visualMode, scene_plans: plans } = state.project;
  const usePhotos = visualMode === "photos";
  const approvedHooks = hooks.filter((hook) => hook.approved);
  const approvedScripts = scripts.filter((script) => script.approved);
  if (!approvedHooks.length || !approvedScripts.length) {
    return h("div", { class: "card" }, h("h2", {}, "Approve your scripts first"),
      h("p", { class: "small", style: "margin:0" }, "Approve at least one script and one hook in the Scripts step."));
  }
  if (!(usePhotos ? photos.length : clips.length)) {
    return h("div", { class: "card" }, h("h2", {}, usePhotos ? "Add photos first" : "Add footage first"),
      h("p", { class: "small", style: "margin:0" }, `Upload at least one ${usePhotos ? "photo" : "clip"} in the Visuals step.`));
  }
  const validKeys = new Set(approvedScripts.flatMap((s) => approvedHooks.map((k) => `${s.id}|${k.id}`)));
  state.selected = new Set([...state.selected].filter((key) => validKeys.has(key)));

  const selectedList = () => [...state.selected].map((key) => {
    const [script_id, hook_id] = key.split("|");
    return { script_id, hook_id };
  });
  const renderButton = h("button", { class: "btn primary" });
  const renderHint = h("span", { class: "small" });
  const refreshButton = () => {
    const count = state.selected.size;
    const missing = state.voiceMode === "own" && selectedList().some((v) => !recordings[`${v.script_id}-${v.hook_id}`]);
    renderButton.textContent = `Render ${count} video${count === 1 ? "" : "s"}`;
    const unplanned = usePhotos && selectedList().some((v) => !plans[v.script_id] || !plans[v.hook_id]);
    renderButton.disabled = !count || missing || unplanned || jobRunning();
    renderHint.textContent = unplanned ? "Plan the scenes before rendering." : "Rendering runs on your PC and can take a few minutes per video.";
  };
  renderButton.addEventListener("click", guarded(async () => {
    const job = await apiJson(`/api/projects/${pid()}/render`, {
      method: "POST",
      json: { variants: selectedList(), voice_mode: state.voiceMode, voice: state.voice, speed: state.speed },
    });
    await followJob(job, () => { state.step = 4; });
  }));
  refreshButton();

  const matrix = h("div", { class: "matrix-wrap" }, h("table", { class: "matrix" },
    h("thead", {}, h("tr", {}, h("th", {}, "Script"), approvedHooks.map((hook) => h("th", { title: hook.text }, hook.id)))),
    h("tbody", {}, approvedScripts.map((script) => h("tr", {},
      h("td", {}, h("strong", {}, script.id), script.angle ? ` ${script.angle}` : ""),
      approvedHooks.map((hook) => {
        const key = `${script.id}|${hook.id}`;
        return h("td", {}, h("input", {
          type: "checkbox", checked: state.selected.has(key), "aria-label": `${script.id} with ${hook.id}`,
          onchange: (event) => {
            if (event.target.checked && state.selected.size >= MAX_VARIANTS) {
              event.target.checked = false;
              showError(new Error(`You can render up to ${MAX_VARIANTS} videos at a time.`));
              return;
            }
            clearError();
            if (event.target.checked) state.selected.add(key); else state.selected.delete(key);
            if (state.voiceMode === "own") renderProject(); else refreshButton();
          },
        }));
      })))),
  ));

  const hookKey = h("div", { class: "small", style: "margin-top:10px" },
    approvedHooks.map((hook) => h("div", {}, h("strong", {}, `${hook.id}: `), hook.text)));

  const modeSelect = h("select", {
    "aria-label": "Voice source",
    onchange: (event) => { state.voiceMode = event.target.value; renderProject(); },
  }, h("option", { value: "ai" }, "AI voice (Kokoro, runs on your PC)"), h("option", { value: "own" }, "My own recording"));
  modeSelect.value = state.voiceMode;

  return h("div", {},
    h("h1", {}, "Choose variations and voice"),
    h("p", { class: "sub" }, "Each ticked box becomes one video. Changing one part at a time shows you which hook or script works."),
    h("div", { class: "card" }, h("h2", {}, "Variations"), matrix, hookKey),
    h("div", { class: "card" }, h("h2", {}, "Voice"),
      h("label", { class: "field" }, "Source", modeSelect),
      state.voiceMode === "ai" ? aiVoiceControls() : ownVoiceControls(selectedList(), hooks, scripts, recordings)),
    usePhotos ? scenePanel(approvedHooks, approvedScripts) : null,
    h("div", { class: "row" }, renderButton, renderHint),
  );
}

function scenePanel(approvedHooks, approvedScripts) {
  const { scene_plans: plans, photos } = state.project;
  const items = [...approvedHooks, ...approvedScripts];
  const planned = items.filter((item) => plans[item.id]);
  const planButton = h("button", {
    class: `btn${planned.length === items.length ? "" : " primary"}`,
    disabled: jobRunning(),
    onclick: guarded(async () => {
      if (planned.length && !await askConfirm("This replaces your current scene choices.", "Plan again")) return;
      const job = await apiJson(`/api/projects/${pid()}/plan-scenes`, { method: "POST" });
      await followJob(job);
    }),
  }, planned.length ? "Plan scenes again" : "Plan scenes with Claude");
  const saveScenes = (id) => guarded(() => {
    const projectId = pid();
    saveQueue = saveQueue.catch(() => {}).then(async () => {
      const saved = await apiJson(`/api/projects/${projectId}/scene-plans/${id}`, { method: "PUT", json: plans[id] });
      if (state.project && state.project.id === projectId) state.project = saved;
    });
    return saveQueue;
  });
  const select = (scene, key, label, id, options, afterChange) => {
    const control = h("select", {
      "aria-label": label,
      onchange: (event) => { scene[key] = event.target.value; if (afterChange) afterChange(event.target.value); saveScenes(id)(); },
    }, options.map(([value, text]) => h("option", { value }, text)));
    control.value = scene[key];
    return control;
  };
  const photoOptions = photos.map((photo) => [photo.id, photo.name]);
  const layouts = [["product", "Cut out product"], ["fit", "Whole photo"], ["full", "Full screen"]];
  const motions = [["pop", "Pop in"], ["zoom_in", "Zoom in"], ["zoom_out", "Zoom out"], ["pan_left", "Pan left"], ["pan_right", "Pan right"], ["float", "Float"]];
  return h("div", { class: "card" },
    h("div", { class: "row between" },
      h("div", {}, h("h2", {}, "Scene plan"),
        h("p", { class: "small", style: "margin:0" }, `${planned.length} of ${items.length} approved hooks and scripts planned. "Plan scenes with Claude" sends your photos and lines to Claude, using your API key if saved or otherwise your Claude plan.`)),
      h("div", { class: "row" },
        h("button", {
          class: "btn",
          disabled: jobRunning(),
          onclick: guarded(async () => {
            if (planned.length && !await askConfirm("This replaces your current scene choices.", "Plan again")) return;
            await autoPlan();
          }),
        }, "Plan automatically"),
        planButton)),
    planned.map((item) => h("details", { class: "plan", open: planned.length <= 3 },
      h("summary", {}, h("strong", {}, item.id), ` ${plans[item.id].scenes.length} scene${plans[item.id].scenes.length === 1 ? "" : "s"}`),
      plans[item.id].scenes.map((scene, index) => {
        const thumb = h("img", { src: photoUrl(scene.photo_id), alt: "" });
        return h("div", { class: "scene" }, thumb, h("div", {},
          h("p", { class: "scene-line" }, scene.text),
          h("div", { class: "scene-grid" },
            select(scene, "photo_id", `${item.id} scene ${index + 1} photo`, item.id, photoOptions, (value) => { thumb.src = photoUrl(value); }),
            select(scene, "layout", `${item.id} scene ${index + 1} layout`, item.id, layouts),
            select(scene, "motion", `${item.id} scene ${index + 1} motion`, item.id, motions),
            h("input", {
              type: "text", value: scene.headline, maxLength: 80, placeholder: "No on-screen headline",
              "aria-label": `${item.id} scene ${index + 1} headline`,
              onchange: (event) => { scene.headline = event.target.value; saveScenes(item.id)(); },
            }))));
      }))));
}

function aiVoiceControls() {
  const select = h("select", { "aria-label": "Voice", onchange: (event) => { state.voice = event.target.value; } });
  apiJson("/api/voices").then((voices) => {
    select.replaceChildren(...Object.entries(voices).map(([id, label]) => h("option", { value: id }, label)));
    select.value = state.voice;
  }).catch(showError);
  const speed = h("select", { "aria-label": "Speed", onchange: (event) => { state.speed = Number(event.target.value); } },
    [["0.9", "Slower"], ["1", "Normal"], ["1.1", "Faster"], ["1.2", "Fastest"]].map(([value, label]) => h("option", { value }, label)));
  speed.value = String(state.speed);
  const player = h("audio", { controls: true, hidden: true });
  const previewButton = h("button", {
    class: "btn",
    onclick: guarded(async () => {
      previewButton.disabled = true;
      previewButton.textContent = "Creating preview...";
      try {
        const response = await api("/api/voice-preview", { method: "POST", json: { voice: state.voice, speed: state.speed } });
        player.src = URL.createObjectURL(await response.blob());
        player.hidden = false;
        await player.play();
      } finally {
        previewButton.disabled = false;
        previewButton.textContent = "Preview voice";
      }
    }),
  }, "Preview voice");
  return h("div", {},
    h("div", { class: "grid2" }, h("label", { class: "field" }, "Voice", select), h("label", { class: "field" }, "Speed", speed)),
    h("div", { class: "row" }, previewButton, player),
    h("p", { class: "small", style: "margin:10px 0 0" }, "AI voices may need disclosing. Check the current rules of the platform you advertise on."));
}

function ownVoiceControls(selected, hooks, scripts, recordings) {
  if (!selected.length) return h("p", { class: "small", style: "margin:0" }, "Tick a variation above, then upload your recording for it.");
  return h("div", {}, selected.map((variant) => {
    const key = `${variant.script_id}-${variant.hook_id}`;
    const hook = hooks.find((item) => item.id === variant.hook_id);
    const script = scripts.find((item) => item.id === variant.script_id);
    return h("div", { class: "item", style: "grid-template-columns:1fr" }, h("div", {},
      h("div", { class: "row between" }, h("strong", {}, key),
        recordings[key] ? h("span", { class: "check-ok" }, "Recording added") : h("span", { class: "check-bad" }, "Recording needed")),
      h("p", { style: "margin:6px 0" }, [hook.text, script.body, script.cta].filter(Boolean).join(" ")),
      h("input", {
        type: "file", accept: "audio/*", "aria-label": `Recording for ${key}`,
        onchange: guarded(async (event) => {
          if (!event.target.files[0]) return;
          const form = new FormData();
          form.append("file", event.target.files[0]);
          state.project = await apiJson(`/api/projects/${pid()}/recordings/${key}`, { method: "POST", body: form });
          renderProject();
        }),
      })));
  }));
}

/* Step 5 */
function stepVideos() {
  const { videos } = state.project;
  if (!videos.length) {
    return h("div", { class: "card" }, h("h2", {}, "No videos yet"),
      h("p", { class: "small", style: "margin:0" }, "Render your first batch in the Voice & render step."));
  }
  return h("div", {},
    h("h1", {}, "Your videos"),
    h("p", { class: "sub" }, "Watch each one fully before using it. Check that the product, wording and captions are correct."),
    h("div", { class: "videos" }, videos.map((video) => {
      const url = `/api/projects/${pid()}/videos/${encodeURIComponent(video.file)}`;
      return h("div", { class: "video" },
        h("video", { src: url, controls: true, preload: "metadata" }),
        h("div", { class: "row between", style: "margin-top:8px" },
          h("strong", {}, video.key), h("span", { class: "small" }, `${video.seconds}s`)),
        h("div", { class: "row between", style: "margin-top:8px" },
          h("a", { class: "btn", href: `${url}?download=true`, download: video.file }, "Download"),
          h("button", {
            class: "btn quiet", "aria-label": `Delete ${video.key}`,
            onclick: guarded(async () => {
              if (!await askConfirm(`Delete video ${video.key}? This can't be undone.`, "Delete video")) return;
              releaseVideos();
              try {
                state.project = await apiJson(`/api/projects/${pid()}/videos/${encodeURIComponent(video.file)}`, { method: "DELETE" });
              } finally {
                renderProject();
              }
            }),
          }, "Delete")));
    })),
  );
}

/* Routing */
async function route() {
  clearError();
  try {
    if (await handleEmailLink()) return;
    if (!state.me) await loadMe();
    if (state.me.hosted && !state.me.user) {
      renderSignIn();
      return;
    }
  } catch (error) {
    showError(error);
    return;
  }
  const id = location.hash.slice(1);
  try {
    if (!id) { await renderHome(); return; }
    if (id === "settings") { await renderSettings(); return; }
    state.project = await apiJson(`/api/projects/${id}`);
    state.step = 0;
    state.job = null;
    state.selected = new Set();
    renderProject();
  } catch (error) {
    showError(error);
  }
}

document.getElementById("home").addEventListener("click", (event) => {
  event.preventDefault();
  location.hash = "";
});
window.addEventListener("hashchange", route);
setUpThemeToggle();
route();
