// FormBuddy side panel. Talks to: (1) background.js via chrome.runtime
// messages for reading/filling the active tab, (2) the FormBuddy backend
// via fetch() for all agent reasoning (CORS-enabled for this, see
// api/main.py).

const BACKEND_URL = "http://localhost:8000";

const $ = (id) => document.getElementById(id);

// --- session id, persisted per-install via chrome.storage.local ---
async function getSessionId() {
  const { formbuddy_session_id } = await chrome.storage.local.get("formbuddy_session_id");
  if (formbuddy_session_id) return formbuddy_session_id;
  const id = "ext-" + Math.random().toString(36).slice(2, 10) + "-" + Date.now().toString(36);
  await chrome.storage.local.set({ formbuddy_session_id: id });
  return id;
}

async function resetSessionId() {
  const id = "ext-" + Math.random().toString(36).slice(2, 10) + "-" + Date.now().toString(36);
  await chrome.storage.local.set({ formbuddy_session_id: id });
  return id;
}

let sessionId = null;
let lastKnownFields = null; // the approved plan's fields, cached for the fill step

// --- profile vault (local + server cross-session memory) ---
async function loadProfile() {
  const { formbuddy_profile } = await chrome.storage.local.get("formbuddy_profile");
  $("profile-text").value = formbuddy_profile || "";
  // also pull server profile (saved via remember_user_details sequential Q&A) to keep in sync
  try {
    const r = await fetch(`${BACKEND_URL}/api/profile`);
    if (r.ok) {
      const data = await r.json();
      const prof = data.profile || {};
      if (Object.keys(prof).length && !formbuddy_profile?.trim()) {
        const text = Object.entries(prof).map(([k,v])=>`${k}: ${v}`).join("\n");
        $("profile-text").value = text;
        await chrome.storage.local.set({ formbuddy_profile: text });
      }
    }
  } catch {}
}

async function pushProfileToServer(text) {
  if (!text.trim()) return;
  const obj={};
  text.split("\n").forEach(l=>{ const i=l.indexOf(":"); if(i>-1) obj[l.slice(0,i).trim()]=l.slice(i+1).trim(); });
  try { await fetch(`${BACKEND_URL}/api/profile`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({profile:obj})}); } catch {}
}

$("profile-save").addEventListener("click", async () => {
  const text=$("profile-text").value;
  await chrome.storage.local.set({ formbuddy_profile: text });
  await pushProfileToServer(text);
  $("profile-saved-note").hidden = false;
  setTimeout(() => ($("profile-saved-note").hidden = true), 1500);
});

$("profile-toggle").addEventListener("click", () => {
  const body = $("profile-body");
  body.hidden = !body.hidden;
  $("profile-toggle-icon").textContent = body.hidden ? "▸" : "▾";
});

// --- file vault (auto-upload DB) ---
async function refreshFileList(){
  try{
    const r=await fetch(`${BACKEND_URL}/api/files`);
    if(!r.ok) return;
    const files=await r.json();
    const list=$("file-list");
    if(!files.length){ list.innerHTML='<span class="hint">No files yet — upload citizenship/photo (max 10MB).</span>'; return;}
    list.innerHTML = files.map(f=> `<div style="display:flex; gap:6px; align-items:center; padding:6px; border:1px solid #e2e8f0; border-radius:6px; margin-top:6px; background:#f8fafc;">
      <span style="flex:1; overflow:hidden; text-overflow:ellipsis;"><b>${escapeHtml(f.filename)}</b><br><span class="hint">${escapeHtml(f.stored_as)} · ${(f.size/1024).toFixed(1)}KB</span></span>
      <a href="${BACKEND_URL}/api/files/${encodeURIComponent(f.stored_as)}" target="_blank" style="font-size:11px;">View</a>
      <button data-del="${escapeHtml(f.stored_as)}" class="file-del" style="font-size:11px; color:#991b1b; border:1px solid #fca5a5; border-radius:4px; background:white; padding:2px 6px;">Delete</button>
    </div>`).join("");
    list.querySelectorAll(".file-del").forEach(b=> b.addEventListener("click", async ()=>{
      if(!confirm("Delete "+b.dataset.del+"?")) return;
      await fetch(`${BACKEND_URL}/api/files/${encodeURIComponent(b.dataset.del)}`,{method:"DELETE"});
      refreshFileList();
    }));
  }catch{}
}
$("file-toggle").addEventListener("click", ()=>{
  const body=$("file-body");
  body.hidden=!body.hidden;
  $("file-toggle-icon").textContent=body.hidden?"▸":"▾";
  if(!body.hidden) refreshFileList();
});
$("file-upload").addEventListener("click", async ()=>{
  const inp=$("file-input");
  const note=$("file-upload-note");
  if(!inp.files.length){ note.hidden=false; note.textContent="Choose files first."; setTimeout(()=>note.hidden=true,2000); return;}
  note.hidden=false; note.textContent="Uploading...";
  $("file-upload").disabled=true;
  for(const f of inp.files){
    const fd=new FormData(); fd.append("file", f);
    try{ const r=await fetch(`${BACKEND_URL}/api/files/upload`,{method:"POST", body: fd}); note.textContent= r.ok ? "Uploaded "+f.name+" ✅" : "Failed "+f.name+": HTTP "+r.status; }catch(e){ note.textContent="Backend not reachable at "+BACKEND_URL+" — start it: uv run uvicorn api.main:app --port 8000 ("+e+")"; }
  }
  inp.value=""; $("file-upload").disabled=false;
  setTimeout(()=>note.hidden=true,2500);
  refreshFileList();
});

