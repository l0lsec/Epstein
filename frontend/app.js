/**
 * Epstein Files Public Archive - Frontend Application
 */

const API_BASE = window.location.origin + '/api';

// State
let state = {
    currentView: 'search',
    stats: null,
    categories: [],
    subcategories: [],
    browsePage: 0,
    browseLimit: 24,
    browseCategory: '',
    browseSubcategory: '',
    browseFileType: '',
    browseFilename: '',
    browseKeyword: '',
    browseTotal: 0,
    searchSubcategory: '',
    currentDocument: null,
    // Search pagination state
    searchPage: 0,
    searchLimit: 50,  // Results per page
    searchTotal: 0,
    lastSearchParams: null,  // Store last search to enable pagination
    // Document navigation state
    documentList: [],  // Current list of documents (from search or browse)
    documentIndex: -1,  // Current index within documentList
    // User category exclusion preferences (stored in localStorage)
    excludedCategories: loadExcludedCategories(),
    // Prefetched first browse page (from bootstrap), consumed once
    _prefetchedBrowse: null,
    // URL state / async guards (see ux modules below)
    restoreSeq: 0,           // newest URL restore wins (Back/Forward during a slow restore)
    renderedKey: '',         // canonical base-query of what is currently rendered
    modalPushed: false,      // the open document pushed its own history entry
    openDocId: null,
    searchSeq: 0, browseSeq: 0, docSeq: 0,   // drop stale responses
    ageConfirmed: false,
    _initialized: false,
    _delegated: false,
    askDisabled: false,
    _subSeq: {},
    _refocusSearchBtn: false,
    _ageGateBound: false
};

// DOM Elements
const elements = {};

// ---- UX plumbing: constants and shared mutable bits ----------------------------------
const SITE_URL = 'https://epsteinfta.com';
const SITE_TITLE = 'Epstein Files Public Archive';
const DEFAULT_TITLE = 'Epstein Files Public Archive | Public Document Search';
const SEARCH_DEFAULT_TYPE = 'fulltext';
const SEARCH_TYPES = ['fulltext', 'hybrid', 'semantic'];
const SEARCH_TYPE_HINTS = {
    fulltext: 'Full Text matches your exact words. Best for names, dates and file numbers.',
    hybrid: 'Hybrid adds AI-found related passages to the exact matches. Slower.',
    semantic: 'Semantic AI finds passages with a similar meaning, even without your exact words.'
};
const FILE_TYPE_LABELS = { pdf: 'PDF Documents', document: 'Scanned Documents', audio: 'Audio', image: 'Images', video: 'Video' };
const VALID_KINDS = ['pdf', 'document', 'image', 'audio', 'video'];
const VALID_VIEWS = ['search', 'browse', 'ask', 'about', 'privacy'];
const VIEW_TITLES = { search: 'Search', browse: 'Browse Documents', ask: 'Ask the AI Assistant', about: 'About This Archive', privacy: 'Privacy Policy' };
const SEARCH_BTN_HTML = '<span>Search</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M5 12h14m-7-7l7 7-7 7"/></svg>';
const ASK_BTN_HTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg> Ask Question';
const MOBILE_QUERY = window.matchMedia ? window.matchMedia('(max-width: 768px)') : { matches: false };
const REDUCED_MOTION = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
const dialogStack = [];
// Buttons that open a popover (share menus, exclude menu). Disclosures such as Filters/Tips are NOT popovers.
const _announceQueue = { status: [], alert: [] };
const _announceTimer = {};
const POPOVER_BTN = 'button[aria-controls][aria-haspopup], button[aria-controls][data-popover]';
const POPOVER_OPEN = '[aria-expanded="true"][aria-controls][aria-haspopup], [aria-expanded="true"][data-popover]';

// Every former inline onclick="" is now a data-action handled by one delegated listener.
const ACTIONS = {
    'open-donate': (el) => openDonateModal(el.dataset.source),
    'close-donate': () => closeDonateModal(),
    'dismiss-banner': () => dismissDonationBanner(),
    'clear-exclusions': () => clearExclusions(),
    'goto-view': (el, e) => {
        if (isModifiedClick(e)) return;
        e.preventDefault();
        switchView(el.dataset.view, { userInitiated: true });
        window.scrollTo({ top: 0, behavior: scrollBehavior() });
    },
    'copy-site-link': (el) => copySiteLink(el),
    'open-gallery': () => openAlteredGallery(),
    'close-gallery': () => closeAlteredGallery(),
    'open-compare': (el) => openPublicCompare(el.dataset.old, el.dataset.new, el.dataset.title),
    'open-exposed-compare': () => openExposedCompare(),
    'close-compare': () => closePublicCompare(),
    'compare-tab': (el) => switchPublicCompareTab(el.dataset.tab),
    'open-doc': (el, e) => {
        if (isModifiedClick(e)) return;
        e.preventDefault();
        openDocument(el.dataset.docId, docIndexFor(el), { trigger: el });
    },
    'edit-search': () => editSearch(),
    'remove-filter': (el) => removeSearchFilter(el.dataset.filter),
    'clear-search-filters': () => clearSearchFilters(),
    'switch-search-type': (el) => switchSearchType(el.dataset.type),
    'open-search-help': () => openSearchHelp(),
    'search-first-page': () => { state.searchPage = 0; runSearch({ history: 'push', focusHeading: true }); },
    'retry-search': () => runSearch({ history: 'none', focusHeading: true }),
    'retry-browse': () => loadDocuments({ history: 'none', focusHeading: true }),
    'retry-doc': (el) => openDocument(el.dataset.docId, -1, { history: 'none' }),   // -1: recompute the index for THIS document
    'close-doc': () => requestCloseDocument(),
    'clear-browse-filters': () => clearBrowseFilters(),
    'toggle-pinned': (el) => togglePinnedMotion(el)
};

// =============================================================================
// UX modules: core plumbing, URL state, search flow, document modal
// =============================================================================

function isModifiedClick(e) {
    return !!e && (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey);
}

function setHidden(el, hidden) {
    if (el) el.classList.toggle('hidden', !!hidden);
}

function scrollBehavior() {
    return REDUCED_MOTION.matches ? 'auto' : 'smooth';
}

function docIndexFor(el) {
    const n = parseInt(el && el.dataset ? el.dataset.index : '', 10);
    return Number.isFinite(n) ? n : -1;
}

/** Per-dialog live regions: some screen readers ignore content outside an aria-modal dialog. */
function ensureDialogLive(content) {
    let host = content.querySelector(':scope > [data-dlg-live]');
    if (!host) {
        host = document.createElement('div');
        host.className = 'sr-only';
        host.setAttribute('data-dlg-live', '');
        host.innerHTML = '<div role="status" aria-live="polite" aria-atomic="true"></div><div role="alert" aria-live="assertive" aria-atomic="true"></div>';
        content.appendChild(host);
    }
    return { polite: host.children[0], alert: host.children[1] };
}

function liveRegion(assertive) {
    const top = topDialog();
    return top && top.live ? (assertive ? top.live.alert : top.live.polite)
        : document.getElementById(assertive ? 'a11y-alert' : 'a11y-status');
}

/**
 * Speak a message through a visually hidden live region (the open dialog's own region when there is one).
 * Polite by default; assertive for errors that need immediate attention.
 * Messages sent within the same ~60ms window are joined, so one never silences another.
 * The target region is resolved when the text is written, so a dialog that closed in between is never used.
 */
function announce(message, { assertive = false } = {}) {
    const key = assertive ? 'alert' : 'status';
    const queue = _announceQueue[key];
    if (!queue.includes(message)) queue.push(message);
    clearTimeout(_announceTimer[key]);
    const first = liveRegion(assertive);
    if (first) first.textContent = '';   // clearing first, then writing later, makes screen readers re-announce identical text
    _announceTimer[key] = setTimeout(() => {
        const el = liveRegion(assertive);
        if (el) el.textContent = queue.join('. ');
        queue.length = 0;
    }, 60);
}

/** Toasts live inside the top dialog while one is open (same reason as above). */
function toastRegion() {
    const top = topDialog();
    if (!top) return document.getElementById('toast-region');
    let region = top.content.querySelector(':scope > .toast-region');
    if (!region) {
        region = document.createElement('div');
        region.className = 'toast-region';
        region.setAttribute('role', 'region');
        region.setAttribute('aria-label', 'Notifications');
        top.content.appendChild(region);
    }
    return region;
}

/** Non-blocking replacement for alert(). */
function showToast(message, { type = 'info', actionLabel = '', onAction = null, timeout = 7000 } = {}) {
    const region = toastRegion();
    if (!region) return null;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
    const msg = document.createElement('span');
    msg.className = 'toast-msg';
    msg.textContent = message;
    toast.appendChild(msg);
    if (actionLabel && onAction) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = actionLabel;
        btn.addEventListener('click', () => {
            const dlg = topDialog();
            const inDialog = !!(dlg && dlg.content.contains(toast));
            toast.remove();
            onAction();
            // the clicked button is gone: keep keyboard focus inside the dialog instead of dropping to <body>
            if (inDialog && dlg.content.isConnected) dlg.content.focus({ preventScroll: true });
        });
        toast.appendChild(btn);
    }
    region.appendChild(toast);
    if (timeout) setTimeout(() => toast.remove(), timeout);
    return toast;
}

function focusableIn(root) {
    const sel = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), iframe, audio[controls], video[controls], [tabindex]:not([tabindex="-1"])';
    return [...root.querySelectorAll(sel)].filter(el =>
        el.tabIndex >= 0 && !el.closest('[inert]') && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden');
}

/** Make everything outside the top dialog inert (not focusable, not announced). */
function applyDialogInert() {
    document.querySelectorAll('[data-dlg-inert]').forEach(el => {
        el.inert = false;
        el.removeAttribute('data-dlg-inert');
    });
    const top = dialogStack[dialogStack.length - 1];
    if (!top) return;
    let node = top.el;
    while (node && node !== document.body && node.parentElement) {
        const parent = node.parentElement;
        for (const sib of parent.children) {
            if (sib === node || sib.hasAttribute('data-keep-active') || ['SCRIPT', 'STYLE', 'LINK'].includes(sib.tagName)) continue;
            if (!sib.inert) {
                sib.inert = true;
                sib.setAttribute('data-dlg-inert', '');
            }
        }
        node = parent;
    }
}

function openDialog(modalEl, { trigger = null, initialFocus = null, onRequestClose = null } = {}) {
    if (!modalEl) return;
    const content = modalEl.matches('[role="dialog"]') ? modalEl : (modalEl.querySelector('[role="dialog"]') || modalEl);
    const existing = dialogStack.findIndex(d => d.el === modalEl);
    if (existing !== -1) dialogStack.splice(existing, 1);
    const entry = { el: modalEl, content, trigger: trigger || document.activeElement, onRequestClose, live: ensureDialogLive(content) };
    dialogStack.push(entry);
    modalEl.classList.remove('hidden');
    document.body.classList.add('modal-open');
    document.body.style.overflow = 'hidden';
    applyDialogInert();
    const target = (initialFocus && content.querySelector(initialFocus)) || content;
    if (target && typeof target.focus === 'function') target.focus({ preventScroll: true });
}

function closeDialog(modalEl, { restoreFocus = true } = {}) {
    if (!modalEl) return;
    const idx = dialogStack.findIndex(d => d.el === modalEl);
    const entry = idx !== -1 ? dialogStack.splice(idx, 1)[0] : null;
    modalEl.classList.add('hidden');
    // Whatever the dialog was showing must not leak into its next opening: stale toasts, an open share menu.
    modalEl.querySelectorAll('.toast-region .toast').forEach(t => t.remove());
    closePopovers();
    applyDialogInert();
    if (!dialogStack.length) {
        document.body.classList.remove('modal-open');
        document.body.style.overflow = '';
    }
    if (entry && restoreFocus) {
        const t = entry.trigger;
        if (t && t.isConnected && typeof t.focus === 'function' && t.getClientRects().length > 0) {
            t.focus({ preventScroll: true });
        } else {
            const fallback = document.getElementById('results-title') && !document.getElementById('search-results').classList.contains('hidden')
                ? document.getElementById('results-title')
                : document.getElementById('main');
            if (fallback) fallback.focus({ preventScroll: true });
        }
    }
}

function topDialog() {
    return dialogStack[dialogStack.length - 1] || null;
}

