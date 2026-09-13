// FormBuddy extension service worker. Two jobs:
//   1. Make the toolbar icon open the side panel (standard MV3 pattern).
//   2. Relay EXTRACT_PAGE / FILL_FIELDS requests from the side panel into
//      the active tab via chrome.scripting.executeScript.
//
// Deliberately does NOT declare a persistent content script -- these two
// functions are injected on demand, only when the side panel asks for
// them, keeping the extension's footprint on every page minimal.

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);
});

// --- Functions injected into the page. Everything inside must be
// self-contained (no closures over outer scope -- executeScript serializes
// the function body and runs it in the page's own JS context). ---

function extractPageForInspection() {
  const form = document.querySelector("form");
  const scope = form || document.body;
  return {
    html: scope.outerHTML,
    url: window.location.href,
    title: document.title,
  };
}

function fillFieldsInPage(fields) {
  // fields: [{selector, label, field_type, value, options}]. Never clicks submit.
  // Supports: text/email/tel/number/textarea/date/time/file/radio/checkbox/select/rating/linear_scale/grid
  function findEl(f) {
    let el = null;
    try { el = document.querySelector(f.selector); } catch {}
    // Google Forms text questions: NEVER return the hidden entry input.
    // Its Closure framework ignores programmatic changes there and the
    // visible box stays empty (verified live 2026-09-14: 5 "filled", form
    // visibly empty). Target the visible input in the same listitem instead.
    const textLike = ['text','email','tel','number','textarea','date','time'].includes(f.field_type);
    const m0 = (f.selector && f.selector.match(/entry\.(\d+)/)) || null;
    if (textLike && m0) {
      const holder0 = document.querySelector('[data-params*="' + m0[1] + '"]');
      const li0 = holder0 ? (holder0.closest('[role="listitem"]') || holder0) : null;
      if (li0) {
        const vis0 = li0.querySelector('input[type="text"], input[type="email"], input[type="tel"], input[type="number"], textarea, input:not([type])');
        if (vis0 && vis0.type !== 'hidden') return vis0;
      }
    }
    if (el && el.type === 'hidden' && el.name && el.name.includes('_sentinel')) {
      const li = el.closest('[role="listitem"]');
      if (li) {
        const cand = li.querySelector('input[type="text"], textarea, div[role="textbox"], [contenteditable="true"], [role="radio"], [role="listbox"], input[type="file"], input[type="date"], input[type="time"]');
        if (cand) return cand;
      }
    }
    if (el) {
      if (el.closest('[role="listitem"]') && el.closest('[role="listitem"]').querySelector('[role="radio"]') && (f.field_type === 'select' || f.field_type === 'radio' || f.field_type === 'rating' || f.field_type === 'linear_scale')) return el;
      if (el.tagName === 'SELECT' || el.getAttribute('role') === 'radio' || el.getAttribute('role') === 'textbox' || el.getAttribute('role') === 'checkbox' || el.getAttribute('role') === 'listbox') return el;
      if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.getAttribute('contenteditable') === 'true') return el;
    }
    const m = (f.selector && f.selector.match(/entry\.(\d+)/)) || null;
    const entryFull = m ? m[0] : null;
    const entryNum = m ? m[1] : null;
    if (entryFull) {
      el = document.querySelector('[name="' + entryFull + '"]');
      if (el && el.type !== 'hidden') return el;
      el = document.querySelector('textarea[name="' + entryFull + '"], input[name="' + entryFull + '"]:not([type="hidden"])');
      if (el && el.type !== 'hidden') return el;
      const holder = entryNum ? document.querySelector('[data-params*="' + entryNum + '"]') : null;
      if (holder) {
        const parent = holder.closest('[role="listitem"]') || holder.parentElement;
        if (parent) {
          if (['select','radio','checkbox','rating','linear_scale','grid_radio','grid_checkbox'].includes(f.field_type)) return parent;
          const cand = parent.querySelector('input[type="text"], textarea, input[type="email"], div[role="textbox"], [contenteditable="true"], input[type="file"], input[type="date"], input[type="time"], [role="radio"], [role="checkbox"]');
          if (cand) return cand;
          return parent;
        }
      }
    }
    const labelLower = (f.label || "").toLowerCase().trim();
    if (labelLower) {
      const short = labelLower.slice(0, 18);
      for (const c of document.querySelectorAll('input, textarea, div[role="textbox"], [contenteditable="true"], [role="radio"], [role="checkbox"]')) {
        const aria = (c.getAttribute('aria-label') || c.getAttribute('data-value') || "").toLowerCase();
        if (aria && (aria.includes(labelLower) || labelLower.includes(aria.slice(0, 10)))) return c;
      }
      for (const item of document.querySelectorAll('[role="listitem"]')) {
        const txt = (item.innerText || "").toLowerCase();
        if (txt.includes(short)) {
          const cand = item.querySelector('input, textarea, div[role="textbox"], [contenteditable="true"], [role="radio"], [role="checkbox"], input[type="file"]');
          if (cand) return cand;
          return item;
        }
      }
    }
    return null;
  }

  function fillTextEl(el, value) {
    el.focus && el.focus();
    try { el.style.outline = '3px solid #10b981'; setTimeout(()=>{ try{el.style.outline='';}catch{} }, 3500); } catch {}
    if (el.getAttribute('role') === 'textbox' || el.getAttribute('contenteditable') === 'true') {
      el.textContent = value;
      el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      el.blur && el.blur();
      return;
    }
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
    el.blur && el.blur();
  }

  function clickOption(searchRoot, targetVal) {
    const norm = (s) => s.replace(/\s+/g,' ').trim().toLowerCase();
    const want = norm(String(targetVal));
    // 1) radio with data-value
    let radio = [...searchRoot.querySelectorAll('[role="radio"][data-value]')].find(r => norm(r.getAttribute('data-value')||'') === want || norm(r.getAttribute('aria-label')||'') === want || norm(r.textContent).includes(want));
    if (radio) { radio.click(); return true; }
    // 2) checkbox with data-value
    let cb = [...searchRoot.querySelectorAll('[role="checkbox"][data-value]')].find(r => norm(r.getAttribute('data-value')||'') === want || norm(r.textContent).includes(want));
    if (cb) { const checked = cb.getAttribute('aria-checked') === 'true'; const wantBool = String(targetVal).toLowerCase()==='true' || want==='checked'; if (wantBool!==checked) cb.click(); else if (!checked) cb.click(); return true; }
    // 3) listbox option
    let opt = [...searchRoot.querySelectorAll('[role="option"]')].find(o => norm(o.textContent) === want || norm(o.getAttribute('data-value')||'') === want);
    if (opt) { opt.click(); return true; }
    // 4) any span/label
    let span = [...searchRoot.querySelectorAll('span, label')].find(s => norm(s.textContent) === want);
    if (span) { (span.closest('[role="radio"]')||span.closest('[role="checkbox"]')||span.closest('label')||span).click(); return true; }
    // 5) native select option
    const sel = searchRoot.querySelector('select');
    if (sel) {
      const options = Array.from(sel.options || []);
      const match = options.find(o => norm(o.value) === want || norm(o.text) === want);
      if (match) { sel.value = match.value; sel.dispatchEvent(new Event('change', { bubbles: true })); return true; }
    }
    return false;
  }

  function getGoogleListItem(f) {
    let holder = null;
    if (f.selector && /entry\.(\d+)/.test(f.selector)) {
      const num = f.selector.match(/entry\.(\d+)/)[1];
      holder = document.querySelector('[data-params*="' + num + '"]');
    }
    if (!holder) {
      const lab = (f.label||"").trim();
      if (lab) for (const li of document.querySelectorAll('[role="listitem"]')) {
        const h = li.querySelector('[role="heading"]');
        if (h && h.textContent.trim() === lab) { holder = li; break; }
      }
    }
    return holder ? (holder.closest('[role="listitem"]')||holder) : null;
  }

  const results = [];
  for (const f of fields) {
    if (f.value === null || f.value === undefined || f.value === "") {
      results.push({ label: f.label, ok: true, skipped: true });
      continue;
    }
    try {
      const rawVal = f.value;
      const strVal = String(rawVal).trim();
      // --- FILE --- vault-backed auto-upload via DataTransfer (no manual click)
      if (f.field_type === 'file') {
        let li = getGoogleListItem(f);
        let fileInput = li ? li.querySelector('input[type="file"]') : null;
        if (!fileInput) {
          const el = findEl(f);
          fileInput = el && el.querySelector ? el.querySelector('input[type="file"]') : null;
          if (!fileInput && el && el.type === 'file') fileInput = el;
          if (!fileInput && li) fileInput = li.querySelector('input[type="file"]');
        }
        // If vault provided base64, try fully automatic attach
        if (f.file_b64) {
          try {
            const target = li || fileInput || findEl(f);
            if (target && target.scrollIntoView) target.scrollIntoView({behavior:'smooth', block:'center'});
            if (target && target.style) { target.style.outline='3px solid #10b981'; setTimeout(()=>target.style.outline='', 3500); }
            // Ensure we have a file input to receive the File
            let input = fileInput;
            if (!input) {
              // Google Forms sometimes hides input; try broad search inside listitem
              if (li) input = li.querySelector('input[type="file"]') || li.querySelector('input');
              if (!input) input = document.querySelector('input[type="file"]');
            }
            if (input) {
              const b64 = f.file_b64;
              const bin = atob(b64);
              const bytes = new Uint8Array(bin.length);
              for(let i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);
              const mime = f.file_mime || 'application/octet-stream';
              const name = f.file_name || f.value || 'upload.bin';
              const blob = new Blob([bytes], {type: mime});
              const file = new File([blob], name, {type: mime});
              const dt = new DataTransfer();
              dt.items.add(file);
              input.files = dt.files;
              input.dispatchEvent(new Event('input', {bubbles:true}));
              input.dispatchEvent(new Event('change', {bubbles:true}));
              // Some Google Forms Drive widgets listen on the container
              if (li) li.dispatchEvent(new Event('change', {bubbles:true}));
              // Also try to trigger the Add file button's change handler if present
              results.push({ label: f.label, ok: true, auto_file: true, warning: "Auto-attached '"+name+"' ("+bytes.length+" bytes) to '"+f.label+"' via vault." });
              continue;
            }
          } catch(e) {
            // fall through to manual fallback
          }
        }
        // No vault file or auto-attach failed -> highlight manual but still count as needing manual
        const target = li || fileInput || findEl(f);
        if (target && target.scrollIntoView) target.scrollIntoView({behavior:'smooth', block:'center'});
        if (target && target.style) { target.style.outline='3px solid #f59e0b'; setTimeout(()=>target.style.outline='', 4000); }
        let btn = li ? [...li.querySelectorAll('div[role="button"], button')].find(b=>b.textContent.toLowerCase().includes('add file')||b.textContent.toLowerCase().includes('add')) : null;
        if (btn) { try{btn.click();}catch{} }
        // If value was a vault filename but fetch failed, tell user to upload via vault
        const msg = f.file_b64 ? "Auto-attach failed for '"+strVal+"' - please click 'Add file' and pick '"+strVal+"' manually, or re-upload it to the vault." : "File '"+strVal+"' requires manual 'Add file' - not in vault or vault fetch failed. Upload it to the Document Vault first for auto-attach next time.";
        results.push({ label: f.label, ok: true, skipped: false, manual_file: true, warning: msg });
        continue;
      }
      // --- DATE ---
      if (f.field_type === 'date') {
        // Native HTML fast path: selector already points at the input itself
        // (e.g. mock-rsvp #event_date). Old code searched *inside* the input
        // for another input, which always fails since inputs have no children.
        try {
          const direct = f.selector ? document.querySelector(f.selector) : null;
          if (direct && (direct.type === 'date' || (direct.tagName === 'INPUT' && direct.getAttribute('type') === 'date'))) {
            let normDate = strVal;
            const mdy = strVal.match(/(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})/);
            if (mdy) normDate = mdy[3] + '-' + mdy[1].padStart(2,'0') + '-' + mdy[2].padStart(2,'0');
            fillTextEl(direct, normDate);
            results.push({ label: f.label, ok: true }); continue;
          }
        } catch {}
        let li = getGoogleListItem(f) || findEl(f);
        if (!li) { results.push({ label: f.label, ok: false, error: "date container not found" }); continue; }
        const root = li.closest ? (li.closest('[role="listitem"]')||li) : li;
        // Try native date input first
        let dateInput = root.querySelector ? root.querySelector('input[type="date"]') : null;
        if (dateInput) {
          // expect YYYY-MM-DD or MM/DD/YYYY, normalize to YYYY-MM-DD
          let normDate = strVal;
          const mdy = strVal.match(/(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})/);
          if (mdy) normDate = mdy[3] + '-' + mdy[1].padStart(2,'0') + '-' + mdy[2].padStart(2,'0');
          fillTextEl(dateInput, normDate);
          dateInput.dispatchEvent(new Event('change', { bubbles: true }));
          results.push({ label: f.label, ok: true }); continue;
        }
        // Google Forms date: 3 inputs (month, day, year) with aria-label or placeholder
        const inputs = root.querySelectorAll ? [...root.querySelectorAll('input[type="text"], input[type="number"]')] : [];
        // Heuristic: parse date string into month/day/year
        let month='', day='', year='';
        let parsed = strVal.match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
        if (parsed) { year=parsed[1]; month=parsed[2]; day=parsed[3]; }
        else { parsed = strVal.match(/(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})/); if (parsed){ month=parsed[1]; day=parsed[2]; year=parsed[3]; } else { year=strVal; } }
        if (inputs.length >=3) {
          // order in Google Forms is typically Month Day Year
          fillTextEl(inputs[0], month);
          fillTextEl(inputs[1], day);
          fillTextEl(inputs[2], year);
          results.push({ label: f.label, ok: true }); continue;
        } else if (inputs.length===1) {
          fillTextEl(inputs[0], strVal);
          results.push({ label: f.label, ok: true }); continue;
        }
        // Fallback: try contenteditable
        const editable = root.querySelector ? root.querySelector('div[role="textbox"], [contenteditable="true"]') : null;
        if (editable) { fillTextEl(editable, strVal); results.push({ label: f.label, ok: true }); continue; }
        results.push({ label: f.label, ok: false, error: "date: no date inputs found, tried 3-input fallback" }); continue;
      }
      // --- TIME ---
      if (f.field_type === 'time') {
        // Native HTML fast path, same reason as date above.
        try {
          const direct = f.selector ? document.querySelector(f.selector) : null;
          if (direct && (direct.type === 'time' || (direct.tagName === 'INPUT' && direct.getAttribute('type') === 'time'))) {
            let norm = strVal.toLowerCase();
            let h=0,m=0;
            let pm = norm.includes('pm');
            let am = norm.includes('am');
            let hm = norm.match(/(\d{1,2}):(\d{2})/);
            if (hm){ h=parseInt(hm[1]); m=parseInt(hm[2]); }
            else { let hh = norm.match(/(\d{1,2})/); if (hh) h=parseInt(hh[1]); }
            if (pm && h<12) h+=12; if (am && h===12) h=0;
            fillTextEl(direct, String(h).padStart(2,'0')+':'+String(m).padStart(2,'0'));
            results.push({ label: f.label, ok: true }); continue;
          }
        } catch {}
        let li = getGoogleListItem(f) || findEl(f);
        if (!li) { results.push({ label: f.label, ok: false, error: "time container not found" }); continue; }
        const root = li.closest ? (li.closest('[role="listitem"]')||li) : li;
        let timeInput = root.querySelector ? root.querySelector('input[type="time"]') : null;
        if (timeInput) {
          // normalize "11am" -> "11:00", "11:30 PM" -> "23:30"
          let norm = strVal.toLowerCase();
          let h=0,m=0; let ampm='';
          let pm = norm.includes('pm');
          let am = norm.includes('am');
          let hm = norm.match(/(\d{1,2}):(\d{2})/);
          if (hm){ h=parseInt(hm[1]); m=parseInt(hm[2]); }
          else { let hh = norm.match(/(\d{1,2})/); if (hh) h=parseInt(hh[1]); }
          if (pm && h<12) h+=12; if (am && h===12) h=0;
          const hh = String(h).padStart(2,'0'); const mm = String(m).padStart(2,'0');
          fillTextEl(timeInput, hh+':'+mm);
          results.push({ label: f.label, ok: true }); continue;
        }
        const inputs = root.querySelectorAll ? [...root.querySelectorAll('input[type="text"], input[type="number"]')] : [];
        // Google Forms time: hour, minute + AM/PM listbox
        if (inputs.length >=2) {
          let hm = strVal.match(/(\d{1,2}):(\d{2})/);
          let h='', min='', ap='';
          if (hm){ h=hm[1]; min=hm[2]; ap = strVal.toLowerCase().includes('pm') ? 'PM' : (strVal.toLowerCase().includes('am') ? 'AM' : ''); }
          else { let hh = strVal.match(/(\d{1,2})\s*(am|pm)?/i); if (hh){ h=hh[1]; min='00'; ap = hh[2] ? hh[2].toUpperCase() : ''; } }
          fillTextEl(inputs[0], h);
          fillTextEl(inputs[1], min);
          if (ap) {
            const listbox = root.querySelector('[role="listbox"]');
            if (listbox) { listbox.click(); setTimeout(()=>{},100);
              const opt = [...document.querySelectorAll('[role="option"]')].find(o=>o.textContent.trim().toUpperCase()===ap);
              if (opt) opt.click(); else { const alt = root.querySelector('[data-value="'+ap+'"]'); if (alt) alt.click(); }
            }
          }
          results.push({ label: f.label, ok: true }); continue;
        }
        if (inputs.length===1){ fillTextEl(inputs[0], strVal); results.push({ label: f.label, ok: true }); continue; }
        results.push({ label: f.label, ok: false, error: "time: no time inputs found" }); continue;
      }
      // --- RADIO / RATING / LINEAR SCALE / CHECKBOX / SELECT / GRID ---
      if (['select','radio','rating','linear_scale','grid_radio','grid_checkbox','checkbox'].includes(f.field_type)) {
        const el = findEl(f);
        // Native HTML fast path (mock-rsvp + any plain <form>, not Google Forms).
        // Google path below searches *inside* a listitem container; for native
        // forms the selector already IS the input(s), so handle directly.
        try {
          const norm = (s) => String(s).replace(/\s+/g,' ').trim().toLowerCase();
          // 1) <select> directly (e.g. #tshirt_size = M)
          if (el && el.tagName === 'SELECT') {
            const want = norm(rawVal);
            const opts = Array.from(el.options || []);
            const match = opts.find(o => norm(o.value) === want || norm(o.text) === want);
            if (match) { el.value = match.value; el.dispatchEvent(new Event('change', { bubbles: true })); results.push({ label: f.label, ok: true }); continue; }
            // fall through to Google path which will report the real options
          }
          // 2) native radio group (e.g. [name="event_type"] = Workshop,
          //    [name="satisfaction"] = 1). document.querySelector returns only
          //    the first radio, so expand to the whole group by name.
          if (el && el.type === 'radio') {
            const name = el.name || (f.selector && (f.selector.match(/name=["']?([^"'\]]+)/)||[])[1]);
            const group = name ? Array.from(document.querySelectorAll('input[type="radio"][name="'+name+'"]')) : [el];
            const vals = Array.isArray(rawVal) ? rawVal : [String(rawVal)];
            let okAll = true;
            for (const v of vals) {
              const hit = group.find(r => norm(r.value) === norm(v) || norm(r.getAttribute('aria-label')||'') === norm(v));
              if (hit) { if (!hit.checked) hit.click(); }
              else okAll = false;
            }
            if (okAll) { results.push({ label: f.label, ok: true }); continue; }
            // else fall through to report failure with options below
          }
          // 3) native checkbox (single #plus_one or group [name="skills"])
          if (el && el.type === 'checkbox') {
            // Single checkbox with boolean false (optional, unchecked) = success, nothing to do.
            if (rawVal === false || rawVal === null || rawVal === undefined || String(rawVal).trim() === '' || String(rawVal).toLowerCase() === 'false') {
              if (el.checked) el.click(); // ensure unchecked
              results.push({ label: f.label, ok: true }); continue;
            }
            if (String(rawVal).toLowerCase() === 'true' || String(rawVal).toLowerCase() === 'checked') {
              if (!el.checked) el.click();
              results.push({ label: f.label, ok: true }); continue;
            }
            // Group by name (skills=Coding): match by value attribute.
            const name = el.name;
            const group = name ? Array.from(document.querySelectorAll('input[type="checkbox"][name="'+name+'"]')) : [el];
            const vals = Array.isArray(rawVal) ? rawVal : String(rawVal).split(',').map(s=>s.trim()).filter(Boolean);
            let okAll = true;
            for (const v of vals) {
              const hit = group.find(c => norm(c.value) === norm(v));
              if (hit) { if (!hit.checked) hit.click(); }
              else okAll = false;
            }
            if (okAll) { results.push({ label: f.label, ok: true }); continue; }
          }
        } catch {}
        let searchRoot = null;
        const li = getGoogleListItem(f);
        if (li) searchRoot = li;
        else if (el) searchRoot = el.closest ? (el.closest('[role="listitem"]')||el) : el;
        else searchRoot = document;
        // For checkbox with multiple values (array or comma-separated), click each
        let vals = Array.isArray(rawVal) ? rawVal : String(rawVal).split(',').map(s=>s.trim()).filter(Boolean);
        // For single-value radio/select/rating, vals has 1 entry
        let allOk = true; let lastErr='';
        for (const v of vals) {
          if (f.field_type === 'checkbox' || f.field_type === 'grid_checkbox') {
            // For checkbox, value may be boolean true/false for generic checkbox
            if (v.toLowerCase()==='true' || v.toLowerCase()==='checked') {
              const cb = searchRoot.querySelector('[role="checkbox"]');
              if (cb && cb.getAttribute('aria-checked')!=='true') cb.click();
              continue;
            }
          }
          // Handle case where checkbox value is true/false string but we have no option text
          if (f.field_type==='checkbox' && (v==='true'||v==='false')) {
            const cb = searchRoot.querySelector('input[type="checkbox"]') || searchRoot.querySelector('[role="checkbox"]');
            if (cb) { const checked = cb.checked || cb.getAttribute('aria-checked')==='true'; if (String(checked)!==v) cb.click(); continue; }
          }
          const ok = clickOption(searchRoot, v);
          if (!ok) { allOk=false; lastErr='no matching option for ' + v; }
        }
        if (allOk) { results.push({ label: f.label, ok: true }); }
        else { results.push({ label: f.label, ok: false, error: lastErr + ' (options: '+(f.options||[]).join(', ')+')' }); }
        continue;
      }

      // --- FALLBACK generic text/textarea/email/tel/number ---
      const el = findEl(f);
      if (!el) {
        results.push({ label: f.label, ok: false, error: "selector not found: " + f.selector + " (tried entry/aria fallbacks)" });
        continue;
      }
      if (f.field_type === "checkbox") {
        if (Boolean(rawVal) !== el.checked) el.click();
      } else {
        // For Google Forms radio mistakenly typed as text, try radio path first
        if (f.field_type === "text" || f.field_type === "textarea") {
          let holder = null;
          if (f.selector && /entry\.(\d+)/.test(f.selector)) {
            const num = f.selector.match(/entry\.(\d+)/)[1];
            holder = document.querySelector('[data-params*="' + num + '"]');
          }
          if (holder) {
            const li = holder.closest('[role="listitem"]');
            if (li && li.querySelector('[role="radio"]')) {
              const norm = (s) => s.replace(/\s+/g,' ').trim().toLowerCase();
              const want = norm(String(rawVal));
              const radio = [...li.querySelectorAll('[role="radio"][data-value]')].find(r => norm(r.getAttribute('data-value')||'') === want || norm(r.textContent).includes(want));
              if (radio) { radio.click(); results.push({ label: f.label, ok: true }); continue; }
            }
          }
        }
        fillTextEl(el, strVal);
      }
      results.push({ label: f.label, ok: true });
    } catch (e) {
      results.push({ label: f.label, ok: false, error: String(e) });
    }
  }
  return results;
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error("No active tab found");
  return tab;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "EXTRACT_PAGE") {
    (async () => {
      try {
        const tab = await getActiveTab();
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: extractPageForInspection,
        });
        sendResponse({ ok: true, ...result });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    })();
    return true; // keep sendResponse alive for the async work above
  }

  if (msg.type === "VERIFY_VALUES") {
    // Post-report read-back: did the filled values survive? Catches
    // Live Server auto-reload wipes (server writes session JSON ->
    // :5500 reloads tab -> form emptied after a "success" message).
    (async () => {
      try {
        const tab = await getActiveTab();
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: (selectors) => selectors.map((sel) => {
            let el = null;
            try { el = document.querySelector(sel); } catch { return { selector: sel, present: false, value: null }; }
            if (!el) return { selector: sel, present: false, value: null };
            // Hidden entry inputs always read back whatever was set on them,
            // even when the visible box is empty — read the visible input
            // in the same question instead (false-success fix, 2026-09-14).
            if (el.type === "hidden" && el.name) {
              const li = el.closest('[role="listitem"]');
              const vis = li && li.querySelector('input[type="text"], input[type="email"], input[type="tel"], input[type="number"], textarea, div[role="textbox"]');
              if (vis) el = vis;
            }
            if (el.type === "checkbox" || el.type === "radio") return { selector: sel, present: true, value: String(el.checked) };
            return { selector: sel, present: true, value: el.value ?? el.textContent ?? null };
          }),
          args: [msg.selectors || []],
        });
        sendResponse({ ok: true, values: result, url: tab.url });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    })();
    return true;
  }

  if (msg.type === "FILL_FIELDS") {    (async () => {
      try {
        const tab = await getActiveTab();
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: fillFieldsInPage,
          args: [msg.fields],
        });
        // Independent read-back in the same tab: which document did the
        // filler actually see? (Catches wrong-frame / stale-DOM cases where
        // results say ok but the visible page is untouched.)
        let diag = null;
        try {
          const [d] = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: () => ({
              href: window.location.href,
              title: document.title,
              inputs: document.querySelectorAll("input,select,textarea").length,
              visible_text_filled: [...document.querySelectorAll('input[type="text"], input[type="email"], input[type="tel"], input[type="number"], textarea')].filter(e => e.offsetParent !== null && (e.value || "").trim() !== "").length,
            }),
          });
          diag = d.result;
        } catch (e) { diag = { diag_error: String(e) }; }
        sendResponse({ ok: true, results: result, filled_tab_url: tab.url, filled_tab_id: tab.id, diag });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    })();
    return true;
  }

  return false;
});
