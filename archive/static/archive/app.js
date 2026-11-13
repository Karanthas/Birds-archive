"use strict";

// 仅tab内显式UUID集合进入sessionStorage；不存路径、照片资料或写请求。
const reviewSelectionKey = "birds.review.selection.v1";
const reviewUUID = (value) => typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
let reviewSelection = null;
function loadReviewSelection() {
  const state = {ids:new Set(), writable:true, issue:"", notice:""};
  let raw;
  try { raw = window.sessionStorage.getItem(reviewSelectionKey); }
  catch (_) {
    state.writable = false;
    state.issue = "跨页保存不可用，当前选择仅在本页有效。";
    return state;
  }
  if (raw === null) return state;
  try {
    if (raw.length > 16384) throw new Error("selection_too_large");
    const value = JSON.parse(raw);
    if (!value || Object.keys(value).sort().join(",") !== "photo_ids,updated_at,version" ||
        value.version !== 1 || !Array.isArray(value.photo_ids) || value.photo_ids.length > 200 ||
        value.photo_ids.some((id) => !reviewUUID(id) || id !== id.toLowerCase()) ||
        new Set(value.photo_ids).size !== value.photo_ids.length ||
        typeof value.updated_at !== "string" ||
        !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$/.test(value.updated_at) ||
        !Number.isFinite(Date.parse(value.updated_at))) throw new Error("invalid_selection");
    state.ids = new Set(value.photo_ids);
  } catch (_) {
    state.writable = false;
    state.issue = "选择记录损坏，当前选择仅在本页有效；可清空后重新选择。";
  }
  return state;
}
function renderReviewSelection(form) {
  const boxes = [...form.querySelectorAll('[name="photo_ids"][type="checkbox"]')];
  for (const box of boxes) box.checked = reviewSelection.ids.has(box.value);
  const visible = boxes.filter((box) => box.checked).length;
  form.querySelector("[data-selection-count]").textContent = `已选 ${reviewSelection.ids.size} 张 · 本页 ${visible} 张`;
  form.querySelector("[data-selection-tools]").hidden = reviewSelection.ids.size === 0 && !reviewSelection.issue;
  form.querySelector("[data-selection-message]").textContent = [reviewSelection.issue, reviewSelection.notice].filter(Boolean).join(" ");
  const all = form.querySelector("[data-selection-all]");
  all.hidden = false;
  all.disabled = boxes.length === 0;
  all.checked = boxes.length > 0 && visible === boxes.length;
  all.indeterminate = visible > 0 && visible < boxes.length;
  form.querySelector("[data-selection-clear]").hidden = false;
}
function changeReviewSelection(form, boxes, checked) {
  const ids = new Set(reviewSelection.ids);
  for (const box of boxes) {
    if (!reviewUUID(box.value) || box.value !== box.value.toLowerCase()) {
      reviewSelection.notice = "照片标识不可用，请刷新核对。";
      renderReviewSelection(form); return;
    }
    if (checked) ids.add(box.value); else ids.delete(box.value);
  }
  if (ids.size > 200) {
    reviewSelection.notice = "每批最多选择 200 张；已保留原选择，请减少后再选。";
  } else {
    reviewSelection.ids = ids;
    reviewSelection.notice = "";
    if (reviewSelection.writable) {
      try {
        window.sessionStorage.setItem(reviewSelectionKey, JSON.stringify({version:1,
          photo_ids:[...ids], updated_at:new Date().toISOString()}));
      } catch (_) {
        reviewSelection.writable = false;
        reviewSelection.issue = "跨页保存不可用，当前选择仅在本页有效。";
      }
    }
  }
  renderReviewSelection(form);
}
function bindReviewSelection() {
  const form = document.querySelector("[data-review-selection]");
  if (!form) return;
  if (!reviewSelection) reviewSelection = loadReviewSelection();
  if (form.dataset.reviewBound !== "true") {
    form.dataset.reviewBound = "true";
    const boxes = [...form.querySelectorAll('[name="photo_ids"][type="checkbox"]')];
    for (const box of boxes) {
      box.dataset.bound = "true";
      box.addEventListener("change", () => changeReviewSelection(form, [box], box.checked));
    }
    const all = form.querySelector("[data-selection-all]");
    all.addEventListener("change", () => changeReviewSelection(form, boxes, all.checked));
    form.querySelector("[data-selection-clear]").addEventListener("click", () => {
      reviewSelection = {ids:new Set(), writable:true, issue:"", notice:""};
      try {
        window.sessionStorage.removeItem(reviewSelectionKey);
        if (window.sessionStorage.getItem(reviewSelectionKey) !== null) throw new Error("selection_not_cleared");
      }
      catch (_) {
        reviewSelection.writable = false;
        reviewSelection.issue = "跨页保存不可用，当前选择已清空；存储记录无法清除。";
      }
      renderReviewSelection(form);
    });
    // Native successful controls keep the submitter/CSRF. Only bird POSTs replace
    // page checkboxes with the complete explicit set; location GET stays page-only.
    form.addEventListener("formdata", (event) => {
      if (form.dataset.reviewSubmit !== "species") return;
      event.formData.delete("photo_ids");
      for (const id of reviewSelection.ids) event.formData.append("photo_ids", id);
    });
  }
  renderReviewSelection(form);
}
function updateSelection() {
  const form = document.querySelector("[data-selection]");
  if (!form) return;
  if (form.hasAttribute("data-review-selection")) { renderReviewSelection(form); return; }
  const count = form.querySelectorAll('[name="photo_ids"]:checked').length;
  const tools = form.querySelector("[data-selection-tools]");
  if (tools) tools.hidden = count === 0;
  const label = form.querySelector("[data-selection-count]");
  if (label) label.textContent = `本页 ${count} 张`;
}
const personalInFlight = new Set();
async function submitPersonalQuick(form, button) {
  const group = form.closest('[data-personal-controls]');
  const id = group.dataset.photoId;
  if (!button || personalInFlight.has(id) || group.dataset.resultUnknown === 'true') return;
  const groups = [...document.querySelectorAll('[data-personal-controls]')].filter((node) => node.dataset.photoId === id);
  const controls = groups.flatMap((node) => [...node.querySelectorAll('button')]);
  const body = new FormData(form); body.append(button.name, button.value);
  personalInFlight.add(id); controls.forEach((node) => { node.disabled = true; });
  groups.forEach((node) => { node.setAttribute('aria-busy', 'true'); node.querySelector('[data-personal-message]').textContent = '正在保存…'; });
  try {
    const target = new URL(form.action, window.location.origin);
    if (target.origin !== window.location.origin) throw new Error('invalid_url');
    const response = await fetch(target, {method:'POST', body, credentials:'same-origin', cache:'no-store',
      redirect:'manual', headers:{Accept:'application/json'}, signal:AbortSignal.timeout(15000)});
    const state = await response.json();
    if (state.photo_id !== id || !Number.isSafeInteger(state.metadata_version) || state.metadata_version < 1 ||
        typeof state.favorite !== 'boolean' || !(state.rating === null || Number.isInteger(state.rating) && state.rating >= 1 && state.rating <= 5) ||
        typeof state.next_request_id !== 'string' || (response.status !== 200 && typeof state.error !== 'string')) throw new Error('invalid_state');
    groups.forEach((node) => {
      node.dataset.version = String(state.metadata_version);
      const favorite = node.querySelector('[data-favorite]');
      favorite.value = String(!state.favorite); favorite.setAttribute('aria-pressed', String(state.favorite));
      favorite.setAttribute('aria-label', state.favorite ? '取消收藏' : '收藏照片');
      favorite.textContent = state.favorite ? '♥ 已收藏' : '♡ 收藏';
      node.querySelectorAll('[data-rating]').forEach((star) => star.setAttribute('aria-pressed', String(star.value === (state.rating === null ? '' : String(state.rating)))));
      node.querySelectorAll('[name="request_id"]').forEach((input) => { input.value = state.next_request_id; });
      node.querySelectorAll('[name="metadata_version"]').forEach((input) => { input.value = String(state.metadata_version); });
      node.querySelector('[data-personal-message]').textContent = state.error || (state.rating ? `${state.rating} 星 · 已保存` : '未评分 · 已保存');
    });
    if (response.status === 200) document.querySelectorAll('form [name="personal_field"]').forEach((field) => {
      // Only this detail photo is rendered here; preserve drafts while advancing its independent personal CAS.
      const version = field.form.querySelector('[name="metadata_version"]');
      if (version && !field.form.hasAttribute('data-personal-quick')) version.value = String(state.metadata_version);
    });
  } catch (_) {
    groups.forEach((node) => {
      node.dataset.resultUnknown = 'true';
      node.querySelector('[data-personal-message]').textContent = '未收到保存结果，显示原已知值；请刷新核对后再操作。';
    });
  } finally {
    personalInFlight.delete(id);
    groups.forEach((node) => node.removeAttribute('aria-busy'));
    controls.forEach((node) => { node.disabled = node.closest('[data-personal-controls]').dataset.resultUnknown === 'true'; });
  }
}
function bindPersonalNotes() {
  document.querySelectorAll('[data-note-form]').forEach((form) => {
    if (form.dataset.noteBound) return;
    form.dataset.noteBound = 'true';
    const dialog = document.querySelector('[data-note-leave]');
    dialog?.addEventListener('cancel', (event) => {
      if (form.dataset.noteSaving === 'true') event.preventDefault();
      else { noteContinuation = null; form.querySelector('[data-note-draft]').focus(); }
    });
    form.querySelector('[data-note-cancel]').addEventListener('click', (event) => {
      event.preventDefault(); form.querySelector('[data-note-draft]').value = form.dataset.noteOriginal;
    });
  });
}
function dirtyNoteForm() {
  return [...document.querySelectorAll('[data-note-form]')].find((form) =>
    form.querySelector('[data-note-draft]').value !== form.dataset.noteOriginal);
}
let noteContinuation = null;
function guardNoteLeave(continueNavigation) {
  const form = dirtyNoteForm();
  if (!form) { continueNavigation(); return; }
  const dialog = document.querySelector('[data-note-leave]');
  if (!dialog || dialog.open) return;
  noteContinuation = continueNavigation;
  dialog.querySelector('[data-note-leave-error]').textContent = '';
  dialog.showModal();
}
async function saveNoteBeforeLeave(form) {
  const draft = form.querySelector('[data-note-draft]');
  const body = new FormData(form);
  const target = new URL(form.action, window.location.href);
  if (target.origin !== window.location.origin || personalInFlight.size || form.dataset.noteSaving === 'true') return false;
  form.dataset.noteSaving = 'true'; draft.disabled = true;
  try {
    const response = await fetch(target, {method:'POST', body, credentials:'same-origin', cache:'no-store',
      redirect:'manual', headers:{Accept:'application/json'}, signal:AbortSignal.timeout(15000)});
    if (response.redirected || response.headers.get('content-type')?.split(';')[0] !== 'application/json') throw new Error('invalid_response');
    const state = await response.json();
    const id = document.querySelector('[data-personal-controls]')?.dataset.photoId;
    if (response.status !== 200 || state.photo_id !== id || state.notes !== body.get('notes') ||
        !Number.isSafeInteger(state.metadata_version) || typeof state.next_request_id !== 'string')
      throw new Error(state.error || '保存未确认，请留在当前页核对。');
    form.dataset.noteOriginal = state.notes;
    form.querySelector('[name="request_id"]').value = state.next_request_id;
    document.querySelectorAll('form [name="metadata_version"]').forEach((input) => { input.value = String(state.metadata_version); });
    document.querySelector('[data-note-saved]').textContent = state.notes || '无';
    return true;
  } catch (error) {
    document.querySelector('[data-note-leave-error]').textContent =
      '备注尚未确认保存，草稿仍在当前页。请核对后重试。'+(error.message?.includes('照片') ? error.message : '');
    return false;
  } finally { form.dataset.noteSaving = 'false'; draft.disabled = false; }
}
document.addEventListener('click', (event) => {
  const link = event.target.closest('a[href]');
  if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey ||
      link.hasAttribute('download') || link.hasAttribute('data-note-cancel') || link.getAttribute('href').startsWith('#')) return;
  if (dirtyNoteForm()) {
    event.preventDefault(); event.stopImmediatePropagation();
    guardNoteLeave(() => window.location.assign(link.href));
  }
}, true);
document.addEventListener('submit', (event) => {
  const form = event.target;
  if (!dirtyNoteForm() || form.hasAttribute('data-note-form') || form.hasAttribute('data-personal-quick')) return;
  event.preventDefault(); event.stopImmediatePropagation();
  const button = event.submitter;
  guardNoteLeave(() => { form.dataset.submitting = 'false'; form.requestSubmit(button || undefined); });
}, true);
document.addEventListener('click', async (event) => {
  const dialog = event.target.closest('[data-note-leave]');
  if (!dialog) return;
  const form = dirtyNoteForm();
  if (event.target.closest('[data-note-stay]')) {
    noteContinuation = null; dialog.close(); form?.querySelector('[data-note-draft]').focus();
  } else if (event.target.closest('[data-note-discard]')) {
    if (form) form.querySelector('[data-note-draft]').value = form.dataset.noteOriginal;
    const next = noteContinuation; noteContinuation = null; dialog.close(); next?.();
  } else if (event.target.closest('[data-note-save-leave]')) {
    const controls = [...dialog.querySelectorAll('button')]; controls.forEach((button) => { button.disabled = true; });
    const saved = form && await saveNoteBeforeLeave(form);
    controls.forEach((button) => { button.disabled = false; });
    if (saved) { const next = noteContinuation; noteContinuation = null; dialog.close(); next?.(); }
  }
});
document.addEventListener('keydown', (event) => {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey ||
      document.querySelector('dialog[open]') || event.target.closest('input, textarea, select, button, [contenteditable]')) return;
  const selector = {ArrowLeft:'[data-detail-previous]', ArrowRight:'[data-detail-next]'}[event.key];
  const link = selector && document.querySelector(selector);
  if (link) { event.preventDefault(); link.click(); }
});
window.addEventListener('beforeunload', (event) => {
  if ([...document.querySelectorAll('[data-note-form]')].some((form) =>
    form.dataset.submitting !== 'true' && form.querySelector('[data-note-draft]').value !== form.dataset.noteOriginal)) {
    event.preventDefault(); event.returnValue = '';
  }
});
function bindGuideIndex() {
  const region = document.querySelector('[data-guide-index]');
  if (!region || region.dataset.guideBound) return;
  region.dataset.guideBound = 'true';
  const form = region.querySelector('[data-guide-search]');
  let busy = false, retryUrl = null;
  async function navigate(url, operation) {
    if (busy) return;
    const target = new URL(url, window.location.href);
    if (target.origin !== window.location.origin || target.pathname !== form.action.replace(window.location.origin, '') ||
        target.username || target.password) return;
    busy = true; retryUrl = target;
    operation.setAttribute('aria-busy', 'true');
    const button = operation.querySelector?.('button[type="submit"]');
    if (button) button.disabled = true;
    region.querySelector('[data-guide-message]').textContent = '正在加载…';
    region.querySelector('[data-guide-retry]').hidden = true;
    try {
      const response = await fetch(target, {credentials:'same-origin', cache:'no-store', redirect:'manual',
        signal:AbortSignal.timeout(15000)});
      if (response.status !== 200 || response.redirected || new URL(response.url).origin !== window.location.origin)
        throw new Error('guide_unavailable');
      const parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
      const next = parsed.querySelector('[data-guide-index]');
      if (!next || next.querySelector('script, iframe, object') ||
          [...next.querySelectorAll('*')].some((node) => [...node.attributes].some((attr) => /^on/i.test(attr.name))))
        throw new Error('guide_invalid');
      if (!region.isConnected) return;
      region.replaceWith(next);
      history.pushState({guide:true}, '', target);
      bindPage();
      next.querySelector('[data-guide-heading]').focus();
    } catch (_) {
      if (region.isConnected) {
        region.querySelector('[data-guide-message]').textContent = '加载失败，已保留当前条目和搜索词，请重试。';
        region.querySelector('[data-guide-retry]').hidden = false;
      }
    } finally {
      busy = false; operation.removeAttribute('aria-busy');
      if (button) button.disabled = false;
    }
  }
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const target = new URL(form.action);
    target.search = new URLSearchParams(new FormData(form)).toString();
    navigate(target, form);
  });
  region.addEventListener('click', (event) => {
    const link = event.target.closest('[data-guide-page]');
    if (link && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && event.button === 0) {
      event.preventDefault(); navigate(link.href, link);
    }
    if (event.target.closest('[data-guide-retry]') && retryUrl) navigate(retryUrl, form);
  });
}
window.addEventListener('popstate', () => {
  if (document.querySelector('[data-guide-index]')) window.location.reload();
});
function bindPage() {
  bindGuideIndex();
  bindPersonalNotes();
  bindReviewSelection();
  bindGalleries();
  document.querySelectorAll("img").forEach((image) => {
    if (image.dataset.bound) return;
    image.dataset.bound = "true";
    const waiting = image.parentElement.querySelector("[data-media-loading]");
    let failed = false;
    const showError = () => {
      failed = true;
      if (waiting) waiting.hidden = true;
      image.hidden = true;
      const message = image.parentElement.querySelector(".media-error");
      if (message) message.hidden = false;
    };
    image.addEventListener("error", showError);
    if (waiting) {
      const showLoaded = async () => {
        try {
          await image.decode();
          if (!failed) waiting.hidden = true;
        } catch (_) { showError(); }
      };
      image.addEventListener("load", showLoaded);
      if (!image.complete) waiting.hidden = false;
      else if (image.naturalWidth > 0) showLoaded();
    }
    if (image.complete && image.naturalWidth === 0) showError();
  });
  document.querySelectorAll('[name="photo_ids"][type="checkbox"]').forEach((input) => {
    if (input.dataset.bound) return;
    input.dataset.bound = "true";
    if (!input.closest('[data-management-preview-form]')) input.checked = false;
    input.addEventListener("change", updateSelection);
  });
  updateSelection();
  bindManagementSelection();
  const errors = document.querySelector("[data-error-summary]");
  if (errors) {
    errors.focus();
    errors.querySelectorAll('a[href^="#"]').forEach((link) => {
      const field = document.getElementById(link.hash.slice(1));
      if (field) { field.setAttribute("aria-invalid", "true"); field.setAttribute("aria-describedby", errors.id); }
    });
  } else { document.querySelector("[data-form-heading]")?.focus(); }
  document.querySelectorAll("form").forEach((form) => {
    if (form.dataset.bound) return;
    form.dataset.bound = "true";
    form.addEventListener("submit", async (event) => {
      if (event.defaultPrevented) return;
      if (form.hasAttribute('data-personal-quick')) {
        event.preventDefault(); submitPersonalQuick(form, event.submitter); return;
      }
      if (form.dataset.submitting === "true" || form.dataset.resultUnknown === "true") {
        event.preventDefault(); return;
      }
      if (!form.reportValidity()) return;
      const reviewSpecies = form.hasAttribute("data-review-selection") &&
        ["own", "uniform_species"].includes(event.submitter?.dataset?.reviewSpecies);
      if (form.hasAttribute("data-review-selection"))
        form.dataset.reviewSubmit = reviewSpecies ? "species" : "page";
      if (form.hasAttribute("data-selection") &&
          !(reviewSpecies ? reviewSelection.ids.size : form.querySelector('[name="photo_ids"]:checked'))) {
        event.preventDefault(); return;
      }
      const method = (event.submitter?.getAttribute("formmethod") || form.method).toLowerCase();
      if (event.submitter?.dataset?.recognitionAction) {
        event.preventDefault(); submitRecognitionAction(form, event.submitter); return;
      }
      // Draft POST follows its native 303 response; only final confirmation uses JSON.
      // Keep the successful submitter and CSRF field enabled for native serialization.
      if (form.hasAttribute("data-archive-confirm") && event.submitter?.name === "draft_action") return;
      const enhancedPost = method === "post" &&
        (form.hasAttribute("data-pending-text") || form.hasAttribute("data-task-retry"));
      // 禁用控件前冻结 ID、版本和值；每次操作最多发送一次 POST。
      const frozen = new FormData(form);
      if (event.submitter?.name) frozen.append(event.submitter.name, event.submitter.value);
      const confirming = form.hasAttribute("data-archive-confirm");
      form.dataset.submitting = "true"; form.setAttribute("aria-busy", "true");
      const controls = (enhancedPost && !confirming ? [...document.querySelectorAll('#main input, #main button, #main select, #main textarea')] : [...form.elements])
        .filter((control) => !control.disabled);
      const button = event.submitter || form.querySelector("button[type=submit]");
      const originalLabel = button?.textContent;
      // Native serialization still needs the enabled successful submitter (action/CSRF).
      if (!enhancedPost) return;
      if (button) { button.disabled = true; button.textContent = form.dataset.submitState ||
        form.dataset.pendingText || button.dataset.pending || (enhancedPost ? "正在提交…" : "正在加载…"); }
      event.preventDefault(); controls.forEach((control) => { control.disabled = true; });
      try {
        const response = await fetch(form.action, {method:"POST", body:frozen,
          credentials:"same-origin", cache:"no-store", ...(confirming ? {redirect:"manual"} : {}),
          headers:{"HX-Request":"true", ...(confirming ? {"X-Archive-Confirm":"true"} : {})},
          signal:AbortSignal.timeout(20000)});
        if (confirming && response.status === 200) {
          const result = await response.json();
          if (result.accepted !== true || typeof result.next_url !== "string" || !result.next_url)
            throw new Error("invalid_acceptance");
          const next = new URL(result.next_url, window.location.origin);
          if (next.origin !== window.location.origin || next.username || next.password || response.redirected)
            throw new Error("invalid_next_url");
          window.location.assign(next.href);
          return;
        }
        if (response.status === 403) {
          form.dataset.resultUnknown = "true";
          if (button) button.textContent = originalLabel;
          const warning = document.createElement("p");
          warning.className="error"; warning.setAttribute("role","alert"); warning.tabIndex=-1;
          warning.textContent="页面已过期，请刷新后重试。";
          const refresh=document.createElement("a"); refresh.href=window.location.href; refresh.textContent="刷新页面";
          warning.append(" ",refresh); form.append(warning); warning.focus();
          return;
        }
        if (![200,400,409].includes(response.status)) throw new Error("result_unknown");
        const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
        const next = parsed.querySelector("main#main");
        if (!next || new URL(response.url).origin !== window.location.origin) throw new Error("invalid_result");
        document.getElementById("main").replaceWith(next);
        // 未重定向的错误正文可能来自POST专用地址；不能把它变成历史GET。
        if (response.redirected) window.history.replaceState(null, "", response.url);
        bindPage();
      } catch (_) {
        controls.forEach((control) => { control.disabled = false; });
        form.dataset.resultUnknown = "true";
        if (button) { button.disabled = true; button.textContent = originalLabel; }
        const warning = document.createElement("p");
        warning.className="error"; warning.setAttribute("role","alert"); warning.tabIndex=-1;
        warning.textContent="暂未收到保存结果。请刷新核对后再试。";
        const check = document.createElement("a");
        // Server-rendered named URL stays correct when the homepage becomes the album.
        check.href=form.dataset.verifyUrl || document.querySelector('main > a[href]')?.href || window.location.href;
        check.textContent="核对当前状态";
        warning.append(" ",check); form.append(warning);
        if (!confirming) warning.focus();
      } finally { form.dataset.submitting="false"; form.removeAttribute("aria-busy"); }
    });
  });
}
// 相册使用服务端签名链；只追加预览，不重置冻结集合。
const galleryStates = new Map();
let galleryLifecycleBound = false;
function bindGalleries() {
  for (const [gallery, state] of galleryStates) {
    if (!gallery.isConnected) { state.dispose(); galleryStates.delete(gallery); }
  }
  if (!("IntersectionObserver" in window)) return;
  document.querySelectorAll("[data-gallery]").forEach((gallery) => {
    if (galleryStates.has(gallery)) return;
    if (!galleryLifecycleBound) {
      galleryLifecycleBound = true;
      window.addEventListener("pagehide", () => {
        for (const state of galleryStates.values()) state.dispose();
        galleryStates.clear();
      });
      window.addEventListener("pageshow", bindGalleries);
      document.addEventListener("htmx:beforeCleanupElement", (event) => {
        for (const [node, state] of galleryStates) {
          if (event.target === node || event.target.contains(node)) { state.dispose(); galleryStates.delete(node); }
        }
      });
    }
    const species = gallery.dataset.gallerySpecies;
    const pageTokens = gallery.dataset.galleryPages?.split(',').filter(Boolean) || [];
    const location = window.location.href;
    const consumed = new Set(pageTokens.length ? pageTokens : [gallery.querySelector("[data-gallery-batch]")?.dataset.galleryCursor]);
    const batchOrigins = new WeakMap();
    const photos = new Set([...gallery.querySelectorAll("article[data-photo-id]")].map((node) => node.dataset.photoId));
    let next = gallery.querySelector("[data-gallery-batch]")?.dataset.galleryNextCursor;
    let sentinel, request, stopped = false;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.target === sentinel && entry.isIntersecting)) load();
    }, {rootMargin:"300px"});
    const retry = (event) => {
      if (event.target.closest("[data-gallery-retry]")) { stopped = false; load(); }
    };
    const rememberWindow = (event) => {
      const link = event.target.closest('[data-gallery-detail]');
      if (!link) return;
      const target = new URL(link.href, window.location.origin);
      if (!batchOrigins.has(link)) batchOrigins.set(link, target.searchParams.get('cursor'));
      const ownPage = Math.max(0, pageTokens.indexOf(batchOrigins.get(link)));
      const start = Math.min(ownPage, Math.max(0, pageTokens.length-20));
      target.searchParams.set('cursor', pageTokens[start]);
      target.searchParams.set('loaded', String(Math.min(20, pageTokens.length-start)));
      link.href = target.href;
    };
    const guideEntry = gallery.hasAttribute('data-guide-photos') ? null : document.querySelector('header a[data-guide-entry]');
    const rememberAlbum = () => {
      const start = Math.max(0, pageTokens.length-20);
      const target = new URL(guideEntry.href, window.location.origin);
      target.searchParams.set('album_cursor', pageTokens[start]);
      target.searchParams.set('album_loaded', String(Math.min(20, pageTokens.length-start)));
      guideEntry.href = target.href;
    };
    guideEntry?.addEventListener('click', rememberAlbum);
    gallery.addEventListener("click", retry);
    gallery.addEventListener('click', rememberWindow);
    const dispose = () => {
      stopped = true; request?.abort(); observer.disconnect(); gallery.removeEventListener("click", retry);
      gallery.removeEventListener('click', rememberWindow);
      guideEntry?.removeEventListener('click', rememberAlbum);
    };
    galleryStates.set(gallery, {dispose});
    function watch() {
      observer.disconnect();
      sentinel = gallery.querySelector("[data-gallery-sentinel]");
      if (!next || !sentinel) return;
      sentinel.querySelector("[data-gallery-next]").hidden = true;
      observer.observe(sentinel);
    }
    async function load() {
      if (window.location.href !== location || gallery.dataset.gallerySpecies !== species) { dispose(); return; }
      if (stopped || request || !next || consumed.has(next) || !gallery.isConnected) return;
      const cursor = next;
      const link = sentinel.querySelector("[data-gallery-next]");
      const controller = new AbortController();
      request = controller;
      const timeout = setTimeout(() => controller.abort(), 20000);
      gallery.setAttribute("aria-busy", "true");
      sentinel.querySelector("[data-gallery-message]").textContent = "正在加载…";
      sentinel.querySelector("[data-gallery-retry]").hidden = true;
      let refreshRequired = false;
      try {
        const url = new URL(link.dataset.galleryUrl, window.location.origin);
        if (url.origin !== window.location.origin || url.username || url.password ||
            url.searchParams.get("cursor") !== cursor || (url.searchParams.get("species") || "") !== species)
          throw new Error("invalid_chain");
        const response = await fetch(url.href, {credentials:"same-origin", cache:"no-store", redirect:"manual",
          headers:{"HX-Request":"true"}, signal:controller.signal});
        refreshRequired = response.status === 400 || response.status === 404;
        if (response.status !== 200 || response.redirected || new URL(response.url).origin !== window.location.origin)
          throw new Error("gallery_unavailable");
        refreshRequired = true; // 无法验证的片段不能继续追加到现有冻结集合。
        const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
        // 导航、容器替换、筛选变化或取消后的晚响应不可修改当前页。
        if (controller.signal.aborted || stopped || !gallery.isConnected || request !== controller ||
            next !== cursor || gallery.dataset.gallerySpecies !== species || window.location.href !== location) return;
        const batches = parsed.querySelectorAll("[data-gallery-batch]");
        const batch = batches[0];
        if (batches.length !== 1 || batch.dataset.galleryCursor !== cursor || batch.dataset.gallerySpecies !== species ||
            parsed.querySelector("script, iframe, object, [data-gallery]") ||
            [...parsed.querySelectorAll("*")].some((node) => [...node.attributes].some((attr) => /^on/i.test(attr.name))))
          throw new Error("invalid_chain");
        const following = batch.dataset.galleryNextCursor;
        const cards = [...batch.querySelectorAll("article[data-photo-id]")];
        const groups = [...batch.querySelectorAll("[data-gallery-month]")];
        if (typeof following !== "string" || (following && (following === cursor || consumed.has(following))) ||
            cards.some((card) => !/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(card.dataset.photoId)) ||
            groups.some((group) => !/^(unknown|\d{4}-(0[1-9]|1[0-2]))$/.test(group.dataset.galleryMonth) ||
              !group.querySelector(".photo-grid"))) throw new Error("invalid_chain");
        const nextSentinel = parsed.querySelector("[data-gallery-sentinel]");
        const nextLink = nextSentinel?.querySelector("[data-gallery-next]");
        if (following && (!nextLink || nextLink.dataset.galleryNext !== following ||
            !nextSentinel.querySelector("[data-gallery-message]") || !nextSentinel.querySelector("[data-gallery-retry]") ||
            !nextSentinel.querySelector("[data-gallery-refresh]"))) throw new Error("invalid_chain");
        for (const anchor of parsed.querySelectorAll("a[href]")) {
          const target = new URL(anchor.getAttribute("href"), window.location.origin);
          if (target.origin !== window.location.origin || target.username || target.password) throw new Error("invalid_link");
        }
        for (const image of batch.querySelectorAll("img")) {
          const src = new URL(image.getAttribute("src"), window.location.origin);
          if (src.origin !== window.location.origin || !/\/(preview|thumb)\/$/.test(src.pathname))
            throw new Error("invalid_image");
        }
        // 所有校验均在修改DOM之前完成；只合并末组，防止跨链月份乱序。
        const existingGroups = [...gallery.querySelectorAll("[data-gallery-month]")];
        let lastMonth = existingGroups.at(-1)?.dataset.galleryMonth;
        const months = new Set(existingGroups.map((group) => group.dataset.galleryMonth));
        for (const group of groups) {
          const month = group.dataset.galleryMonth;
          if ((months.has(month) && month !== lastMonth) ||
              (lastMonth && month !== lastMonth && (lastMonth === "unknown" || (month !== "unknown" && month >= lastMonth))))
            throw new Error("invalid_month_chain");
          months.add(month); lastMonth = month;
        }
        let appended = 0;
        for (const group of groups) {
          for (const card of [...group.querySelectorAll("article[data-photo-id]")]) {
            if (photos.has(card.dataset.photoId)) card.remove();
            else { photos.add(card.dataset.photoId); appended++; }
          }
          if (!group.querySelector("article[data-photo-id]")) continue;
          const last = [...gallery.querySelectorAll("[data-gallery-month]")].at(-1);
          if (last?.dataset.galleryMonth === group.dataset.galleryMonth)
            last.querySelector(".photo-grid").append(...group.querySelector(".photo-grid").children);
          else sentinel.before(group);
        }
        consumed.add(cursor);
        if (batch.dataset.galleryDirty === 'true') {
          const changes = document.querySelector('[data-gallery-changes]');
          if (changes) {
            changes.hidden = false;
            changes.querySelector('[data-gallery-remaining]').textContent = `本集合剩余 ${batch.dataset.galleryRemaining} 张。`;
          }
        }
        pageTokens.push(cursor);
        gallery.dataset.galleryPages = pageTokens.join(',');
        gallery.dataset.galleryLoadedCursor = cursor;
        next = gallery.dataset.guidePhotos === 'true' ? following : (cards.length && appended ? following : "");
        observer.disconnect(); sentinel.remove();
        if (next) gallery.append(nextSentinel);
        else { const end = document.createElement("p"); end.dataset.galleryEnd = ""; end.textContent = "已到末尾"; gallery.append(end); }
        bindPage();
      } catch (_) {
        if (stopped || !gallery.isConnected || gallery.dataset.gallerySpecies !== species) return;
        observer.disconnect(); stopped = true;
        sentinel.querySelector("[data-gallery-message]").textContent = refreshRequired ?
          "相册已过期或无法继续，请刷新。" : "加载失败，请重试。";
        sentinel.querySelector("[data-gallery-retry]").hidden = refreshRequired;
        sentinel.querySelector("[data-gallery-refresh]").hidden = !refreshRequired;
      } finally {
        clearTimeout(timeout);
        if (request === controller) { request = null; gallery.removeAttribute("aria-busy"); }
        if (!stopped && gallery.isConnected) watch();
      }
    }
    watch();
  });
}
// Separate from review and trash: filters never define this explicit UUID set.
const managementSelectionKey = "birds.management-selection.v1";
const trashSelectionKey = "birds.trash-selection.v1";
try {
  const completed = [...document.querySelectorAll('[data-restored-photo]')].map((row) => row.dataset.restoredPhoto);
  if (completed.length) {
    const selected = JSON.parse(sessionStorage.getItem(trashSelectionKey) || '[]');
    sessionStorage.setItem(trashSelectionKey, JSON.stringify(selected.filter((id) => !completed.includes(id))));
  }
} catch (_) { /* Selection storage is optional; explicit preview remains authoritative. */ }
let managementSelection = null;
function bindManagementSelection() {
  const form = document.querySelector("[data-management-selection]");
  if (!form) return;
  const selectionKey = form.dataset.managementScope === 'trash' ? trashSelectionKey : managementSelectionKey;
  if (!managementSelection) {
    managementSelection = {ids:new Set(), persistent:true, message:""};
    try {
      const raw = sessionStorage.getItem(selectionKey);
      const ids = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(ids) || ids.length > 200 || ids.some((id) =>
        typeof id !== "string" || !/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(id))) throw new Error("invalid_selection");
      managementSelection.ids = new Set(ids);
      sessionStorage.setItem(selectionKey, JSON.stringify([...managementSelection.ids]));
    } catch (_) {
      managementSelection.persistent = false;
      managementSelection.message = "跨页存储不可用：仅当前页选择有效，请在离开前核对。";
    }
  }
  const render = () => {
    const boxes = [...document.querySelectorAll("[data-management-photo]")];
    const visible = new Set(boxes.map((box) => box.value));
    boxes.forEach((box) => { box.checked = managementSelection.ids.has(box.value); });
    form.querySelector("[data-management-count]").textContent = `已选 ${managementSelection.ids.size} 张`;
    form.querySelector("[data-management-message]").textContent = managementSelection.message || "改变筛选不会增加已选对象；提交前逐张核对。";
    const hidden = form.querySelector("[data-management-hidden]");
    hidden.replaceChildren();
    for (const id of managementSelection.ids) {
      if (visible.has(id)) continue;
      const input = document.createElement("input");
      input.type = "hidden"; input.name = "photo_ids"; input.value = id; hidden.append(input);
    }
  };
  const save = () => {
    if (managementSelection.persistent) {
      try { sessionStorage.setItem(selectionKey, JSON.stringify([...managementSelection.ids])); }
      catch (_) {
        managementSelection.persistent = false;
        managementSelection.message = "跨页存储不可用：当前已选仍保留在此页，请在离开前核对。";
      }
    }
    render();
  };
  document.querySelectorAll("[data-management-photo]").forEach((box) => {
    if (box.dataset.managementBound) return;
    box.dataset.managementBound = "true";
    box.addEventListener("change", () => {
      if (box.checked && managementSelection.ids.size >= 200) {
        box.checked = false; managementSelection.message = "每次最多选择200张，请先处理当前选择。";
      } else if (box.checked) managementSelection.ids.add(box.value);
      else managementSelection.ids.delete(box.value);
      save();
    });
  });
  if (!form.dataset.managementBound) {
    form.dataset.managementBound = "true";
    form.querySelector("[data-management-clear]").addEventListener("click", () => {
      managementSelection.ids.clear(); save();
    });
    form.addEventListener("submit", (event) => {
      if (!managementSelection.ids.size) {
        event.preventDefault(); managementSelection.message = "请先明确选择照片。"; render();
      }
    });
  }
  render();
}
bindPage();
window.addEventListener("pageshow", (event) => {
  if (!event.persisted || !document.querySelector("[data-management-selection]")) return;
  const form = document.querySelector("[data-management-selection]");
  form.dataset.submitting = "false"; form.removeAttribute("aria-busy");
  managementSelection = null; bindManagementSelection();
});
// A restored native form must allow a fresh action and use the newest tab set.
if (document.querySelector("[data-review-selection]")) window.addEventListener?.("pageshow", (event) => {
  if (!event.persisted) return;
  document.querySelectorAll("[data-review-selection]").forEach((form) => {
    form.dataset.submitting = "false";
    form.dataset.reviewSubmit = "page";
    form.removeAttribute("aria-busy");
  });
  reviewSelection = null;
  bindReviewSelection();
});
let pollingStopped=false;
let pollingActive=false;
let archivePollingActive=false;
let archivePollingStopped=false;
const seenArchiveFailures=new Set([...document.querySelectorAll("[data-failure-key]")]
  .map((item)=>item.dataset.failureKey));