/** Keep Tab / Shift+Tab inside the top dialog. */
function trapDialogTab(e) {
    const top = topDialog();
    if (!top) return;
    const items = focusableIn(top.content);
    if (!items.length) { e.preventDefault(); top.content.focus(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    const inside = top.content.contains(active);
    if (e.shiftKey && (active === first || active === top.content || !inside)) {
        e.preventDefault();
        last.focus();
    } else if (!e.shiftKey && (active === last || !inside)) {
        e.preventDefault();
        first.focus();
    }
}

function togglePopover(btn, force) {
    const menu = document.getElementById(btn.getAttribute('aria-controls'));
    if (!menu) return;
    const open = typeof force === 'boolean' ? force : menu.classList.contains('hidden');
    if (open) closePopovers(menu);
    menu.classList.toggle('hidden', !open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && menu.getAttribute('role') === 'menu') {
        const first = menu.querySelector('[role="menuitem"]');
        if (first) first.focus();
    }
}

/** Close every open popover (optionally keeping one). Returns true when something closed. */
function closePopovers(except = null) {
    let closed = false;
    document.querySelectorAll(POPOVER_OPEN).forEach(btn => {
        const menu = document.getElementById(btn.getAttribute('aria-controls'));
        if (menu && menu === except) return;
        if (menu) menu.classList.add('hidden');
        btn.setAttribute('aria-expanded', 'false');
        closed = true;
    });
    return closed;
}

function handleMenuKeydown(e) {
    const menu = e.target.closest('[role="menu"]');
    if (!menu) return;
    const items = [...menu.querySelectorAll('[role="menuitem"]')];
    if (!items.length) return;
    const i = items.indexOf(document.activeElement);
    let next = -1;
    if (e.key === 'ArrowDown') next = (i + 1) % items.length;
    else if (e.key === 'ArrowUp') next = (i - 1 + items.length) % items.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = items.length - 1;
    if (next >= 0) { e.preventDefault(); items[next].focus(); }
}

function syncTabs(tablist, selectedTab) {
    if (!tablist) return;
    tablist.querySelectorAll('[role="tab"]').forEach(t => {
        const sel = t === selectedTab;
        t.setAttribute('aria-selected', sel ? 'true' : 'false');
        t.tabIndex = sel ? 0 : -1;
        t.classList.toggle('active', sel);
    });
}

function handleTablistKeydown(e) {
    const tab = e.target.closest('[role="tab"]');
    if (!tab) return;
    const tabs = [...tab.closest('[role="tablist"]').querySelectorAll('[role="tab"]')].filter(t => t.getClientRects().length > 0);
    const i = tabs.indexOf(tab);
    if (i === -1) return;
    let next = i;
    if (e.key === 'ArrowRight') next = (i + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = tabs.length - 1;
    e.preventDefault();
    tabs[next].focus();
    tabs[next].click();
}

function handleEscape(e) {
    const popBtn = document.querySelector(POPOVER_OPEN);
    if (popBtn) {
        e.preventDefault();
        closePopovers();
        popBtn.focus();
        return;
    }
    const pdf = document.getElementById('modal-pdf-viewer');
    if (pdf && pdf.classList.contains('fullscreen')) {
        e.preventDefault();
        togglePdfFullscreen(pdf);
        return;
    }
    const top = topDialog();
    if (top && typeof top.onRequestClose === 'function') {
        e.preventDefault();
        top.onRequestClose();
    }
}

function setupGlobalDelegation() {
    document.addEventListener('click', (e) => {
        const target = e.target;
        if (!(target instanceof Element)) return;

        // Header Share: phones get the native share sheet; desktops get the menu (with Copy link) even when
        // the browser also exposes navigator.share.
        if (target.closest('#site-share-btn') && typeof navigator.share === 'function' && (MOBILE_QUERY.matches || isMobileDevice())) {
            closePopovers();
            navigator.share({ title: SITE_TITLE, text: 'Search court records, flight logs & DOJ disclosures. Free & open source.', url: SITE_URL }).catch(() => {});
            return;
        }

        // Popovers: close any that the click landed outside of.
        const popBtn = target.closest(POPOVER_BTN);
        const keepMenu = popBtn ? document.getElementById(popBtn.getAttribute('aria-controls')) : null;
        document.querySelectorAll(POPOVER_OPEN).forEach(btn => {
            const menu = document.getElementById(btn.getAttribute('aria-controls'));
            if (btn.contains(target) || (menu && menu.contains(target))) return;
            if (menu && menu === keepMenu) return;
            if (menu) menu.classList.add('hidden');
            btn.setAttribute('aria-expanded', 'false');
        });
        if (popBtn) togglePopover(popBtn);

        // Share menu options
        const opt = target.closest('.share-menu .share-option');
        if (opt) {
            const menu = opt.closest('.share-menu');
            const owner = document.querySelector(`[aria-controls="${menu.id}"]`);
            closePopovers();
            if (owner) owner.focus({ preventScroll: true });
            if (menu.id === 'site-share-menu') handleSiteShare(opt.dataset.platform, opt);
            else if (menu.id === 'search-share-menu') handleSearchShare(opt.dataset.platform, opt);
            else handleShare(opt.dataset.platform, opt);
            return;
        }

        // data-action
        const actionEl = target.closest('[data-action]');
        if (actionEl) {
            const fn = ACTIONS[actionEl.dataset.action];
            if (fn) { fn(actionEl, e); return; }
        }

        // Whole-card click (results and browse cards): open unless the click was on a link/button or text is being selected.
        const card = target.closest('[data-doc-card]');
        if (card && !target.closest('a[href], button') && !(window.getSelection && String(window.getSelection()).length)) {
            openDocument(card.dataset.docId, docIndexFor(card), { trigger: card.querySelector('a[data-action="open-doc"]') || card });
        }
    });

    document.addEventListener('keydown', (e) => {
        if (e.defaultPrevented) return;
        if (e.key === 'Escape') { handleEscape(e); return; }
        if (e.key === 'Tab') {
            if (e.target instanceof Element && e.target.closest('[role="menu"]')) closePopovers();
            trapDialogTab(e);
            return;
        }
        const t = e.target instanceof Element ? e.target : null;
        if (t && t.closest('[role="tablist"]') && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
            handleTablistKeydown(e);
            return;
        }
        if (t && t.closest('[role="menu"]') && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
            handleMenuKeydown(e);
            return;
        }
        // Arrow keys step through the result list while the document modal is the top dialog.
        if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && !(e.metaKey || e.ctrlKey || e.altKey || e.shiftKey)
            && isDocumentOpen() && topDialog() && topDialog().el === elements.modal && state.documentList.length > 1
            && !(t && t.closest('input, textarea, select, audio, video, [contenteditable], [role="tablist"], [role="menu"]'))) {
            e.preventDefault();
            navigateDocument(e.key === 'ArrowLeft' ? -1 : 1);
        }
    });

    // Thumbnails that fail to load reveal their icon fallback (replaces inline onerror="").
    document.addEventListener('error', (e) => {
        const img = e.target;
        if (!(img instanceof HTMLImageElement)) return;
        const wrap = img.closest('.result-thumbnail, .document-thumbnail, .pinned-card-thumbnail');
        if (!wrap) return;
        img.classList.add('hidden');
        const fb = wrap.querySelector('.thumbnail-fallback');
        if (fb) fb.classList.add('is-visible');
    }, true);

    // Media downloads are intentionally discouraged in the viewer (replaces inline oncontextmenu="").
    document.addEventListener('contextmenu', (e) => {
        if (e.target instanceof Element && e.target.closest('.audio-element, .video-element')) e.preventDefault();
    });
}

/** The header and the refine bar are sticky: keep their heights in CSS variables (sticky offset + scroll-padding). */
function syncHeaderHeight() {
    const header = document.querySelector('.header');
    if (!header) return;
    const root = document.documentElement;
    const set = () => root.style.setProperty('--header-h', `${header.offsetHeight}px`);
    set();
    if (window.ResizeObserver) new ResizeObserver(set).observe(header);
    else window.addEventListener('resize', set);

    const bar = document.getElementById('refine-bar');
    if (bar) {
        const setBar = () => root.style.setProperty('--refine-h', bar.offsetParent ? `${bar.offsetHeight}px` : '0px');
        setBar();
        if (window.ResizeObserver) new ResizeObserver(setBar).observe(bar);
    }
}

function updateDocumentTitle() {
    let title = DEFAULT_TITLE;
    if (isDocumentOpen() && state.currentDocument && state.currentDocument.filename) {
        title = `${state.currentDocument.filename} | ${SITE_TITLE}`;
    } else if (state.currentView === 'search') {
        if (state.lastSearchParams && state.lastSearchParams.query) title = `“${state.lastSearchParams.query}” – Search | ${SITE_TITLE}`;
    } else {
        title = `${VIEW_TITLES[state.currentView] || 'Search'} | ${SITE_TITLE}`;
    }
    document.title = title;
}

function readUrlState() {
    const p = new URLSearchParams(window.location.search);
    const view = VALID_VIEWS.includes(p.get('view')) ? p.get('view') : 'search';
    const page = (() => { const n = parseInt(p.get('page') || '', 10); return Number.isFinite(n) && n > 0 ? n : 1; })();
    // Real calendar dates only (2020-13-99 matches the shape but is not a date).
    const date = (k) => {
        const v = p.get(k) || '';
        const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
        if (!m) return '';
        const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
        return d.getUTCFullYear() === +m[1] && d.getUTCMonth() === +m[2] - 1 && d.getUTCDate() === +m[3] ? v : '';
    };
    const kind = VALID_KINDS.includes(p.get('kind')) ? p.get('kind') : '';
    const doc = /^[a-zA-Z0-9_\-]+$/.test(p.get('doc') || '') ? p.get('doc') : '';
    return {
        view,
        q: (p.get('q') || '').trim().slice(0, 500),
        type: SEARCH_TYPES.includes(p.get('type')) ? p.get('type') : SEARCH_DEFAULT_TYPE,
        set: (p.get('set') || '').slice(0, 200),
        section: (p.get('section') || '').slice(0, 200),
        kind,
        from: date('from'),
        to: date('to'),
        page,
        name: (p.get('name') || '').slice(0, 200),
        topic: (p.get('topic') || '').slice(0, 200),
        doc
    };
}

function fieldsForView(view) {
    if (view === 'search') {
        const s = state.lastSearchParams;
        if (!s) return {};
        return {
            q: s.query, type: s.search_type, set: s.category || '', section: s.subcategory || '', kind: s.file_type || '',
            from: s.date_from || '', to: s.date_to || '', page: state.searchPage + 1
        };
    }
    if (view === 'browse') {
        return {
            name: state.browseFilename, topic: state.browseKeyword, set: state.browseCategory,
            section: state.browseSubcategory, kind: state.browseFileType, page: state.browsePage + 1
        };
    }
    return {};
}

function baseQuery(view, f) {
    const p = new URLSearchParams();
    if (view !== 'search') p.set('view', view);
    const add = (k, v) => { if (v) p.set(k, v); };
    if (view === 'search' && f.q) {
        p.set('q', f.q);
        if (f.type && f.type !== SEARCH_DEFAULT_TYPE) p.set('type', f.type);
        add('set', f.set); add('section', f.section); add('kind', f.kind); add('from', f.from); add('to', f.to);
        if (f.page > 1) p.set('page', String(f.page));
    } else if (view === 'browse') {
        add('name', f.name); add('topic', f.topic); add('set', f.set); add('section', f.section); add('kind', f.kind);
        if (f.page > 1) p.set('page', String(f.page));
    }
    return p;
}

function currentBaseKey() {
    return baseQuery(state.currentView, fieldsForView(state.currentView)).toString();
}

function urlBaseKey(u) {
    return baseQuery(u.view, u).toString();
}

function buildUrl({ doc = '' } = {}) {
    const p = baseQuery(state.currentView, fieldsForView(state.currentView));
    if (doc) p.set('doc', doc);
    const qs = p.toString();
    return window.location.pathname + (qs ? `?${qs}` : '');
}

/**
 * Write the current state to the address bar (no-op for mode 'none' or when unchanged).
 * Restores from the URL pass history:'none' everywhere, so there is no global "restoring" guard: a visitor who acts
 * while a slow Back/Forward re-render is still loading must still get their action written to the URL.
 */
function syncUrl(mode, { doc = '', overlay = false } = {}) {
    if (mode === 'none') return;
    const url = buildUrl({ doc });
    if (url === window.location.pathname + window.location.search) return;
    try {
        history[mode === 'replace' ? 'replaceState' : 'pushState'](overlay ? { ov: 1 } : {}, '', url);
    } catch (e) { /* history API can be blocked in sandboxed frames */ }
}

function setSelectValue(select, value) {
    if (!select) return;
    if (value && ![...select.options].some(o => o.value === value)) {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = value;
        select.appendChild(opt);
    }
    select.value = value || '';
}

/** Re-render everything from the address bar (initial load and Back/Forward). */
async function applyUrlState() {
    const token = ++state.restoreSeq;        // a newer navigation supersedes this restore at its next await
    const u = readUrlState();
    let corrected = false;
    try {
        if (u.view === 'ask' && state.askDisabled) { u.view = 'search'; u.q = ''; corrected = true; }   // feature switched off
        if (u.view !== state.currentView || urlBaseKey(u) !== state.renderedKey) {
            switchView(u.view, { history: 'none', skipLoad: true });
            // The restore writes the controls/state before its first await, but nothing is rendered for this URL until it
            // finishes: a Back+Forward inside that window must run a full restore, not be skipped as "already rendered".
            if (u.view === 'search' || u.view === 'browse') state.renderedKey = null;
            if (u.view === 'search') await restoreSearchFromUrl(u, token);
            else if (u.view === 'browse') await restoreBrowseFromUrl(u, token);
            if (token !== state.restoreSeq) return;
        }
        if (u.doc) {
            if (!isDocumentOpen() || state.openDocId !== u.doc) await openDocument(u.doc, -1, { history: 'none' });
        } else if (isDocumentOpen()) {
            closeDocumentDom();
        }
    } finally {
        updateDocumentTitle();
        if (corrected && token === state.restoreSeq) syncUrl('replace', { doc: isDocumentOpen() ? state.openDocId : '', overlay: state.modalPushed });
    }
}

async function restoreSearchFromUrl(u, token) {
    if (!u.q) { showSearchHome(); return; }
    elements.searchInput.value = u.q;
    elements.searchType.value = u.type;
    updateSearchTypeHint();
    setSelectValue(elements.searchCategory, u.set);
    await loadSubcategories(u.set, 'search');
    if (token !== state.restoreSeq) return;      // the visitor navigated again while this was loading
    if (u.section) {
        setSelectValue(elements.searchSubcategory, u.section);
        setHidden(elements.searchSubcategoryGroup, false);
    }
    setSelectValue(elements.searchFileType, u.kind);
    elements.searchDateFrom.value = u.from;
    elements.searchDateTo.value = u.to;
    state.lastSearchParams = {
        query: u.q, search_type: u.type, category: u.set || null, subcategory: u.section || null,
        file_type: u.kind || null, date_from: u.from || null, date_to: u.to || null
    };
    state.searchPage = u.page - 1;
    if (hasActiveFilterControls()) setFiltersOpen(true);
    updateFiltersCount();
    if (!validateDateRange()) {          // e.g. ?from=2020-01-01&to=2019-01-01: explain, let the visitor fix it
        state.lastSearchParams = null;
        setHidden(elements.searchResults, true);
        setHidden(document.getElementById('home-extras'), false);
        return;
    }
    await runSearch({ history: 'none', scroll: false });
}

async function restoreBrowseFromUrl(u, token) {
    state.browseFilename = u.name;
    state.browseKeyword = u.topic;
    state.browseCategory = u.set;
    state.browseSubcategory = u.section;
    state.browseFileType = u.kind;
    state.browsePage = u.page - 1;
    if (elements.browseFilename) elements.browseFilename.value = u.name;
    setSelectValue(elements.browseKeyword, u.topic);
    setSelectValue(elements.browseCategory, u.set);
    await loadSubcategories(u.set, 'browse');
    if (token !== state.restoreSeq) return;
    if (u.section) {
        setSelectValue(elements.browseSubcategory, u.section);
        setHidden(elements.browseSubcategory, false);
    }
    setSelectValue(elements.browseFileType, u.kind);
    await loadDocuments({ history: 'none' });
}

function readSearchControls() {
    const subVisible = elements.searchSubcategoryGroup && !elements.searchSubcategoryGroup.classList.contains('hidden');
    return {
        query: elements.searchInput.value.trim(),
        search_type: elements.searchType.value,
        category: elements.searchCategory.value || null,
        subcategory: (subVisible && elements.searchSubcategory && elements.searchSubcategory.value) || null,
        file_type: (elements.searchFileType && elements.searchFileType.value) || null,
        date_from: (elements.searchDateFrom && elements.searchDateFrom.value) || null,
        date_to: (elements.searchDateTo && elements.searchDateTo.value) || null
    };
}

/** An inverted date range is rejected with a message instead of silently returning nothing. */
function validateDateRange() {
    const from = elements.searchDateFrom ? elements.searchDateFrom.value : '';
    const to = elements.searchDateTo ? elements.searchDateTo.value : '';
    const bad = !!(from && to && from > to);
    const err = document.getElementById('date-range-error');
    if (err) {
        err.textContent = bad ? 'The start date must be on or before the end date.' : '';
        setHidden(err, !bad);
    }
    [elements.searchDateFrom, elements.searchDateTo].forEach(input => {
        if (!input) return;
        if (bad) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
    });
    return !bad;
}

function updateSearchTypeHint() {
    const hint = document.getElementById('search-type-hint');
    if (hint && elements.searchType) hint.textContent = SEARCH_TYPE_HINTS[elements.searchType.value] || '';
}

function hasActiveFilterControls() {
    const c = readSearchControls();
    return !!(c.category || c.subcategory || c.file_type || c.date_from || c.date_to || state.excludedCategories.length);
}

function updateFiltersCount() {
    const c = readSearchControls();
    let n = 0;
    if (c.category) n++;
    if (c.subcategory) n++;
    if (c.file_type) n++;
    if (c.date_from || c.date_to) n++;
    if (state.excludedCategories.length) n++;
    const badge = document.getElementById('filters-count');
    const toggle = document.getElementById('filters-toggle');
    if (badge) { badge.textContent = String(n); setHidden(badge, n === 0); }
    if (toggle) toggle.setAttribute('aria-label', n ? `Filters, ${n} active` : 'Filters');
}

function setFiltersOpen(open) {
    const panel = document.getElementById('search-filters');
    const toggle = document.getElementById('filters-toggle');
    setHidden(panel, !open);
    if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function setSearchLoading(on) {
    if (on && document.activeElement === elements.searchBtn) state._refocusSearchBtn = true;   // disabling drops focus
    elements.searchBtn.disabled = on;
    elements.searchBtn.innerHTML = on
        ? '<span class="loading-spinner" aria-hidden="true"></span><span>Searching…</span>'
        : SEARCH_BTN_HTML;
    elements.resultsList.setAttribute('aria-busy', on ? 'true' : 'false');
    if (!on) {
        if (state._refocusSearchBtn) {
            state._refocusSearchBtn = false;
            if (document.activeElement === document.body) elements.searchBtn.focus({ preventScroll: true });
        }
        return;
    }
    setHidden(elements.searchResults, false);
    setHidden(document.getElementById('home-extras'), true);
    setHidden(elements.clearSearchBtn, false);
    setHidden(elements.searchPagination, true);
    elements.resultsCount.textContent = 'Searching…';
    elements.resultsList.innerHTML = Array.from({ length: 6 }, () =>
        '<div class="result-skeleton" aria-hidden="true"><div class="skeleton sk-thumb"></div><div class="sk-lines">' +
        '<div class="skeleton sk-line"></div><div class="skeleton sk-line mid"></div><div class="skeleton sk-line short"></div></div></div>'
    ).join('');
    announce('Searching…');
}

/** Bring the results to the top of the screen, just under the sticky header (the refine bar then sticks right there). */
function scrollToResults() {
    if (state.currentView !== 'search' || !elements.searchResults.offsetParent) return;   // visitor has moved on: do not scroll their current page
    const header = document.querySelector('.header');
    const top = window.scrollY + elements.searchResults.getBoundingClientRect().top - (header ? header.offsetHeight : 0) - 4;
    window.scrollTo({ top: Math.max(0, top), behavior: scrollBehavior() });
}

async function runSearch({ history: mode = 'push', focusHeading = false, scroll = true } = {}) {
    const params = state.lastSearchParams;
    if (!params) return;
    const seq = ++state.searchSeq;
    syncUrl(mode);
    state.renderedKey = null;            // nothing is rendered for this URL until the response lands (Back must re-render)
    updateDocumentTitle();
    setSearchLoading(true);
    updateRefineBar();

    const body = { ...params, limit: state.searchLimit, offset: state.searchPage * state.searchLimit };
    // Exclusions are applied by the server (so totals and pages stay correct); a specific file set overrides them.
    if (!params.category && state.excludedCategories.length) body.exclude_categories = state.excludedCategories.slice();

    try {
        const response = await fetch(`${API_BASE}/search`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (!response.ok) {
            let detail = '';
            try { detail = (await response.json()).detail || ''; } catch (e) { /* not JSON */ }
            const err = new Error(detail || 'Search failed');
            err.status = response.status;
            err.detail = typeof detail === 'string' ? detail : '';
            throw err;
        }
        const data = await response.json();
        if (seq !== state.searchSeq) return;            // a newer search superseded this one
        state.searchTotal = data.total;
        renderSearchResults(data, { focusHeading });
        if (data.parsed_query) displayQueryFeedback(data.parsed_query);
        state.renderedKey = currentBaseKey();
        if (scroll) scrollToResults();
    } catch (error) {
        if (seq !== state.searchSeq) return;
        console.error('Search error:', error);
        renderSearchError(error);
        if (focusHeading && elements.resultsTitle) elements.resultsTitle.focus({ preventScroll: true });
    } finally {
        if (seq === state.searchSeq) setSearchLoading(false);
    }
}

function activeFilters(params) {
    const list = [];
    if (!params) return list;
    if (params.category) list.push({ key: 'category', label: params.category });
    if (params.subcategory) list.push({ key: 'subcategory', label: params.subcategory });
    if (params.file_type) list.push({ key: 'file_type', label: FILE_TYPE_LABELS[params.file_type] || params.file_type });
    if (params.date_from || params.date_to) {
        const label = params.date_from && params.date_to ? `${params.date_from} to ${params.date_to}`
            : params.date_from ? `From ${params.date_from}` : `Until ${params.date_to}`;
        list.push({ key: 'date', label });
    }
    if (params.search_type && params.search_type !== SEARCH_DEFAULT_TYPE) {
        list.push({ key: 'search_type', label: params.search_type === 'hybrid' ? 'Hybrid search' : 'Semantic search' });
    }
    if (!params.category && state.excludedCategories.length) {
        const n = state.excludedCategories.length;
        list.push({ key: 'exclusions', label: `Excluding ${n} file set${n > 1 ? 's' : ''}` });
    }
    return list;
}

function updateRefineBar() {
    const bar = document.getElementById('refine-bar');
    const chips = document.getElementById('refine-chips');
    if (!bar || !chips) return;
    const filters = activeFilters(state.lastSearchParams);
    chips.innerHTML = filters.map(f => `
        <li><button type="button" class="refine-chip" data-action="remove-filter" data-filter="${f.key}" aria-label="Remove filter: ${escapeHtml(f.label)}">
            <span>${escapeHtml(f.label)}</span><span class="chip-x" aria-hidden="true">×</span>
        </button></li>`).join('');
}

function removeSearchFilter(key) {
    const p = state.lastSearchParams;
    if (!p) return;
    switch (key) {
        case 'category':
            p.category = null; p.subcategory = null;
            elements.searchCategory.value = '';
            loadSubcategories('', 'search');
            break;
        case 'subcategory':
            p.subcategory = null;
            if (elements.searchSubcategory) elements.searchSubcategory.value = '';
            break;
        case 'file_type':
            p.file_type = null;
            elements.searchFileType.value = '';
            break;
        case 'date':
            p.date_from = null; p.date_to = null;
            elements.searchDateFrom.value = ''; elements.searchDateTo.value = '';
            validateDateRange();
            break;
        case 'search_type':
            p.search_type = SEARCH_DEFAULT_TYPE;
            elements.searchType.value = SEARCH_DEFAULT_TYPE;
            updateSearchTypeHint();
            break;
        case 'exclusions':
            state.excludedCategories = [];
            saveExcludedCategories();
            renderExcludeDropdowns();
            loadCategories();
            break;
        default:
            return;
    }
    state.searchPage = 0;
    updateFiltersCount();
    runSearch({ history: 'push', focusHeading: true });
}

function clearSearchFilters() {
    const p = state.lastSearchParams;
    if (!p) return;
    p.category = null; p.subcategory = null; p.file_type = null; p.date_from = null; p.date_to = null;
    elements.searchCategory.value = '';
    if (elements.searchSubcategory) elements.searchSubcategory.value = '';
    elements.searchFileType.value = '';
    elements.searchDateFrom.value = ''; elements.searchDateTo.value = '';
    loadSubcategories('', 'search');
    validateDateRange();
    state.searchPage = 0;
    updateFiltersCount();
    runSearch({ history: 'push', focusHeading: true });
}

function switchSearchType(type) {
    if (!SEARCH_TYPES.includes(type) || !state.lastSearchParams) return;
    elements.searchType.value = type;
    state.lastSearchParams.search_type = type;
    updateSearchTypeHint();
    state.searchPage = 0;
    runSearch({ history: 'push', focusHeading: true });
}

function openSearchHelp() {
    if (elements.searchHelpToggle && elements.searchHelpToggle.getAttribute('aria-expanded') !== 'true') toggleSearchHelp();
    if (elements.searchHelpContent) elements.searchHelpContent.scrollIntoView({ behavior: scrollBehavior(), block: 'center' });
}

/** Back to the search box (and filters, when any are active) from anywhere in the results. */
function editSearch() {
    if (hasActiveFilterControls()) setFiltersOpen(true);
    elements.searchInput.scrollIntoView({ behavior: scrollBehavior(), block: 'center' });
    elements.searchInput.focus({ preventScroll: true });
    elements.searchInput.select();
}

function renderSearchError(error) {
    const syntax = error && error.status === 400;
    const message = syntax
        ? (error.detail || 'That search could not be run. Check the spelling and any quotes or operators, then try again.')
        : 'We could not reach the archive just now. Check your connection and try again.';
    elements.resultsCount.textContent = '';
    setHidden(elements.searchPagination, true);
    elements.resultsList.innerHTML = `
        <div class="state-panel state-error" role="alert">
            <h3>${syntax ? 'That search didn’t work' : 'Search didn’t go through'}</h3>
            <p>${escapeHtml(message)}</p>
            <div class="state-actions">
                <button type="button" class="state-btn primary" data-action="retry-search">Try again</button>
                <button type="button" class="state-btn" data-action="edit-search">Edit search</button>
            </div>
        </div>`;
    updateRefineBar();
}

function renderSearchEmpty() {
    const p = state.lastSearchParams;
    const filters = activeFilters(p).filter(f => f.key !== 'exclusions' && f.key !== 'search_type');
    const tips = ['<li>Check the spelling, or try fewer or more general words.</li>'];
    if (filters.length) {
        tips.push(`<li>${filters.length} filter${filters.length > 1 ? 's are' : ' is'} narrowing this search: <button type="button" class="inline-link" data-action="clear-search-filters">remove ${filters.length > 1 ? 'all filters' : 'the filter'}</button>.</li>`);
    }
    if (!p.category && state.excludedCategories.length) {
        tips.push(`<li>You are excluding ${state.excludedCategories.length} file set${state.excludedCategories.length > 1 ? 's' : ''}: <button type="button" class="inline-link" data-action="clear-exclusions">include all file sets</button>.</li>`);
    }
    if (p.search_type === 'fulltext') {
        tips.push('<li>Full Text matches exact words. <button type="button" class="inline-link" data-action="switch-search-type" data-type="hybrid">Try Hybrid search</button> to include related passages.</li>');
    }
    tips.push('<li>Use quotes for an exact phrase (<code>"flight log"</code>), or <button type="button" class="inline-link" data-action="open-search-help">see the search tips</button>.</li>');
    elements.resultsList.innerHTML = `
        <div class="state-panel state-empty">
            <h3>No documents matched “${escapeHtml(p.query)}”</h3>
            <p>Nothing in the archive fits this search yet.</p>
            <ul class="state-tips">${tips.join('')}</ul>
        </div>`;
}

function resultCardHtml(result, index) {
    const id = String(result.id);
    const href = buildUrl({ doc: id });
    const score = result.score ? formatRelevanceScore(result.score, result.search_type) : '';
    return `
        <article class="result-item" data-doc-card data-doc-id="${escapeHtml(id)}" data-index="${index}">
            <div class="result-thumbnail" data-file-type="${escapeHtml(result.file_type || 'pdf')}" aria-hidden="true">
                <img src="${API_BASE}/documents/${encodeURIComponent(id)}/thumbnail" alt="" loading="lazy" />
                <div class="thumbnail-fallback">${getDocumentIcon(result.file_type)}</div>
            </div>
            <div class="result-content">
                <div class="result-header">
                    <h3 class="result-title"><a class="result-filename" href="${escapeHtml(href)}" data-action="open-doc" data-doc-id="${escapeHtml(id)}" data-index="${index}">${escapeHtml(result.filename)}</a></h3>
                    ${score ? `<span class="result-score">${score}</span>` : ''}
                </div>
                <div class="result-meta">
                    <span class="result-category">${escapeHtml(result.category)}</span>
                    ${result.document_date ? `<span class="result-date">${formatDocumentDate(result.document_date)}</span>` : ''}
                    ${result.subcategory ? `<span>${escapeHtml(result.subcategory)}</span>` : ''}
                    <span>${getSearchResultMeta(result)}</span>
                </div>
                ${result.snippet ? `<div class="result-snippet">${sanitizeSnippet(result.snippet)}</div>` : ''}
            </div>
        </article>`;
}

/** Reset the search UI to the home state without touching the network (except refreshing category counts). */
function showSearchHome() {
    state.lastSearchParams = null;
    state.searchPage = 0;
    state.searchTotal = 0;
    state.searchSeq++;
    elements.searchInput.value = '';
    elements.searchCategory.value = '';
    if (elements.searchSubcategory) elements.searchSubcategory.value = '';
    elements.searchFileType.value = '';
    elements.searchDateFrom.value = '';
    elements.searchDateTo.value = '';
    validateDateRange();
    setHidden(elements.searchSubcategoryGroup, true);
    setHidden(elements.searchResults, true);
    setHidden(document.getElementById('home-extras'), false);
    setHidden(elements.clearSearchBtn, true);
    setHidden(elements.queryFeedback, true);
    setHidden(elements.searchPagination, true);
    elements.resultsList.setAttribute('aria-busy', 'false');
    elements.searchBtn.disabled = false;
    elements.searchBtn.innerHTML = SEARCH_BTN_HTML;
    state.renderedKey = '';
    updateFiltersCount();
    updateDocumentTitle();
    renderStats();         // restores the unfiltered file-type counts
    loadCategories();      // restores the unfiltered file-set counts
}

function browseHasFilters() {
    return !!(state.browseCategory || state.browseSubcategory || state.browseFileType || state.browseFilename ||
        state.browseKeyword || state.excludedCategories.length);
}

function updateBrowseClearButton() {
    setHidden(document.getElementById('browse-clear'), !browseHasFilters());
}

async function clearBrowseFilters() {
    state.browseCategory = '';
    state.browseSubcategory = '';
    state.browseFileType = '';
    state.browseFilename = '';
    state.browseKeyword = '';
    state.browsePage = 0;
    state.excludedCategories = [];
    saveExcludedCategories();
    if (elements.browseFilename) elements.browseFilename.value = '';
    if (elements.browseKeyword) elements.browseKeyword.value = '';
    elements.browseCategory.value = '';
    if (elements.browseFileType) elements.browseFileType.value = '';
    await loadSubcategories('', 'browse');
    renderExcludeDropdowns();
    loadCategories();
    loadDocuments({ history: 'push', focusHeading: true });
}

function focusBrowseHeading() {
    const h = document.getElementById('browse-heading');
    if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); }
}

function renderBrowseError() {
    state.documentList = [];
    elements.browseCount.textContent = '';
    elements.documentsGrid.innerHTML = `
        <div class="state-panel state-error" role="alert">
            <h3>We couldn’t load the documents</h3>
            <p>Check your connection and try again.</p>
            <div class="state-actions"><button type="button" class="state-btn primary" data-action="retry-browse">Try again</button></div>
        </div>`;
}

function documentCardHtml(doc, index) {
    const id = String(doc.id);
    const href = buildUrl({ doc: id });
    const titleId = `doc-title-${id}`;
    // The whole card is the link (large touch target); it is named by the filename heading only.
    return `
        <article class="document-card" data-doc-card data-doc-id="${escapeHtml(id)}" data-index="${index}">
            <a class="document-link" href="${escapeHtml(href)}" data-action="open-doc" data-doc-id="${escapeHtml(id)}" data-index="${index}" aria-labelledby="${escapeHtml(titleId)}">
                <div class="document-thumbnail" data-file-type="${escapeHtml(doc.file_type || 'pdf')}" aria-hidden="true">
                    <img src="${API_BASE}/documents/${encodeURIComponent(id)}/thumbnail" alt="" loading="lazy" />
                    <div class="thumbnail-fallback">${getDocumentIcon(doc.file_type)}</div>
                </div>
                <h3 class="document-title" id="${escapeHtml(titleId)}">${escapeHtml(doc.filename)}</h3>
                <div class="document-meta">
                    ${getDocumentTileLabel(doc)} • ${getDocumentMeta(doc)}
                </div>
            </a>
        </article>`;
}

/**
 * Load a URL into the document preview iframe WITHOUT adding session-history entries.
 * Assigning iframe.src adds an entry to the joint session history, so Back would rewind the frame
 * instead of closing the modal. A freshly inserted iframe's first navigation does not add one.
 */
function loadPdfFrame(url) {
    const old = elements.pdfIframe;
    if (!old) return;
    const fresh = old.cloneNode(false);
    fresh.removeAttribute('src');
    if (url) fresh.src = url;
    old.replaceWith(fresh);
    elements.pdfIframe = fresh;
}

function isDocumentOpen() {
    return !!elements.modal && !elements.modal.classList.contains('hidden');
}

function showModalLoading({ trigger = null } = {}) {
    const content = elements.modalContent;
    // Stepping between documents keeps the tabs and Prev/Next in the DOM (so keyboard focus is not destroyed);
    // a first open, or a retry after an error, shows the full loading state.
    const refreshing = isDocumentOpen() && !content.classList.contains('is-error');
    content.classList.remove('is-error');
    if (refreshing) {
        content.classList.add('is-refreshing');
        content.setAttribute('aria-busy', 'true');
        elements.modalState.textContent = '';
    } else {
        content.classList.add('is-loading');
        elements.modalTitle.textContent = 'Loading document…';
        elements.modalMeta.textContent = '';
        elements.modalAlteration.innerHTML = '';
        setHidden(elements.modalAlteration, true);
        elements.modalState.setAttribute('role', 'status');
        elements.modalState.innerHTML = '<span class="loading-spinner" aria-hidden="true"></span><p>Loading document…</p>';
    }
    // Stop whatever the previous document was playing / showing.
    loadPdfFrame('');
    const mediaViewer = document.getElementById('media-viewer');
    if (mediaViewer) mediaViewer.innerHTML = '';
    if (!isDocumentOpen()) {
        openDialog(elements.modal, { trigger: trigger || document.activeElement, onRequestClose: requestCloseDocument });
    }
}

function showModalError(docId, error) {
    const content = elements.modalContent;
    content.classList.remove('is-loading', 'is-refreshing');
    content.removeAttribute('aria-busy');
    content.classList.add('is-error');
    // The previous document's details must not sit under "Couldn't open document" (nor its altered badge / compare button).
    elements.modalMeta.textContent = '';
    elements.modalAlteration.innerHTML = '';
    setHidden(elements.modalAlteration, true);
    state.currentDocument = null;
    updateDocumentTitle();
    const notFound = error && error.status === 404;
    elements.modalTitle.textContent = 'Couldn’t open document';
    elements.modalState.setAttribute('role', 'alert');
    elements.modalState.innerHTML = `
        <h3>${notFound ? 'This document wasn’t found' : 'Something went wrong'}</h3>
        <p>${notFound
            ? 'It may have been removed, or the link may be incorrect.'
            : 'We couldn’t load this document. Check your connection and try again.'}</p>
        <div class="state-actions">
            ${notFound ? '' : `<button type="button" class="state-btn primary" data-action="retry-doc" data-doc-id="${escapeHtml(docId)}">Try again</button>`}
            <button type="button" class="state-btn${notFound ? ' primary' : ''}" data-action="close-doc">Close</button>
        </div>`;
    const first = elements.modalState.querySelector('button');
    if (first && (document.activeElement === elements.modalContent || document.activeElement === document.body)) first.focus();
}

/** Close from the UI (X, Escape, backdrop). Undoes the history entry the open pushed, if any. */
function requestCloseDocument() {
    if (!isDocumentOpen()) return;
    if (state.modalPushed) {
        state.modalPushed = false;
        history.back();                 // popstate -> applyUrlState() closes the dialog
    } else {
        closeDocumentDom();
        syncUrl('replace');
    }
}

/** Pure DOM close (also used when Back/Forward removes ?doc= from the URL). */
function closeDocumentDom() {
    state.docSeq++;                     // discard any in-flight load
    loadPdfFrame('');
    const mediaViewer = document.getElementById('media-viewer');
    if (mediaViewer) mediaViewer.innerHTML = '';
    const pdfViewer = document.getElementById('modal-pdf-viewer');
    if (pdfViewer && pdfViewer.classList.contains('fullscreen')) togglePdfFullscreen(pdfViewer);
    state.currentDocument = null;
    state.openDocId = null;
    elements.modalContent.classList.remove('is-loading', 'is-error', 'is-refreshing');   // a closed-mid-load dialog must not stay dimmed/busy
    elements.modalContent.removeAttribute('aria-busy');
    closeDialog(elements.modal);
    updateDocumentTitle();
}

function loadExcludedCategories() {
    try {
        const v = JSON.parse(localStorage.getItem('excludedCategories') || '[]');
        return Array.isArray(v) ? v.filter(x => typeof x === 'string') : [];
    } catch (e) {
        return [];
    }
}

/** WCAG 2.2.2: auto-moving content must be pausable. Works for every marquee bar (featured + altered). */
function togglePinnedMotion(btn) {
    const bar = btn.closest('.pinned-documents-bar');
    const scroll = bar && bar.querySelector('.pinned-scroll');
    if (!scroll) return;
    const paused = scroll.classList.toggle('is-paused');
    btn.setAttribute('aria-pressed', paused ? 'true' : 'false');
    const label = btn.querySelector('.pinned-pause-label');
    const icon = btn.querySelector('.pinned-pause-icon');
    if (label) label.textContent = paused ? 'Play' : 'Pause';
    if (icon) icon.textContent = paused ? '▶' : '⏸';
}

/** Totals, pages and facets all depend on exclusions, so whatever is on screen is re-fetched. */
function refreshAfterExclusionChange() {
    state._prefetchedBrowse = null;
    updateFiltersCount();
    updateBrowseClearButton();
    loadCategories(state.currentView === 'browse' ? (state.browseKeyword || null) : null);
    // Toggling a checkbox must not steal focus from the menu; actions elsewhere (empty-state button, chip) move it to the results.
    const inMenu = !!(document.activeElement && document.activeElement.closest && document.activeElement.closest('.exclude-menu'));
    if (state.currentView === 'browse') {
        state.browsePage = 0;
        loadDocuments({ history: 'replace', focusHeading: !inMenu });
    } else if (state.lastSearchParams) {
        state.searchPage = 0;
        runSearch({ history: 'replace', scroll: false, focusHeading: !inMenu });
    }
}

function shareTo(platform, cfg, itemEl) {
    const enc = encodeURIComponent;
    const { shareUrl, shareText } = cfg;
    let webUrl = '';
    let appUrl = '';
    let intentUrl = '';

    switch (platform) {
        case 'facebook':
            webUrl = `https://www.facebook.com/sharer/sharer.php?u=${enc(shareUrl)}&quote=${enc(shareText)}`;
            appUrl = `fb://share/?link=${enc(shareUrl)}`;
            intentUrl = `intent://share/?link=${enc(shareUrl)}#Intent;package=com.facebook.katana;scheme=fb;end`;
            break;
        case 'twitter':
            // Twitter/X has no reliable share deep link: web only
            webUrl = `https://twitter.com/intent/tweet?url=${enc(shareUrl)}&text=${enc(shareText)}`;
            break;
        case 'linkedin':
            webUrl = cfg.linkedinWeb || `https://www.linkedin.com/sharing/share-offsite/?url=${enc(shareUrl)}`;
            appUrl = `linkedin://shareArticle?url=${enc(shareUrl)}&title=${enc(shareText)}`;
            intentUrl = `intent://shareArticle?url=${enc(shareUrl)}&title=${enc(shareText)}#Intent;package=com.linkedin.android;scheme=linkedin;end`;
            break;
        case 'threads':
            webUrl = `https://www.threads.net/intent/post?text=${enc(shareText + ' ' + shareUrl)}`;
            // iOS: a Universal Link that opens the app when installed
            appUrl = webUrl;
            intentUrl = `intent://post?text=${enc(shareText + ' ' + shareUrl)}#Intent;package=com.instagram.barcelona;scheme=threads;end`;
            break;
        case 'reddit':
            webUrl = `https://reddit.com/submit?url=${enc(shareUrl)}&title=${enc(shareText)}`;
            break;
        case 'bluesky':
            webUrl = `https://bsky.app/intent/compose?text=${enc(shareText + ' ' + shareUrl)}`;
            break;
        case 'telegram':
            webUrl = `https://t.me/share/url?url=${enc(shareUrl)}&text=${enc(shareText)}`;
            break;
        case 'email':
            window.location.href = `mailto:?subject=${enc(cfg.emailSubject)}&body=${enc(cfg.emailBody)}`;
            return;
        case 'copy':
            copyToClipboard(shareUrl).then(success => {
                if (success) showToast('Link copied to the clipboard.', { type: 'success', timeout: 2500 });
            });
            return;
        default:
            return;
    }

    // On mobile, try to launch the native app; otherwise open the web share page.
    if (isMobileDevice() && (appUrl || intentUrl)) {
        if (isIOS()) {
            window.location.href = appUrl;
        } else if (isAndroid() && intentUrl) {
            window.location.href = intentUrl;
        } else {
            openNativeAppOrFallback(appUrl, webUrl, intentUrl);
        }
    } else {
        window.open(webUrl, '_blank', 'width=600,height=400,menubar=no,toolbar=no');
    }
}

/** Share the site itself (header Share button menu). */
function handleSiteShare(platform, el) {
    const shareText = 'Epstein Files Public Archive — search court records, flight logs & DOJ disclosures. Free & open source.';
    shareTo(platform, {
        shareUrl: SITE_URL, shareText,
        emailSubject: 'Epstein Files Public Archive',
        emailBody: `Search court records, flight logs, and DOJ disclosures from the Epstein Files — free & open source.\n\n${SITE_URL}`
    }, el);
}

function copySiteLink(btn) {
    copyToClipboard(SITE_URL).then(ok => {
        const note = btn.nextElementSibling;
        if (!ok || !note || !note.classList.contains('share-bar-copied')) return;
        note.textContent = 'Copied';
        note.classList.add('is-visible');
        setTimeout(() => { note.classList.remove('is-visible'); note.textContent = ''; }, 1500);
    });
}

/** Runs one export. `type` and `includeText` are passed in so the toast's "Try again" can repeat exactly this export. */
async function performExport(exportType, includeText) {
    if (!exportType) return;
    if (exportType === 'search' && !state.lastSearchParams) {
        showToast('Run a search first, then export its results.');
        return;
    }
    const confirmBtn = document.getElementById('export-modal-confirm');
    confirmBtn.textContent = 'Exporting...';
    confirmBtn.disabled = true;

    try {
        const params = new URLSearchParams();
        const p = state.lastSearchParams;
        if (exportType === 'search') {
            if (p.query) params.append('search_query', p.query);
            if (p.search_type) params.append('search_type', p.search_type);
            if (p.category) params.append('category', p.category);
            if (p.subcategory) params.append('subcategory', p.subcategory);
            if (p.file_type) params.append('file_type', p.file_type);
            if (p.date_from) params.append('date_from', p.date_from);
            if (p.date_to) params.append('date_to', p.date_to);
            if (!p.category) state.excludedCategories.forEach(c => params.append('exclude_category', c));
        } else {
            if (state.browseCategory) params.append('category', state.browseCategory);
            if (state.browseSubcategory) params.append('subcategory', state.browseSubcategory);
            if (state.browseFileType) params.append('file_type', state.browseFileType);
            if (state.browseFilename) params.append('filename', state.browseFilename);
            if (state.browseKeyword) params.append('keyword', state.browseKeyword);
            if (!state.browseCategory) state.excludedCategories.forEach(c => params.append('exclude_category', c));
        }
        if (includeText) params.append('include_text', 'true');

        const response = await fetch(`${API_BASE}/documents/export?${params}`);
        if (!response.ok) throw new Error('Export failed');

        const data = await response.json();
        const prefix = exportType === 'search' ? 'search_export' : 'documents_export';
        const exported = downloadCSV(data.documents, `${prefix}_${new Date().toISOString().split('T')[0]}.csv`);
        if (exported) closeExportModal();
    } catch (error) {
        console.error('Export error:', error);
        showToast('Export failed. Please try again.', { type: 'error', actionLabel: 'Try again', onAction: () => performExport(exportType, includeText) });
    } finally {
        confirmBtn.textContent = 'Export';
        confirmBtn.disabled = false;
    }
}

/** Duplicate marquee cards stay clickable (they pass under the pointer too) but are hidden from AT and the tab order. */
function asDuplicateCards(html) {
    return html.replace(/<(a|button) /g, '<$1 tabindex="-1" ');
}

/** Ask AI switched off by the server: no nav button, no view, and ?view=ask falls back to search. */
function hideAskAi() {
    state.askDisabled = true;
    setHidden(document.querySelector('.nav-btn[data-view="ask"]'), true);
    setHidden(document.getElementById('ask-view'), true);
}

/** Both exclude menus mirror state.excludedCategories immediately (not only after the categories reload succeeds). */
function syncExcludeCheckboxes() {
    document.querySelectorAll('#search-exclude-options input[type="checkbox"][data-category], #browse-exclude-options input[type="checkbox"][data-category]')
        .forEach(cb => { cb.checked = state.excludedCategories.includes(cb.dataset.category); });
}

// Initialize
document.addEventListener('DOMContentLoaded', init);

async function init() {
    // Age verification gate: blocks everything until the visitor confirms 18+.
    // Returning visitors never see it: html.age-ok (set by an inline script in <head>) hides it before first paint.
    // Key/click delegation must exist before the gate is shown: the gate is itself a dialog (Tab trap).
    if (!state._delegated) {
        state._delegated = true;
        setupGlobalDelegation();
    }
    const ageGate = document.getElementById('age-gate');
    let verified = !!state.ageConfirmed;
    try { verified = verified || !!localStorage.getItem('ageVerified'); } catch (e) { /* storage blocked */ }
    if (!verified) {
        document.documentElement.classList.remove('age-ok');
        if (!state._ageGateBound) {
            state._ageGateBound = true;
            document.getElementById('age-gate-confirm').addEventListener('click', () => {
                state.ageConfirmed = true;
                try { localStorage.setItem('ageVerified', 'true'); } catch (e) { /* storage blocked */ }
                document.documentElement.classList.add('age-ok');
                closeDialog(ageGate, { restoreFocus: false });
                init();
            });
            document.getElementById('age-gate-deny').addEventListener('click', () => {
                window.location.href = 'https://www.google.com/search?q=epsteinfta.com';
            });
        }
        openDialog(ageGate, { initialFocus: '#age-gate-confirm' });
        return;
    }
    setHidden(ageGate, true);
    if (state._initialized) return;
    state._initialized = true;

    cacheElements();
    setupEventListeners();
    syncHeaderHeight();
    updateSearchTypeHint();
    setFiltersOpen(!MOBILE_QUERY.matches);
    updateFiltersCount();

    // Single bootstrap request (stats + categories + keywords + settings) for faster load
    try {
        const bootstrapRes = await fetch(`${API_BASE}/bootstrap`);
        if (bootstrapRes.ok) {
            const data = await bootstrapRes.json();
            state.stats = data.stats;
            state.categories = (data.categories && data.categories.categories) ? data.categories.categories : [];
            renderStats();
            updateLLMStatus();
            applyCategoriesToDropdowns();
            applyKeywordsToDropdowns(data.keywords ? data.keywords.keywords : {});
            applyPublicSettings(data.settings || {});
            // Cache the prefetched first browse page so the Browse tab is instant (unfiltered only).
            if (data.browse && data.browse.documents && !state.excludedCategories.length) {
                state._prefetchedBrowse = data.browse;
            }
            if (data.settings && data.settings.pinned_documents_enabled !== false) {
                const pinnedDocs = data.pinned_documents || [];
                if (pinnedDocs.length > 0) renderPinnedDocumentsBar(pinnedDocs);
                else removePinnedSkeleton();
            } else {
                removePinnedSkeleton();
            }
        } else {
            await loadFallbackInit();
        }
    } catch (e) {
        console.warn('Bootstrap failed, falling back to separate requests:', e);
        await loadFallbackInit();
    }

    // Surface the "Altered by DOJ" censored bar (harmless no-op when there are no exposed alterations).
    loadCensoredBar();

    // Set timestamp for spam protection
    const timestampField = document.getElementById('feedback-timestamp');
    if (timestampField) timestampField.value = Date.now().toString();

    // Restore whatever the URL describes (?q=, ?doc=, ?view=, filters, page...).
    await applyUrlState();

    // After the age gate, put the cursor in the search box on desktop (not on touch: it would pop the keyboard).
    if (state.ageConfirmed && !state.lastSearchParams && window.matchMedia && window.matchMedia('(pointer: fine)').matches) {
        elements.searchInput.focus({ preventScroll: true });
    }
}

async function loadFallbackInit() {
    await Promise.all([
        loadStats(),
        loadCategories(),
        loadKeywords(),
        loadPublicSettings()
    ]);
    await loadPinnedDocuments();
}

function applyCategoriesToDropdowns() {
    if (!state.categories || !state.categories.length) return;
    const visibleCategories = state.categories.filter(
        c => !state.excludedCategories.includes(c.category)
    );
    const categoryOptions = visibleCategories.map(c =>
        `<option value="${escapeHtml(c.category)}">${escapeHtml(c.category)} (${c.count})</option>`
    ).join('');
    const currentBrowseCategory = elements.browseCategory?.value;
    const currentSearchCategory = elements.searchCategory?.value;
    if (elements.searchCategory) {
        elements.searchCategory.innerHTML = '<option value="">All File Sets</option>' + categoryOptions;
    }
    if (elements.browseCategory) {
        elements.browseCategory.innerHTML = '<option value="">All File Sets</option>' + categoryOptions;
    }
    if (currentBrowseCategory && visibleCategories.some(c => c.category === currentBrowseCategory)) {
        elements.browseCategory.value = currentBrowseCategory;
    }
    if (currentSearchCategory && visibleCategories.some(c => c.category === currentSearchCategory)) {
        elements.searchCategory.value = currentSearchCategory;
    }
    renderExcludeDropdowns();
}

function applyKeywordsToDropdowns(keywords) {
    if (!keywords || !elements.browseKeyword) return;
    const categoryIcons = { 'People': '👤', 'Locations': '📍', 'Topics': '📋' };
    let optionsHtml = '<option value="">All Topics</option>';
    const categoryOrder = ['People', 'Locations', 'Topics'];
    for (const category of categoryOrder) {
        const items = keywords[category];
        if (items && items.length > 0) {
            const icon = categoryIcons[category] || '🏷️';
            optionsHtml += `<optgroup label="${icon} ${category}">`;
            for (const kw of items) {
                const countText = kw.document_count > 0 ? ` (${formatNumber(kw.document_count)})` : '';
                optionsHtml += `<option value="${escapeHtml(kw.search_term)}">${escapeHtml(kw.name)}${countText}</option>`;
            }
            optionsHtml += '</optgroup>';
        }
    }
    for (const [category, items] of Object.entries(keywords)) {
        if (!categoryOrder.includes(category) && items && items.length > 0) {
            const icon = categoryIcons[category] || '🏷️';
            optionsHtml += `<optgroup label="${icon} ${category}">`;
            for (const kw of items) {
                const countText = kw.document_count > 0 ? ` (${formatNumber(kw.document_count)})` : '';
                optionsHtml += `<option value="${escapeHtml(kw.search_term)}">${escapeHtml(kw.name)}${countText}</option>`;
            }
            optionsHtml += '</optgroup>';
        }
    }
    const currentValue = elements.browseKeyword.value;
    elements.browseKeyword.innerHTML = optionsHtml;
    if (currentValue) elements.browseKeyword.value = currentValue;
}

function applyPublicSettings(settings) {
    if (settings.ask_ai_enabled === false) hideAskAi();
    initMonetization({
        adsEnabled: settings.ads_enabled === true,
        affiliateEnabled: settings.affiliate_enabled !== false
    });
}

function cacheElements() {
    // Navigation
    elements.navBtns = document.querySelectorAll('.nav-btn');
    elements.views = document.querySelectorAll('.view');

    // Search
    elements.searchInput = document.getElementById('search-input');
    elements.searchBtn = document.getElementById('search-btn');
    elements.clearSearchBtn = document.getElementById('clear-search-btn');
    elements.searchType = document.getElementById('search-type');
    elements.searchCategory = document.getElementById('search-category');
    elements.searchSubcategory = document.getElementById('search-subcategory');
    elements.searchSubcategoryGroup = document.getElementById('search-subcategory-group');
    elements.searchFileType = document.getElementById('search-file-type');
    elements.searchDateFrom = document.getElementById('search-date-from');
    elements.searchDateTo = document.getElementById('search-date-to');
    elements.filtersToggle = document.getElementById('filters-toggle');
    elements.searchResults = document.getElementById('search-results');
    elements.resultsList = document.getElementById('results-list');
    elements.resultsCount = document.getElementById('results-count');
    elements.resultsTitle = document.getElementById('results-title');
    elements.searchPagination = document.getElementById('search-pagination');
    elements.searchPrevPage = document.getElementById('search-prev-page');
    elements.searchNextPage = document.getElementById('search-next-page');
    elements.searchPageNumbers = document.getElementById('search-page-numbers');
    elements.searchPageInput = document.getElementById('search-page-input');
    elements.searchGoPage = document.getElementById('search-go-page');
    elements.statsGrid = document.getElementById('stats-grid');
    elements.statsDisplay = document.getElementById('stats-display');

    // Browse
    elements.browseFilename = document.getElementById('browse-filename');
    elements.browseKeyword = document.getElementById('browse-keyword');
    elements.browseCategory = document.getElementById('browse-category');
    elements.browseSubcategory = document.getElementById('browse-subcategory');
    elements.browseFileType = document.getElementById('browse-file-type');
    elements.documentsGrid = document.getElementById('documents-grid');
    elements.browseCount = document.getElementById('browse-count');
    elements.prevPage = document.getElementById('prev-page');
    elements.nextPage = document.getElementById('next-page');
    elements.pageInfo = document.getElementById('page-info');
    elements.browsePageInput = document.getElementById('browse-page-input');
    elements.browseGoPage = document.getElementById('browse-go-page');

    // Ask AI
    elements.askInput = document.getElementById('ask-input');
    elements.askBtn = document.getElementById('ask-btn');
    elements.askResponse = document.getElementById('ask-response');
    elements.answerText = document.getElementById('answer-text');
    elements.sourcesList = document.getElementById('sources-list');
    elements.llmStatus = document.getElementById('llm-status');
    elements.exampleBtns = document.querySelectorAll('.example-btn');

    // Document modal
    elements.modal = document.getElementById('document-modal');
    elements.modalContent = elements.modal.querySelector('.modal-content');
    elements.modalBackdrop = elements.modal.querySelector('.modal-backdrop');
    elements.modalClose = elements.modal.querySelector('.modal-close');
    elements.modalTitle = document.getElementById('modal-title');
    elements.modalMeta = document.getElementById('modal-meta');
    elements.modalAlteration = document.getElementById('modal-alteration');
    elements.modalState = document.getElementById('modal-state');
    elements.modalText = document.getElementById('modal-text');
    elements.modalSummary = document.getElementById('modal-summary');
    elements.pdfIframe = document.getElementById('pdf-iframe');
    elements.pdfFallback = document.getElementById('pdf-fallback');
    elements.modalTabs = elements.modal.querySelectorAll('[role="tab"]');

    // Document navigation
    elements.docNavigation = document.getElementById('document-navigation');
    elements.docPrevBtn = document.getElementById('doc-prev-btn');
    elements.docNextBtn = document.getElementById('doc-next-btn');
    elements.docNavInfo = document.getElementById('doc-nav-info');

    // Search help & query feedback
    elements.searchHelpToggle = document.getElementById('search-help-toggle');
    elements.searchHelpContent = document.getElementById('search-help-content');
    elements.queryFeedback = document.getElementById('query-feedback');
    elements.feedbackTerms = document.getElementById('feedback-terms');
}

function setupEventListeners() {
    // Navigation
    elements.navBtns.forEach(btn => {
        btn.addEventListener('click', () => switchView(btn.dataset.view, { userInitiated: true }));
    });

    // Back / Forward re-render from the URL
    window.addEventListener('popstate', (e) => {
        state.modalPushed = !!(e.state && e.state.ov);
        applyUrlState();
    });

    // Search
    elements.searchBtn.addEventListener('click', performSearch);
    elements.searchInput.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.isComposing) performSearch();
    });
    if (elements.clearSearchBtn) elements.clearSearchBtn.addEventListener('click', clearSearch);
    if (elements.searchHelpToggle) elements.searchHelpToggle.addEventListener('click', toggleSearchHelp);
    if (elements.filtersToggle) {
        elements.filtersToggle.addEventListener('click', () => setFiltersOpen(elements.filtersToggle.getAttribute('aria-expanded') !== 'true'));
    }
    if (elements.searchType) elements.searchType.addEventListener('change', updateSearchTypeHint);

    // Filter changes: re-run the active search (and keep the badge honest)
    const rerun = () => {
        updateFiltersCount();
        if (state.lastSearchParams) {
            state.searchPage = 0;
            runSearch({ history: 'push' });
        }
    };

    if (elements.searchCategory) {
        elements.searchCategory.addEventListener('change', async () => {
            const category = elements.searchCategory.value;
            state.searchSubcategory = '';
            if (elements.searchSubcategory) elements.searchSubcategory.value = '';
            await loadSubcategories(category, 'search');
            if (elements.searchCategory.value !== category) return;   // a newer change superseded this one
            if (state.lastSearchParams) {
                state.lastSearchParams.category = category || null;
                state.lastSearchParams.subcategory = null;
            }
            rerun();
        });
    }
    if (elements.searchSubcategory) {
        elements.searchSubcategory.addEventListener('change', () => {
            state.searchSubcategory = elements.searchSubcategory.value;
            if (state.lastSearchParams) state.lastSearchParams.subcategory = state.searchSubcategory || null;
            rerun();
        });
    }
    if (elements.searchFileType) {
        elements.searchFileType.addEventListener('change', () => {
            if (state.lastSearchParams) state.lastSearchParams.file_type = elements.searchFileType.value || null;
            rerun();
        });
    }
    ['searchDateFrom', 'searchDateTo'].forEach((name) => {
        if (!elements[name]) return;
        elements[name].addEventListener('change', () => {
            const ok = validateDateRange();
            updateFiltersCount();
            if (!ok) return;
            // Re-read both controls: the other bound may have been changed while the range was invalid.
            if (state.lastSearchParams) {
                state.lastSearchParams.date_from = elements.searchDateFrom.value || null;
                state.lastSearchParams.date_to = elements.searchDateTo.value || null;
            }
            rerun();
        });
    });

    // Browse
    let filenameSearchTimeout;
    if (elements.browseFilename) {
        elements.browseFilename.addEventListener('input', () => {
            clearTimeout(filenameSearchTimeout);
            filenameSearchTimeout = setTimeout(() => {
                state.browseFilename = elements.browseFilename.value.trim();
                state.browsePage = 0;
                loadDocuments({ history: 'replace' });   // typing must not spam the history
            }, 300);
        });
    }
    if (elements.browseKeyword) {
        elements.browseKeyword.addEventListener('change', async () => {
            state.browseKeyword = elements.browseKeyword.value;
            state.browsePage = 0;
            await loadCategories(state.browseKeyword || null);
            loadDocuments();
        });
    }
    elements.browseCategory.addEventListener('change', async () => {
        state.browseCategory = elements.browseCategory.value;
        state.browseSubcategory = '';
        if (elements.browseSubcategory) elements.browseSubcategory.value = '';
        state.browsePage = 0;
        const chosen = state.browseCategory;
        await loadSubcategories(chosen, 'browse');
        if (elements.browseCategory.value !== chosen) return;       // superseded by a newer change
        loadDocuments();
    });
    if (elements.browseSubcategory) {
        elements.browseSubcategory.addEventListener('change', () => {
            state.browseSubcategory = elements.browseSubcategory.value;
            state.browsePage = 0;
            loadDocuments();
        });
    }
    if (elements.browseFileType) {
        elements.browseFileType.addEventListener('change', () => {
            state.browseFileType = elements.browseFileType.value;
            state.browsePage = 0;
            loadDocuments();
        });
    }

    elements.prevPage.addEventListener('click', () => {
        if (state.browsePage > 0) { state.browsePage--; loadDocuments(); }
    });
    elements.nextPage.addEventListener('click', () => {
        state.browsePage++;
        loadDocuments();
    });
    if (elements.browseGoPage) elements.browseGoPage.addEventListener('click', goToBrowsePage);
    if (elements.browsePageInput) {
        elements.browsePageInput.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); goToBrowsePage(); }
        });
    }

    // Search pagination
    if (elements.searchPrevPage) {
        elements.searchPrevPage.addEventListener('click', () => {
            if (state.searchPage > 0) { state.searchPage--; runSearch({ history: 'push', focusHeading: true }); }
        });
    }
    if (elements.searchNextPage) {
        elements.searchNextPage.addEventListener('click', () => {
            state.searchPage++;
            runSearch({ history: 'push', focusHeading: true });
        });
    }
    if (elements.searchGoPage) elements.searchGoPage.addEventListener('click', goToSearchPage);
    if (elements.searchPageInput) {
        elements.searchPageInput.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); goToSearchPage(); }
        });
    }

    // Ask AI
    elements.askBtn.addEventListener('click', askQuestion);
    elements.askInput.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            askQuestion();
        }
    });
    elements.exampleBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            elements.askInput.value = btn.textContent;
            askQuestion();
        });
    });

    // Document modal
    elements.modalClose.addEventListener('click', requestCloseDocument);
    elements.modalBackdrop.addEventListener('click', requestCloseDocument);
    elements.modalTabs.forEach(tab => {
        tab.addEventListener('click', () => switchModalTab(tab.dataset.tab));
    });
    if (elements.docPrevBtn) elements.docPrevBtn.addEventListener('click', () => navigateDocument(-1));
    if (elements.docNextBtn) elements.docNextBtn.addEventListener('click', () => navigateDocument(1));
    const pdfFullscreenBtn = document.getElementById('pdf-fullscreen-btn');
    const pdfViewer = document.getElementById('modal-pdf-viewer');
    if (pdfFullscreenBtn && pdfViewer) pdfFullscreenBtn.addEventListener('click', () => togglePdfFullscreen(pdfViewer));

    // Feedback form - use event delegation for reliability
    document.addEventListener('submit', (e) => {
        if (e.target && e.target.id === 'feedback-form') handleFeedbackSubmit(e);
    });

    // Export CSV buttons -> open modal
    const exportSearchBtn = document.getElementById('export-search-results');
    if (exportSearchBtn) exportSearchBtn.addEventListener('click', () => openExportModal('search'));
    const exportBrowseBtn = document.getElementById('export-browse-results');
    if (exportBrowseBtn) exportBrowseBtn.addEventListener('click', () => openExportModal('browse'));

    const exportModalConfirm = document.getElementById('export-modal-confirm');
    const exportModalCancel = document.getElementById('export-modal-cancel');
    const exportModalClose = document.getElementById('export-modal-close');
    const exportModalBackdrop = document.querySelector('#export-modal > .modal-backdrop');
    if (exportModalConfirm) exportModalConfirm.addEventListener('click', confirmExport);
    if (exportModalCancel) exportModalCancel.addEventListener('click', closeExportModal);
    if (exportModalClose) exportModalClose.addEventListener('click', closeExportModal);
    if (exportModalBackdrop) exportModalBackdrop.addEventListener('click', closeExportModal);
}