// --- messaging helpers ---
function addMessage(text, role) {
  const container = $("messages");
  container.hidden = false;
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = text;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

function extractPageFromTab() {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ type: "EXTRACT_PAGE" }, (resp) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      if (!resp || !resp.ok) return reject(new Error((resp && resp.error) || "extraction failed"));
      resolve(resp);
    });
  });
}

function fillFieldsOnTab(fields) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ type: "FILL_FIELDS", fields }, (resp) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      if (!resp || !resp.ok) return reject(new Error((resp && resp.error) || "fill failed"));
      resolve(resp);
    });
  });
}

async function sendChat(message, { pageHtml, pageUrl } = {}) {
  const body = { session_id: sessionId, message };
  if (pageHtml) {
    body.page_html = pageHtml;
    body.page_url = pageUrl;
  }
  const res = await fetch(`${BACKEND_URL}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`chat failed: HTTP ${res.status}`);
  return (await res.json()).reply;
}

async function fetchSession() {
  const res = await fetch(`${BACKEND_URL}/api/session/${encodeURIComponent(sessionId)}`);
  return res.json();
}

// --- main flow ---
$("analyze-btn").addEventListener("click", async () => {
  $("analyze-btn").disabled = true;
  const thinking = addMessage("Reading the page...", "thinking");
  try {
    // Auto-rotate: a session ID can only ever propose once (proposal.py
    // refuses to overwrite submitted/rejected/failed). If the last run
    // ended terminal, start a fresh ID silently so Analyze just works
    // instead of the agent replying "already submitted".
    try {
      const prev = await fetchSession();
      if (prev && ["submitted", "rejected", "submission_failed", "approved"].includes(prev.status)) {
        sessionId = await resetSessionId();
        $("proposal-section").hidden = true;
        $("result-section").hidden = true;
      }
    } catch {}
    const page = await extractPageFromTab();
    thinking.textContent = "FormBuddy is analyzing the form...";

    const profile = (await chrome.storage.local.get("formbuddy_profile")).formbuddy_profile || "";
    const message = profile
      ? `Please analyze the form on this page for me. Here's what you know about me:\n${profile}`
      : "Please analyze the form on this page for me.";

    const reply = await sendChat(message, { pageHtml: page.html, pageUrl: page.url });
    thinking.remove();
    addMessage(reply, "agent");
    await refreshProposalCard();
  } catch (e) {
    thinking.remove();
    addMessage("⚠️ " + e.message, "agent");
  } finally {
    $("analyze-btn").disabled = false;
  }
});

$("reply-send").addEventListener("click", async () => {
  const text = $("reply-text").value.trim();
  if (!text) return;
  $("reply-text").value = "";
  addMessage(text, "user");
  const thinking = addMessage("Thinking...", "thinking");
  try {
    const reply = await sendChat(text);
    thinking.remove();
    addMessage(reply, "agent");
    await refreshProposalCard();
  } catch (e) {
    thinking.remove();
    addMessage("⚠️ " + e.message, "agent");
  }
});