async function pollArchiveStatus(manual=false) {
  const region=document.querySelector("[data-archive-status]");
  if (!region || archivePollingActive || (!manual && archivePollingStopped) ||
      document.visibilityState!=="visible" || document.querySelector('[data-submitting="true"]')) return;
  archivePollingActive=true;
  try {
    const url=new URL(region.dataset.archivePoll,window.location.origin);
    if (url.origin!==window.location.origin) throw new Error("invalid_status_url");
    const response=await fetch(url,{credentials:"same-origin",cache:"no-store",redirect:"manual",
      headers:{"HX-Request":"true"},signal:AbortSignal.timeout(10000)});
    if (response.status!==200 || response.redirected) throw new Error("status_unavailable");
    const parsed=new DOMParser().parseFromString(await response.text(),"text/html");
    const next=parsed.querySelector("[data-archive-status]");
    if (!next) throw new Error("status_invalid");
    if (document.visibilityState!=="visible" || document.querySelector('[data-submitting="true"]')) return;
    region.querySelector("[data-archive-counts]").textContent=next.querySelector("[data-archive-counts]").textContent;
    const failures=region.querySelector("[data-archive-failures]");
    const keys=new Set([...next.querySelectorAll("[data-failure-key]")].map((item)=>item.dataset.failureKey));
    failures.querySelectorAll("[data-failure-key]").forEach((item)=>{
      if (!keys.has(item.dataset.failureKey) && !item.contains(document.activeElement)) item.remove();
    });
    next.querySelectorAll("[data-failure-key]").forEach((item)=>{
      if (!seenArchiveFailures.has(item.dataset.failureKey)) {
        seenArchiveFailures.add(item.dataset.failureKey); failures.append(item);
      }
    });
    region.querySelector("[data-archive-refresh-message]").textContent="";
    archivePollingStopped=false;
  } catch (_) {
    archivePollingStopped=true;
    region.querySelector("[data-archive-refresh-message]").textContent="归档状态暂未更新。请查看任务。";
  } finally { archivePollingActive=false; }
}
async function pollStatus(manual=false) {
  if (document.querySelector("[data-archive-status]")) return pollArchiveStatus(manual);
  let region=document.querySelector("[data-summary-poll], [data-task-poll]");
  if (!region || pollingActive || (!manual && pollingStopped) ||
      document.visibilityState!=="visible" || document.querySelector('[data-submitting="true"]')) return;
  const summaryOnly=region.hasAttribute("data-summary-poll");
  if (!summaryOnly && region.contains(document.activeElement)) return;
  pollingActive=true;
  try {
    const url=new URL(region.dataset.summaryPoll || region.dataset.taskPoll,window.location.origin);
    url.search=summaryOnly ? new URLSearchParams({workbench_filter:region.dataset.filter,
      workbench_page:region.dataset.page}).toString() : window.location.search;
    const response=await fetch(url,{credentials:"same-origin",cache:"no-store",redirect:"manual",
      headers:{"HX-Request":"true"},signal:AbortSignal.timeout(10000)});
    if (response.status!==200 || response.redirected) throw new Error("status_unavailable");
    const parsed=new DOMParser().parseFromString(await response.text(),"text/html");
    if (document.visibilityState!=="visible" || document.querySelector('[data-submitting="true"]')) return;
    const next=parsed.querySelector(summaryOnly ? "[data-workbench-summary]" : "[data-task-poll]");
    if (!next) throw new Error("status_invalid");
    if (summaryOnly) {
      const old=region.querySelector("[data-summary-value]").textContent;
      const value=next.querySelector("[data-summary-value]").textContent;
      region.querySelector("[data-summary-value]").textContent=value;
      const counts=document.querySelector(".page-heading [data-workbench-counts]");
      if (counts) counts.textContent=next.querySelector("[data-workbench-counts]").textContent;
      region.querySelector("[data-last-success]").replaceWith(next.querySelector("[data-last-success]"));
      region.querySelector("[data-refresh-message]").textContent=old===value ? "" : "状态已更新";
    } else {
      if (region.contains(document.activeElement)) return;
      const facts=(node)=>[...node.querySelectorAll(".page-heading span, [data-task-fact]")].map((item)=>item.textContent).join("|");
      const changed=facts(region)!==facts(next);
      region.replaceWith(next); region=next; bindPage();
      if (changed) region.querySelector("[data-task-refresh-message]").textContent="任务状态已更新";
    }
    pollingStopped=false;
  } catch (_) {
    pollingStopped=true;
    const message=region.querySelector("[data-refresh-message], [data-task-refresh-message]");
    const stamp=region.querySelector("[data-last-success]")?.textContent || "未知时间";
    if (message) message.textContent=`状态暂未更新，显示的是 ${stamp} 的结果。请刷新状态。`;
  } finally { pollingActive=false; }
}
document.addEventListener("click",(event) => {
  if (event.target.closest("[data-refresh-status]")) {
    event.preventDefault(); pollingStopped=false; pollStatus(true);
  }
});