async function handleFeedbackSubmit(e) {
    if (e) e.preventDefault();
    console.log('Feedback form submitted');
    submitFeedback();
}

// Submit handler (wired through the form's submit event)
window.submitFeedback = async function() {
    const form = document.getElementById('feedback-form');
    const btn = document.querySelector('.feedback-btn');
    
    const feedbackType = document.getElementById('feedback-type').value;
    const email = document.getElementById('feedback-email').value;
    const message = document.getElementById('feedback-message').value;
    
    // Spam protection checks
    const honeypot = document.getElementById('feedback-website').value;
    const timestamp = document.getElementById('feedback-timestamp').value;
    
    // Check honeypot (should be empty)
    if (honeypot) {
        console.log('Honeypot triggered');
        showFeedbackStatus('Thank you! Your feedback has been submitted.', 'success'); // Fake success
        return;
    }
    
    // Check timing (must be at least 3 seconds since page load)
    const elapsed = Date.now() - parseInt(timestamp || '0');
    if (elapsed < 3000) {
        console.log('Too fast submission');
        showFeedbackStatus('Please wait a moment before submitting.', 'error');
        return;
    }
    
    if (!feedbackType || !email.trim() || !message.trim()) {
        showFeedbackStatus('Please fill in all required fields.', 'error');
        return;
    }
    
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Verifying...';
    
    // Get reCAPTCHA v3 token (gracefully skip if blocked by browser extension)
    let recaptchaResponse = null;
    if (typeof grecaptcha !== 'undefined' && !window.__recaptchaFailed) {
        try {
            recaptchaResponse = await grecaptcha.execute('6Lf9EDYsAAAAANDlA_xYFIM7Ylccgmc24LhZgDIr', {action: 'submit_feedback'});
        } catch (e) {
            console.warn('reCAPTCHA unavailable, proceeding without:', e);
        }
    }
    
    btn.innerHTML = '<span class="spinner"></span> Sending...';
    
    try {
        const response = await fetch(`${API_BASE}/feedback`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                type: feedbackType,
                email: email || null,
                message: message.trim(),
                recaptcha_token: recaptchaResponse,
                _ts: timestamp  // Send timestamp for server-side validation
            })
        });
        
        const data = await response.json();
        
        if (response.ok) {
            showFeedbackStatus('Thank you! Your feedback has been submitted.', 'success');
            form.reset();
            // Reset timestamp for next submission
            document.getElementById('feedback-timestamp').value = Date.now().toString();
        } else {
            showFeedbackStatus(data.detail || 'Failed to submit feedback.', 'error');
        }
    } catch (error) {
        console.error('Feedback error:', error);
        showFeedbackStatus('Failed to submit feedback. Please try again.', 'error');
    }
    
    btn.disabled = false;
    btn.innerHTML = `
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/>
        </svg>
        Submit Feedback
    `;
}

