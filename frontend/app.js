/**
 * Epstein Library Files Public Archive - Frontend Application
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
    searchSubcategory: '',
    currentDocument: null,
    // Search pagination state
    searchPage: 0,
    searchLimit: 50,  // Results per page
    searchTotal: 0,
    lastSearchParams: null,  // Store last search to enable pagination
    // Document navigation state
    documentList: [],  // Current list of documents (from search or browse)
    documentIndex: -1  // Current index within documentList
};

// DOM Elements
const elements = {};

// Initialize
document.addEventListener('DOMContentLoaded', init);

async function init() {
    cacheElements();
    setupEventListeners();
    await loadStats();
    await loadCategories();
    await loadPublicSettings();
    await loadPinnedDocuments();
    
    // Set timestamp for spam protection
    const timestampField = document.getElementById('feedback-timestamp');
    if (timestampField) {
        timestampField.value = Date.now().toString();
    }
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
    elements.searchResults = document.getElementById('search-results');
    elements.resultsList = document.getElementById('results-list');
    elements.resultsCount = document.getElementById('results-count');
    elements.searchPagination = document.getElementById('search-pagination');
    elements.searchPrevPage = document.getElementById('search-prev-page');
    elements.searchNextPage = document.getElementById('search-next-page');
    elements.searchPageNumbers = document.getElementById('search-page-numbers');
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
    
    // Ask AI
    elements.askInput = document.getElementById('ask-input');
    elements.askBtn = document.getElementById('ask-btn');
    elements.askResponse = document.getElementById('ask-response');
    elements.answerText = document.getElementById('answer-text');
    elements.sourcesList = document.getElementById('sources-list');
    elements.llmStatus = document.getElementById('llm-status');
    elements.exampleBtns = document.querySelectorAll('.example-btn');
    
    // Modal
    elements.modal = document.getElementById('document-modal');
    elements.modalBackdrop = elements.modal.querySelector('.modal-backdrop');
    elements.modalClose = elements.modal.querySelector('.modal-close');
    elements.modalTitle = document.getElementById('modal-title');
    elements.modalMeta = document.getElementById('modal-meta');
    elements.modalText = document.getElementById('modal-text');
    elements.modalSummary = document.getElementById('modal-summary');
    elements.pdfIframe = document.getElementById('pdf-iframe');
    elements.pdfFallback = document.getElementById('pdf-fallback');
    elements.modalTabs = document.querySelectorAll('.modal-tab');
    
    // Document Navigation
    elements.docNavigation = document.getElementById('document-navigation');
    elements.docPrevBtn = document.getElementById('doc-prev-btn');
    elements.docNextBtn = document.getElementById('doc-next-btn');
    elements.docNavInfo = document.getElementById('doc-nav-info');
}

function setupEventListeners() {
    // Navigation
    elements.navBtns.forEach(btn => {
        btn.addEventListener('click', () => switchView(btn.dataset.view));
    });
    
    // Search
    elements.searchBtn.addEventListener('click', performSearch);
    elements.searchInput.addEventListener('keypress', e => {
        if (e.key === 'Enter') performSearch();
    });
    
    // Clear search button
    if (elements.clearSearchBtn) {
        elements.clearSearchBtn.addEventListener('click', clearSearch);
    }
    
    // Search category change - load subcategories and re-run search
    if (elements.searchCategory) {
        elements.searchCategory.addEventListener('change', async () => {
            const category = elements.searchCategory.value;
            state.searchSubcategory = '';
            if (elements.searchSubcategory) {
                elements.searchSubcategory.value = '';
            }
            await loadSubcategories(category, 'search');
            
            // Re-run search if there's an active search
            if (state.lastSearchParams) {
                state.searchPage = 0;
                state.lastSearchParams.category = category || null;
                state.lastSearchParams.subcategory = null;
                performSearchWithPagination();
            }
        });
    }
    
    // Search subcategory change - re-run search
    if (elements.searchSubcategory) {
        elements.searchSubcategory.addEventListener('change', () => {
            state.searchSubcategory = elements.searchSubcategory.value;
            
            // Re-run search if there's an active search
            if (state.lastSearchParams) {
                state.searchPage = 0;
                state.lastSearchParams.subcategory = state.searchSubcategory || null;
                performSearchWithPagination();
            }
        });
    }
    
    // Search file type change - re-run search
    if (elements.searchFileType) {
        elements.searchFileType.addEventListener('change', () => {
            // Re-run search if there's an active search
            if (state.lastSearchParams) {
                state.searchPage = 0;
                state.lastSearchParams.file_type = elements.searchFileType.value || null;
                performSearchWithPagination();
            }
        });
    }
    
    // Browse
    let filenameSearchTimeout;
    if (elements.browseFilename) {
        elements.browseFilename.addEventListener('input', () => {
            clearTimeout(filenameSearchTimeout);
            filenameSearchTimeout = setTimeout(() => {
                state.browseFilename = elements.browseFilename.value.trim();
                state.browsePage = 0;
                loadDocuments();
            }, 300); // Debounce 300ms
        });
    }
    
    // Topic keyword dropdown
    if (elements.browseKeyword) {
        elements.browseKeyword.addEventListener('change', async () => {
            state.browseKeyword = elements.browseKeyword.value;
            state.browsePage = 0;
            // Reload categories with filtered counts
            await loadCategories(state.browseKeyword || null);
            loadDocuments();
        });
    }
    
    elements.browseCategory.addEventListener('change', async () => {
        state.browseCategory = elements.browseCategory.value;
        state.browseSubcategory = '';
        if (elements.browseSubcategory) {
            elements.browseSubcategory.value = '';
        }
        state.browsePage = 0;
        await loadSubcategories(state.browseCategory, 'browse');
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
        if (state.browsePage > 0) {
            state.browsePage--;
            loadDocuments();
        }
    });
    elements.nextPage.addEventListener('click', () => {
        state.browsePage++;
        loadDocuments();
    });
    
    // Search pagination
    if (elements.searchPrevPage) {
        elements.searchPrevPage.addEventListener('click', () => {
            if (state.searchPage > 0) {
                state.searchPage--;
                performSearchWithPagination();
            }
        });
    }
    if (elements.searchNextPage) {
        elements.searchNextPage.addEventListener('click', () => {
            state.searchPage++;
            performSearchWithPagination();
        });
    }
    
    // Ask AI
    elements.askBtn.addEventListener('click', askQuestion);
    elements.askInput.addEventListener('keypress', e => {
        if (e.key === 'Enter' && !e.shiftKey) {
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
    
    // Modal
    elements.modalClose.addEventListener('click', closeModal);
    elements.modalBackdrop.addEventListener('click', closeModal);
    elements.modalTabs.forEach(tab => {
        tab.addEventListener('click', () => switchModalTab(tab.dataset.tab));
    });
    
    // Document Navigation
    if (elements.docPrevBtn) {
        elements.docPrevBtn.addEventListener('click', () => navigateDocument(-1));
    }
    if (elements.docNextBtn) {
        elements.docNextBtn.addEventListener('click', () => navigateDocument(1));
    }
    
    // PDF Fullscreen toggle
    const pdfFullscreenBtn = document.getElementById('pdf-fullscreen-btn');
    const pdfViewer = document.getElementById('modal-pdf-viewer');
    
    if (pdfFullscreenBtn && pdfViewer) {
        pdfFullscreenBtn.addEventListener('click', () => {
            togglePdfFullscreen(pdfViewer);
        });
        
        // Exit fullscreen on Escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && pdfViewer.classList.contains('fullscreen')) {
                togglePdfFullscreen(pdfViewer);
            }
        });
    }
    
    // Share button
    const shareBtn = document.getElementById('share-btn');
    const shareMenu = document.getElementById('share-menu');
    
    if (shareBtn && shareMenu) {
        shareBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            shareMenu.classList.toggle('hidden');
        });
        
        // Close share menu when clicking outside
        document.addEventListener('click', (e) => {
            if (!shareBtn.contains(e.target) && !shareMenu.contains(e.target)) {
                shareMenu.classList.add('hidden');
            }
        });
        
        // Share option clicks
        shareMenu.querySelectorAll('.share-option').forEach(option => {
            option.addEventListener('click', () => {
                handleShare(option.dataset.platform);
                shareMenu.classList.add('hidden');
            });
        });
    }
    
    // Feedback form - use event delegation for reliability
    document.addEventListener('submit', (e) => {
        if (e.target && e.target.id === 'feedback-form') {
            handleFeedbackSubmit(e);
        }
    });
    
    // Keyboard shortcuts
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') closeModal();
        // Arrow key navigation for documents when modal is open
        if (!elements.modal.classList.contains('hidden') && state.documentList.length > 1) {
            if (e.key === 'ArrowLeft') {
                e.preventDefault();
                navigateDocument(-1);
            } else if (e.key === 'ArrowRight') {
                e.preventDefault();
                navigateDocument(1);
            }
        }
    });
}

async function handleFeedbackSubmit(e) {
    if (e) e.preventDefault();
    console.log('Feedback form submitted');
    submitFeedback();
}

// Global function for onclick handler - exposed to window for inline onclick
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
    
    if (!feedbackType || !message.trim()) {
        showFeedbackStatus('Please fill in all required fields.', 'error');
        return;
    }
    
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Verifying...';
    
    // Get reCAPTCHA v3 token
    let recaptchaResponse;
    try {
        recaptchaResponse = await grecaptcha.execute('6Lf9EDYsAAAAANDlA_xYFIM7Ylccgmc24LhZgDIr', {action: 'submit_feedback'});
    } catch (e) {
        console.error('reCAPTCHA error:', e);
        showFeedbackStatus('reCAPTCHA verification failed. Please refresh and try again.', 'error');
        btn.disabled = false;
        btn.innerHTML = `
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/>
            </svg>
            Submit Feedback
        `;
        return;
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

function switchView(viewName) {
    state.currentView = viewName;
    
    elements.navBtns.forEach(btn => {
        btn.classList.toggle('active', btn.dataset.view === viewName);
    });
    
    elements.views.forEach(view => {
        view.classList.toggle('active', view.id === `${viewName}-view`);
    });
    
    // Load data for specific views
    if (viewName === 'browse') {
        loadDocuments();
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
        
        // Populate category dropdowns
        const categoryOptions = state.categories.map(c => 
            `<option value="${c.category}">${c.category} (${c.count})</option>`
        ).join('');
        
        // Preserve current selection
        const currentBrowseCategory = elements.browseCategory.value;
        const currentSearchCategory = elements.searchCategory.value;
        
        elements.searchCategory.innerHTML = '<option value="">All File Sets</option>' + categoryOptions;
        elements.browseCategory.innerHTML = '<option value="">All File Sets</option>' + categoryOptions;
        
        // Restore selection if still valid
        if (currentBrowseCategory && state.categories.some(c => c.category === currentBrowseCategory)) {
            elements.browseCategory.value = currentBrowseCategory;
        }
        if (currentSearchCategory && state.categories.some(c => c.category === currentSearchCategory)) {
            elements.searchCategory.value = currentSearchCategory;
        }
    } catch (error) {
        console.error('Error loading categories:', error);
    }
}

// Load public settings (like Ask AI visibility)
async function loadPublicSettings() {
    try {
        const response = await fetch(`${API_BASE}/settings`);
        if (!response.ok) return;
        
        const settings = await response.json();
        
        // Handle Ask AI visibility
        if (settings.ask_ai_enabled === false) {
            // Hide the Ask AI nav button and view
            const askNavBtn = document.querySelector('.nav-btn[data-view="ask"]');
            const askView = document.getElementById('ask-view');
            
            if (askNavBtn) askNavBtn.style.display = 'none';
            if (askView) askView.style.display = 'none';
        }
    } catch (error) {
        console.error('Error loading public settings:', error);
    }
}

// Load and display pinned documents on homepage
async function loadPinnedDocuments() {
    try {
        const response = await fetch(`${API_BASE}/pinned-documents`);
        if (!response.ok) return;
        
        const data = await response.json();
        const pinnedDocs = data.pinned_documents || [];
        
        if (pinnedDocs.length > 0) {
            renderPinnedDocumentsBar(pinnedDocs);
        }
    } catch (error) {
        console.error('Error loading pinned documents:', error);
    }
}

function renderPinnedDocumentsBar(docs) {
    // Only show on search view / homepage
    const searchView = document.getElementById('search-view');
    if (!searchView) return;
    
    // Check if bar already exists
    let pinnedBar = document.getElementById('pinned-documents-bar');
    if (pinnedBar) {
        pinnedBar.remove();
    }
    
    // Generate the card HTML
    const generateCardHTML = (doc) => `
        <div class="pinned-card" onclick="openDocument('${escapeHtml(doc.document_id)}')">
            <div class="pinned-card-thumbnail">
                <img src="${API_BASE}/documents/${doc.document_id}/thumbnail" 
                     alt="${escapeHtml(doc.filename)}"
                     onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
                <div class="thumbnail-fallback" style="display: none;">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                    </svg>
                </div>
            </div>
            <div class="pinned-card-content">
                <div class="pinned-card-filename">${escapeHtml(doc.filename)}</div>
                ${doc.reason ? `<div class="pinned-card-reason">"${escapeHtml(doc.reason)}"</div>` : ''}
                <div class="pinned-card-meta">${escapeHtml(doc.category || 'Document')}</div>
            </div>
        </div>
    `;
    
    // Create cards HTML - duplicate for seamless infinite scroll
    const cardsHTML = docs.map(generateCardHTML).join('');
    const duplicatedCardsHTML = cardsHTML + cardsHTML; // Duplicate for seamless loop
    
    // Create the pinned documents bar
    pinnedBar = document.createElement('div');
    pinnedBar.id = 'pinned-documents-bar';
    pinnedBar.className = 'pinned-documents-bar';
    
    pinnedBar.innerHTML = `
        <div class="pinned-header">
            <span class="pinned-icon">📌</span>
            <span class="pinned-title">Featured Documents</span>
            <span class="pinned-subtitle">Controversial & Notable Files</span>
            <span class="pinned-suggestion-note">Have a document that should be featured? Use the "Send Feedback" form below to submit your suggestion!</span>
        </div>
        <div class="pinned-scroll-container">
            <div class="pinned-scroll" id="pinned-scroll">
                ${duplicatedCardsHTML}
            </div>
        </div>
    `;
    
    // Insert right above the Archive Statistics section
    const statsDisplay = document.getElementById('stats-display');
    if (statsDisplay) {
        statsDisplay.parentNode.insertBefore(pinnedBar, statsDisplay);
    } else {
        // Fallback: append to search view
        searchView.appendChild(pinnedBar);
    }
}

async function loadSubcategories(category, target = 'search') {
    const subcategoryEl = target === 'search' ? elements.searchSubcategory : elements.browseSubcategory;
    const groupEl = target === 'search' ? elements.searchSubcategoryGroup : null;
    
    if (!subcategoryEl) return;
    
    // Hide if no category selected
    if (!category) {
        if (groupEl) groupEl.style.display = 'none';
        subcategoryEl.style.display = 'none';
        subcategoryEl.innerHTML = '<option value="">All Sections</option>';
        return;
    }
    
    try {
        const response = await fetch(`${API_BASE}/subcategories?category=${encodeURIComponent(category)}`);
        if (!response.ok) throw new Error('Failed to load subcategories');
        
        const data = await response.json();
        const subcategories = data.subcategories || [];
        
        // Only show if there are multiple subcategories
        if (subcategories.length > 1) {
            const options = subcategories.map(s => 
                `<option value="${escapeHtml(s.subcategory)}">${escapeHtml(s.subcategory)} (${s.count})</option>`
            ).join('');
            
            subcategoryEl.innerHTML = '<option value="">All Sections</option>' + options;
            if (groupEl) groupEl.style.display = 'block';
            subcategoryEl.style.display = 'block';
        } else {
            if (groupEl) groupEl.style.display = 'none';
            subcategoryEl.style.display = 'none';
            subcategoryEl.innerHTML = '<option value="">All Sections</option>';
        }
    } catch (error) {
        console.error('Error loading subcategories:', error);
        if (groupEl) groupEl.style.display = 'none';
        subcategoryEl.style.display = 'none';
    }
}

function renderStats() {
    if (!state.stats) return;
    
    const stats = state.stats;
    
    // Get file type counts
    const fileTypes = stats.by_file_type || [];
    const pdfCount = fileTypes.find(f => f.file_type === 'pdf')?.count || stats.total_documents;
    const audioCount = fileTypes.find(f => f.file_type === 'audio')?.count || 0;
    const videoCount = fileTypes.find(f => f.file_type === 'video')?.count || 0;
    const imageCount = fileTypes.find(f => f.file_type === 'image')?.count || 0;
    
    elements.statsGrid.innerHTML = `
        <div class="stat-card clickable" data-browse="all" title="Browse all files">
            <div class="stat-value">${formatNumber(stats.total_documents)}</div>
            <div class="stat-label">Total Files</div>
            <div class="stat-action">Browse All →</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${formatNumber(stats.total_pages)}</div>
            <div class="stat-label">Total Pages</div>
        </div>
        <div class="stat-card file-type-card clickable" data-browse="pdf" title="Browse PDF documents">
            <div class="stat-value">📄 ${formatNumber(pdfCount)}</div>
            <div class="stat-label">PDF Documents</div>
            <div class="stat-action">Browse →</div>
        </div>
        <div class="stat-card file-type-card clickable" data-browse="audio" title="Browse audio files">
            <div class="stat-value">🎵 ${formatNumber(audioCount)}</div>
            <div class="stat-label">Audio Files</div>
            <div class="stat-action">Browse →</div>
        </div>
        <div class="stat-card file-type-card clickable" data-browse="image" title="Browse image files">
            <div class="stat-value">🖼️ ${formatNumber(imageCount)}</div>
            <div class="stat-label">Image Files</div>
            <div class="stat-action">Browse →</div>
        </div>
        <div class="stat-card file-type-card clickable" data-browse="video" title="Browse video files">
            <div class="stat-value">🎬 ${formatNumber(videoCount)}</div>
            <div class="stat-label">Video Files</div>
            <div class="stat-action">Browse →</div>
        </div>
    `;
    
    // Add click handlers for browsable stat cards
    elements.statsGrid.querySelectorAll('.stat-card.clickable').forEach(card => {
        card.addEventListener('click', async () => {
            const browseType = card.dataset.browse;
            if (browseType === 'all') {
                state.browseFileType = '';
            } else {
                state.browseFileType = browseType;
            }
            state.browseCategory = '';
            state.browseKeyword = '';
            state.browsePage = 0;
            
            // Update the browse filter dropdown
            if (elements.browseFileType) {
                elements.browseFileType.value = state.browseFileType;
            }
            if (elements.browseCategory) {
                elements.browseCategory.value = '';
            }
            if (elements.browseKeyword) {
                elements.browseKeyword.value = '';
            }
            
            // Reload categories with full counts (no keyword filter)
            await loadCategories();
            
            // Switch to browse view
            switchView('browse');
        });
    });
    
    // Also update the file type filter with counts
    if (elements.searchFileType) {
        elements.searchFileType.innerHTML = `
            <option value="">All Files (${formatNumber(stats.total_documents)})</option>
            <option value="pdf">📄 Documents (${formatNumber(pdfCount)})</option>
            <option value="audio">🎵 Audio (${formatNumber(audioCount)})</option>
            <option value="image">🖼️ Images (${formatNumber(imageCount)})</option>
            <option value="video">🎬 Video (${formatNumber(videoCount)})</option>
        `;
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
    if (!query) return;
    
    // Reset to first page on new search
    state.searchPage = 0;
    
    // Store search params for pagination
    state.lastSearchParams = {
        query: query,
        search_type: elements.searchType.value,
        category: elements.searchCategory.value || null,
        subcategory: elements.searchSubcategory?.value || null,
        file_type: elements.searchFileType?.value || null
    };
    
    await performSearchWithPagination();
}

async function performSearchWithPagination() {
    if (!state.lastSearchParams) return;
    
    elements.searchBtn.disabled = true;
    elements.searchBtn.innerHTML = '<span class="loading-spinner"></span> Searching...';
    
    const offset = state.searchPage * state.searchLimit;
    
    try {
        const response = await fetch(`${API_BASE}/search`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ...state.lastSearchParams,
                limit: state.searchLimit,
                offset: offset
            })
        });
        
        if (!response.ok) throw new Error('Search failed');
        
        const data = await response.json();
        state.searchTotal = data.total;
        renderSearchResults(data);
        
        // Hide stats, show results
        elements.statsDisplay.classList.add('hidden');
        elements.searchResults.classList.remove('hidden');
        
        // Scroll to top of results
        elements.searchResults.scrollIntoView({ behavior: 'smooth', block: 'start' });
        
    } catch (error) {
        console.error('Search error:', error);
        elements.resultsList.innerHTML = '<p class="error">Search failed. Please try again.</p>';
        elements.searchResults.classList.remove('hidden');
    } finally {
        elements.searchBtn.disabled = false;
        elements.searchBtn.innerHTML = '<span>Search</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14m-7-7l7 7-7 7"/></svg>';
        
        // Show clear button after search
        if (elements.clearSearchBtn) {
            elements.clearSearchBtn.classList.remove('hidden');
        }
    }
}

async function clearSearch() {
    // Clear the search input
    elements.searchInput.value = '';
    
    // Reset search state
    state.lastSearchParams = null;
    state.searchPage = 0;
    state.searchTotal = 0;
    
    // Reset filter dropdowns to "All" 
    if (elements.searchCategory) {
        elements.searchCategory.value = '';
    }
    if (elements.searchSubcategory) {
        elements.searchSubcategory.value = '';
    }
    if (elements.searchFileType) {
        elements.searchFileType.value = '';
    }
    
    // Hide subcategory group
    if (elements.searchSubcategoryGroup) {
        elements.searchSubcategoryGroup.style.display = 'none';
    }
    
    // Hide search results and show stats
    elements.searchResults.classList.add('hidden');
    elements.statsDisplay.classList.remove('hidden');
    
    // Hide pagination
    if (elements.searchPagination) {
        elements.searchPagination.classList.add('hidden');
    }
    
    // Hide clear button
    if (elements.clearSearchBtn) {
        elements.clearSearchBtn.classList.add('hidden');
    }
    
    // Reload original categories with full counts
    await loadCategories();
    
    // Reset file type dropdown to original counts
    await loadStats();
    
    // Scroll to top
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderSearchResults(data) {
    const totalPages = Math.ceil(data.total / state.searchLimit);
    const startResult = state.searchPage * state.searchLimit + 1;
    const endResult = Math.min((state.searchPage + 1) * state.searchLimit, data.total);
    
    // Build filter context string
    let filterContext = '';
    if (state.lastSearchParams) {
        const filters = [];
        if (state.lastSearchParams.category) {
            filters.push(state.lastSearchParams.category);
        }
        if (state.lastSearchParams.subcategory) {
            filters.push(state.lastSearchParams.subcategory);
        }
        if (state.lastSearchParams.file_type) {
            const typeLabels = { pdf: 'Documents', audio: 'Audio', video: 'Video' };
            filters.push(typeLabels[state.lastSearchParams.file_type] || state.lastSearchParams.file_type);
        }
        if (filters.length > 0) {
            filterContext = ` in ${filters.join(' › ')}`;
        }
    }
    
    // Update results count with range and filter context
    if (data.total > state.searchLimit) {
        elements.resultsCount.textContent = `Showing ${startResult}-${endResult} of ${formatNumber(data.total)} results for "${data.query}"${filterContext}`;
    } else {
        elements.resultsCount.textContent = `${formatNumber(data.total)} results for "${data.query}"${filterContext}`;
    }
    
    // Update pagination controls
    if (elements.searchPagination) {
        if (data.total > state.searchLimit) {
            elements.searchPagination.classList.remove('hidden');
            elements.searchPrevPage.disabled = state.searchPage === 0;
            // Disable next button if there are no more results to show
            const hasMoreResults = (state.searchPage + 1) * state.searchLimit < data.total;
            elements.searchNextPage.disabled = !hasMoreResults;
            
            // Generate page number buttons
            renderSearchPageNumbers(totalPages);
        } else {
            elements.searchPagination.classList.add('hidden');
        }
    }
    
    if (!data.results || data.results.length === 0) {
        elements.resultsList.innerHTML = '<p class="no-results">No documents found matching your query.</p>';
        return;
    }
    
    // Store document list for navigation
    state.documentList = data.results.map(r => ({ id: r.id, filename: r.filename }));
    
    elements.resultsList.innerHTML = data.results.map((result, index) => `
        <div class="result-item" data-id="${result.id}" data-index="${index}">
            <div class="result-thumbnail" data-file-type="${result.file_type || 'pdf'}">
                <img src="${API_BASE}/documents/${result.id}/thumbnail" 
                     alt="${escapeHtml(result.filename)}" 
                     loading="lazy" />
                <div class="thumbnail-fallback">${getDocumentIcon(result.file_type)}</div>
            </div>
            <div class="result-content">
                <div class="result-header">
                    <span class="result-filename">${escapeHtml(result.filename)}</span>
                    ${result.score ? `<span class="result-score">${formatRelevanceScore(result.score, result.search_type)}</span>` : ''}
                </div>
                <div class="result-meta">
                    <span class="result-category">${escapeHtml(result.category)}</span>
                    ${result.subcategory ? `<span>${escapeHtml(result.subcategory)}</span>` : ''}
                    <span>${getSearchResultMeta(result)}</span>
                </div>
                ${result.snippet ? `<div class="result-snippet">${sanitizeSnippet(result.snippet)}</div>` : ''}
            </div>
        </div>
    `).join('');
    
    // Add error handlers for thumbnail images
    elements.resultsList.querySelectorAll('.result-thumbnail img').forEach(img => {
        img.addEventListener('error', function() {
            this.style.display = 'none';
            this.parentElement.querySelector('.thumbnail-fallback').style.display = 'flex';
        });
        img.addEventListener('load', function() {
            this.parentElement.querySelector('.thumbnail-fallback').style.display = 'none';
        });
    });
    
    // Add click handlers
    elements.resultsList.querySelectorAll('.result-item').forEach(item => {
        item.addEventListener('click', () => {
            const index = parseInt(item.dataset.index);
            openDocument(item.dataset.id, index);
        });
    });
    
    // Update filter dropdowns with faceted counts
    if (data.facets) {
        updateSearchFilterCounts(data.facets);
    }
}

function updateSearchFilterCounts(facets) {
    // Update category dropdown with search-specific counts
    if (facets.categories && elements.searchCategory) {
        const currentCategory = elements.searchCategory.value;
        const totalResults = facets.categories.reduce((sum, c) => sum + c.count, 0);
        
        let categoryOptions = `<option value="">All File Sets (${formatNumber(totalResults)})</option>`;
        categoryOptions += facets.categories.map(c => 
            `<option value="${c.category}"${c.category === currentCategory ? ' selected' : ''}>${c.category} (${formatNumber(c.count)})</option>`
        ).join('');
        
        elements.searchCategory.innerHTML = categoryOptions;
    }
    
    // Update subcategory dropdown with search-specific counts
    if (facets.subcategories && elements.searchSubcategory) {
        const currentSubcategory = elements.searchSubcategory.value;
        const totalSubResults = facets.subcategories.reduce((sum, s) => sum + s.count, 0);
        
        let subcategoryOptions = `<option value="">All Sections (${formatNumber(totalSubResults)})</option>`;
        subcategoryOptions += facets.subcategories.map(s => 
            `<option value="${s.subcategory}"${s.subcategory === currentSubcategory ? ' selected' : ''}>${s.subcategory} (${formatNumber(s.count)})</option>`
        ).join('');
        
        elements.searchSubcategory.innerHTML = subcategoryOptions;
        
        // Show/hide subcategory group based on whether there are subcategories
        if (elements.searchSubcategoryGroup) {
            elements.searchSubcategoryGroup.style.display = facets.subcategories.length > 0 ? '' : 'none';
        }
    }
    
    // Update file type dropdown with search-specific counts
    if (facets.file_types && elements.searchFileType) {
        const currentFileType = elements.searchFileType.value;
        const totalFileResults = facets.file_types.reduce((sum, f) => sum + f.count, 0);
        
        const typeLabels = {
            'pdf': '📄 Documents',
            'audio': '🎵 Audio',
            'video': '🎬 Video'
        };
        
        let fileTypeOptions = `<option value="">All Files (${formatNumber(totalFileResults)})</option>`;
        fileTypeOptions += facets.file_types.map(f => {
            const label = typeLabels[f.file_type] || f.file_type;
            return `<option value="${f.file_type}"${f.file_type === currentFileType ? ' selected' : ''}>${label} (${formatNumber(f.count)})</option>`;
        }).join('');
        
        elements.searchFileType.innerHTML = fileTypeOptions;
    }
}

function renderSearchPageNumbers(totalPages) {
    if (!elements.searchPageNumbers) return;
    
    const currentPage = state.searchPage;
    const maxVisible = 7; // Maximum number of page buttons to show
    let pages = [];
    
    if (totalPages <= maxVisible) {
        // Show all pages if total is small
        for (let i = 0; i < totalPages; i++) {
            pages.push(i);
        }
    } else {
        // Always show first page
        pages.push(0);
        
        // Calculate range around current page
        let start = Math.max(1, currentPage - 2);
        let end = Math.min(totalPages - 2, currentPage + 2);
        
        // Adjust if near the beginning
        if (currentPage < 3) {
            end = Math.min(totalPages - 2, 4);
        }
        
        // Adjust if near the end
        if (currentPage > totalPages - 4) {
            start = Math.max(1, totalPages - 5);
        }
        
        // Add ellipsis before middle section if needed
        if (start > 1) {
            pages.push('...');
        }
        
        // Add middle pages
        for (let i = start; i <= end; i++) {
            pages.push(i);
        }
        
        // Add ellipsis after middle section if needed
        if (end < totalPages - 2) {
            pages.push('...');
        }
        
        // Always show last page
        pages.push(totalPages - 1);
    }
    
    // Generate HTML
    elements.searchPageNumbers.innerHTML = pages.map(page => {
        if (page === '...') {
            return '<span class="page-ellipsis">…</span>';
        }
        const isActive = page === currentPage;
        return `<button class="page-num ${isActive ? 'active' : ''}" data-page="${page}">${page + 1}</button>`;
    }).join('');
    
    // Add click handlers
    elements.searchPageNumbers.querySelectorAll('.page-num').forEach(btn => {
        btn.addEventListener('click', () => {
            const page = parseInt(btn.dataset.page);
            if (page !== state.searchPage) {
                state.searchPage = page;
                performSearchWithPagination();
            }
        });
    });
}

async function loadDocuments() {
    const offset = state.browsePage * state.browseLimit;
    
    try {
        const params = new URLSearchParams({
            limit: state.browseLimit,
            offset: offset
        });
        
        if (state.browseCategory) {
            params.append('category', state.browseCategory);
        }
        
        if (state.browseSubcategory) {
            params.append('subcategory', state.browseSubcategory);
        }
        
        if (state.browseFileType) {
            params.append('file_type', state.browseFileType);
        }
        
        if (state.browseFilename) {
            params.append('filename', state.browseFilename);
        }
        
        if (state.browseKeyword) {
            params.append('keyword', state.browseKeyword);
        }
        
        const response = await fetch(`${API_BASE}/documents?${params}`);
        if (!response.ok) throw new Error('Failed to load documents');
        
        const data = await response.json();
        renderDocuments(data);
        
    } catch (error) {
        console.error('Error loading documents:', error);
        elements.documentsGrid.innerHTML = '<p class="error">Failed to load documents.</p>';
    }
}

function renderDocuments(data) {
    elements.browseCount.textContent = `${formatNumber(data.total)} documents`;
    
    const totalPages = Math.ceil(data.total / state.browseLimit);
    elements.pageInfo.textContent = `Page ${state.browsePage + 1} of ${Math.max(1, totalPages)}`;
    
    elements.prevPage.disabled = state.browsePage === 0;
    // Disable next button if there are no more results to show
    const hasMoreResults = (state.browsePage + 1) * state.browseLimit < data.total;
    elements.nextPage.disabled = !hasMoreResults || totalPages <= 1;
    
    if (!data.documents || data.documents.length === 0) {
        elements.documentsGrid.innerHTML = '<p class="no-results">No documents found.</p>';
        state.documentList = [];
        return;
    }
    
    // Store document list for navigation
    state.documentList = data.documents.map(d => ({ id: d.id, filename: d.filename }));
    
    elements.documentsGrid.innerHTML = data.documents.map((doc, index) => `
        <div class="document-card" data-id="${doc.id}" data-index="${index}">
            <div class="document-thumbnail" data-file-type="${doc.file_type || 'pdf'}">
                <img src="${API_BASE}/documents/${doc.id}/thumbnail" 
                     alt="${escapeHtml(doc.filename)}" 
                     loading="lazy" />
                <div class="thumbnail-fallback">${getDocumentIcon(doc.file_type)}</div>
            </div>
            <div class="document-title">${escapeHtml(doc.filename)}</div>
            <div class="document-meta">
                ${getDocumentTileLabel(doc)} • ${getDocumentMeta(doc)}
            </div>
        </div>
    `).join('');
    
    // Add error handlers for thumbnail images
    elements.documentsGrid.querySelectorAll('.document-thumbnail img').forEach(img => {
        img.addEventListener('error', function() {
            this.style.display = 'none';
            this.parentElement.querySelector('.thumbnail-fallback').style.display = 'flex';
        });
        img.addEventListener('load', function() {
            this.parentElement.querySelector('.thumbnail-fallback').style.display = 'none';
        });
    });
    
    // Add click handlers
    elements.documentsGrid.querySelectorAll('.document-card').forEach(card => {
        card.addEventListener('click', () => {
            const index = parseInt(card.dataset.index);
            openDocument(card.dataset.id, index);
        });
    });
}

async function openDocument(docId, index = -1) {
    try {
        const response = await fetch(`${API_BASE}/documents/${docId}`);
        if (!response.ok) throw new Error('Document not found');
        
        const doc = await response.json();
        state.currentDocument = doc;
        
        // Track document index for navigation
        if (index >= 0) {
            state.documentIndex = index;
        } else {
            // Try to find the document in the current list
            state.documentIndex = state.documentList.findIndex(d => d.id === docId);
        }
        
        // Update navigation UI
        updateDocumentNavigation();
        
        // Determine file type icon
        const fileType = doc.file_type || 'pdf';
        const fileIcon = fileType === 'audio' ? '🎵' : fileType === 'video' ? '🎬' : fileType === 'image' ? '🖼️' : '📄';
        
        // Populate modal
        elements.modalTitle.textContent = doc.filename;
        elements.modalMeta.innerHTML = `
            <span>📁 ${doc.category}</span>
            ${doc.subcategory ? `<span>📂 ${doc.subcategory}</span>` : ''}
            <span>${fileIcon} ${fileType.toUpperCase()}</span>
            ${doc.page_count ? `<span>📄 ${doc.page_count} pages</span>` : ''}
            <span>📝 ${formatNumber(doc.char_count || 0)} characters</span>
        `;
        
        elements.modalText.textContent = doc.full_text || 'No text content available.';
        elements.modalSummary.innerHTML = '<p class="loading">Click to load AI summary...</p>';
        
        // Get file URL for viewer
        const fileUrl = `${API_BASE}/documents/${docId}/file`;
        
        // Load appropriate media viewer
        const mediaViewer = document.getElementById('media-viewer');
        
        if (fileType === 'pdf') {
            const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
            
            if (isIOS) {
                // iOS Safari has issues with PDF scrolling in iframes
                // Show a preview with button to open PDF directly
                elements.pdfIframe.src = '';
                elements.pdfIframe.style.display = 'none';
                elements.pdfFallback.classList.add('hidden');
                if (mediaViewer) {
                    mediaViewer.classList.remove('hidden');
                    mediaViewer.innerHTML = `
                        <div class="ios-pdf-fallback">
                            <div class="pdf-icon">📄</div>
                            <h3>${doc.filename}</h3>
                            <p class="pdf-info">${doc.page_count || ''} ${doc.page_count ? 'pages' : ''}</p>
                            <p class="ios-pdf-message">For the best experience, open the PDF directly with the link below.</p>
                            <a href="${fileUrl}" target="_blank" class="ios-pdf-open-btn">
                                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2">
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
                // Non-iOS: Load PDF in iframe normally
                elements.pdfIframe.src = `${fileUrl}#toolbar=0&navpanes=0&view=FitH`;
                elements.pdfIframe.style.display = 'block';
                elements.pdfFallback.classList.add('hidden');
                if (mediaViewer) mediaViewer.classList.add('hidden');
            }
        } else if (fileType === 'audio') {
            // Show audio player
            elements.pdfIframe.src = '';
            elements.pdfIframe.style.display = 'none';
            elements.pdfFallback.classList.add('hidden');
            if (mediaViewer) {
                mediaViewer.classList.remove('hidden');
                mediaViewer.innerHTML = `
                    <div class="media-player-container audio-player">
                        <div class="media-icon">🎵</div>
                        <h3>Audio Recording</h3>
                        <div class="media-notice">
                            <span class="notice-icon">⏳</span>
                            <span>Large files may take time to buffer. Please click play only once and allow time for loading.</span>
                        </div>
                        <audio controls controlsList="nodownload noplaybackrate" preload="metadata" class="audio-element" oncontextmenu="return false;">
                            <source src="${fileUrl}" type="audio/mpeg">
                            <source src="${fileUrl}" type="audio/wav">
                            Your browser does not support the audio element.
                        </audio>
                        <p class="media-hint">See "Text Content" tab for the full transcription</p>
                    </div>
                `;
            }
        } else if (fileType === 'video') {
            // Show video player
            elements.pdfIframe.src = '';
            elements.pdfIframe.style.display = 'none';
            elements.pdfFallback.classList.add('hidden');
            if (mediaViewer) {
                mediaViewer.classList.remove('hidden');
                mediaViewer.innerHTML = `
                    <div class="media-player-container video-player">
                        <div class="media-notice">
                            <span class="notice-icon">⏳</span>
                            <span>Large files may take time to buffer. Please click play only once and allow time for loading.</span>
                        </div>
                        <video controls controlsList="nodownload" preload="metadata" class="video-element" oncontextmenu="return false;">
                            <source src="${fileUrl}" type="video/mp4">
                            <source src="${fileUrl}" type="video/webm">
                            Your browser does not support the video element.
                        </video>
                        <p class="media-hint">See "Text Content" tab for the full transcription</p>
                    </div>
                `;
            }
        } else if (fileType === 'image') {
            // Show image viewer
            elements.pdfIframe.src = '';
            elements.pdfIframe.style.display = 'none';
            elements.pdfFallback.classList.add('hidden');
            
            // Check if it's a TIF/TIFF file - browsers don't support these natively
            const isTiff = doc.filename && /\.(tif|tiff)$/i.test(doc.filename);
            
            if (mediaViewer) {
                mediaViewer.classList.remove('hidden');
                if (isTiff) {
                    // TIF files need special handling - show download option
                    mediaViewer.innerHTML = `
                        <div class="media-player-container image-viewer tiff-fallback">
                            <div class="media-icon">🖼️</div>
                            <h3>TIFF Image</h3>
                            <p class="tiff-notice">TIFF files cannot be displayed directly in the browser.</p>
                            <a href="${fileUrl}" download="${escapeHtml(doc.filename)}" class="tiff-download-btn">
                                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2">
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
            // Generic fallback
            elements.pdfIframe.src = '';
            elements.pdfIframe.style.display = 'none';
            if (mediaViewer) mediaViewer.classList.add('hidden');
            elements.pdfFallback.classList.remove('hidden');
            elements.pdfFallback.innerHTML = `
                <p>${fileIcon}</p>
                <p>File Preview Not Available</p>
                <p class="pdf-fallback-hint">View the text content tab to see the extracted content.</p>
            `;
        }
        
        // Reset to document tab (default)
        switchModalTab('document');
        
        // Show modal
        elements.modal.classList.remove('hidden');
        document.body.style.overflow = 'hidden';
        
    } catch (error) {
        console.error('Error loading document:', error);
        alert('Failed to load document.');
    }
}

function closeModal() {
    elements.modal.classList.add('hidden');
    document.body.style.overflow = '';
    state.currentDocument = null;
    // Clear PDF iframe to stop loading
    if (elements.pdfIframe) {
        elements.pdfIframe.src = '';
    }
    // Exit PDF fullscreen if active
    const pdfViewer = document.getElementById('modal-pdf-viewer');
    if (pdfViewer && pdfViewer.classList.contains('fullscreen')) {
        pdfViewer.classList.remove('fullscreen');
        document.body.style.overflow = '';
    }
}

function updateDocumentNavigation() {
    if (!elements.docNavigation) return;
    
    const hasMultipleDocs = state.documentList.length > 1;
    const currentIndex = state.documentIndex;
    
    // Show/hide navigation
    if (hasMultipleDocs && currentIndex >= 0) {
        elements.docNavigation.classList.remove('hidden');
        
        // Update info text
        elements.docNavInfo.textContent = `${currentIndex + 1} of ${state.documentList.length}`;
        
        // Update button states
        elements.docPrevBtn.disabled = currentIndex <= 0;
        elements.docNextBtn.disabled = currentIndex >= state.documentList.length - 1;
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
    const isFullscreen = pdfViewer.classList.contains('fullscreen');
    
    if (isFullscreen) {
        // Exit fullscreen
        pdfViewer.classList.remove('fullscreen');
        document.body.style.overflow = '';
    } else {
        // Enter fullscreen
        pdfViewer.classList.add('fullscreen');
        document.body.style.overflow = 'hidden';
    }
}

async function switchModalTab(tabName) {
    elements.modalTabs.forEach(tab => {
        tab.classList.toggle('active', tab.dataset.tab === tabName);
    });
    
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.toggle('active', content.id === `modal-${tabName}-tab`);
    });
    
    // Load summary if switching to summary tab
    if (tabName === 'summary' && state.currentDocument) {
        const summaryEl = elements.modalSummary;
        if (summaryEl.querySelector('.loading')) {
            await loadDocumentSummary(state.currentDocument.id);
        }
    }
}

async function loadDocumentSummary(docId) {
    try {
        elements.modalSummary.innerHTML = '<p class="loading">Generating AI summary...</p>';
        
        const response = await fetch(`${API_BASE}/documents/${docId}/summary`);
        
        if (!response.ok) {
            if (response.status === 503) {
                elements.modalSummary.innerHTML = '<p class="error">AI summarization not available. Set OPENAI_API_KEY to enable this feature.</p>';
            } else {
                throw new Error('Failed to generate summary');
            }
            return;
        }
        
        const data = await response.json();
        
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
        console.error('Error loading summary:', error);
        elements.modalSummary.innerHTML = '<p class="error">Failed to generate summary.</p>';
    }
}

async function askQuestion() {
    const question = elements.askInput.value.trim();
    if (!question) return;
    
    elements.askBtn.disabled = true;
    elements.askBtn.innerHTML = '<span class="loading-spinner"></span> Thinking...';
    elements.askResponse.classList.remove('hidden');
    elements.answerText.textContent = 'Analyzing documents...';
    elements.sourcesList.innerHTML = '';
    
    try {
        const response = await fetch(`${API_BASE}/ask`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                question: question,
                num_context_docs: 5
            })
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
            elements.sourcesList.innerHTML = data.sources.map(source => 
                `<li><a href="#" class="source-link" data-id="${source.id}">📄 ${escapeHtml(source.filename)} (${source.category}) - ${formatRelevanceScore(source.score, 'semantic')}</a></li>`
            ).join('');
            
            // Add click handlers to source links
            elements.sourcesList.querySelectorAll('.source-link').forEach(link => {
                link.addEventListener('click', (e) => {
                    e.preventDefault();
                    openDocument(link.dataset.id);
                });
            });
        }
        
    } catch (error) {
        console.error('Ask error:', error);
        elements.answerText.textContent = 'Failed to get answer. Please try again.';
    } finally {
        elements.askBtn.disabled = false;
        elements.askBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg> Ask Question';
    }
}

// Utility functions
function formatNumber(num) {
    if (num === null || num === undefined) return '0';
    return num.toLocaleString();
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

/**
 * Get document icon as escaped string for use in onerror handlers
 */