// 识别轮询只写独立文本节点；不会重绑页面、替换表单或处理选中集合。
let recognitionSequence = 0;
let recognitionRequest = null;
let recognitionStopped = false;
const recognitionUUID = (value) => typeof value === "string" &&
  /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value);
const recognitionStates = new Set(["not_requested", "queued", "running", "retry_wait", "succeeded",
  "failed", "stale", "unavailable", "cancelled"]);
function recognitionIdentity(row) {
  return row && recognitionUUID(row.photo_id) && recognitionStates.has(row.state) &&
    typeof row.message === "string" && row.message.length <= 200 &&
    Number.isSafeInteger(row.metadata_version) && row.metadata_version > 0 &&
    Number.isSafeInteger(row.generation) && row.generation >= 0 &&
    Number.isSafeInteger(row.active_generation) && row.active_generation >= 0 &&
    row.active_generation <= row.generation &&
    (row.generation === 0 ? row.run_id === null : recognitionUUID(row.run_id)) &&
    (row.active_generation === 0 ? row.active_run_id === null : recognitionUUID(row.active_run_id)) &&
    typeof row.retry_allowed === "boolean" && typeof row.rerun_allowed === "boolean";
}
async function recognitionJSON(url, options = {}) {
  const target = new URL(url, window.location.origin);
  if (target.origin !== window.location.origin || target.username || target.password)
    throw new Error("invalid_recognition_url");
  const response = await fetch(target, {credentials:"same-origin", cache:"no-store", redirect:"manual",
    ...options});
  if (response.status !== 200 || response.redirected || new URL(response.url).origin !== window.location.origin ||
      response.headers.get("content-type")?.split(";")[0].trim() !== "application/json")
    throw new Error("invalid_recognition_response");
  return response.json();
}
async function pollRecognitionStatus(manual = false) {
  const region = document.querySelector("[data-recognition-poll]");
  if (!region || (!manual && (recognitionStopped || recognitionRequest)) ||
      document.visibilityState !== "visible") return;
  const nodes = [...document.querySelectorAll("[data-recognition-status]")];
  const nodesById = new Map(nodes.map((node) => [node.dataset.photoId, node]));
  const url = new URL(region.dataset.recognitionPoll, window.location.origin);
  url.search = "";
  for (const id of nodesById.keys()) url.searchParams.append("photo_id", id);
  recognitionRequest?.abort();
  const controller = new AbortController();
  recognitionRequest = controller;
  const sequence = ++recognitionSequence;
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const result = await recognitionJSON(url, {signal:controller.signal});
    if (controller.signal.aborted || sequence !== recognitionSequence || !region.isConnected ||
        document.visibilityState !== "visible") return;
    if (!Array.isArray(result.photos) || result.photos.length > nodes.length ||
        new Set(result.photos.map((row) => row.photo_id)).size !== result.photos.length ||
        result.photos.some((row) => !recognitionIdentity(row) || !nodesById.has(row.photo_id)) ||
        !Number.isSafeInteger(result.pending_count) || result.pending_count < 0 ||
        !Number.isSafeInteger(result.review_count) || result.review_count < 0 ||
        typeof result.checked_at !== "string" || !Number.isFinite(Date.parse(result.checked_at)))
      throw new Error("invalid_recognition_status");
    // 先整体校验身份，旧代次/同代异run不得连带倒退数量。
    if (result.photos.some((row) => {
      const old = nodesById.get(row.photo_id).dataset;
      return row.generation < Number(old.generation) || row.active_generation < Number(old.activeGeneration) ||
        (row.generation === Number(old.generation) && (row.run_id || "") !== old.runId) ||
        (row.active_generation === Number(old.activeGeneration) && (row.active_run_id || "") !== old.activeRunId);
    })) return;
    for (const row of result.photos) {
      const node = nodesById.get(row.photo_id);
      if (!node.isConnected) return;
      const changed = row.metadata_version !== Number(node.dataset.metadataVersion);
      node.querySelector("[data-recognition-message]").textContent = changed ? "照片信息已变化，请核对。" : row.message;
      Object.assign(node.dataset, {generation:String(row.generation), runId:row.run_id || "",
        activeGeneration:String(row.active_generation), activeRunId:row.active_run_id || ""});
      const retry = node.querySelector('[data-recognition-kind="retry"]');
      if (retry && retry !== document.activeElement) retry.hidden = !row.retry_allowed || changed;
      const rerun = node.querySelector('[data-recognition-kind="rerun"]');
      if (rerun && rerun !== document.activeElement) rerun.disabled = !row.rerun_allowed || changed;
      const shown = document.querySelector("[data-recognition-candidates]")?.dataset.runId || "";
      const newer = node.querySelector("[data-recognition-new]");
      if (newer && newer !== document.activeElement)
        newer.hidden = !row.active_run_id || row.active_run_id === shown || changed;
    }
    region.querySelector("[data-recognition-count]").textContent =
      `待审核 ${result.review_count} 张 · 识别处理中 ${result.pending_count} 张`;
    const stamp = region.querySelector("[data-recognition-last-success]");
    stamp.dateTime = result.checked_at;
    stamp.textContent = new Date(result.checked_at).toISOString().slice(11, 19) + " UTC";
    region.querySelector("[data-recognition-refresh-message]").textContent = "";
    recognitionStopped = false;
  } catch (_) {
    if (sequence !== recognitionSequence || !region.isConnected) return;
    recognitionStopped = true;
    region.querySelector("[data-recognition-refresh-message]").textContent = "识别状态暂未更新，请刷新状态。";
  } finally {
    clearTimeout(timeout);
    if (recognitionRequest === controller) recognitionRequest = null;
  }
}
async function submitRecognitionAction(form, button) {
  const node = button.closest("[data-recognition-status]");
  if (!node || form.dataset.recognitionSubmitting === "true") return;
  const controls = [...node.querySelectorAll("[data-recognition-action]")];
  const version = Number(new FormData(form).get("metadata_version"));
  const requestId = crypto.randomUUID();
  const payload = button.dataset.recognitionKind === "retry" ?
    {request_id:requestId, metadata_version:version} :
    {request_id:requestId, photos:[{photo_id:node.dataset.photoId, metadata_version:version}]};
  const message = node.querySelector("[data-recognition-action-message]");
  form.dataset.recognitionSubmitting = "true";
  const enabled = controls.filter((control) => !control.disabled);
  enabled.forEach((control) => { control.disabled = true; });
  recognitionSequence++; recognitionRequest?.abort();
  try {
    const result = await recognitionJSON(button.dataset.recognitionAction, {method:"POST",
      signal:AbortSignal.timeout(20000), body:JSON.stringify(payload), headers:{
        "Content-Type":"application/json", "X-CSRFToken":new FormData(form).get("csrfmiddlewaretoken")}});
    const row = button.dataset.recognitionKind === "retry" ? result : result.results?.[0];
    if (!row || row.photo_id !== node.dataset.photoId || typeof row.accepted !== "boolean")
      throw new Error("invalid_acceptance");
    if (!row.accepted) {
      const reasons = {photo_changed:"照片信息已变化，请核对。", source_changed:"来源已变化，请核对。",
        retry_not_allowed:"当前不可重试，可手动选种。", database_busy:"暂时繁忙，请稍后重试。"};
      message.textContent = reasons[row.reason] || "请求未受理，请核对。";
      return;
    }
    if (!recognitionUUID(row.run_id) || !Number.isSafeInteger(row.generation) || row.generation < 1)
      throw new Error("invalid_acceptance");
    if (row.generation >= Number(node.dataset.generation)) {
      node.dataset.generation = String(row.generation); node.dataset.runId = row.run_id;
    }
    message.textContent = "";
    recognitionStopped = false;
  } catch (_) {
    // POST不自动重放；网络不确定时先读状态，原草稿与焦点保持。
    message.textContent = "暂未收到识别请求结果，请刷新状态核对。";
  } finally {
    form.dataset.recognitionSubmitting = "false";
    enabled.forEach((control) => { control.disabled = false; });
    pollRecognitionStatus(true);
  }
}
setInterval(async () => { await pollStatus(); await pollRecognitionStatus(); }, 5000);
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-recognition-refresh]")) {
    event.preventDefault(); pollRecognitionStatus(true);
  }
});