function showFeedbackStatus(message, type) {
    const status = document.getElementById('feedback-status');
    if (!status) return;
    
    status.textContent = message;
    status.className = `feedback-status ${type}`;
    
    // Auto-hide success messages
    if (type === 'success') {
        setTimeout(() => {
            status.classList.add('hidden');
        }, 5000);
    }
}

function switchView(viewName, opts = {}) {
    const { history: mode = 'push', skipLoad = false, userInitiated = false } = opts;
    if (!VALID_VIEWS.includes(viewName) || (viewName === 'ask' && state.askDisabled)) viewName = 'search';
    state.currentView = viewName;

    elements.navBtns.forEach(btn => {
        const on = btn.dataset.view === viewName;
        btn.classList.toggle('active', on);
        if (on) btn.setAttribute('aria-current', 'page'); else btn.removeAttribute('aria-current');
    });
    elements.views.forEach(view => view.classList.toggle('active', view.id === `${viewName}-view`));
    closePopovers();
    if (userInitiated) announce(VIEW_TITLES[viewName]);   // before any content announcement (e.g. the Browse count)

    if (viewName === 'browse') {
        if (!skipLoad) loadDocuments({ history: mode });
    } else {
        state.renderedKey = currentBaseKey();
        syncUrl(mode);
    }
    updateDocumentTitle();

    if (userInitiated) {
        const heading = document.querySelector(`#${viewName}-view h1`);
        if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
    }
}

async function loadStats() {
    try {
        const response = await fetch(`${API_BASE}/stats`);
        if (!response.ok) throw new Error('Failed to load stats');
        
        state.stats = await response.json();
        renderStats();
        updateLLMStatus();
    } catch (error) {
        console.error('Error loading stats:', error);
        elements.statsGrid.innerHTML = '<p class="error">Failed to load statistics. Make sure the server is running and documents are indexed.</p>';
    }
}

async function loadCategories(keyword = null) {
    try {
        let url = `${API_BASE}/categories`;
        if (keyword) {
            url += `?keyword=${encodeURIComponent(keyword)}`;
        }
        
        const response = await fetch(url);
        if (!response.ok) throw new Error('Failed to load categories');
        
        const data = await response.json();
        state.categories = data.categories || [];
        
        // Filter out user-excluded categories for dropdowns
        const visibleCategories = state.categories.filter(
            c => !state.excludedCategories.includes(c.category)
        );
        
        // Populate category dropdowns with visible categories only
        const categoryOptions = visibleCategories.map(c => 
            `<option value="${escapeHtml(c.category)}">${escapeHtml(c.category)} (${c.count})</option>`
        ).join('');
        
        // Preserve current selection
        const currentBrowseCategory = elements.browseCategory.value;
        const currentSearchCategory = elements.searchCategory.value;
        
        elements.searchCategory.innerHTML = '<option value="">All File Sets</option>' + categoryOptions;
        elements.browseCategory.innerHTML = '<option value="">All File Sets</option>' + categoryOptions;
        
        // Restore selection if still valid (and not excluded)
        if (currentBrowseCategory && visibleCategories.some(c => c.category === currentBrowseCategory)) {
            elements.browseCategory.value = currentBrowseCategory;
        }
        if (currentSearchCategory && visibleCategories.some(c => c.category === currentSearchCategory)) {
            elements.searchCategory.value = currentSearchCategory;
        }
        
        // Populate exclude dropdowns with ALL categories (so user can toggle)
        renderExcludeDropdowns();
    } catch (error) {
        console.error('Error loading categories:', error);
    }
}