function getDocumentIconEscaped(fileType) {
    return getDocumentIcon(fileType).replace(/'/g, "\\'").replace(/"/g, '\\"').replace(/\n/g, '');
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

/**
 * Detect if user is on iOS
 */
function isIOS() {
    return /iPhone|iPad|iPod/i.test(navigator.userAgent);
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
    
    // Try to open the native app
    window.location.href = urlToTry;
    
    // After a short delay, check if we're still here and fall back to web
    setTimeout(() => {
        document.removeEventListener('visibilitychange', handleVisibility);
        
        // If the page is still visible and we haven't navigated away,
        // the app probably isn't installed, so open the web version
        if (!didNavigate && !document.hidden) {
            window.open(webUrl, '_blank', 'width=600,height=400,menubar=no,toolbar=no');
        }
    }, 2000);
}

/**
 * Handle sharing document to social platforms
 */
function handleShare(platform) {
    if (!state.currentDocument) return;
    
    const doc = state.currentDocument;
    const siteUrl = 'https://epsteinfta.com';
    const shareUrl = `${siteUrl}/?doc=${doc.id}`;
    
    // Build context string with category and subcategory
    let contextParts = [];
    if (doc.category) contextParts.push(doc.category);
    if (doc.subcategory) contextParts.push(doc.subcategory);
    const context = contextParts.length > 0 ? ` (${contextParts.join(' - ')})` : '';
    
    const shareText = `Check out this document from the Epstein Library Files Public Archive: "${doc.filename}"${context}`;
    
    const isMobile = isMobileDevice();
    let webUrl = '';
    let appUrl = '';
    let intentUrl = '';
    
    switch (platform) {
        case 'facebook':
            // Web URL for desktop/fallback
            webUrl = `https://www.facebook.com/sharer/sharer.php?u=${encodeURIComponent(shareUrl)}&quote=${encodeURIComponent(shareText)}`;
            // iOS deep link - opens share dialog
            appUrl = `fb://share/?link=${encodeURIComponent(shareUrl)}`;
            // Android Intent - more reliable for triggering share dialog
            intentUrl = `intent://share/?link=${encodeURIComponent(shareUrl)}#Intent;package=com.facebook.katana;scheme=fb;end`;
            break;
        case 'twitter':
            webUrl = `https://twitter.com/intent/tweet?url=${encodeURIComponent(shareUrl)}&text=${encodeURIComponent(shareText)}`;
            // Twitter/X doesn't have a reliable share deep link, use web
            appUrl = null;
            intentUrl = null;
            break;
        case 'linkedin':
            // Web URL for desktop/fallback
            webUrl = `https://www.linkedin.com/sharing/share-offsite/?url=${encodeURIComponent(shareUrl)}`;
            // LinkedIn deep link for share
            appUrl = `linkedin://shareArticle?url=${encodeURIComponent(shareUrl)}&title=${encodeURIComponent(shareText)}`;
            // Android Intent for LinkedIn
            intentUrl = `intent://shareArticle?url=${encodeURIComponent(shareUrl)}#Intent;package=com.linkedin.android;scheme=linkedin;end`;
            break;
        case 'copy':
            copyToClipboard(shareUrl).then(success => {
                if (success) {
                    // Show temporary feedback
                    const copyBtn = document.querySelector('.share-option[data-platform="copy"]');
                    if (copyBtn) {
                        const originalText = copyBtn.innerHTML;
                        copyBtn.innerHTML = `
                            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M20 6L9 17l-5-5"/>
                            </svg>
                            Copied!
                        `;
                        setTimeout(() => {
                            copyBtn.innerHTML = originalText;
                        }, 2000);
                    }
                }
            });
            return;
        default:
            return;
    }
    
    // On mobile with a valid app URL, try to open native app first
    if (isMobile && appUrl) {
        openNativeAppOrFallback(appUrl, webUrl, intentUrl);
    } else {
        // Desktop or no app URL - open web version
        window.open(webUrl, '_blank', 'width=600,height=400,menubar=no,toolbar=no');
    }
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Copy text to clipboard with iOS fallback
 * iOS Safari doesn't support navigator.clipboard in all contexts
 */
async function copyToClipboard(text) {
    // Try the modern Clipboard API first
    if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch (err) {
            console.log('Clipboard API failed, trying fallback:', err);
        }
    }
    
    // Fallback for iOS and older browsers
    try {
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
            return true;
        } else {
            throw new Error('execCommand copy failed');
        }
    } catch (err) {
        console.error('Fallback copy failed:', err);
        // Last resort: show prompt with the URL
        prompt('Copy this link:', text);
        return false;
    }
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
 * Sanitize HTML snippet - only allow <mark> tags for search highlighting
 * Prevents XSS from malicious content in database
 */
function sanitizeSnippet(html) {
    if (!html) return '';
    const div = document.createElement('div');
    div.innerHTML = html;
    
    // Remove all elements except <mark> tags
    div.querySelectorAll('*').forEach(el => {
        if (el.tagName !== 'MARK') {
            // Replace element with its text content
            el.replaceWith(document.createTextNode(el.textContent));
        }
    });
    
    return div.innerHTML;
}