function renderFieldTable(fields) {
  const table = $("proposal-fields");
  table.innerHTML = fields
    .map((f) => {
      const val = f.value === null || f.value === undefined || f.value === "" ? "(empty)" : String(f.value);
      const req = f.required ? " *" : "";
      return `<tr><td>${escapeHtml(f.label)}${req}</td><td>${escapeHtml(val)}</td></tr>`;
    })
    .join("");
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

async function refreshProposalCard() {
  const session = await fetchSession();
  if (session.status !== "pending_approval") {
    $("proposal-section").hidden = true;
    return;
  }
  const p = session.proposal;
  lastKnownFields = p.fields;
  $("proposal-section").hidden = false;
  $("proposal-url").textContent = p.url;
  $("proposal-summary").textContent = p.summary_for_human;
  renderFieldTable(p.fields);
  $("reject-btn").disabled = false;
  $("authorize-btn").disabled = false;
}

$("reject-btn").addEventListener("click", async () => {
  $("reject-btn").disabled = true;
  $("authorize-btn").disabled = true;
  await fetch(`${BACKEND_URL}/api/session/${encodeURIComponent(sessionId)}/decide`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision: "rejected" }),
  });
  $("proposal-section").hidden = true;
  showResult(false, "Rejected. Nothing was filled.");
});