// ============================================================================
// Category Exclusion Functions (User Preferences)
// ============================================================================

/** Rebuild the exclude checkboxes only when the list of file sets changed; otherwise just sync them (keeps keyboard focus). */
function renderExcludeDropdowns() {
    const key = JSON.stringify(state.categories.map(c => c.category));
    const html = state.categories.map(c => `
        <label>
            <input type="checkbox"
                   value="${escapeHtml(c.category)}"
                   data-category="${escapeHtml(c.category)}"
                   ${state.excludedCategories.includes(c.category) ? 'checked' : ''}>
            ${escapeHtml(c.category)}
        </label>
    `).join('');

    ['search-exclude-options', 'browse-exclude-options'].forEach(id => {
        const container = document.getElementById(id);
        if (!container) return;
        if (container.dataset.key === key) {
            container.querySelectorAll('input[type="checkbox"][data-category]').forEach(cb => {
                cb.checked = state.excludedCategories.includes(cb.dataset.category);
            });
            return;
        }
        container.dataset.key = key;
        container.innerHTML = html;
        container.querySelectorAll('input[type="checkbox"][data-category]').forEach(cb => {
            cb.addEventListener('change', () => toggleCategoryExclusion(cb.dataset.category));
        });
    });
    updateExcludeButtons();
}

function toggleCategoryExclusion(category) {
    const index = state.excludedCategories.indexOf(category);
    if (index === -1) state.excludedCategories.push(category);
    else state.excludedCategories.splice(index, 1);
    saveExcludedCategories();
    refreshAfterExclusionChange();
}

function saveExcludedCategories() {
    try { localStorage.setItem('excludedCategories', JSON.stringify(state.excludedCategories)); } catch (e) { /* storage blocked */ }
    syncExcludeCheckboxes();
    updateExcludeButtons();
}

function clearExclusions() {
    state.excludedCategories = [];
    saveExcludedCategories();
    renderExcludeDropdowns();
    refreshAfterExclusionChange();
}

function updateExcludeButtons() {
    const count = state.excludedCategories.length;
    const searchBtn = document.getElementById('search-exclude-toggle');
    const browseBtn = document.getElementById('browse-exclude-toggle');
    const arrow = ' <span aria-hidden="true">▼</span>';
    if (searchBtn) {
        searchBtn.innerHTML = (count > 0 ? `(${count})` : 'None') + arrow;
        searchBtn.classList.toggle('active', count > 0);
    }
    if (browseBtn) {
        browseBtn.innerHTML = (count > 0 ? `Exclude (${count})` : 'Exclude') + arrow;
        browseBtn.classList.toggle('active', count > 0);
    }
}


// Load keywords for topic filtering
async function loadKeywords() {
    try {
        const response = await fetch(`${API_BASE}/keywords`);
        if (!response.ok) throw new Error('Failed to load keywords');
        
        const data = await response.json();
        const keywords = data.keywords || {};
        
        // Category icons
        const categoryIcons = {
            'People': '👤',
            'Locations': '📍',
            'Topics': '📋'
        };
        
        // Build options HTML grouped by category
        let optionsHtml = '<option value="">All Topics</option>';
        
        // Order categories consistently
        const categoryOrder = ['People', 'Locations', 'Topics'];
        
        for (const category of categoryOrder) {
            const items = keywords[category];
            if (items && items.length > 0) {
                const icon = categoryIcons[category] || '🏷️';
                optionsHtml += `<optgroup label="${icon} ${category}">`;
                
                for (const kw of items) {
                    const countText = kw.document_count > 0 ? ` (${formatNumber(kw.document_count)})` : '';
                    optionsHtml += `<option value="${escapeHtml(kw.search_term)}">${escapeHtml(kw.name)}${countText}</option>`;
                }
                
                optionsHtml += '</optgroup>';
            }
        }
        
        // Handle any additional categories not in the standard order
        for (const [category, items] of Object.entries(keywords)) {
            if (!categoryOrder.includes(category) && items && items.length > 0) {
                const icon = categoryIcons[category] || '🏷️';
                optionsHtml += `<optgroup label="${icon} ${category}">`;
                
                for (const kw of items) {
                    const countText = kw.document_count > 0 ? ` (${formatNumber(kw.document_count)})` : '';
                    optionsHtml += `<option value="${escapeHtml(kw.search_term)}">${escapeHtml(kw.name)}${countText}</option>`;
                }
                
                optionsHtml += '</optgroup>';
            }
        }
        
        // Update the browse keyword dropdown
        if (elements.browseKeyword) {
            const currentValue = elements.browseKeyword.value;
            elements.browseKeyword.innerHTML = optionsHtml;
            
            // Restore selection if still valid
            if (currentValue) {
                elements.browseKeyword.value = currentValue;
            }
        }
    } catch (error) {
        console.error('Error loading keywords:', error);
        // Fallback: keep the dropdown as-is or show basic option
        if (elements.browseKeyword && elements.browseKeyword.options.length <= 1) {
            elements.browseKeyword.innerHTML = '<option value="">All Topics</option>';
        }
    }
}

// Fallback path (bootstrap failed): same effect as applyPublicSettings, no inline styles.
async function loadPublicSettings() {
    try {
        const response = await fetch(`${API_BASE}/settings`);
        if (!response.ok) return;
        const settings = await response.json();
        if (settings.ask_ai_enabled === false) hideAskAi();
    } catch (error) {
        console.error('Error loading public settings:', error);
    }
}

// Load and display pinned documents on homepage
// skipSettingsCheck: when true (e.g. from bootstrap), skip fetching /settings and assume pinned is enabled
async function loadPinnedDocuments(skipSettingsCheck = false) {
    try {
        if (!skipSettingsCheck) {
            const settingsResponse = await fetch(`${API_BASE}/settings`);
            if (settingsResponse.ok) {
                const settings = await settingsResponse.json();
                if (settings.pinned_documents_enabled === false) {
                    const existingBar = document.getElementById('pinned-documents-bar');
                    if (existingBar) existingBar.remove();
                    removePinnedSkeleton();
                    return;
                }
            }
        }
        
        const response = await fetch(`${API_BASE}/pinned-documents`);
        if (!response.ok) { removePinnedSkeleton(); return; }
        
        const data = await response.json();
        const pinnedDocs = data.pinned_documents || [];
        
        if (pinnedDocs.length > 0) {
            renderPinnedDocumentsBar(pinnedDocs);
        } else {
            removePinnedSkeleton();
        }
    } catch (error) {
        console.error('Error loading pinned documents:', error);
        removePinnedSkeleton();
    }
}

function removePinnedSkeleton() {
    const skeleton = document.getElementById('pinned-documents-skeleton');
    if (skeleton) skeleton.remove();
}

function renderPinnedDocumentsBar(docs) {
    const searchView = document.getElementById('search-view');
    if (!searchView) return;

    let pinnedBar = document.getElementById('pinned-documents-bar');
    if (pinnedBar) pinnedBar.remove();

    const generateCardHTML = (doc) => {
        const id = String(doc.document_id);
        return `
        <a class="pinned-card" href="${escapeHtml(buildUrl({ doc: id }))}" data-action="open-doc" data-doc-id="${escapeHtml(id)}">
            <div class="pinned-card-thumbnail">
                <img src="${API_BASE}/documents/${encodeURIComponent(id)}/thumbnail" alt="" loading="lazy">
                <div class="thumbnail-fallback">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                        <path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                    </svg>
                </div>
            </div>
            <div class="pinned-card-content">
                <div class="pinned-card-filename">${escapeHtml(doc.filename)}</div>
                ${doc.reason ? `<div class="pinned-card-reason">"${escapeHtml(doc.reason)}"</div>` : ''}
                <div class="pinned-card-meta">${escapeHtml(doc.category || 'Document')}</div>
            </div>
        </a>`;
    };

    const cardsHTML = docs.map(generateCardHTML).join('');

    pinnedBar = document.createElement('div');
    pinnedBar.id = 'pinned-documents-bar';
    pinnedBar.className = 'pinned-documents-bar pinned-bar-fadein';
    pinnedBar.innerHTML = `
        <div class="pinned-header">
            <span class="pinned-icon" aria-hidden="true">📌</span>
            <h2 class="pinned-title">Featured Documents</h2>
            <span class="pinned-subtitle">Controversial & Notable Files</span>
            <button type="button" class="pinned-pause" data-action="toggle-pinned" aria-pressed="false">
                <span class="pinned-pause-icon" aria-hidden="true">⏸</span><span class="pinned-pause-label">Pause</span>
            </button>
            <span class="pinned-suggestion-note">Have a document that should be featured? Use the "Send Feedback" form below to submit your suggestion!</span>
        </div>
        <div class="pinned-scroll-container" role="region" aria-label="Featured documents">
            <div class="pinned-scroll" id="pinned-scroll">
                ${cardsHTML}
                <div class="pinned-dup" aria-hidden="true">${asDuplicateCards(cardsHTML)}</div>
            </div>
        </div>
    `;

    removePinnedSkeleton();
    const statsDisplay = document.getElementById('stats-display');
    if (statsDisplay) statsDisplay.parentNode.insertBefore(pinnedBar, statsDisplay);
    else searchView.appendChild(pinnedBar);
}

// =============================================================================
// Document alterations — public transparency. Shows where DOJ re-issued/redacted
// a document after release. Older (pre-redaction) versions are served ONLY when
// an admin has EXPOSED that document — the API enforces this; the UI just reflects it.
// =============================================================================

function formatAlteredOn(stamp) {
    const m = /^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/.exec(stamp || '');
    return m ? `${m[1]}-${m[2]}-${m[3]}` : (stamp || '');
}

let _alterationCompare = { oldId: null, newId: null, title: '' };

async function loadDocumentAlterationBadge(docId, seq) {
    try {
        const resp = await fetch(`${API_BASE}/documents/${encodeURIComponent(docId)}/alteration`);
        if (!resp.ok) return;
        const a = await resp.json();
        // Ignore a late answer for a document that is no longer the open one.
        if (seq !== state.docSeq || state.openDocId !== docId) return;
        if (!a.altered || !elements.modalAlteration) return;
        const removed = a.lines_removed || 0;
        const when = a.altered_on ? formatAlteredOn(a.altered_on) : '';
        // Full wording on desktop, a compact one-row version on phones (CSS swaps which span is shown).
        let html = `<span class="altered-badge"><span aria-hidden="true">⚠</span> <span class="alt-long">DOJ altered this document${removed ? ` — ${formatNumber(removed)} lines removed` : ''}${when ? ` · ${escapeHtml(when)}` : ''}</span><span class="alt-short">Altered by DOJ</span></span>`;
        if (a.exposed && a.old_id) {
            _alterationCompare = { oldId: a.old_id, newId: a.new_id, title: (state.currentDocument && state.currentDocument.filename) || '' };
            html += '<button type="button" class="altered-compare-btn" data-action="open-exposed-compare"><span class="alt-long">See what changed</span><span class="alt-short">Compare</span> →</button>';
        }
        elements.modalAlteration.innerHTML = html;
        setHidden(elements.modalAlteration, false);
    } catch (e) { /* best-effort */ }
}

function openExposedCompare() {
    openPublicCompare(_alterationCompare.oldId, _alterationCompare.newId, _alterationCompare.title);
}

function openPublicCompare(oldId, newId, title) {
    const modal = document.getElementById('public-compare-modal');
    if (!modal || !oldId || !newId) return;
    modal.dataset.oldId = oldId;
    modal.dataset.newId = newId;
    const titleEl = document.getElementById('public-compare-title');
    if (titleEl) titleEl.textContent = title ? `What changed: ${title}` : 'What changed';
    openDialog(modal, { trigger: document.activeElement, onRequestClose: closePublicCompare });
    switchPublicCompareTab('visual');
}

function closePublicCompare() {
    const modal = document.getElementById('public-compare-modal');
    const body = document.getElementById('public-compare-body');
    if (body) body.innerHTML = '';
    closeDialog(modal);
}

function switchPublicCompareTab(tab) {
    const vb = document.getElementById('public-compare-tab-visual');
    const tb = document.getElementById('public-compare-tab-text');
    const body = document.getElementById('public-compare-body');
    syncTabs(vb && vb.closest('[role="tablist"]'), tab === 'visual' ? vb : tb);
    if (body) body.setAttribute('aria-labelledby', tab === 'visual' ? 'public-compare-tab-visual' : 'public-compare-tab-text');
    if (tab === 'visual') renderPublicCompareVisual(); else renderPublicCompareText();
}

function renderPublicCompareVisual() {
    const modal = document.getElementById('public-compare-modal');
    const body = document.getElementById('public-compare-body');
    if (!modal || !body) return;
    const oldUrl = `${API_BASE}/documents/${encodeURIComponent(modal.dataset.oldId)}/file`;
    const newUrl = `${API_BASE}/documents/${encodeURIComponent(modal.dataset.newId)}/file`;
    body.innerHTML = `
        <div class="compare-panes">
            <div class="compare-pane">
                <div class="compare-pane-label old">Original — before DOJ's change</div>
                <iframe title="Original document, before DOJ's change" src="${escapeHtml(oldUrl)}#view=FitH"></iframe>
            </div>
            <div class="compare-pane">
                <div class="compare-pane-label new">Current — after DOJ's change</div>
                <iframe title="Current document, after DOJ's change" src="${escapeHtml(newUrl)}#view=FitH"></iframe>
            </div>
        </div>`;
}

async function renderPublicCompareText() {
    const modal = document.getElementById('public-compare-modal');
    const body = document.getElementById('public-compare-body');
    if (!modal || !body) return;
    const note = (cls, text) => { body.innerHTML = `<div class="compare-state ${cls}">${text}</div>`; };
    note('', 'Loading diff…');
    try {
        const resp = await fetch(`${API_BASE}/version-diff?old=${encodeURIComponent(modal.dataset.oldId)}&new=${encodeURIComponent(modal.dataset.newId)}`);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        if (!data.has_text) { note('', 'No extracted text to diff — use the Side-by-side view.'); return; }
        if (data.identical) { note('ok', 'The transcribed text is identical — the change was visual (e.g. a redaction box). Use Side-by-side.'); return; }
        let rows = '';
        for (const ln of (data.lines || [])) {
            let cls = '', prefix = ' ';
            if (ln.type === 'add') { cls = 'add'; prefix = '+'; }
            else if (ln.type === 'del') { cls = 'del'; prefix = '−'; }
            else if (ln.type === 'hunk') { cls = 'hunk'; prefix = ''; }
            rows += `<div class="diff-row ${cls}">${escapeHtml(prefix + (ln.text || ''))}</div>`;
        }
        body.innerHTML = `<div class="diff-wrap">
            <div class="diff-summary"><strong class="removed">−${Number(data.removed) || 0} removed</strong> · <strong class="added">+${Number(data.added) || 0} added</strong> <span>(original → current)</span></div>
            <div class="diff-rows" tabindex="0" role="region" aria-label="Text differences">${rows}</div></div>`;
    } catch (e) {
        note('error', 'Couldn\'t load the diff.');
    }
}

async function loadCensoredBar() {
    try {
        const resp = await fetch(`${API_BASE}/altered-documents?limit=30`);
        if (!resp.ok) return;
        const data = await resp.json();
        const items = data.altered_documents || [];
        if (items.length) renderCensoredBar(items, data.total || items.length);
    } catch (e) {}
}

function renderCensoredBar(items, total) {
    const searchView = document.getElementById('search-view');
    if (!searchView) return;
    const existing = document.getElementById('censored-documents-bar');
    if (existing) existing.remove();
    const card = (d) => `
        <button type="button" class="pinned-card" data-action="open-compare" data-old="${escapeHtml(d.old_id)}" data-new="${escapeHtml(d.new_id)}" data-title="${escapeHtml(d.filename || '')}">
            <div class="pinned-card-thumbnail">
                <img src="${API_BASE}/documents/${encodeURIComponent(d.new_id)}/thumbnail" alt="" loading="lazy">
                <div class="thumbnail-fallback"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg></div>
            </div>
            <div class="pinned-card-content">
                <div class="pinned-card-filename">${escapeHtml(d.filename || '')}</div>
                <div class="pinned-card-reason removed">−${formatNumber(d.lines_removed || 0)} lines removed by DOJ</div>
                <div class="pinned-card-meta">Set ${escapeHtml(d.dataset_num)} · click to compare</div>
            </div>
        </button>`;
    const cards = items.map(card).join('');
    const bar = document.createElement('div');
    bar.id = 'censored-documents-bar';
    bar.className = 'pinned-documents-bar pinned-bar-fadein';
    bar.innerHTML = `
        <div class="pinned-header">
            <span class="pinned-icon" aria-hidden="true">🚩</span>
            <h2 class="pinned-title">Altered by DOJ</h2>
            <span class="pinned-subtitle">Documents changed after release — see what was removed</span>
            <button type="button" class="pinned-pause" data-action="toggle-pinned" aria-pressed="false">
                <span class="pinned-pause-icon" aria-hidden="true">⏸</span><span class="pinned-pause-label">Pause</span>
            </button>
            <span class="pinned-suggestion-note"><button type="button" class="inline-link link-subtle" data-action="open-gallery">View all ${formatNumber(total)} →</button></span>
        </div>
        <div class="pinned-scroll-container" role="region" aria-label="Documents altered by DOJ"><div class="pinned-scroll">${cards}<div class="pinned-dup" aria-hidden="true">${asDuplicateCards(cards)}</div></div></div>`;
    const pinnedBar = document.getElementById('pinned-documents-bar');
    const statsDisplay = document.getElementById('stats-display');
    if (pinnedBar) pinnedBar.parentNode.insertBefore(bar, pinnedBar.nextSibling);
    else if (statsDisplay) statsDisplay.parentNode.insertBefore(bar, statsDisplay);
    else searchView.appendChild(bar);
}

async function openAlteredGallery() {
    const modal = document.getElementById('altered-gallery-modal');
    const body = document.getElementById('altered-gallery-body');
    if (!modal || !body) return;
    openDialog(modal, { trigger: document.activeElement, onRequestClose: closeAlteredGallery });
    body.innerHTML = '<div class="compare-state">Loading…</div>';
    try {
        const resp = await fetch(`${API_BASE}/altered-documents?limit=200`);
        const data = await resp.json();
        const items = data.altered_documents || [];
        if (!items.length) { body.innerHTML = '<div class="compare-state">No exposed alterations yet.</div>'; return; }
        body.innerHTML = '<div class="gallery-grid">' + items.map(d => `
            <button type="button" class="gallery-card" data-action="open-compare" data-old="${escapeHtml(d.old_id)}" data-new="${escapeHtml(d.new_id)}" data-title="${escapeHtml(d.filename || '')}">
                <div class="g-name">${escapeHtml(d.filename || '')}</div>
                <div class="g-removed">−${formatNumber(d.lines_removed || 0)} lines removed</div>
                <div class="g-meta">Set ${escapeHtml(d.dataset_num)}${d.altered_on ? ' · ' + escapeHtml(formatAlteredOn(d.altered_on)) : ''}</div>
            </button>`).join('') + '</div>';
    } catch (e) {
        body.innerHTML = '<div class="compare-state error">Couldn\'t load.</div>';
    }
}

function closeAlteredGallery() {
    closeDialog(document.getElementById('altered-gallery-modal'));
}

async function loadSubcategories(category, target = 'search') {
    const subcategoryEl = target === 'search' ? elements.searchSubcategory : elements.browseSubcategory;
    const groupEl = target === 'search' ? elements.searchSubcategoryGroup : null;
    if (!subcategoryEl) return;
    // The browse select hides itself; the search select hides its labelled group.
    const toggle = (show) => setHidden(groupEl || subcategoryEl, !show);

    const seq = (state._subSeq[target] = (state._subSeq[target] || 0) + 1);
    if (!category) {
        toggle(false);
        subcategoryEl.innerHTML = '<option value="">All Sections</option>';
        return;
    }
    try {
        const response = await fetch(`${API_BASE}/subcategories?category=${encodeURIComponent(category)}`);
        if (!response.ok) throw new Error('Failed to load subcategories');
        const data = await response.json();
        if (seq !== state._subSeq[target]) return;                 // a newer request owns the dropdown now
        const subcategories = data.subcategories || [];
        if (subcategories.length > 1) {
            const options = subcategories.map(s =>
                `<option value="${escapeHtml(s.subcategory)}">${escapeHtml(s.subcategory)} (${s.count})</option>`).join('');
            subcategoryEl.innerHTML = '<option value="">All Sections</option>' + options;
            toggle(true);
        } else {
            toggle(false);
            subcategoryEl.innerHTML = '<option value="">All Sections</option>';
        }
    } catch (error) {
        console.error('Error loading subcategories:', error);
        toggle(false);
    }
}