// Online prose lives only in this page's bounded, disposable memory.
const guideOnlineCache = new Map();
const guideOnlinePending = new Map();
const guideOnlineCooldown = new Map();
const guideOnlineEndpoints = Object.freeze({wd:"https://www.wikidata.org/w/api.php",
  zh:"https://zh.wikipedia.org/w/api.php", en:"https://en.wikipedia.org/w/api.php"});
const guideNow = () => performance.now();
function guideLink(language, pageid) {
  if (!Number.isSafeInteger(pageid) || pageid < 1) throw new Error("invalid_page");
  return `https://${language}.wikipedia.org/w/index.php?curid=${pageid}`;
}
function guideSearch(name) {
  return "https://zh.wikipedia.org/w/index.php?" + new URLSearchParams({title:"Special:Search", search:name});
}
async function guideReadJSON(response, signal) {
  if (!response.body?.getReader) throw new Error("invalid_stream");
  const reader = response.body.getReader();
  const chunks = []; let length = 0;
  try {
    while (true) {
      if (signal.aborted) throw new Error("cancelled");
      const {done, value} = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > 256 * 1024) throw new Error("response_limit");
      chunks.push(value);
    }
  } catch (error) { await reader.cancel().catch(() => {}); throw error; }
  finally { reader.releaseLock(); }
  const bytes = new Uint8Array(length); let position = 0;
  for (const chunk of chunks) { bytes.set(chunk, position); position += chunk.byteLength; }
  const result = JSON.parse(new TextDecoder("utf-8", {fatal:true}).decode(bytes));
  if (!result || typeof result !== "object") throw new Error("api_error");
  return result;
}
async function guideLookup(config, controller) {
  const started = guideNow(); let calls = 0;
  async function get(provider, fields) {
    if (controller.signal.aborted || guideNow() - started >= 10000) throw new Error("cancelled");
    if ((guideOnlineCooldown.get(provider) || 0) > guideNow()) throw new Error("cooldown");
    if (++calls > 4) throw new Error("request_limit");
    const endpoint = new URL(guideOnlineEndpoints[provider]);
    endpoint.search = new URLSearchParams({format:"json", origin:"*", ...fields});
    const response = await fetch(endpoint.href, {method:"GET", mode:"cors", credentials:"omit",
      referrerPolicy:"no-referrer", cache:"no-store", redirect:"error", signal:controller.signal});
    if (response.status === 429 || response.status === 503) {
      const retry = response.headers.get("Retry-After");
      const seconds = retry && /^\d+$/.test(retry) ? Number(retry) :
        retry && Number.isFinite(Date.parse(retry)) ? Math.max(0, (Date.parse(retry)-Date.now())/1000) : 60;
      guideOnlineCooldown.set(provider, guideNow() + seconds * 1000);
      throw new Error("cooldown");
    }
    if (!response.ok) throw new Error("network");
    const result = await guideReadJSON(response, controller.signal);
    if (controller.signal.aborted || guideNow()-started >= 10000) throw new Error("cancelled");
    if (result.error || result.errors || result.success === 0 || result.warnings) {
      if (result.error?.code === "maxlag" || result.error?.code === "ratelimited") {
        guideOnlineCooldown.set(provider, guideNow()+60000);
        throw new Error("cooldown");
      }
      throw new Error("api_error");
    }
    return result;
  }
  const name = (config.scientific_name || config.name || "").slice(0, 300);
  if (!name) return {status:"missing_name"};
  if (!/^Q[1-9]\d*$/.test(config.qid || "") || !config.scope || !config.concept_key || !config.mapping_url) {
    const data = await get("zh", {action:"query", formatversion:"2", list:"search", srnamespace:"0",
      srlimit:"3", srprop:"", srsearch:name});
    const candidates = (data.query?.search || []).slice(0, 3).filter(row =>
      Number.isSafeInteger(row.pageid) && row.pageid > 0 && typeof row.title === "string")
      .map(row => ({title:row.title.slice(0, 300), url:guideLink("zh", row.pageid)}));
    return {status:candidates.length ? "unmatched" : "no_result", candidates};
  }
  const data = await get("wd", {action:"wbgetentities", ids:config.qid, props:"claims|sitelinks", sitefilter:"zhwiki|enwiki"});
  const entity = data.entities?.[config.qid];
  const names = (entity?.claims?.P225 || []).filter(claim => claim.rank !== "deprecated")
    .map(claim => claim.mainsnak?.datavalue?.value);
  if (entity?.id !== config.qid || Object.prototype.hasOwnProperty.call(entity, "missing") ||
      Object.prototype.hasOwnProperty.call(entity, "invalid") ||
      !names.length || names.some(value => value !== config.scientific_name))
    return {status:"unmatched"};
  const language = entity.sitelinks?.zhwiki?.title ? "zh" : entity.sitelinks?.enwiki?.title ? "en" : null;
  if (!language) return {status:"no_result"};
  const title = entity.sitelinks[language + "wiki"].title;
  if (typeof title !== "string" || !title.trim() || title.length > 300) throw new Error("invalid_title");
  const pageData = await get(language, {action:"query", formatversion:"2", titles:title,
    prop:"extracts|pageprops|info|revisions", ppprop:"wikibase_item|disambiguation", inprop:"url",
    explaintext:"1", exintro:"1", exchars:"800", exlimit:"1", rvprop:"ids|timestamp"});
  const pages = pageData.query?.pages;
  if (!Array.isArray(pages) || pages.length !== 1 || pages[0].missing !== undefined || pages[0].invalid !== undefined) return {status:"no_result"};
  const page = pages[0];
  if (Object.prototype.hasOwnProperty.call(page.pageprops || {}, "disambiguation")) return {status:"ambiguous"};
  if (page.pageprops?.wikibase_item !== config.qid) return {status:"unmatched"};
  const url = guideLink(language, page.pageid);
  return {status:language !== "zh" ? "foreign" : typeof page.extract === "string" && page.extract.trim() ? "ready" : "empty",
    text:language === "zh" && typeof page.extract === "string" ? page.extract.slice(0, 800) : "",
    url, title:typeof page.title === "string" ? page.title.slice(0, 300) : title, language,
    edited:page.revisions?.[0]?.timestamp || "未知", revision:page.revisions?.[0]?.revid || "未知",
    retrieved:new Date().toISOString()};
}
function guideRender(region, result) {
  const output = region.querySelector("[data-guide-online-result]");
  const append = (tag, text, href) => {
    const element = document.createElement(tag); element.textContent = text;
    if (href) { element.href = href; element.rel = "noreferrer"; }
    output.appendChild(element); return element;
  };
  // On failure we never enter this renderer: existing reading stays in place.
  output.replaceChildren();
  if (result.text) append("p", result.text);
  if (result.url) {
    append("a", `${result.title} · 原站阅读（${result.language}）`, result.url);
    append("p", "维基百科贡献者 · 摘录／截断，当前页面摘要（非固定版本快照）");
    const sourceLinks = append("div", ""); sourceLinks.className = "guide-source-links";
    sourceLinks.appendChild(append("a", "CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"));
    sourceLinks.appendChild(append("a", "作者历史", `https://${result.language}.wikipedia.org/w/index.php?` +
      new URLSearchParams({title:result.title, action:"history"})));
    append("p", `页面编辑于：${result.edited} · 观测版本：${result.revision} · 检索于：${result.retrieved}；不是评估日期。`);
    for (const heading of ["外形", "习性", "分布"]) append("p", heading + "：").appendChild(
      (() => { const a = document.createElement("a"); a.textContent = "原站阅读"; a.href = result.url; a.rel = "noreferrer"; return a; })());
  }
  for (const candidate of result.candidates || []) append("p", "原站候选（未经物种匹配）：").appendChild(
    (() => { const a = document.createElement("a"); a.textContent = candidate.title; a.href = candidate.url; a.rel = "noreferrer"; return a; })());
}
const guideOnlineMessages = {ready:"已取得中文摘要。", empty:"中文摘要暂缺，可在原站阅读。",
  foreign:"暂无已匹配中文资料；仅有外文来源。", unmatched:"资料待匹配；检索结果不能证明为同一物种。",
  ambiguous:"来源为消歧页面，资料待匹配。", no_result:"未找到资料，可重试或原站检索。", missing_name:"缺少公开鸟名，资料待匹配。"};