$("authorize-btn").addEventListener("click", async () => {
  $("reject-btn").disabled = true;
  $("authorize-btn").disabled = true;
  $("proposal-section").hidden = true;
  const thinking = addMessage("Filling the form in your tab...", "thinking");

  try {
    const decideRes = await fetch(`${BACKEND_URL}/api/session/${encodeURIComponent(sessionId)}/decide`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision: "approved" }),
    });
    const decideData = await decideRes.json();
    if (decideData.status !== "approved") {
      thinking.remove();
      showResult(false, decideData.message || "Unexpected status after approval.");
      return;
    }

    // Enrich file fields with vault bytes so background can auto-attach via DataTransfer
    const fieldsForFill = await Promise.all(lastKnownFields.map(async f=>{
      if(f.field_type==='file' && f.value){
        try{
          const r=await fetch(`${BACKEND_URL}/api/files/${encodeURIComponent(f.value)}`);
          if(!r.ok) return f; // not in vault -> background will show manual prompt
          const blob=await r.blob();
          const b64=await new Promise((res,rej)=>{
            const reader=new FileReader();
            reader.onload=()=> res(reader.result.split(',')[1]);
            reader.onerror=rej;
            reader.readAsDataURL(blob);
          });
          return {...f, file_b64: b64, file_name: f.value, file_mime: blob.type || 'application/octet-stream'};
        }catch{ return f; }
      }
      return f;
    }));
    const fillResp = await fillFieldsOnTab(fieldsForFill);
    const results = fillResp.results || fillResp;
    const filledTabUrl = fillResp.filled_tab_url || "";
    const diag = fillResp.diag || null;
    const diagLine = diag
      ? `\n\nFiller saw page: ${diag.href || "?"} (${diag.inputs ?? "?"} inputs, #full_name=${JSON.stringify(diag.full_name_val)})`
      : "";
    thinking.remove();

    const filled = results.filter((r) => r.ok && !r.skipped).length;
    const skipped = results.filter((r) => r.skipped).length;
    const failed = results.filter((r) => !r.ok);
    const manualFiles = results.filter((r) => r.manual_file);
    const autoFiles = results.filter((r) => r.auto_file);
    const allOk = failed.length === 0;

    let notes = failed.length
      ? "Failed fields: " + failed.map((f) => `${f.label} (${f.error})`).join("; ")
      : `${filled} filled, ${skipped} left empty (no data)`;
    if (manualFiles.length) {
      const mf = manualFiles.map((f)=> f.warning || (f.label + " needs manual Add file")).join("; ");
      notes += (notes ? " | " : "") + "Manual file step: " + mf;
    }
    if (autoFiles.length) {
      const af = autoFiles.map(f=> f.warning || f.label).join("; ");
      notes += (notes ? " | " : "") + "Auto-attached: " + af;
    }
    if (diag) {
      notes += ` | diag: href=${diag.href || "?"} inputs=${diag.inputs ?? "?"} full_name=${JSON.stringify(diag.full_name_val)}`;
    }

    await fetch(`${BACKEND_URL}/api/session/${encodeURIComponent(sessionId)}/extension-result`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ok: allOk,
        confirmation_text: `${filled}/${lastKnownFields.length} fields filled` + (autoFiles.length ? ` (${autoFiles.length} file(s) auto-attached)` : "") + (manualFiles.length ? ` (${manualFiles.length} manual)` : ""),
        notes,
      }),
    });

    // Post-report verify: Live Server (:5500) reloads the tab when the
    // backend writes session/audit files, wiping a just-reported "success".
    // Re-read one filled value AFTER the report POST to catch it.
    let reloadWarning = "";
    try {
      const probe = (fieldsForFill || []).find((f) => f.value !== false && ["text", "email", "tel", "number", "textarea"].includes(f.field_type) && f.selector);
      const onLiveServer = /:5500(\/|$)/.test(filledTabUrl || "");
      if (probe) {
        const verify = await new Promise((resolve) => {
          chrome.runtime.sendMessage({ type: "VERIFY_VALUES", selectors: [probe.selector] }, (resp) => {
            if (chrome.runtime.lastError) return resolve(null);
            resolve(resp && resp.ok ? resp.values : null);
          });
        });
        const back = verify && verify[0];
        if (back && back.present && (back.value === null || back.value === "")) {
          reloadWarning = `\n\n⚠️ The page appears to have RELOADED after filling (${probe.label} is now empty). Cause: VS Code Live Server (:5500) reloads on backend file writes. Fix: stop Live Server, run: python -m http.server 8001 --directory demo/mock-rsvp — then open http://localhost:8001 and Analyze again.`;
        } else if (onLiveServer) {
          reloadWarning = `\n\n⚠️ You are on Live Server (:5500), which can wipe fills by auto-reloading. If the form looks empty, re-serve with: python -m http.server 8001 --directory demo/mock-rsvp → http://localhost:8001`;
        }
      } else if (/:5500(\/|$)/.test(filledTabUrl || "")) {
        reloadWarning = `\n\n⚠️ You are on Live Server (:5500), which can wipe fills by auto-reloading. If the form looks empty, re-serve with: python -m http.server 8001 --directory demo/mock-rsvp → http://localhost:8001`;
      }
    } catch {}

    if (allOk) {
      const extraAuto = autoFiles.length ? `\n\n📎 ${autoFiles.length} file(s) auto-attached from vault: ${autoFiles.map(f=>f.label).join(", ")} — check the form shows them, then click Submit.` : "";
      const extraManual = manualFiles.length ? `\n\n⚠️ ${manualFiles.length} file field(s) highlighted in orange — vault file not found, please click "Add file" and pick: ${manualFiles.map(f=>f.label+": "+(lastKnownFields.find(x=>x.label===f.label)?.value||"")).join(", ")}. Upload to vault next time for auto-attach.` : "";
      showResult(
        true,
        `✅ ${filled} field(s) filled in your tab. Nothing was submitted — please review the form and click Submit yourself when ready.` + extraAuto + extraManual + (filledTabUrl ? `\n\nFilled tab: ${filledTabUrl} — if this is not the tab you are looking at, switch to it. Fields flash green briefly.` : "") + diagLine + reloadWarning
      );
    } else {
      showResult(false, `⚠️ Some fields could not be filled: ${notes}`);
    }
  } catch (e) {
    thinking.remove();
    showResult(false, "⚠️ " + e.message);
  }
});

function showResult(success, text) {
  const section = $("result-section");
  const body = $("result-body");
  section.hidden = false;
  body.className = success ? "success" : "error";
  body.textContent = text;
}

$("new-session-btn").addEventListener("click", async () => {
  if (!confirm("Start a new session? This clears the current view (nothing is deleted server-side).")) return;
  sessionId = await resetSessionId();
  $("messages").innerHTML = "";
  $("messages").hidden = true;
  $("proposal-section").hidden = true;
  $("result-section").hidden = true;
});

// --- init ---
(async () => {
  sessionId = await getSessionId();
  await loadProfile();
  refreshFileList();
})();