function renderStats() {
    if (!state.stats) return;
    const stats = state.stats;
    const fileTypes = stats.by_file_type || [];
    const pdfCount = fileTypes.find(f => f.file_type === 'pdf')?.count || 0;
    const documentCount = fileTypes.find(f => f.file_type === 'document')?.count || 0;
    const totalDocCount = pdfCount + documentCount || stats.total_documents;
    const audioCount = fileTypes.find(f => f.file_type === 'audio')?.count || 0;
    const videoCount = fileTypes.find(f => f.file_type === 'video')?.count || 0;
    const imageCount = fileTypes.find(f => f.file_type === 'image')?.count || 0;

    const card = (browse, icon, value, label, title, action) => `
        <button type="button" class="stat-card file-type-card clickable" data-browse="${browse}" title="${title}">
            <span class="stat-value"><span aria-hidden="true">${icon}</span> ${formatNumber(value)}</span>
            <span class="stat-label">${label}</span>
            <span class="stat-action">${action}</span>
        </button>`;

    elements.statsGrid.innerHTML = `
        <button type="button" class="stat-card clickable" data-browse="all" title="Browse all files">
            <span class="stat-value">${formatNumber(stats.total_documents)}</span>
            <span class="stat-label">Total Files</span>
            <span class="stat-action">Browse All →</span>
        </button>
        <div class="stat-card">
            <div class="stat-value">${formatNumber(stats.total_pages)}</div>
            <div class="stat-label">Total Pages</div>
        </div>
        ${card('documents', '📄', totalDocCount, 'Documents', 'Browse all documents', 'Browse →')}
        ${card('audio', '🎵', audioCount, 'Audio Files', 'Browse audio files', 'Browse →')}
        ${imageCount > 0 ? card('image', '🖼️', imageCount, 'Image Files', 'Browse image files', 'Browse →') : ''}
        ${card('video', '🎬', videoCount, 'Video Files', 'Browse video files', 'Browse →')}
    `;

    elements.statsGrid.querySelectorAll('.stat-card.clickable').forEach(btn => {
        btn.addEventListener('click', async () => {
            const browseType = btn.dataset.browse;
            state.browseFileType = (browseType === 'all' || browseType === 'documents') ? '' : browseType;
            state.browseCategory = '';
            state.browseSubcategory = '';
            state.browseKeyword = '';
            state.browseFilename = '';
            state.browsePage = 0;
            if (elements.browseFileType) elements.browseFileType.value = state.browseFileType;
            if (elements.browseCategory) elements.browseCategory.value = '';
            if (elements.browseKeyword) elements.browseKeyword.value = '';
            if (elements.browseFilename) elements.browseFilename.value = '';
            await loadCategories();
            await loadSubcategories('', 'browse');
            switchView('browse', { userInitiated: true });
        });
    });

    // Also update the file type filter with counts
    if (elements.searchFileType) {
        const current = elements.searchFileType.value;
        let html = `
            <option value="">All Files (${formatNumber(stats.total_documents)})</option>
            <option value="pdf">📄 PDF Documents (${formatNumber(pdfCount)})</option>
            <option value="document">📄 Scanned Documents (${formatNumber(documentCount)})</option>
            <option value="audio">🎵 Audio (${formatNumber(audioCount)})</option>`;
        if (imageCount > 0) html += `<option value="image">🖼️ Images (${formatNumber(imageCount)})</option>`;
        html += `<option value="video">🎬 Video (${formatNumber(videoCount)})</option>`;
        elements.searchFileType.innerHTML = html;
        if (current && [...elements.searchFileType.options].some(o => o.value === current)) elements.searchFileType.value = current;
    }
}

function updateLLMStatus() {
    if (!state.stats) return;
    
    if (state.stats.llm_available) {
        elements.llmStatus.className = 'llm-status available';
        elements.llmStatus.textContent = 'AI Assistant Ready';
        elements.askBtn.disabled = false;
    } else {
        elements.llmStatus.className = 'llm-status unavailable';
        elements.llmStatus.textContent = 'AI Assistant Unavailable (Set OPENAI_API_KEY)';
        elements.askBtn.disabled = true;
    }
}

async function performSearch() {
    const query = elements.searchInput.value.trim();
    if (query.length < 2) {
        announce('Type at least two characters to search.');
        elements.searchInput.focus();
        return;
    }
    if (!validateDateRange()) {
        if (elements.searchDateFrom) elements.searchDateFrom.focus();
        return;
    }
    state.searchPage = 0;
    state.lastSearchParams = readSearchControls();
    await runSearch({ history: 'push' });
}


async function clearSearch() {
    showSearchHome();
    syncUrl('push');
    window.scrollTo({ top: 0, behavior: scrollBehavior() });
    elements.searchInput.focus({ preventScroll: true });
}

/**
 * Toggle search help section visibility
 */
function toggleSearchHelp() {
    if (!elements.searchHelpContent || !elements.searchHelpToggle) return;
    
    const isExpanded = elements.searchHelpToggle.getAttribute('aria-expanded') === 'true';
    
    elements.searchHelpToggle.setAttribute('aria-expanded', !isExpanded);
    elements.searchHelpContent.classList.toggle('hidden');
    
    // Animate the chevron
    const chevron = elements.searchHelpToggle.querySelector('.chevron-icon');
    if (chevron) {
        chevron.style.transform = isExpanded ? 'rotate(0deg)' : 'rotate(180deg)';
    }
}

/**
 * Display visual feedback showing how the search query was interpreted
 * @param {Object} parsedQuery - The parsed_query object from the search response
 */
function displayQueryFeedback(parsedQuery) {
    if (!elements.queryFeedback || !elements.feedbackTerms || !parsedQuery) {
        return;
    }
    
    const { excluded_terms, required_terms, phrases, has_or, has_wildcards } = parsedQuery;
    
    // Don't show feedback if query is simple (no special operators)
    const hasSpecialOperators = (excluded_terms && excluded_terms.length > 0) ||
                                (phrases && phrases.length > 0) ||
                                has_or ||
                                has_wildcards;
    
    if (!hasSpecialOperators) {
        elements.queryFeedback.classList.add('hidden');
        return;
    }
    
    // Build the feedback HTML
    let feedbackHTML = '';
    
    // Show required terms
    if (required_terms && required_terms.length > 0) {
        const termsWithoutPhrases = required_terms.filter(t => !phrases.includes(t));
        if (termsWithoutPhrases.length > 0) {
            feedbackHTML += termsWithoutPhrases.map(term => 
                `<span class="feedback-term feedback-required">${escapeHtml(term)}${term.endsWith('*') ? '' : ''}</span>`
            ).join(' ');
        }
    }
    
    // Show phrases
    if (phrases && phrases.length > 0) {
        if (feedbackHTML) feedbackHTML += ' ';
        feedbackHTML += phrases.map(phrase => 
            `<span class="feedback-term feedback-phrase">"${escapeHtml(phrase)}"</span>`
        ).join(' ');
    }
    
    // Show OR indicator
    if (has_or) {
        feedbackHTML += ' <span class="feedback-operator">OR</span> ';
    }
    
    // Show excluded terms
    if (excluded_terms && excluded_terms.length > 0) {
        if (feedbackHTML && !has_or) feedbackHTML += ' ';
        feedbackHTML += '<span class="feedback-excluding">excluding:</span> ';
        feedbackHTML += excluded_terms.map(term => 
            `<span class="feedback-term feedback-excluded">${escapeHtml(term)}</span>`
        ).join(' ');
    }
    
    // Show wildcard indicator
    if (has_wildcards) {
        feedbackHTML += ' <span class="feedback-note">(prefix matching)</span>';
    }
    
    elements.feedbackTerms.innerHTML = feedbackHTML;
    elements.queryFeedback.classList.remove('hidden');
}

function renderSearchResults(data, { focusHeading = false } = {}) {
    const results = data.results || [];
    const params = state.lastSearchParams || {};
    const totalPages = Math.ceil(data.total / state.searchLimit);
    const startResult = state.searchPage * state.searchLimit + 1;
    const endResult = startResult + results.length - 1;

    let filterContext = '';
    const ctx = [];
    if (params.category) ctx.push(params.category);
    if (params.subcategory) ctx.push(params.subcategory);
    if (params.file_type) ctx.push(FILE_TYPE_LABELS[params.file_type] || params.file_type);
    if (ctx.length) filterContext = ` in ${ctx.join(' › ')}`;

    let exclusionNote = '';
    if (!params.category && state.excludedCategories.length) {
        const n = state.excludedCategories.length;
        exclusionNote = ` (excluding ${n} file set${n > 1 ? 's' : ''})`;
    }

    const summary = data.total > state.searchLimit && results.length
        ? `Showing ${startResult}-${endResult} of ${formatNumber(data.total)} results for "${data.query}"${filterContext}${exclusionNote}`
        : `${formatNumber(data.total)} results for "${data.query}"${filterContext}${exclusionNote}`;
    elements.resultsCount.textContent = summary;
    announce(data.total ? summary : `No results for "${data.query}"`);

    if (elements.searchPagination) {
        if (data.total > state.searchLimit) {
            setHidden(elements.searchPagination, false);
            elements.searchPrevPage.disabled = state.searchPage === 0;
            elements.searchNextPage.disabled = !((state.searchPage + 1) * state.searchLimit < data.total);
            renderSearchPageNumbers(totalPages);
            if (elements.searchPageInput) {
                elements.searchPageInput.max = Math.max(1, totalPages);
                elements.searchPageInput.value = state.searchPage + 1;
            }
        } else {
            setHidden(elements.searchPagination, true);
        }
    }

    if (data.facets) updateSearchFilterCounts(data.facets);
    updateRefineBar();
    updateFiltersCount();

    if (!results.length) {
        state.documentList = [];
        if (data.total > 0 && state.searchPage > 0) {
            elements.resultsList.innerHTML = `
                <div class="state-panel state-empty">
                    <h3>There are no results on this page</h3>
                    <p>This search has ${formatNumber(data.total)} results, but not this many pages.</p>
                    <div class="state-actions"><button type="button" class="state-btn primary" data-action="search-first-page">Go to the first page</button></div>
                </div>`;
        } else {
            renderSearchEmpty();
        }
        return;
    }

    state.documentList = results.map(r => ({ id: r.id, filename: r.filename }));
    elements.resultsList.innerHTML = results.map(resultCardHtml).join('');
    if (focusHeading && elements.resultsTitle) elements.resultsTitle.focus({ preventScroll: true });
}

function updateSearchFilterCounts(facets) {
    // Facet counts are search-specific. Keep the user's current choice selectable even when it has zero hits,
    // otherwise the dropdown would silently snap back to "All" while the filter is still applied.
    // A search with no hits has empty facets; rebuilding from them would collapse the dropdowns to "All" and make
    // it impossible to pick a filter until some later search succeeds. Keep the previous options instead.
    if (facets.categories && facets.categories.length && elements.searchCategory) {
        const current = elements.searchCategory.value;
        const total = facets.categories.reduce((sum, c) => sum + c.count, 0);
        let html = `<option value="">All File Sets (${formatNumber(total)})</option>`;
        html += facets.categories.map(c =>
            `<option value="${escapeHtml(c.category)}"${c.category === current ? ' selected' : ''}>${escapeHtml(c.category)} (${formatNumber(c.count)})</option>`).join('');
        if (current && !facets.categories.some(c => c.category === current)) {
            html += `<option value="${escapeHtml(current)}" selected>${escapeHtml(current)} (0)</option>`;
        }
        elements.searchCategory.innerHTML = html;
    }

    if (facets.subcategories && elements.searchSubcategory) {
        const current = elements.searchSubcategory.value;
        const total = facets.subcategories.reduce((sum, s) => sum + s.count, 0);
        let html = `<option value="">All Sections (${formatNumber(total)})</option>`;
        html += facets.subcategories.map(s =>
            `<option value="${escapeHtml(s.subcategory)}"${s.subcategory === current ? ' selected' : ''}>${escapeHtml(s.subcategory)} (${formatNumber(s.count)})</option>`).join('');
        if (current && !facets.subcategories.some(s => s.subcategory === current)) {
            html += `<option value="${escapeHtml(current)}" selected>${escapeHtml(current)} (0)</option>`;
        }
        elements.searchSubcategory.innerHTML = html;
        setHidden(elements.searchSubcategoryGroup, facets.subcategories.length === 0 && !current);
    }

    if (facets.file_types && facets.file_types.length && elements.searchFileType) {
        const current = elements.searchFileType.value;
        const total = facets.file_types.reduce((sum, f) => sum + f.count, 0);
        let html = `<option value="">All Files (${formatNumber(total)})</option>`;
        html += facets.file_types.map(f => {
            const label = { pdf: '📄 PDF Documents', document: '📄 Scanned Documents', audio: '🎵 Audio', image: '🖼️ Images', video: '🎬 Video' }[f.file_type] || escapeHtml(f.file_type);
            return `<option value="${escapeHtml(f.file_type)}"${f.file_type === current ? ' selected' : ''}>${label} (${formatNumber(f.count)})</option>`;
        }).join('');
        if (current && !facets.file_types.some(f => f.file_type === current)) {
            html += `<option value="${escapeHtml(current)}" selected>${escapeHtml(FILE_TYPE_LABELS[current] || current)} (0)</option>`;
        }
        elements.searchFileType.innerHTML = html;
    }
}

function renderSearchPageNumbers(totalPages) {
    if (!elements.searchPageNumbers) return;
    const currentPage = state.searchPage;
    const maxVisible = 7;
    let pages = [];
    if (totalPages <= maxVisible) {
        for (let i = 0; i < totalPages; i++) pages.push(i);
    } else {
        pages.push(0);
        let start = Math.max(1, currentPage - 2);
        let end = Math.min(totalPages - 2, currentPage + 2);
        if (currentPage < 3) end = Math.min(totalPages - 2, 4);
        if (currentPage > totalPages - 4) start = Math.max(1, totalPages - 5);
        if (start > 1) pages.push('...');
        for (let i = start; i <= end; i++) pages.push(i);
        if (end < totalPages - 2) pages.push('...');
        pages.push(totalPages - 1);
    }

    elements.searchPageNumbers.innerHTML = pages.map(page => {
        if (page === '...') return '<span class="page-ellipsis" aria-hidden="true">…</span>';
        const isActive = page === currentPage;
        return `<button type="button" class="page-num ${isActive ? 'active' : ''}" data-page="${page}" aria-label="Page ${page + 1}"${isActive ? ' aria-current="page"' : ''}>${page + 1}</button>`;
    }).join('');

    elements.searchPageNumbers.querySelectorAll('.page-num').forEach(btn => {
        btn.addEventListener('click', () => {
            const page = parseInt(btn.dataset.page, 10);
            if (page !== state.searchPage) {
                state.searchPage = page;
                runSearch({ history: 'push', focusHeading: true });
            }
        });
    });
}

function goToSearchPage() {
    const val = parseInt(elements.searchPageInput?.value, 10);
    const totalPages = Math.max(1, Math.ceil(state.searchTotal / state.searchLimit));
    if (isNaN(val)) return;
    const clamped = Math.max(1, Math.min(val, totalPages));
    if (clamped - 1 === state.searchPage) return;
    state.searchPage = clamped - 1;
    runSearch({ history: 'push', focusHeading: true });
}

function goToBrowsePage() {
    const val = parseInt(elements.browsePageInput?.value, 10);
    const totalPages = Math.max(1, Math.ceil(state.browseTotal / state.browseLimit));
    if (isNaN(val)) return;
    const clamped = Math.max(1, Math.min(val, totalPages));
    if (clamped - 1 === state.browsePage) return;
    state.browsePage = clamped - 1;
    loadDocuments();
}

async function loadDocuments({ history: mode = 'push', focusHeading = false } = {}) {
    const seq = ++state.browseSeq;
    syncUrl(mode);
    updateBrowseClearButton();
    updateDocumentTitle();

    const offset = state.browsePage * state.browseLimit;
    const noFilters = !state.browseCategory && !state.browseSubcategory && !state.browseFileType &&
        !state.browseFilename && !state.browseKeyword;
    // Exclusions are applied by the server; picking a specific file set overrides them.
    const excluded = state.browseCategory ? [] : state.excludedCategories;

    // Fast path: the first browse page prefetched by /api/bootstrap, only valid when nothing narrows it.
    if (state._prefetchedBrowse && offset === 0 && noFilters && !excluded.length) {
        const data = state._prefetchedBrowse;
        state._prefetchedBrowse = null;
        renderDocuments(data);
        state.renderedKey = currentBaseKey();
        elements.documentsGrid.setAttribute('aria-busy', 'false');   // an earlier slower load may have left these set
        elements.documentsGrid.classList.remove('is-loading');
        announce(`${formatNumber(data.total)} documents, page 1`);
        if (focusHeading) focusBrowseHeading();
        return;
    }

    state.renderedKey = null;            // nothing is rendered for this URL until the response lands
    const grid = elements.documentsGrid;
    grid.setAttribute('aria-busy', 'true');
    if (!grid.querySelector('[data-doc-card]')) {
        grid.innerHTML = Array.from({ length: 12 }, () => '<div class="skeleton document-skeleton" aria-hidden="true"></div>').join('');
    } else {
        grid.classList.add('is-loading');
    }

    try {
        const params = new URLSearchParams({ limit: state.browseLimit, offset });
        if (state.browseCategory) params.append('category', state.browseCategory);
        if (state.browseSubcategory) params.append('subcategory', state.browseSubcategory);
        if (state.browseFileType) params.append('file_type', state.browseFileType);
        if (state.browseFilename) params.append('filename', state.browseFilename);
        if (state.browseKeyword) params.append('keyword', state.browseKeyword);
        excluded.forEach(c => params.append('exclude_category', c));

        const response = await fetch(`${API_BASE}/documents?${params}`);
        if (!response.ok) throw new Error('Failed to load documents');
        const data = await response.json();
        if (seq !== state.browseSeq) return;
        renderDocuments(data);
        state.renderedKey = currentBaseKey();
        announce(`${formatNumber(data.total)} documents, page ${state.browsePage + 1}`);
        if (focusHeading) focusBrowseHeading();
    } catch (error) {
        if (seq !== state.browseSeq) return;
        console.error('Error loading documents:', error);
        renderBrowseError();
        if (focusHeading) focusBrowseHeading();
    } finally {
        if (seq === state.browseSeq) {
            grid.setAttribute('aria-busy', 'false');
            grid.classList.remove('is-loading');
        }
    }
}

function renderDocuments(data) {
    const docs = data.documents || [];
    const excludedApplied = !state.browseCategory && state.excludedCategories.length;
    let countText = `${formatNumber(data.total)} documents`;
    if (excludedApplied) {
        const n = state.excludedCategories.length;
        countText += ` (excluding ${n} file set${n > 1 ? 's' : ''})`;
    }
    elements.browseCount.textContent = countText;

    state.browseTotal = data.total;
    const totalPages = Math.ceil(data.total / state.browseLimit);
    elements.pageInfo.textContent = `Page ${state.browsePage + 1} of ${Math.max(1, totalPages)}`;
    if (elements.browsePageInput) {
        elements.browsePageInput.max = Math.max(1, totalPages);
        elements.browsePageInput.value = state.browsePage + 1;
    }
    elements.prevPage.disabled = state.browsePage === 0;
    elements.nextPage.disabled = !((state.browsePage + 1) * state.browseLimit < data.total) || totalPages <= 1;

    if (!docs.length) {
        state.documentList = [];
        if (data.total > 0 && state.browsePage > 0) {
            elements.documentsGrid.innerHTML = `
                <div class="state-panel state-empty">
                    <h3>There are no documents on this page</h3>
                    <p>These filters match ${formatNumber(data.total)} documents, but not this many pages.</p>
                </div>`;
        } else {
            elements.documentsGrid.innerHTML = `
                <div class="state-panel state-empty">
                    <h3>No documents match these filters</h3>
                    <p>Try a different file set or file type, or a shorter filename.</p>
                    ${browseHasFilters() ? '<div class="state-actions"><button type="button" class="state-btn primary" data-action="clear-browse-filters">Clear filters</button></div>' : ''}
                </div>`;
        }
        return;
    }

    state.documentList = docs.map(d => ({ id: d.id, filename: d.filename }));
    elements.documentsGrid.innerHTML = docs.map(documentCardHtml).join('');
}