async function guideLoad(region, retry = false) {
  const config = JSON.parse(document.getElementById(region.dataset.configId).textContent);
  const key = config.species_id; const sequence = (region.guideSequence || 0) + 1;
  region.guideSequence = sequence;
  const status = region.querySelector("[data-guide-online-status]");
  const button = region.querySelector("[data-guide-online-retry]");
  const cancel = region.querySelector("[data-guide-online-cancel]");
  status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
  button.disabled = false; cancel.hidden = true; cancel.disabled = true;
  region.guideController = null;
  const cached = guideOnlineCache.get(key);
  if (!retry && cached && cached.expires > guideNow()) {
    guideRender(region, cached.result); status.textContent = guideOnlineMessages[cached.result.status]; return;
  }
  status.textContent = "正在检索资料…"; button.disabled = true;
  cancel.hidden = false; cancel.disabled = false;
  let pending = guideOnlinePending.get(key);
  if (!pending) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    pending = {controller};
    pending.promise = guideLookup(config, controller).then(result => {
      if (controller.signal.aborted) throw new Error("cancelled");
      guideOnlineCache.set(key, {expires:guideNow()+60000, result});
      while (guideOnlineCache.size > 32) guideOnlineCache.delete(guideOnlineCache.keys().next().value);
      return result;
    }).finally(() => { clearTimeout(timer); if (guideOnlinePending.get(key) === pending) guideOnlinePending.delete(key); });
    guideOnlinePending.set(key, pending);
  }
  region.guideController = pending.controller;
  try {
    const result = await pending.promise;
    if (!region.isConnected || region.guideSequence !== sequence || pending.controller.signal.aborted) return;
    guideRender(region, result); status.textContent = guideOnlineMessages[result.status];
  } catch (error) {
    if (region.isConnected && region.guideSequence === sequence) {
      if (!pending.controller.signal.aborted) {
        status.setAttribute("role", "alert"); status.removeAttribute("aria-live");
      }
      status.textContent = pending.controller.signal.aborted ?
        "检索已取消或超时，可重试或原站阅读。" : error.message === "cooldown" ?
        "来源繁忙，请稍后再试（冷却中）。" : "在线资料暂不可用（检索失败），可重试或原站阅读。";
    }
  } finally {
    if (region.guideSequence === sequence) {
      button.disabled = false; cancel.hidden = true; cancel.disabled = true;
      region.guideController = null;
    }
  }
}
function bindGuideOnline() {
  document.querySelectorAll("[data-guide-online]").forEach(region => {
    if (region.dataset.onlineBound) return;
    region.dataset.onlineBound = "true";
    region.querySelector("[data-guide-online-retry]").addEventListener("click", () => guideLoad(region, true));
    region.querySelector("[data-guide-online-cancel]").addEventListener("click", () => region.guideController?.abort());
    region.querySelector("[data-guide-online-retry]").hidden = false;
    guideLoad(region);
  });
}
bindGuideOnline();
document.addEventListener("htmx:afterSwap", bindGuideOnline);
function cancelGuideOnline() {
  for (const pending of guideOnlinePending.values()) pending.controller.abort();
  guideOnlinePending.clear();
}
window.addEventListener("pagehide", cancelGuideOnline);
window.addEventListener("pageshow", event => {
  if (event.persisted) document.querySelectorAll("[data-guide-online]").forEach(region => guideLoad(region));
});
window.addEventListener("popstate", cancelGuideOnline);
document.addEventListener("htmx:beforeCleanupElement", event => {
  const region = event.detail?.elt;
  if (region?.matches?.("[data-guide-online]") || region?.querySelector?.("[data-guide-online]")) cancelGuideOnline();
});