async function openDocument(docId, index = -1, opts = {}) {
    const { history: mode = 'push', trigger = null } = opts;
    if (!docId) return;
    const seq = ++state.docSeq;
    const wasOpen = isDocumentOpen();

    if (mode === 'push') {
        if (!wasOpen) {
            syncUrl('push', { doc: docId, overlay: true });
            state.modalPushed = true;
        } else {
            syncUrl('replace', { doc: docId, overlay: state.modalPushed });
        }
    }
    state.openDocId = docId;
    showModalLoading({ trigger });

    try {
        // Metadata only for a faster open; the full text loads when the Text Content tab is first used.
        const response = await fetch(`${API_BASE}/documents/${encodeURIComponent(docId)}?include_text=false`);
        if (seq !== state.docSeq) return;
        if (!response.ok) {
            const err = new Error('Document not found');
            err.status = response.status;
            throw err;
        }
        const doc = await response.json();
        if (seq !== state.docSeq) return;
        doc._fullTextLoaded = !!(doc.full_text);
        state.currentDocument = doc;

        state.documentIndex = index >= 0 ? index : state.documentList.findIndex(d => d.id === docId);
        updateDocumentNavigation();

        const fileType = doc.file_type || 'pdf';
        const fileIcon = fileType === 'audio' ? '🎵' : fileType === 'video' ? '🎬' : (fileType === 'image') ? '🖼️' : '📄';

        elements.modalTitle.textContent = doc.filename;
        elements.modalMeta.innerHTML = `
            <span><span aria-hidden="true">📁</span> ${escapeHtml(doc.category)}</span>
            ${doc.subcategory ? `<span><span aria-hidden="true">📂</span> ${escapeHtml(doc.subcategory)}</span>` : ''}
            <span><span aria-hidden="true">${fileIcon}</span> ${escapeHtml(fileType.toUpperCase())}</span>
            ${doc.page_count ? `<span><span aria-hidden="true">📄</span> ${escapeHtml(doc.page_count)} pages</span>` : ''}
            <span><span aria-hidden="true">📝</span> ${formatNumber(doc.char_count || 0)} characters</span>
        `;

        // Flag documents DOJ re-issued/redacted after release (non-blocking). Clear the previous document's notice first.
        elements.modalAlteration.innerHTML = '';
        setHidden(elements.modalAlteration, true);
        loadDocumentAlterationBadge(docId, seq);

        elements.modalText.textContent = '';
        elements.modalSummary.innerHTML = '<p class="loading">Open this tab to generate an AI summary.</p>';

        const dojLink = document.getElementById('doj-original-link');
        if (dojLink) {
            const dojMatch = doc.category === 'DOJ Disclosures' && doc.subcategory
                ? doc.subcategory.match(/^Data Set (\d+)$/)
                : null;
            if (dojMatch) {
                dojLink.href = `https://www.justice.gov/epstein/files/DataSet%20${dojMatch[1]}/${encodeURIComponent(doc.filename)}`;
                setHidden(dojLink, false);
            } else {
                setHidden(dojLink, true);
            }
        }

        const fileUrl = `${API_BASE}/documents/${encodeURIComponent(docId)}/file`;
        const mediaViewer = document.getElementById('media-viewer');
        const hideIframe = () => { loadPdfFrame(''); setHidden(elements.pdfIframe, true); };

        if (fileType === 'pdf') {
            if (isIOS()) {
                // iOS Safari has issues with PDF scrolling in iframes: offer a direct link instead.
                hideIframe();
                setHidden(elements.pdfFallback, true);
                if (mediaViewer) {
                    setHidden(mediaViewer, false);
                    mediaViewer.innerHTML = `
                        <div class="ios-pdf-fallback">
                            <div class="pdf-icon" aria-hidden="true">📄</div>
                            <h3>${escapeHtml(doc.filename)}</h3>
                            <p class="pdf-info">${doc.page_count || ''} ${doc.page_count ? 'pages' : ''}</p>
                            <p class="ios-pdf-message">For the best experience, open the PDF directly with the link below.</p>
                            <a href="${fileUrl}" target="_blank" rel="noopener noreferrer" class="ios-pdf-open-btn">
                                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                                    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                                    <polyline points="15 3 21 3 21 9"></polyline>
                                    <line x1="10" y1="14" x2="21" y2="3"></line>
                                </svg>
                                Open PDF
                            </a>
                            <p class="ios-pdf-hint">You can also view the extracted text in the "Text Content" tab</p>
                        </div>
                    `;
                }
            } else {
                loadPdfFrame(`${fileUrl}#toolbar=0&navpanes=0&view=FitH`);
                setHidden(elements.pdfIframe, false);
                setHidden(elements.pdfFallback, true);
                if (mediaViewer) { setHidden(mediaViewer, true); mediaViewer.innerHTML = ''; }
            }
        } else if (fileType === 'audio') {
            hideIframe();
            setHidden(elements.pdfFallback, true);
            if (mediaViewer) {
                setHidden(mediaViewer, false);
                mediaViewer.innerHTML = `
                    <div class="media-player-container audio-player">
                        <div class="media-icon" aria-hidden="true">🎵</div>
                        <h3>Audio Recording</h3>
                        <div class="media-notice">
                            <span class="notice-icon" aria-hidden="true">⏳</span>
                            <span>Large files may take time to buffer. Please click play only once and allow time for loading.</span>
                        </div>
                        <audio controls controlsList="nodownload noplaybackrate" preload="metadata" class="audio-element" aria-label="Audio recording: ${escapeHtml(doc.filename)}">
                            <source src="${fileUrl}" type="audio/mpeg">
                            <source src="${fileUrl}" type="audio/wav">
                            Your browser does not support the audio element.
                        </audio>
                        <p class="media-hint">See "Text Content" tab for the full transcription</p>
                    </div>
                `;
            }
        } else if (fileType === 'video') {
            hideIframe();
            setHidden(elements.pdfFallback, true);
            if (mediaViewer) {
                setHidden(mediaViewer, false);
                mediaViewer.innerHTML = `
                    <div class="media-player-container video-player">
                        <div class="media-notice">
                            <span class="notice-icon" aria-hidden="true">⏳</span>
                            <span>Large files may take time to buffer. Please click play only once and allow time for loading.</span>
                        </div>
                        <video controls controlsList="nodownload" preload="metadata" class="video-element" aria-label="Video recording: ${escapeHtml(doc.filename)}">
                            <source src="${fileUrl}" type="video/mp4">
                            <source src="${fileUrl}" type="video/webm">
                            Your browser does not support the video element.
                        </video>
                        <p class="media-hint">See "Text Content" tab for the full transcription</p>
                    </div>
                `;
            }
        } else if (fileType === 'image' || fileType === 'document') {
            hideIframe();
            setHidden(elements.pdfFallback, true);
            const isTiff = doc.filename && /\.(tif|tiff)$/i.test(doc.filename);
            if (mediaViewer) {
                setHidden(mediaViewer, false);
                if (isTiff) {
                    mediaViewer.innerHTML = `
                        <div class="media-player-container image-viewer tiff-fallback">
                            <div class="media-icon" aria-hidden="true">🖼️</div>
                            <h3>TIFF Image</h3>
                            <p class="tiff-notice">TIFF files cannot be displayed directly in the browser.</p>
                            <a href="${fileUrl}" download="${escapeHtml(doc.filename)}" class="tiff-download-btn">
                                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                                    <polyline points="7 10 12 15 17 10"></polyline>
                                    <line x1="12" y1="15" x2="12" y2="3"></line>
                                </svg>
                                Download TIFF Image
                            </a>
                            <p class="media-hint">See "Text Content" tab for OCR-extracted text</p>
                        </div>
                    `;
                } else {
                    mediaViewer.innerHTML = `
                        <div class="media-player-container image-viewer">
                            <img src="${fileUrl}" alt="${escapeHtml(doc.filename)}" class="image-preview" />
                            <p class="media-hint">See "Text Content" tab for OCR-extracted text</p>
                        </div>
                    `;
                }
            }
        } else {
            hideIframe();
            if (mediaViewer) setHidden(mediaViewer, true);
            setHidden(elements.pdfFallback, false);
            elements.pdfFallback.innerHTML = `
                <p aria-hidden="true">${fileIcon}</p>
                <p>File Preview Not Available</p>
                <p class="pdf-fallback-hint">View the text content tab to see the extracted content.</p>
            `;
        }

        switchModalTab('document');

        elements.modalContent.classList.remove('is-loading', 'is-error', 'is-refreshing');
        elements.modalContent.removeAttribute('aria-busy');
        elements.modalState.textContent = '';
        // Whatever had focus (e.g. a retry button) may be gone: keep keyboard users inside the dialog.
        if (!elements.modalContent.contains(document.activeElement)) elements.modalContent.focus({ preventScroll: true });
        updateDocumentTitle();
        announce(`Opened ${doc.filename}`);
        try { onDocumentModalOpened(); } catch (e) { /* monetization is best-effort */ }
    } catch (error) {
        if (seq !== state.docSeq) return;
        console.error('Error loading document:', error);
        showModalError(docId, error);
    }
}


function updateDocumentNavigation() {
    if (!elements.docNavigation) return;
    const hasMultipleDocs = state.documentList.length > 1;
    const currentIndex = state.documentIndex;
    if (hasMultipleDocs && currentIndex >= 0) {
        elements.docNavigation.classList.remove('hidden');
        elements.docNavInfo.textContent = `${currentIndex + 1} of ${state.documentList.length}`;
        elements.docPrevBtn.disabled = currentIndex <= 0;
        elements.docNextBtn.disabled = currentIndex >= state.documentList.length - 1;
        // A focused button that just became disabled (end of the list) loses focus at the next rendering update
        // (the browser's "focus fixup"), so hand focus to the other direction now.
        const active = document.activeElement;
        const lost = isDocumentOpen() && (!elements.modalContent.contains(active)
            || ((active === elements.docNextBtn || active === elements.docPrevBtn) && active.disabled));
        if (lost) {
            const target = !elements.docNextBtn.disabled ? elements.docNextBtn
                : !elements.docPrevBtn.disabled ? elements.docPrevBtn : elements.modalContent;
            target.focus({ preventScroll: true });
        }
    } else {
        elements.docNavigation.classList.add('hidden');
    }
}

async function navigateDocument(direction) {
    if (state.documentList.length === 0 || state.documentIndex < 0) return;
    
    const newIndex = state.documentIndex + direction;
    
    // Bounds check
    if (newIndex < 0 || newIndex >= state.documentList.length) return;
    
    const nextDoc = state.documentList[newIndex];
    if (nextDoc && nextDoc.id) {
        await openDocument(nextDoc.id, newIndex);
    }
}

function togglePdfFullscreen(pdfViewer) {
    const isFullscreen = pdfViewer.classList.toggle('fullscreen');
    const btn = document.getElementById('pdf-fullscreen-btn');
    if (btn) btn.setAttribute('aria-pressed', isFullscreen ? 'true' : 'false');
}

async function switchModalTab(tabName) {
    const tablist = elements.modal.querySelector('[role="tablist"]');
    const selected = [...elements.modalTabs].find(t => t.dataset.tab === tabName);
    syncTabs(tablist, selected);
    elements.modal.querySelectorAll('.tab-content').forEach(content => {
        content.classList.toggle('active', content.id === `modal-${tabName}-tab`);
    });

    // Full text is loaded on demand when the Text Content tab is first opened.
    if (tabName === 'content' && state.currentDocument && !state.currentDocument._fullTextLoaded) {
        await loadDocumentFullText(state.currentDocument.id);
    }
    if (tabName === 'summary' && state.currentDocument) {
        const summaryEl = elements.modalSummary;
        if (summaryEl.querySelector('.loading')) {
            await loadDocumentSummary(state.currentDocument.id);
        }
    }
}

async function loadDocumentFullText(docId) {
    if (!elements.modalText) return;
    elements.modalText.textContent = 'Loading text...';
    try {
        const response = await fetch(`${API_BASE}/documents/${docId}/text`);
        if (!response.ok) throw new Error('Failed to load text');
        const data = await response.json();
        if (state.currentDocument && state.currentDocument.id === docId) {
            state.currentDocument.full_text = data.full_text;
            state.currentDocument._fullTextLoaded = true;
            elements.modalText.textContent = data.full_text || 'No text content available.';
        }
    } catch (e) {
        console.error('Error loading document text:', e);
        elements.modalText.textContent = 'Failed to load text content.';
    }
}

async function loadDocumentSummary(docId) {
    // A slow summary for document A must never land in document B's pane (openDocId changes immediately).
    const stale = () => state.openDocId !== docId;
    try {
        elements.modalSummary.innerHTML = '<p class="loading">Generating AI summary...</p>';

        const response = await fetch(`${API_BASE}/documents/${encodeURIComponent(docId)}/summary`);
        if (stale()) return;

        if (!response.ok) {
            if (response.status === 503) {
                elements.modalSummary.innerHTML = '<p class="error">AI summarization not available. Set OPENAI_API_KEY to enable this feature.</p>';
            } else {
                throw new Error('Failed to generate summary');
            }
            return;
        }

        const data = await response.json();
        if (stale()) return;

        // Show cached indicator if summary was retrieved from cache
        const cacheIndicator = data.cached
            ? `<div class="summary-meta">
                <span class="cache-badge cached">📦 Cached Summary</span>
                ${data.generated_at ? `<span class="generated-date">Generated: ${new Date(data.generated_at).toLocaleDateString()}</span>` : ''}
               </div>`
            : `<div class="summary-meta">
                <span class="cache-badge fresh">✨ Freshly Generated</span>
               </div>`;

        elements.modalSummary.innerHTML = `
            ${cacheIndicator}
            <div class="summary-text">${renderMarkdown(data.summary)}</div>
        `;
    } catch (error) {
        if (stale()) return;
        console.error('Error loading summary:', error);
        elements.modalSummary.innerHTML = '<p class="error">Failed to generate summary.</p>';
    }
}

async function askQuestion() {
    const question = elements.askInput.value.trim();
    if (!question) {
        announce('Type a question first.');
        elements.askInput.focus();
        return;
    }

    elements.askBtn.disabled = true;
    elements.askBtn.innerHTML = '<span class="loading-spinner" aria-hidden="true"></span> Thinking...';
    setHidden(elements.askResponse, false);
    elements.askResponse.setAttribute('aria-busy', 'true');
    elements.answerText.textContent = 'Analyzing documents...';
    elements.sourcesList.innerHTML = '';

    try {
        const response = await fetch(`${API_BASE}/ask`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question, num_context_docs: 5 })
        });

        if (!response.ok) {
            if (response.status === 503) {
                elements.answerText.textContent = 'AI assistant not available. Please set OPENAI_API_KEY to enable this feature.';
                return;
            }
            throw new Error('Failed to get answer');
        }

        const data = await response.json();
        elements.answerText.innerHTML = renderMarkdown(data.answer);

        if (data.sources && data.sources.length > 0) {
            elements.sourcesList.innerHTML = data.sources.map(source => {
                const id = String(source.id);
                return `<li><a href="${escapeHtml(buildUrl({ doc: id }))}" class="source-link" data-action="open-doc" data-doc-id="${escapeHtml(id)}"><span aria-hidden="true">📄</span> ${escapeHtml(source.filename)} (${escapeHtml(source.category)}) - ${formatRelevanceScore(source.score, 'semantic')}</a></li>`;
            }).join('');
        }
    } catch (error) {
        console.error('Ask error:', error);
        elements.answerText.textContent = 'Failed to get answer. Please try again.';
    } finally {
        elements.askBtn.disabled = false;
        elements.askBtn.innerHTML = ASK_BTN_HTML;
        elements.askResponse.setAttribute('aria-busy', 'false');
    }
}

// Utility functions
function formatNumber(num) {
    if (num === null || num === undefined) return '0';
    return num.toLocaleString();
}

function formatDocumentDate(dateStr) {
    if (!dateStr) return '';
    try {
        const date = new Date(dateStr + 'T00:00:00'); // Add time to avoid timezone issues
        return date.toLocaleDateString('en-US', { 
            year: 'numeric', 
            month: 'short', 
            day: 'numeric' 
        });
    } catch (e) {
        return '';
    }
}

function formatRelevanceScore(score, searchType) {
    if (!score) return '';
    
    // Semantic search scores are cosine similarity (0-1 range)
    if (searchType === 'semantic') {
        const percent = Math.min(Math.round(score * 100), 100);
        return `${percent}% match`;
    }
    
    // Full-text BM25 scores are not percentages - convert to stars or descriptive
    // BM25 scores typically range from 0 to ~15+ depending on query
    if (score > 10) {
        return '★★★ Excellent match';
    } else if (score > 5) {
        return '★★ Strong match';
    } else if (score > 2) {
        return '★ Good match';
    } else {
        return 'Partial match';
    }
}

function getDocumentIcon(fileType) {
    if (fileType === 'audio') {
        return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M9 18V5l12-2v13M9 18a3 3 0 11-6 0 3 3 0 016 0zm12-2a3 3 0 11-6 0 3 3 0 016 0z"/>
        </svg>`;
    } else if (fileType === 'video') {
        return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/>
        </svg>`;
    } else if (fileType === 'image') {
        return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/>
        </svg>`;
    }
    // Default: PDF/document icon
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
    </svg>`;
}


function getDocumentMeta(doc) {
    if (doc.file_type === 'audio' || doc.file_type === 'video') {
        if (doc.duration_seconds) {
            return formatDuration(doc.duration_seconds);
        }
        return doc.file_type === 'audio' ? '🎵 Audio' : '🎬 Video';
    }
    if (doc.file_type === 'image') {
        return '🖼️ Image';
    }
    if (doc.file_type === 'document') {
        return '📄 Scanned Doc';
    }
    return `${doc.page_count || 0} pages`;
}

/**
 * Get the appropriate tile label based on document category
 * - Court Records: show the court case (subcategory)
 * - DOJ Disclosures: show the dataset (subcategory)
 * - FOIA: show the subcategory (e.g., Florida)
 * - Others: show the main category
 */
function getDocumentTileLabel(doc) {
    const category = doc.category || '';
    const subcategory = doc.subcategory || '';
    
    // For Court Records, DOJ Disclosures, and FOIA, show subcategory if available
    if (category === 'Court Records' && subcategory) {
        return escapeHtml(subcategory);
    }
    if (category === 'DOJ Disclosures' && subcategory) {
        return escapeHtml(subcategory);
    }
    if (category === 'FOIA' && subcategory) {
        return escapeHtml(subcategory);
    }
    
    // Default: show the main category
    return escapeHtml(category);
}

function getSearchResultMeta(result) {
    if (result.file_type === 'audio' || result.file_type === 'video') {
        if (result.duration_seconds) {
            return formatDuration(result.duration_seconds);
        }
        return result.file_type === 'audio' ? '🎵 Audio' : '🎬 Video';
    }
    if (result.file_type === 'image') {
        return '🖼️ Image';
    }
    if (result.file_type === 'document') {
        return '📄 Scanned Doc';
    }
    return result.page_count ? `${result.page_count} pages` : '';
}

function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return 'Unknown duration';
    
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    
    if (hrs > 0) {
        return `${hrs}h ${mins}m ${secs}s`;
    } else if (mins > 0) {
        return `${mins}m ${secs}s`;
    }
    return `${secs}s`;
}

/**
 * Detect if user is on a mobile device
 */
function isMobileDevice() {
    return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
}

/**
 * Detect if user is on Android
 */
function isAndroid() {
    return /Android/i.test(navigator.userAgent);
}

/** iPadOS 13+ reports itself as a Mac, so detect it by touch support as well. */
function isIOS() {
    return /iPhone|iPad|iPod/i.test(navigator.userAgent) ||
        (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

/**
 * Detect if user is on desktop (not mobile and large viewport)
 */
function isDesktop() {
    return window.innerWidth >= 1024 && !isMobileDevice();
}

/**
 * Try to open native app share dialog, fall back to web URL if app not installed
 */
function openNativeAppOrFallback(appUrl, webUrl, intentUrl) {
    // For Android, try intent URL first (more reliable for share dialogs)
    const urlToTry = isAndroid() && intentUrl ? intentUrl : appUrl;
    
    let didNavigate = false;
    
    // Listen for visibility change (app opened successfully)
    const handleVisibility = () => {
        if (document.hidden) {
            didNavigate = true;
        }
    };
    document.addEventListener('visibilitychange', handleVisibility);
    
    // Use a hidden anchor click instead of window.location.href
    // This triggers Universal Links / App Links more reliably
    const a = document.createElement('a');
    a.href = urlToTry;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    
    // After a short delay, check if we're still here and fall back to web
    setTimeout(() => {
        document.removeEventListener('visibilitychange', handleVisibility);
        
        // If the page is still visible and we haven't navigated away,
        // the app probably isn't installed, so open the web version
        if (!didNavigate && !document.hidden) {
            window.open(webUrl, '_blank', 'width=600,height=400,menubar=no,toolbar=no');
        }
    }, 1500);
}

/** Share the open document. */
function handleShare(platform, el) {
    if (!state.currentDocument) return;
    const doc = state.currentDocument;
    const shareUrl = `${SITE_URL}/?doc=${encodeURIComponent(doc.id)}`;
    const context = [doc.category, doc.subcategory].filter(Boolean).join(' - ');
    const shareText = `Check out this document from the Epstein Files Public Archive: "${doc.filename}"${context ? ` (${context})` : ''}`;
    shareTo(platform, {
        shareUrl, shareText,
        emailSubject: `Epstein Files: ${doc.filename}`,
        emailBody: `${shareText}\n\nView the document here: ${shareUrl}`
    }, el);
}

/** Share the current search, including its filters and page (the URL carries all of it). */
function handleSearchShare(platform, el) {
    if (!state.lastSearchParams || !state.lastSearchParams.query) return;
    const query = state.lastSearchParams.query;
    const shareUrl = `${SITE_URL}${buildUrl()}`;
    const shareText = `I found ${state.searchTotal || 0} results for "${query}" on the Epstein Files Public Archive`;
    shareTo(platform, {
        shareUrl, shareText,
        linkedinWeb: `https://www.linkedin.com/shareArticle?mini=true&url=${encodeURIComponent(shareUrl)}&title=${encodeURIComponent('Epstein Files: Search Results')}&summary=${encodeURIComponent(shareText)}&source=${encodeURIComponent('Epstein Files Public Archive')}`,
        emailSubject: `Epstein Files: Search results for "${query}"`,
        emailBody: `${shareText}\n\nView the results here: ${shareUrl}`
    }, el);
}

/**
 * Copy text to clipboard with iOS fallback
 * iOS Safari doesn't support navigator.clipboard in all contexts
 */
async function copyToClipboard(text, btnEl) {
    let ok = false;
    // Try the modern Clipboard API first
    if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
            await navigator.clipboard.writeText(text);
            ok = true;
        } catch (err) {
            console.log('Clipboard API failed, trying fallback:', err);
        }
    }
    
    // Fallback to execCommand only if the async Clipboard API didn't succeed
    if (!ok) try {
        // Create a temporary textarea
        const textarea = document.createElement('textarea');
        textarea.value = text;
        
        // Make it invisible but still selectable
        textarea.style.position = 'fixed';
        textarea.style.left = '-9999px';
        textarea.style.top = '0';
        textarea.style.opacity = '0';
        textarea.setAttribute('readonly', ''); // Prevent keyboard on iOS
        
        document.body.appendChild(textarea);
        
        // Handle iOS specifically
        const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
        
        if (isIOS) {
            // iOS requires special handling
            const range = document.createRange();
            range.selectNodeContents(textarea);
            
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            textarea.setSelectionRange(0, text.length); // For iOS
        } else {
            textarea.select();
        }
        
        // Execute copy command
        const success = document.execCommand('copy');
        
        document.body.removeChild(textarea);
        
        if (success) {
            ok = true;
        } else {
            throw new Error('execCommand copy failed');
        }
    } catch (err) {
        console.error('Fallback copy failed:', err);
        // Last resort: show prompt with the URL
        prompt('Copy this link:', text);
        ok = false;
    }

    // Optional inline button feedback when a button element is passed
    if (btnEl) {
        const original = btnEl.textContent;
        btnEl.textContent = ok ? 'Copied!' : 'Press Ctrl+C';
        if (ok) btnEl.classList.add('copied');
        setTimeout(() => {
            btnEl.textContent = original;
            btnEl.classList.remove('copied');
        }, 1500);
    }

    return ok;
}

/**
 * Simple markdown to HTML converter for AI summaries
 * Handles: bold, italic, headers, lists, code, blockquotes, line breaks
 */
function renderMarkdown(text) {
    if (!text) return '';
    
    // Escape HTML first to prevent XSS
    let html = escapeHtml(text);
    
    // Headers (must come before other processing)
    html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
    
    // Bold and italic (handle both ** and __ for bold, * and _ for italic)
    html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/__(.+?)__/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    html = html.replace(/_(.+?)_/g, '<em>$1</em>');
    
    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    
    // Horizontal rules
    html = html.replace(/^---$/gm, '<hr>');
    html = html.replace(/^\*\*\*$/gm, '<hr>');
    
    // Blockquotes
    html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
    
    // Unordered lists - process multiple lines
    html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
    html = html.replace(/^• (.+)$/gm, '<li>$1</li>');
    
    // Wrap consecutive <li> items in <ul>
    html = html.replace(/(<li>[\s\S]*?<\/li>)(\n<li>[\s\S]*?<\/li>)*/g, (match) => {
        return '<ul>' + match + '</ul>';
    });
    
    // Numbered lists
    html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');
    
    // Process paragraphs - split by double newlines
    const paragraphs = html.split(/\n\n+/);
    html = paragraphs.map(p => {
        p = p.trim();
        // Don't wrap if already has block element
        if (p.startsWith('<h') || p.startsWith('<ul') || p.startsWith('<ol') || 
            p.startsWith('<blockquote') || p.startsWith('<hr') || p.startsWith('<li')) {
            return p;
        }
        // Replace single newlines with <br> within paragraphs
        p = p.replace(/\n/g, '<br>');
        return p ? `<p>${p}</p>` : '';
    }).join('\n');
    
    // Clean up any orphaned list items by wrapping in ul
    html = html.replace(/<\/ul>\s*<ul>/g, '');
    
    return html;
}

/**
 * Keep only <mark> from a search snippet. The snippet is raw document text, so it is parsed with DOMParser
 * (an inert document: nothing is fetched or executed) and every node is re-emitted as escaped text.
 */
function sanitizeSnippet(html) {
    if (!html) return '';
    const doc = new DOMParser().parseFromString(String(html), 'text/html');
    let out = '';
    for (const node of doc.body.childNodes) {
        if (node.nodeType === Node.ELEMENT_NODE && node.tagName === 'MARK') out += `<mark>${escapeHtml(node.textContent)}</mark>`;
        else out += escapeHtml(node.textContent);
    }
    return out;
}

// =============================================================================
// CSV Export Functions
// =============================================================================

/**
 * Export search results to CSV
 * Exports all matching documents (not just current page) with DOJ links
 */
let pendingExportType = null;

function openExportModal(type) {
    if (type === 'search' && !state.lastSearchParams) {
        showToast('Run a search first, then export its results.');
        return;
    }
    pendingExportType = type;
    const checkbox = document.getElementById('export-include-text');
    if (checkbox) checkbox.checked = false;
    openDialog(document.getElementById('export-modal'), {
        trigger: document.activeElement, onRequestClose: closeExportModal, initialFocus: '#export-include-text'
    });
}

function closeExportModal() {
    closeDialog(document.getElementById('export-modal'));
    pendingExportType = null;
}

async function confirmExport() {
    const includeText = !!document.getElementById('export-include-text')?.checked;
    return performExport(pendingExportType, includeText);
}

/** Returns true when a file was produced. */
function downloadCSV(documents, filename) {
    if (!documents || documents.length === 0) {
        showToast('There are no documents to export for this search.');
        return false;
    }

    const hasText = documents[0] && 'full_text' in documents[0];
    let header = 'Filename,Category,Subcategory,File Type,Page Count,Character Count,Document Date,DOJ URL';
    if (hasText) header += ',Text Content';
    let csv = header + '\n';

    for (const doc of documents) {
        const row = [
            escapeCSVField(doc.filename || ''),
            escapeCSVField(doc.category || ''),
            escapeCSVField(doc.subcategory || ''),
            escapeCSVField(doc.file_type || ''),
            escapeCSVField(doc.page_count != null ? doc.page_count : ''),
            escapeCSVField(doc.char_count != null ? doc.char_count : ''),
            escapeCSVField(doc.document_date || ''),
            escapeCSVField(doc.doj_url || '')
        ];
        if (hasText) row.push(escapeCSVField((doc.full_text || '').replace(/[\r\n]+/g, ' ')));
        csv += row.join(',') + '\n';
    }

    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.classList.add('hidden');
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    return true;
}

/**
 * Escape a field for CSV format (hardened against formula injection).
 * Always double-quotes every field. Prefixes formula-triggering characters
 * with a single-quote per OWASP CSV injection mitigation.
 */
function escapeCSVField(field) {
    if (field === null || field === undefined) return '""';
    let str = String(field);
    if (/^[=+\-@\t\r]/.test(str)) {
        str = "'" + str;
    }
    return '"' + str.replace(/"/g, '""') + '"';
}

/* =============================================================================
   Monetization: donation panel, donate modal, crypto, affiliate strip, ads.
   All revenue surfaces live below this line.
   ============================================================================= */

// ---- Configuration (fill in your own links / addresses) ---------------------
// Stripe Payment Links: create at https://dashboard.stripe.com/payment-links
// Crypto.com Pay button: create at https://crypto.com/business/pay  (or paste your hosted-checkout link)
// Crypto wallet addresses: paste your receive addresses from your crypto.com app
// Ko-fi: https://ko-fi.com/<your-handle>
// GitHub Sponsors: https://github.com/sponsors/<your-handle>
// Ad network: paste your AdSense client id; leave blank to keep ad placeholders dormant.
const DONATE_CONFIG = {
    stripe: {
        // null = button disabled; replace with full Payment Link URLs to enable.
        amount5:  "https://buy.stripe.com/14AaERcSF7h17VZd8McjS00", // e.g. "https://buy.stripe.com/test_abc..."
        amount10: "https://buy.stripe.com/bJe3cpdWJdFp0txc4IcjS01",
        amount25: "https://buy.stripe.com/00wbIVdWJ6cX2BFfgUcjS02",
        amount50: "https://buy.stripe.com/14AaER8Cpatd1xBgkYcjS03",
        custom:   "https://buy.stripe.com/dRm4gt05T7h12BF6KocjS04"  // a "name your price" Payment Link
    },
    crypto: {
        // Hosted Crypto.com Pay checkout link (single button). Leave null to hide.
        cryptoComPayUrl: null,
        // Manual wallet addresses fallback. Empty array hides the section.
        wallets: [
             { coin: "BTC",  address: "3L4b2tzZbt1mwY2bHJLBTKWjMRYuFQddLj" },
             { coin: "ETH",  address: "0x62c2e5a1d1B39C6a9C9565170eEc1c8027f8F24c" },
             { coin: "USDC", address: "0x62c2e5a1d1B39C6a9C9565170eEc1c8027f8F24c" },
             { coin: "LTC", address: "M9vxo2iRjALXa8fXphRrgXeJwXwPZN48Hp" },
             { coin: "SOL",  address: "FwoPX5TJwmnN6VMbukd5vpecH3TvggEmkcWDPFiHHwjQ" }
        ]
    },
    github: {
        // Set to null to hide the GitHub Sponsors button until the program is set up.
        sponsorsUrl: "https://github.com/sponsors/l0lsec"
    },
    kofi: {
        url: "https://ko-fi.com/epsteinfta" // e.g. "https://ko-fi.com/yourhandle"
    },
    // Trigger the donate modal once after this many document opens, then snooze 30 days.
    modalTrigger: {
        docsBeforePrompt: 5,
        snoozeDays: 30
    }
};

// Ad network configuration. Until a client id is set, no ads render even when
// the admin "ads_enabled" toggle is on.
const AD_CONFIG = {
    // AdSense client id (e.g. "ca-pub-1234567890123456"). Leave null to keep
    // ad slots dormant.
    adsenseClientId: null
};

// Amazon Associates items. Replace ASIN + tag to enable.
// Leave AFFILIATE_TAG blank to keep the section hidden.
const AFFILIATE_TAG = ""; // e.g. "l0lsec-20"
const AFFILIATE_ITEMS = [
    {
        title: "Filthy Rich",
        author: "James Patterson & John Connolly",
        asin: "B07CY3HZBL",
        cover: "https://m.media-amazon.com/images/I/91MJUygH-DL._SY342_.jpg"
    },
    {
        title: "Perversion of Justice",
        author: "Julie K. Brown",
        asin: "0062950622",
        cover: "https://m.media-amazon.com/images/I/81E7Br5w8ML._SY342_.jpg"
    },
    {
        title: "Relentless Pursuit",
        author: "Bradley J. Edwards",
        asin: "1982128097",
        cover: "https://m.media-amazon.com/images/I/81rRwlOObCL._SY342_.jpg"
    },
    {
        title: "Epstein: Dead Men Tell No Tales",
        author: "Dylan Howard & Melissa Cronin",
        asin: "1510755810",
        cover: "https://m.media-amazon.com/images/I/71F0z0G7e8L._SY342_.jpg"
    }
];

function initDonatePanel(root) {
    if (!root) return;
    const tabs = root.querySelectorAll('.donate-tab');
    const panels = root.querySelectorAll('.donate-tab-panel');
    if (!tabs.length) return;

    tabs.forEach(tab => {
        if (tab.__donateBound) return;
        tab.__donateBound = true;
        tab.addEventListener('click', () => {
            syncTabs(tab.closest('[role="tablist"]'), tab);
            panels.forEach(p => p.classList.toggle('active', p.dataset.donatePanel === tab.dataset.donateTab));
        });
    });

    // Stripe amount buttons
    const grid = root.querySelector('#stripe-amount-grid');
    const fineprint = root.querySelector('#stripe-fineprint');
    if (grid) {
        grid.innerHTML = '';
        const presets = [
            { key: 'amount5', label: '$5' },
            { key: 'amount10', label: '$10' },
            { key: 'amount25', label: '$25' },
            { key: 'amount50', label: '$50' }
        ];
        let anyConfigured = false;
        presets.forEach(p => {
            const url = DONATE_CONFIG.stripe[p.key];
            if (!url) return;
            anyConfigured = true;
            const a = document.createElement('a');
            a.className = 'donate-amount-btn';
            a.href = url;
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            a.textContent = p.label;
            grid.appendChild(a);
        });
        if (DONATE_CONFIG.stripe.custom) {
            anyConfigured = true;
            const a = document.createElement('a');
            a.className = 'donate-amount-btn custom';
            a.href = DONATE_CONFIG.stripe.custom;
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            a.textContent = 'Choose amount';
            grid.appendChild(a);
        }
        setHidden(fineprint, anyConfigured);
    }

    const bindLink = (selector, url) => {
        const link = root.querySelector(selector);
        if (!link) return;
        if (url) link.href = url;
        setHidden(link, !url);
    };
    bindLink('#crypto-pay-link', DONATE_CONFIG.crypto.cryptoComPayUrl);

    // Crypto wallet addresses + copy buttons
    const addrContainer = root.querySelector('#crypto-addresses');
    if (addrContainer) {
        addrContainer.innerHTML = '';
        (DONATE_CONFIG.crypto.wallets || []).forEach(w => {
            if (!w || !w.address) return;
            const row = document.createElement('div');
            row.className = 'crypto-address';
            row.innerHTML = `
                <span class="crypto-address-coin">${escapeHtml(w.coin || '')}</span>
                <span class="crypto-address-value" title="${escapeHtml(w.address)}">${escapeHtml(w.address)}</span>
                <button type="button" class="crypto-address-copy" aria-label="Copy ${escapeHtml(w.coin || '')} address">Copy</button>
            `;
            const btn = row.querySelector('.crypto-address-copy');
            btn.addEventListener('click', () => copyToClipboard(w.address, btn));
            addrContainer.appendChild(row);
        });
    }

    bindLink('#github-sponsors-link', DONATE_CONFIG.github.sponsorsUrl);
    bindLink('#kofi-link', DONATE_CONFIG.kofi.url);
}

/**
 * Escape HTML to prevent XSS. Also escapes quotes so the output is safe inside
 * HTML attribute values, not just text content.
 */
function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ---- Donate modal trigger ---------------------------------------------------
function onDocumentModalOpened() {
    try {
        const key = 'docOpensSinceLastDonatePrompt';
        const snoozeKey = 'donatePromptSnoozedUntil';
        const now = Date.now();
        const snoozedUntil = parseInt(localStorage.getItem(snoozeKey) || '0', 10);
        if (snoozedUntil && now < snoozedUntil) return;

        const count = parseInt(localStorage.getItem(key) || '0', 10) + 1;
        localStorage.setItem(key, String(count));
        if (count >= DONATE_CONFIG.modalTrigger.docsBeforePrompt) {
            // Defer until the user has been looking at the doc for a few seconds;
            // showing it instantly feels jarring.
            setTimeout(() => {
                openDonateModal('auto');
                localStorage.setItem(key, '0');
            }, 4000);
        }
    } catch (e) { /* localStorage may be blocked */ }
}

function openDonateModal(source) {
    const modal = document.getElementById('donate-modal');
    const body = document.getElementById('donate-modal-body');
    const footerPanel = document.querySelector('#donate-section .donate-panel');
    if (!modal || !body) return;

    // Move the footer panel into the modal (preserves any state) and re-init.
    if (footerPanel && !body.contains(footerPanel)) body.appendChild(footerPanel);
    initDonatePanel(body.querySelector('.donate-panel'));
    openDialog(modal, { trigger: document.activeElement, onRequestClose: closeDonateModal });
    if (source === 'auto') {
        // Snooze auto-prompts for 30 days regardless of action.
        const ms = DONATE_CONFIG.modalTrigger.snoozeDays * 24 * 60 * 60 * 1000;
        try { localStorage.setItem('donatePromptSnoozedUntil', String(Date.now() + ms)); } catch (e) { /* storage blocked */ }
    }
}

function closeDonateModal() {
    const modal = document.getElementById('donate-modal');
    const body = document.getElementById('donate-modal-body');
    const footerSection = document.querySelector('#donate-section .donate-content');
    const panel = body ? body.querySelector('.donate-panel') : null;
    // Move the panel back to the footer so the page still has its persistent donate UI.
    if (footerSection && panel) {
        const note = footerSection.querySelector('.donate-note');
        if (note) footerSection.insertBefore(panel, note);
        else footerSection.appendChild(panel);
    }
    closeDialog(modal);
}


// ---- Banner dismiss ---------------------------------------------------------
function dismissDonationBanner() {
    const banner = document.getElementById('donation-banner');
    if (banner) banner.classList.add('dismissed');
    try {
        const ms = 7 * 24 * 60 * 60 * 1000;
        localStorage.setItem('donationBannerDismissedUntil', String(Date.now() + ms));
    } catch (e) {}
}

function maybeHideBannerFromCookie() {
    try {
        const until = parseInt(localStorage.getItem('donationBannerDismissedUntil') || '0', 10);
        if (until && Date.now() < until) {
            const banner = document.getElementById('donation-banner');
            if (banner) banner.classList.add('dismissed');
        }
    } catch (e) {}
}

function renderAffiliateStrip(enabled) {
    const section = document.getElementById('affiliate-section');
    const strip = document.getElementById('affiliate-strip');
    if (!section || !strip) return;
    if (!enabled || !AFFILIATE_TAG || !AFFILIATE_ITEMS.length) {
        setHidden(section, true);
        return;
    }
    setHidden(section, false);
    strip.innerHTML = '';
    AFFILIATE_ITEMS.forEach(item => {
        const a = document.createElement('a');
        a.className = 'affiliate-card';
        a.href = `https://www.amazon.com/dp/${encodeURIComponent(item.asin)}/?tag=${encodeURIComponent(AFFILIATE_TAG)}`;
        a.target = '_blank';
        a.rel = 'sponsored noopener noreferrer';
        a.innerHTML = `
            <img class="affiliate-card-cover" src="${escapeHtml(item.cover || '')}" alt="${escapeHtml(item.title)} cover" loading="lazy">
            <h3 class="affiliate-card-title">${escapeHtml(item.title)}</h3>
            <span class="affiliate-card-author">${escapeHtml(item.author || '')}</span>
        `;
        strip.appendChild(a);
    });
}

// ---- Ads loader -------------------------------------------------------------
// Renders ads into the slots only when an ad-network client id is configured.
let _adsLoaded = false;
function hideAllAdSlots() {
    document.querySelectorAll('.ad-slot').forEach(s => setHidden(s, true));
}
function loadAdNetwork(enabled) {
    if (!enabled || _adsLoaded) {
        if (!enabled) hideAllAdSlots();
        return;
    }

    if (AD_CONFIG.adsenseClientId) {
        const s = document.createElement('script');
        s.async = true;
        s.crossOrigin = 'anonymous';
        s.src = `https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=${encodeURIComponent(AD_CONFIG.adsenseClientId)}`;
        document.head.appendChild(s);
        _adsLoaded = true;
        return;
    }

    // No network configured yet: keep dormant placeholders hidden.
    hideAllAdSlots();
}

// ---- Top-level monetization init -------------------------------------------
function initMonetization(opts) {
    const adsEnabled = !!(opts && opts.adsEnabled);
    const affiliateEnabled = !(opts && opts.affiliateEnabled === false);

    initDonatePanel(document.querySelector('#donate-section .donate-panel'));
    maybeHideBannerFromCookie();
    renderAffiliateStrip(affiliateEnabled);
    loadAdNetwork(adsEnabled);
}
