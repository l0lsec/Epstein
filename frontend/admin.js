/**
 * Epstein Files Archive - Admin Console
 * Telemetry and Usage Statistics Dashboard
 */

const API_BASE = window.location.origin + '/api/admin/telemetry';
const AUTH_API = window.location.origin + '/api/admin';

// Authentication state
let apiKey = null;
const STORAGE_KEY = 'epstein_admin_key';

// State
let currentTimeframe = '1h';
let refreshInterval = null;

// Initialize
document.addEventListener('DOMContentLoaded', init);

function init() {
    setupLoginForm();
    checkStoredAuth();
}

function setupLoginForm() {
    const form = document.getElementById('login-form');
    const errorEl = document.getElementById('login-error');
    const submitBtn = document.getElementById('login-btn');
    
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const keyInput = document.getElementById('api-key');
        const key = keyInput.value.trim();
        
        if (!key) return;
        
        submitBtn.disabled = true;
        submitBtn.textContent = 'Authenticating...';
        errorEl.classList.remove('visible');
        
        try {
            const response = await fetch(`${AUTH_API}/login`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-API-Key': key
                }
            });
            
            if (response.ok) {
                // Store key and proceed
                apiKey = key;
                sessionStorage.setItem(STORAGE_KEY, key);
                onLoginSuccess();
            } else {
                const data = await response.json();
                errorEl.textContent = data.detail || 'Authentication failed';
                errorEl.classList.add('visible');
                submitBtn.disabled = false;
                submitBtn.textContent = 'Authenticate';
            }
        } catch (err) {
            console.error('Login error:', err);
            errorEl.textContent = 'Connection error. Please try again.';
            errorEl.classList.add('visible');
            submitBtn.disabled = false;
            submitBtn.textContent = 'Authenticate';
        }
    });
}

function checkStoredAuth() {
    const storedKey = sessionStorage.getItem(STORAGE_KEY);
    
    if (storedKey) {
        // Verify the key is still valid
        verifyAuth(storedKey);
    }
}

async function verifyAuth(key) {
    try {
        const response = await fetch(`${AUTH_API}/verify`, {
            headers: {
                'X-API-Key': key
            }
        });
        
        if (response.ok) {
            apiKey = key;
            onLoginSuccess();
        } else {
            // Invalid key, clear storage
            sessionStorage.removeItem(STORAGE_KEY);
        }
    } catch (err) {
        console.error('Auth verification failed:', err);
    }
}

function onLoginSuccess() {
    // Hide login overlay
    document.getElementById('login-overlay').classList.add('hidden');
    document.body.classList.remove('logged-out');
    
    // Initialize dashboard
    initDashboard();
}

function initDashboard() {
    setupTabNavigation();
    setupTimeframeButtons();
    refreshAll();
    
    // Auto-refresh every 30 seconds
    refreshInterval = setInterval(refreshAll, 30000);
}

function logout() {
    apiKey = null;
    sessionStorage.removeItem(STORAGE_KEY);
    
    // Show login overlay
    document.getElementById('login-overlay').classList.remove('hidden');
    document.body.classList.add('logged-out');
    
    // Clear refresh interval
    if (refreshInterval) {
        clearInterval(refreshInterval);
        refreshInterval = null;
    }
}

// Helper function to make authenticated API calls
async function authFetch(url, options = {}) {
    if (!apiKey) {
        throw new Error('Not authenticated');
    }
    
    const headers = {
        ...options.headers,
        'X-API-Key': apiKey
    };
    
    const response = await fetch(url, { ...options, headers });
    
    // If unauthorized, trigger logout
    if (response.status === 401) {
        logout();
        throw new Error('Session expired');
    }
    
    return response;
}

async function safeErrorMessage(response, fallbackMsg) {
    try {
        const text = await response.text();
        try {
            const data = JSON.parse(text);
            return data.detail || fallbackMsg;
        } catch {
            return text || fallbackMsg;
        }
    } catch {
        return fallbackMsg;
    }
}

function setupTabNavigation() {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tabId = btn.dataset.tab;
            
            // Update active tab button
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            
            // Update active tab panel
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            document.getElementById(`${tabId}-tab`).classList.add('active');
            
            // Load tab-specific data
            loadTabData(tabId);
        });
    });
}

function setupTimeframeButtons() {
    document.querySelectorAll('.timeframe-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.timeframe-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentTimeframe = btn.dataset.tf;
            loadRequestsData();
        });
    });
}

function updateLastRefresh() {
    document.getElementById('last-refresh').textContent = 
        `Last refresh: ${new Date().toLocaleTimeString()}`;
}

async function refreshAll() {
    updateLastRefresh();
    
    // Load data for active tab
    const activeTab = document.querySelector('.tab-btn.active').dataset.tab;
    
    // Always load overview first
    await loadOverviewData();
    
    // Load current tab data
    if (activeTab !== 'overview') {
        loadTabData(activeTab);
    }
}

function loadTabData(tabId) {
    switch (tabId) {
        case 'overview':
            loadOverviewData();
            break;
        case 'requests':
            loadRequestsData();
            break;
        case 'search':
            loadSearchData();
            break;
        case 'documents':
            loadDocumentsData();
            break;
        case 'ai':
            loadAIData();
            break;
        case 'security':
            loadSecurityData();
            break;
        case 'visitors':
            loadVisitorsData();
            break;
        case 'feedback':
            loadFeedbackData();
            break;
        case 'content':
            loadContentData();
            break;
        case 'system':
            loadSystemData();
            break;
    }
}

// ============================================================================
// DATA LOADING FUNCTIONS
// ============================================================================

async function loadOverviewData() {
    try {
        const response = await authFetch(`${API_BASE}/overview`);
        if (!response.ok) throw new Error('Failed to load overview');
        const data = await response.json();
        
        renderOverviewStats(data);
        renderTopEndpoints(data.top_endpoints);
        renderStatusCodes(data.status_codes);
    } catch (error) {
        console.error('Error loading overview:', error);
        document.getElementById('overview-stats').innerHTML = 
            '<div class="stat-card danger"><div class="stat-value">Error</div><div class="stat-label">Failed to load telemetry</div></div>';
    }
}

async function loadRequestsData() {
    try {
        const response = await authFetch(`${API_BASE}/requests?timeframe=${currentTimeframe}`);
        if (!response.ok) throw new Error('Failed to load requests');
        const data = await response.json();
        
        renderRequestsChart(data.time_series);
        renderHttpMethods(data.methods);
        renderResponseTimes(data.response_time_distribution);
        
        // Render recent requests table with IP geolocation
        if (data.recent_requests) {
            renderRecentRequestsTable(data.recent_requests);
        }
    } catch (error) {
        console.error('Error loading requests:', error);
    }
}

async function loadSearchData() {
    try {
        const response = await authFetch(`${API_BASE}/search`);
        if (!response.ok) throw new Error('Failed to load search');
        const data = await response.json();
        
        renderSearchStats(data);
        renderTopQueries(data.top_queries);
        renderSearchTypes(data.search_types);
        renderCategoryUsage(data.category_usage);
    } catch (error) {
        console.error('Error loading search data:', error);
    }
    loadSearchLog(1);
    initSearchLogControls();
}

async function loadDocumentsData() {
    try {
        const response = await authFetch(`${API_BASE}/documents`);
        if (!response.ok) throw new Error('Failed to load documents');
        const data = await response.json();
        
        renderDocsStats(data);
        renderTopDocs(data.top_documents);
        renderAccessTypes(data.access_types);
    } catch (error) {
        console.error('Error loading documents data:', error);
    }
}

async function loadAIData() {
    try {
        const response = await authFetch(`${API_BASE}/ai`);
        if (!response.ok) throw new Error('Failed to load AI data');
        const data = await response.json();
        
        renderAIStats(data);
        renderTopQuestions(data.top_questions);
        
        // Also load AI summaries data
        loadAISummariesData();
    } catch (error) {
        console.error('Error loading AI data:', error);
    }
}

async function loadAISummariesData() {
    try {
        const response = await authFetch(`${API_BASE}/ai-summaries`);
        if (!response.ok) throw new Error('Failed to load AI summaries data');
        const data = await response.json();
        
        renderAISummariesStats(data);
        renderAISummaryDocs(data.documents);
    } catch (error) {
        console.error('Error loading AI summaries data:', error);
    }
}

async function loadSecurityData() {
    try {
        const response = await authFetch(`${API_BASE}/security`);
        if (!response.ok) throw new Error('Failed to load security');
        const data = await response.json();
        
        renderSecurityStats(data);
        renderSecurityEvents(data.event_types);
        renderSecurityIPs(data.top_ips_by_events);
        renderHighSeverityEvents(data.recent_high_severity);
    } catch (error) {
        console.error('Error loading security data:', error);
    }
}

async function loadVisitorsData() {
    try {
        const response = await authFetch(`${API_BASE}/visitors`);
        if (!response.ok) throw new Error('Failed to load visitors');
        const data = await response.json();
        
        renderVisitorStats(data);
        renderVisitorsChart(data.daily_unique_visitors);
        renderBrowsers(data.browsers);
        renderReferrers(data.top_referrers);
        
        // Render top IPs table with geolocation
        if (data.top_ips) {
            renderTopIpsTable(data.top_ips);
        }
    } catch (error) {
        console.error('Error loading visitors data:', error);
    }
}

async function loadSystemData() {
    try {
        const response = await authFetch(`${API_BASE}/system`);
        if (!response.ok) throw new Error('Failed to load system');
        const data = await response.json();
        
        renderSystemStats(data);
        renderSystemResources(data);
        renderLogSizes(data.log_sizes_mb);
        renderDbStats(data.database);
        renderIndexStatus(data);
    } catch (error) {
        console.error('Error loading system data:', error);
    }
}

// ============================================================================
// INDEX MANAGEMENT FUNCTIONS
// ============================================================================

function renderIndexStatus(data) {
    const statusEl = document.getElementById('index-status');
    if (!statusEl) return;
    
    const isIndexing = data.is_indexing;
    const lastIndex = data.last_index_time ? new Date(data.last_index_time).toLocaleString() : 'Never';
    const autoEnabled = data.auto_index_enabled;
    
    statusEl.innerHTML = `
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: var(--space-md);">
            <div>
                <span style="color: var(--text-muted); font-size: 0.85rem;">Status</span>
                <div style="font-weight: 600; color: ${isIndexing ? 'var(--warning)' : 'var(--success)'};">
                    ${isIndexing ? '⏳ Indexing in progress...' : '✓ Ready'}
                </div>
            </div>
            <div>
                <span style="color: var(--text-muted); font-size: 0.85rem;">Last Index</span>
                <div style="font-weight: 500;">${lastIndex}</div>
            </div>
            <div>
                <span style="color: var(--text-muted); font-size: 0.85rem;">Auto-Index</span>
                <div style="font-weight: 500; color: ${autoEnabled ? 'var(--success)' : 'var(--text-muted)'};">
                    ${autoEnabled ? 'Enabled' : 'Disabled'}
                </div>
            </div>
            <div>
                <span style="color: var(--text-muted); font-size: 0.85rem;">Documents</span>
                <div style="font-weight: 500;">${formatNumber(data.database?.total_documents || 0)}</div>
            </div>
        </div>
    `;
    
    // Update button states
    const reindexBtn = document.getElementById('trigger-reindex-btn');
    const ftsBtn = document.getElementById('rebuild-fts-btn');
    
    if (reindexBtn) {
        reindexBtn.disabled = isIndexing;
        if (isIndexing) {
            reindexBtn.innerHTML = '<span class="spinner" style="width: 16px; height: 16px;"></span> Indexing...';
        }
    }
    if (ftsBtn) {
        ftsBtn.disabled = isIndexing;
    }
}

async function triggerReindex() {
    const btn = document.getElementById('trigger-reindex-btn');
    
    if (!confirm('This will trigger a full reindex of all documents. This may take several minutes. Continue?')) {
        return;
    }
    
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width: 16px; height: 16px;"></span> Starting...';
    
    try {
        const response = await authFetch(`${window.location.origin}/api/index/trigger`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to trigger reindex');
        }
        
        const data = await response.json();
        alert(`Reindex started successfully!\n\nMessage: ${data.message}`);
        
        // Start polling for status updates
        pollIndexStatus();
        
    } catch (error) {
        console.error('Error triggering reindex:', error);
        alert(`Error: ${error.message}`);
        btn.disabled = false;
        btn.innerHTML = `
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M1 4v6h6M23 20v-6h-6"/>
                <path d="M20.49 9A9 9 0 005.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 013.51 15"/>
            </svg>
            Trigger Full Reindex
        `;
    }
}

async function rebuildFTS() {
    const btn = document.getElementById('rebuild-fts-btn');
    
    if (!confirm('This will rebuild the full-text search index. Continue?')) {
        return;
    }
    
    btn.disabled = true;
    const originalHTML = btn.innerHTML;
    btn.innerHTML = '<span class="spinner" style="width: 16px; height: 16px;"></span> Rebuilding...';
    
    try {
        const response = await authFetch(`${window.location.origin}/api/index/rebuild-fts`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to rebuild FTS index');
        }
        
        const data = await response.json();
        alert(`FTS Index rebuilt successfully!\n\n${data.message}`);
        
        // Reload system data
        loadSystemData();
        
    } catch (error) {
        console.error('Error rebuilding FTS:', error);
        alert(`Error: ${error.message}`);
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalHTML;
    }
}

// Date Extraction Functions
let dateExtractionPollInterval = null;

async function triggerDateExtraction() {
    const btn = document.getElementById('extract-dates-btn');
    const progressSpan = document.getElementById('date-extraction-progress');
    
    if (!confirm('This will extract dates from email headers in all documents. This runs in the background and does not re-index files. Continue?')) {
        return;
    }
    
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width: 16px; height: 16px;"></span> Starting...';
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/extract-dates`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to start date extraction');
        }
        
        const data = await response.json();
        
        if (data.status === 'already_running') {
            progressSpan.textContent = 'Extraction already in progress...';
        } else {
            progressSpan.textContent = 'Extraction started...';
        }
        
        // Start polling for status
        pollDateExtractionStatus();
        
    } catch (error) {
        console.error('Error starting date extraction:', error);
        alert(`Error: ${error.message}`);
        resetDateExtractionButton();
    }
}

async function checkDateExtractionStatus() {
    const progressSpan = document.getElementById('date-extraction-progress');
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/extract-dates/status`);
        
        if (!response.ok) {
            throw new Error('Failed to get status');
        }
        
        const data = await response.json();
        updateDateExtractionUI(data);
        
    } catch (error) {
        console.error('Error checking date extraction status:', error);
        progressSpan.textContent = 'Error checking status';
    }
}

function updateDateExtractionUI(status) {
    const btn = document.getElementById('extract-dates-btn');
    const progressSpan = document.getElementById('date-extraction-progress');
    
    if (status.running) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner" style="width: 16px; height: 16px;"></span> Extracting...';
        progressSpan.textContent = `Progress: ${status.processed}/${status.total} (${status.updated} dates found)`;
    } else {
        resetDateExtractionButton();
        if (status.completed_at) {
            progressSpan.textContent = `Last run: ${status.updated} dates extracted from ${status.processed} documents`;
        } else if (status.total === 0 && status.processed === 0) {
            progressSpan.textContent = '';
        }
    }
}

function resetDateExtractionButton() {
    const btn = document.getElementById('extract-dates-btn');
    if (btn) {
        btn.disabled = false;
        btn.innerHTML = `
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
                <line x1="16" y1="2" x2="16" y2="6"/>
                <line x1="8" y1="2" x2="8" y2="6"/>
                <line x1="3" y1="10" x2="21" y2="10"/>
            </svg>
            Extract Dates
        `;
    }
}

function pollDateExtractionStatus() {
    // Clear any existing poll
    if (dateExtractionPollInterval) {
        clearInterval(dateExtractionPollInterval);
    }
    
    // Poll every 2 seconds
    dateExtractionPollInterval = setInterval(async () => {
        try {
            const response = await authFetch(`${window.location.origin}/api/admin/extract-dates/status`);
            if (!response.ok) return;
            
            const data = await response.json();
            updateDateExtractionUI(data);
            
            // Stop polling when complete
            if (!data.running) {
                clearInterval(dateExtractionPollInterval);
                dateExtractionPollInterval = null;
            }
        } catch (error) {
            console.error('Error polling date extraction status:', error);
        }
    }, 2000);
}

let indexPollInterval = null;

function pollIndexStatus() {
    // Clear any existing poll
    if (indexPollInterval) {
        clearInterval(indexPollInterval);
    }
    
    // Poll every 5 seconds while indexing
    indexPollInterval = setInterval(async () => {
        try {
            const response = await authFetch(`${API_BASE}/system`);
            if (!response.ok) return;
            
            const data = await response.json();
            renderIndexStatus(data);
            
            // Stop polling when indexing is complete
            if (!data.is_indexing) {
                clearInterval(indexPollInterval);
                indexPollInterval = null;
                
                // Reset button
                const btn = document.getElementById('trigger-reindex-btn');
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = `
                        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M1 4v6h6M23 20v-6h-6"/>
                            <path d="M20.49 9A9 9 0 005.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 013.51 15"/>
                        </svg>
                        Trigger Full Reindex
                    `;
                }
                
                // Notify completion
                alert('Reindex completed!');
                
                // Refresh system data
                loadSystemData();
            }
        } catch (error) {
            console.error('Error polling index status:', error);
        }
    }, 5000);
}

// ============================================================================
// RENDER FUNCTIONS
// ============================================================================

function renderOverviewStats(data) {
    const o = data.overview;
    const s = data.sessions;
    
    document.getElementById('overview-stats').innerHTML = `
        <div class="stat-card">
            <div class="stat-value">${formatNumber(o.total_requests)}</div>
            <div class="stat-label">Total Requests</div>
        </div>
        <div class="stat-card info">
            <div class="stat-value">${formatNumber(o.requests_last_hour)}</div>
            <div class="stat-label">Requests (Last Hour)</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${formatNumber(o.requests_last_day)}</div>
            <div class="stat-label">Requests (24h)</div>
        </div>
        <div class="stat-card success">
            <div class="stat-value">${o.avg_response_time_ms.toFixed(0)}ms</div>
            <div class="stat-label">Avg Response Time</div>
        </div>
        <div class="stat-card ${o.error_rate_percent > 5 ? 'danger' : o.error_rate_percent > 1 ? 'warning' : 'success'}">
            <div class="stat-value">${o.error_rate_percent.toFixed(1)}%</div>
            <div class="stat-label">Error Rate</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${formatNumber(o.unique_visitors)}</div>
            <div class="stat-label">Unique Visitors</div>
        </div>
        <div class="stat-card ${o.security_events_high > 0 ? 'danger' : 'success'}">
            <div class="stat-value">${formatNumber(o.security_events)}</div>
            <div class="stat-label">Security Events</div>
        </div>
        <div class="stat-card ${o.rate_limited_requests > 100 ? 'warning' : ''}">
            <div class="stat-value">${formatNumber(o.rate_limited_requests)}</div>
            <div class="stat-label">Rate Limited</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${formatNumber(s.active_sessions)}</div>
            <div class="stat-label">Active Sessions</div>
        </div>
        <div class="stat-card ${s.blocked_ips > 0 ? 'danger' : ''}">
            <div class="stat-value">${s.blocked_ips}</div>
            <div class="stat-label">Blocked IPs</div>
        </div>
    `;
}

function renderTopEndpoints(endpoints) {
    const el = document.getElementById('top-endpoints');
    if (!endpoints || endpoints.length === 0) {
        el.innerHTML = '<li><span class="key">No data</span></li>';
        return;
    }
    
    el.innerHTML = endpoints.map(e => `
        <li>
            <span class="key" title="${escapeHtml(e.path)}">${escapeHtml(e.path)}</span>
            <span class="value">${formatNumber(e.count)}</span>
        </li>
    `).join('');
}

function renderStatusCodes(codes) {
    const el = document.getElementById('status-codes');
    if (!codes || Object.keys(codes).length === 0) {
        el.innerHTML = '<li><span class="key">No data</span></li>';
        return;
    }
    
    const sorted = Object.entries(codes).sort((a, b) => b[1] - a[1]);
    el.innerHTML = sorted.map(([code, count]) => {
        let badge = 'success';
        if (code.startsWith('4')) badge = 'warning';
        if (code.startsWith('5')) badge = 'danger';
        if (code === '200') badge = 'success';
        
        return `
            <li>
                <span class="badge badge-${badge}">${code}</span>
                <span class="value">${formatNumber(count)}</span>
            </li>
        `;
    }).join('');
}

function renderRequestsChart(timeSeries) {
    const container = document.getElementById('requests-chart');
    if (!timeSeries || timeSeries.length === 0) {
        container.innerHTML = '<div class="loading">No data available</div>';
        return;
    }
    
    const maxRequests = Math.max(...timeSeries.map(t => t.requests));
    
    container.innerHTML = `
        <div class="simple-chart">
            ${timeSeries.map(t => {
                const height = maxRequests > 0 ? (t.requests / maxRequests * 100) : 0;
                const time = new Date(t.time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                return `<div class="chart-bar" style="height: ${Math.max(2, height)}%;" data-tooltip="${time}: ${t.requests} requests"></div>`;
            }).join('')}
        </div>
    `;
}

function renderHttpMethods(methods) {
    const el = document.getElementById('http-methods');
    if (!methods || Object.keys(methods).length === 0) {
        el.innerHTML = '<li><span class="key">No data</span></li>';
        return;
    }
    
    const sorted = Object.entries(methods).sort((a, b) => b[1] - a[1]);
    el.innerHTML = sorted.map(([method, count]) => `
        <li>
            <span class="badge badge-info">${method}</span>
            <span class="value">${formatNumber(count)}</span>
        </li>
    `).join('');
}

function renderResponseTimes(dist) {
    const el = document.getElementById('response-times');
    if (!dist) {
        el.innerHTML = '<li><span class="key">No data</span></li>';
        return;
    }
    
    el.innerHTML = Object.entries(dist).map(([bucket, count]) => `
        <li>
            <span class="key">${bucket}</span>
            <span class="value">${formatNumber(count)}</span>
        </li>
    `).join('');
}

function renderSearchStats(data) {
    document.getElementById('search-stats').innerHTML = `
        <div class="stat-card">
            <div class="stat-value">${formatNumber(data.total_searches)}</div>
            <div class="stat-label">Total Searches</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${data.avg_results_per_search.toFixed(1)}</div>
            <div class="stat-label">Avg Results/Search</div>
        </div>
        <div class="stat-card ${data.zero_result_searches > data.total_searches * 0.2 ? 'warning' : ''}">
            <div class="stat-value">${formatNumber(data.zero_result_searches)}</div>
            <div class="stat-label">Zero Result Searches</div>
        </div>
    `;
}

function renderTopQueries(queries) {
    const el = document.getElementById('top-queries');
    if (!queries || queries.length === 0) {
        el.innerHTML = '<li><span class="key">No searches yet</span></li>';
        return;
    }
    
    el.innerHTML = queries.map(q => `
        <li>
            <span class="key" title="${escapeHtml(q.query)}">"${escapeHtml(truncate(q.query, 50))}"</span>
            <span class="value">${formatNumber(q.count)}</span>
        </li>
    `).join('');
}

function renderSearchTypes(types) {
    const el = document.getElementById('search-types');
    if (!types || Object.keys(types).length === 0) {
        el.innerHTML = '<li><span class="key">No data</span></li>';
        return;
    }
    
    el.innerHTML = Object.entries(types).map(([type, count]) => `
        <li>
            <span class="key">${type}</span>
            <span class="value">${formatNumber(count)}</span>
        </li>
    `).join('');
}

function renderCategoryUsage(cats) {
    const el = document.getElementById('category-usage');
    if (!cats || Object.keys(cats).length === 0) {
        el.innerHTML = '<li><span class="key">No filters used</span></li>';
        return;
    }
    
    const sorted = Object.entries(cats).sort((a, b) => b[1] - a[1]);
    el.innerHTML = sorted.map(([cat, count]) => `
        <li>
            <span class="key">${escapeHtml(cat)}</span>
            <span class="value">${formatNumber(count)}</span>
        </li>
    `).join('');
}

// ── Search Log (full list of individual queries) ──────────────────────
let _searchLogPage = 1;
let _searchLogData = null;
let _searchLogControlsInit = false;

function _getSearchLogFilters() {
    const val = (id) => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };
    return {
        q: val('slf-query'),
        search_type: val('slf-type'),
        category: val('slf-category'),
        ip: val('slf-ip'),
        min_results: val('slf-min-results'),
        max_results: val('slf-max-results'),
    };
}

function _buildSearchLogUrl(page, perPage) {
    const f = _getSearchLogFilters();
    let url = `${API_BASE}/search/log?page=${page}&per_page=${perPage}`;
    if (f.q) url += `&q=${encodeURIComponent(f.q)}`;
    if (f.search_type) url += `&search_type=${encodeURIComponent(f.search_type)}`;
    if (f.category) url += `&category=${encodeURIComponent(f.category)}`;
    if (f.ip) url += `&ip=${encodeURIComponent(f.ip)}`;
    if (f.min_results) url += `&min_results=${encodeURIComponent(f.min_results)}`;
    if (f.max_results) url += `&max_results=${encodeURIComponent(f.max_results)}`;
    return url;
}

async function loadSearchLog(page) {
    _searchLogPage = page || 1;
    const body = document.getElementById('search-log-body');
    body.innerHTML = '<tr><td colspan="6" style="padding:20px; text-align:center;"><span class="spinner"></span> Loading...</td></tr>';
    try {
        const url = _buildSearchLogUrl(_searchLogPage, 50);
        const resp = await authFetch(url);
        if (!resp.ok) throw new Error('Failed to load search log');
        _searchLogData = await resp.json();
        renderSearchLog(_searchLogData);
    } catch (err) {
        body.innerHTML = `<tr><td colspan="6" style="padding:20px; text-align:center; color:var(--text-secondary);">Error loading search log</td></tr>`;
        console.error('Error loading search log:', err);
    }
}

function renderSearchLog(data) {
    const body = document.getElementById('search-log-body');
    const info = document.getElementById('search-log-info');
    const prevBtn = document.getElementById('search-log-prev');
    const nextBtn = document.getElementById('search-log-next');

    if (!data.searches || data.searches.length === 0) {
        body.innerHTML = '<tr><td colspan="6" style="padding:20px; text-align:center; color:var(--text-secondary);">No search queries found</td></tr>';
        info.textContent = '0 results';
        prevBtn.disabled = true;
        nextBtn.disabled = true;
        return;
    }

    body.innerHTML = data.searches.map(s => {
        const ts = s.timestamp ? new Date(s.timestamp) : null;
        const timeStr = ts ? ts.toLocaleString() : '—';
        const rc = s.result_count != null ? s.result_count : '—';
        const rcStyle = s.result_count === 0 ? 'color:#e74c3c; font-weight:600;' : '';
        return `<tr style="border-bottom:1px solid var(--border);">
            <td style="padding:6px 10px; white-space:nowrap; color:var(--text-secondary);">${escapeHtml(timeStr)}</td>
            <td style="padding:6px 10px; font-weight:500;" title="${escapeHtml(s.query || '')}">${escapeHtml(truncate(s.query || '—', 60))}</td>
            <td style="padding:6px 10px;">${escapeHtml(s.search_type || '—')}</td>
            <td style="padding:6px 10px; ${rcStyle}">${rc}</td>
            <td style="padding:6px 10px;">${escapeHtml(s.category || '—')}</td>
            <td style="padding:6px 10px; font-family:monospace; font-size:12px;">${escapeHtml(s.client_ip || '—')}</td>
        </tr>`;
    }).join('');

    const start = (data.page - 1) * data.per_page + 1;
    const end = Math.min(data.page * data.per_page, data.total);
    info.textContent = `${formatNumber(start)}–${formatNumber(end)} of ${formatNumber(data.total)}`;
    prevBtn.disabled = data.page <= 1;
    nextBtn.disabled = data.page >= data.total_pages;
}

function initSearchLogControls() {
    if (_searchLogControlsInit) return;
    _searchLogControlsInit = true;

    document.getElementById('search-log-prev').addEventListener('click', () => {
        if (_searchLogPage > 1) loadSearchLog(_searchLogPage - 1);
    });
    document.getElementById('search-log-next').addEventListener('click', () => {
        if (_searchLogData && _searchLogPage < _searchLogData.total_pages) loadSearchLog(_searchLogPage + 1);
    });

    let filterTimeout;
    const debounceReload = () => {
        clearTimeout(filterTimeout);
        filterTimeout = setTimeout(() => loadSearchLog(1), 400);
    };

    // Text inputs: debounce on input
    ['slf-query', 'slf-category', 'slf-ip'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', debounceReload);
    });

    // Number inputs: debounce on input
    ['slf-min-results', 'slf-max-results'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', debounceReload);
    });

    // Dropdown: fire immediately on change
    const typeEl = document.getElementById('slf-type');
    if (typeEl) typeEl.addEventListener('change', () => loadSearchLog(1));

    document.getElementById('search-log-export').addEventListener('click', () => {
        exportSearchLog();
    });
}

async function exportSearchLog() {
    try {
        let allRows = [];
        let page = 1;
        let totalPages = 1;
        while (page <= totalPages) {
            const url = _buildSearchLogUrl(page, 500);
            const resp = await authFetch(url);
            if (!resp.ok) break;
            const d = await resp.json();
            allRows = allRows.concat(d.searches);
            totalPages = d.total_pages;
            page++;
            if (page > 200) break;
        }
        const header = 'Timestamp,Query,Type,Results,Category,IP';
        const csvRows = allRows.map(s => {
            const q = (s.query || '').replace(/"/g, '""');
            return `"${s.timestamp || ''}","${q}","${s.search_type || ''}",${s.result_count != null ? s.result_count : ''},"${s.category || ''}","${s.client_ip || ''}"`;
        });
        const csv = header + '\n' + csvRows.join('\n');
        const blob = new Blob([csv], { type: 'text/csv' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `search_queries_${new Date().toISOString().slice(0,10)}.csv`;
        a.click();
    } catch (err) {
        console.error('Export failed:', err);
        alert('Export failed. Check console.');
    }
}

function renderDocsStats(data) {
    document.getElementById('docs-stats').innerHTML = `
        <div class="stat-card">
            <div class="stat-value">${formatNumber(data.total_document_accesses)}</div>
            <div class="stat-label">Document Accesses</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${data.total_data_served_mb.toFixed(1)} MB</div>
            <div class="stat-label">Data Served</div>
        </div>
    `;
}

function renderTopDocs(docs) {
    const el = document.getElementById('top-docs');
    if (!docs || docs.length === 0) {
        el.innerHTML = '<li><span class="key">No documents accessed yet</span></li>';
        return;
    }
    
    el.innerHTML = docs.map(d => `
        <li>
            <span class="key" title="${escapeHtml(d.filename)}">${escapeHtml(truncate(d.filename, 40))}</span>
            <span class="value">${formatNumber(d.count)}</span>
        </li>
    `).join('');
}

function renderAccessTypes(types) {
    const el = document.getElementById('access-types');
    if (!types || Object.keys(types).length === 0) {
        el.innerHTML = '<li><span class="key">No data</span></li>';
        return;
    }
    
    el.innerHTML = Object.entries(types).map(([type, count]) => `
        <li>
            <span class="key">${type}</span>
            <span class="value">${formatNumber(count)}</span>
        </li>
    `).join('');
}

function renderAIStats(data) {
    document.getElementById('ai-stats').innerHTML = `
        <div class="stat-card">
            <div class="stat-value">${formatNumber(data.total_ai_queries)}</div>
            <div class="stat-label">AI Questions</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${formatNumber(data.total_summaries)}</div>
            <div class="stat-label">Summaries Generated</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${formatNumber(data.streaming_queries)}</div>
            <div class="stat-label">Streaming Queries</div>
        </div>
    `;
}

function renderTopQuestions(questions) {
    const el = document.getElementById('top-questions');
    if (!questions || questions.length === 0) {
        el.innerHTML = '<li><span class="key">No AI questions asked yet</span></li>';
        return;
    }
    
    el.innerHTML = questions.map(q => `
        <li>
            <span class="key" title="${escapeHtml(q.question)}">"${escapeHtml(truncate(q.question, 60))}"</span>
            <span class="value">${formatNumber(q.count)}</span>
        </li>
    `).join('');
}

function renderAISummariesStats(data) {
    const el = document.getElementById('ai-summaries-stats');
    if (!el) return;
    
    el.innerHTML = `
        <div style="display: flex; gap: var(--space-lg); flex-wrap: wrap;">
            <div>
                <span style="color: var(--text-muted); font-size: 0.8rem;">Documents with Summaries</span>
                <div style="font-size: 1.2rem; font-weight: 600; color: var(--accent);">${formatNumber(data.total_documents_with_summaries)}</div>
            </div>
            <div>
                <span style="color: var(--text-muted); font-size: 0.8rem;">Total Generations</span>
                <div style="font-size: 1.2rem; font-weight: 600;">${formatNumber(data.total_generations)}</div>
            </div>
            <div>
                <span style="color: var(--text-muted); font-size: 0.8rem;">Cache Hits</span>
                <div style="font-size: 1.2rem; font-weight: 600; color: var(--success);">${formatNumber(data.total_cache_hits)}</div>
            </div>
        </div>
    `;
}

function renderAISummaryDocs(documents) {
    const el = document.getElementById('ai-summary-docs');
    if (!el) return;
    
    if (!documents || documents.length === 0) {
        el.innerHTML = '<li><span class="key">No documents with AI summaries yet</span></li>';
        return;
    }
    
    el.innerHTML = documents.map(doc => `
        <li style="display: flex; justify-content: space-between; align-items: center; padding: var(--space-sm) 0;">
            <div style="flex: 1; min-width: 0;">
                <a href="/?doc=${doc.document_id}" target="_blank" 
                   style="display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--accent); text-decoration: none; transition: color 0.15s ease;" 
                   title="Click to view: ${escapeHtml(doc.filename)}"
                   onmouseover="this.style.color='var(--text-primary)'" 
                   onmouseout="this.style.color='var(--accent)'">
                    📄 ${escapeHtml(truncate(doc.filename, 50))}
                </a>
                <span style="font-size: 0.75rem; color: var(--text-muted);">
                    Last: ${formatTimeAgo(doc.last_generated)}
                </span>
            </div>
            <div style="text-align: right; flex-shrink: 0; margin-left: var(--space-md);">
                <span class="badge badge-info" title="Generated">${doc.generated_count}x</span>
                ${doc.cached_count > 0 ? `<span class="badge badge-success" title="Cached">${doc.cached_count} cached</span>` : ''}
            </div>
        </li>
    `).join('');
}

function renderSecurityStats(data) {
    document.getElementById('security-stats').innerHTML = `
        <div class="stat-card ${data.total_security_events > 1000 ? 'warning' : ''}">
            <div class="stat-value">${formatNumber(data.total_security_events)}</div>
            <div class="stat-label">Security Events</div>
        </div>
        <div class="stat-card ${data.rate_limit_violations > 100 ? 'warning' : ''}">
            <div class="stat-value">${formatNumber(data.rate_limit_violations)}</div>
            <div class="stat-label">Rate Limit Violations</div>
        </div>
        <div class="stat-card ${data.suspicious_activities > 0 ? 'danger' : 'success'}">
            <div class="stat-value">${formatNumber(data.suspicious_activities)}</div>
            <div class="stat-label">Suspicious Activities</div>
        </div>
        <div class="stat-card ${data.blocked_ips.length > 0 ? 'danger' : 'success'}">
            <div class="stat-value">${data.blocked_ips.length}</div>
            <div class="stat-label">Blocked IPs</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${data.blocked_sessions}</div>
            <div class="stat-label">Blocked Sessions</div>
        </div>
    `;
}

function renderSecurityEvents(events) {
    const el = document.getElementById('security-events');
    if (!events || Object.keys(events).length === 0) {
        el.innerHTML = '<li><span class="key">No events</span></li>';
        return;
    }
    
    const sorted = Object.entries(events).sort((a, b) => b[1] - a[1]).slice(0, 10);
    el.innerHTML = sorted.map(([type, count]) => `
        <li>
            <span class="key">${type.replace(/_/g, ' ')}</span>
            <span class="value">${formatNumber(count)}</span>
        </li>
    `).join('');
}

function renderSecurityIPs(ips) {
    const el = document.getElementById('security-ips');
    if (!ips || ips.length === 0) {
        el.innerHTML = '<li><span class="key">No IPs flagged</span></li>';
        return;
    }
    
    el.innerHTML = ips.map(i => `
        <li>
            <span class="key" style="font-family: var(--font-mono);">${i.ip}</span>
            <span class="value">${formatNumber(i.count)}</span>
        </li>
    `).join('');
}

function renderHighSeverityEvents(events) {
    const el = document.getElementById('high-severity-events');
    if (!events || events.length === 0) {
        el.innerHTML = '<div style="color: var(--success); text-align: center; padding: var(--space-lg);">✓ No high-severity events</div>';
        return;
    }
    
    el.innerHTML = events.reverse().map(e => `
        <div class="event-item">
            <span class="event-time">${formatTimestamp(e.timestamp)}</span>
            <span class="event-type"><span class="badge badge-danger">${e.event_type}</span></span>
            <span class="event-message">${escapeHtml(e.message || '')} • ${e.client_ip || 'Unknown IP'}</span>
        </div>
    `).join('');
}

function renderVisitorStats(data) {
    document.getElementById('visitor-stats').innerHTML = `
        <div class="stat-card">
            <div class="stat-value">${formatNumber(data.unique_visitors_today)}</div>
            <div class="stat-label">Unique Visitors Today</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${formatNumber(data.unique_visitors_week)}</div>
            <div class="stat-label">Unique Visitors (7 days)</div>
        </div>
    `;
}

function renderVisitorsChart(dailyData) {
    const container = document.getElementById('visitors-chart');
    if (!dailyData || dailyData.length === 0) {
        container.innerHTML = '<div class="loading">No data available</div>';
        return;
    }
    
    const maxVisitors = Math.max(...dailyData.map(d => d.unique_visitors));
    
    container.innerHTML = `
        <div class="simple-chart">
            ${dailyData.map(d => {
                const height = maxVisitors > 0 ? (d.unique_visitors / maxVisitors * 100) : 0;
                return `<div class="chart-bar" style="height: ${Math.max(2, height)}%;" data-tooltip="${d.date}: ${d.unique_visitors} visitors"></div>`;
            }).join('')}
        </div>
    `;
}

function renderBrowsers(browsers) {
    const el = document.getElementById('browsers');
    if (!browsers || Object.keys(browsers).length === 0) {
        el.innerHTML = '<li><span class="key">No data</span></li>';
        return;
    }
    
    const sorted = Object.entries(browsers).sort((a, b) => b[1] - a[1]);
    el.innerHTML = sorted.map(([browser, count]) => `
        <li>
            <span class="key">${browser}</span>
            <span class="value">${formatNumber(count)}</span>
        </li>
    `).join('');
}

function renderReferrers(referrers) {
    const el = document.getElementById('referrers');
    if (!referrers || referrers.length === 0) {
        el.innerHTML = '<li><span class="key">No external referrers</span></li>';
        return;
    }
    
    el.innerHTML = referrers.map(r => `
        <li>
            <span class="key">${escapeHtml(r.domain)}</span>
            <span class="value">${formatNumber(r.count)}</span>
        </li>
    `).join('');
}

function renderSystemStats(data) {
    const llmStatus = data.llm_available 
        ? '<span class="badge badge-success">Available</span>' 
        : '<span class="badge badge-warning">Unavailable</span>';
    
    const indexStatus = data.is_indexing 
        ? '<span class="badge badge-warning">Indexing...</span>' 
        : '<span class="badge badge-success">Ready</span>';
    
    document.getElementById('system-stats').innerHTML = `
        <div class="stat-card">
            <div class="stat-value">${data.system.platform}</div>
            <div class="stat-label">Platform</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">Python ${data.system.python_version}</div>
            <div class="stat-label">Runtime</div>
        </div>
        <div class="stat-card ${data.cpu.usage_percent > 80 ? 'danger' : data.cpu.usage_percent > 50 ? 'warning' : 'success'}">
            <div class="stat-value">${data.cpu.usage_percent.toFixed(1)}%</div>
            <div class="stat-label">CPU Usage (${data.cpu.cores} cores)</div>
        </div>
        <div class="stat-card ${data.memory.used_percent > 80 ? 'danger' : data.memory.used_percent > 60 ? 'warning' : 'success'}">
            <div class="stat-value">${data.memory.used_percent.toFixed(1)}%</div>
            <div class="stat-label">Memory (${data.memory.total_gb} GB)</div>
        </div>
        <div class="stat-card ${data.disk.used_percent > 80 ? 'danger' : data.disk.used_percent > 60 ? 'warning' : 'success'}">
            <div class="stat-value">${data.disk.used_percent.toFixed(1)}%</div>
            <div class="stat-label">Disk (${data.disk.free_gb.toFixed(1)} GB free)</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" style="font-size: 1.5rem;">${llmStatus}</div>
            <div class="stat-label">LLM Status</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" style="font-size: 1.5rem;">${indexStatus}</div>
            <div class="stat-label">Index Status</div>
        </div>
    `;
}

function renderSystemResources(data) {
    const el = document.getElementById('system-resources');
    
    el.innerHTML = `
        <div class="progress-container">
            <div class="progress-label">
                <span>CPU Usage</span>
                <span>${data.cpu.usage_percent.toFixed(1)}%</span>
            </div>
            <div class="progress-bar">
                <div class="progress-fill ${data.cpu.usage_percent > 80 ? 'danger' : data.cpu.usage_percent > 50 ? 'warning' : ''}" 
                     style="width: ${data.cpu.usage_percent}%"></div>
            </div>
        </div>
        <div class="progress-container">
            <div class="progress-label">
                <span>Memory (${data.memory.available_gb.toFixed(1)} GB available)</span>
                <span>${data.memory.used_percent.toFixed(1)}%</span>
            </div>
            <div class="progress-bar">
                <div class="progress-fill ${data.memory.used_percent > 80 ? 'danger' : data.memory.used_percent > 60 ? 'warning' : ''}" 
                     style="width: ${data.memory.used_percent}%"></div>
            </div>
        </div>
        <div class="progress-container">
            <div class="progress-label">
                <span>Disk (${data.disk.free_gb.toFixed(1)} GB free)</span>
                <span>${data.disk.used_percent.toFixed(1)}%</span>
            </div>
            <div class="progress-bar">
                <div class="progress-fill ${data.disk.used_percent > 80 ? 'danger' : data.disk.used_percent > 60 ? 'warning' : ''}" 
                     style="width: ${data.disk.used_percent}%"></div>
            </div>
        </div>
    `;
}

function renderLogSizes(logs) {
    const el = document.getElementById('log-sizes');
    if (!logs || Object.keys(logs).length === 0) {
        el.innerHTML = '<li><span class="key">No logs</span></li>';
        return;
    }
    
    el.innerHTML = Object.entries(logs).map(([file, size]) => `
        <li>
            <span class="key">${file}</span>
            <span class="value">${size.toFixed(2)} MB</span>
        </li>
    `).join('');
}

function renderDbStats(db) {
    const el = document.getElementById('db-stats');
    if (!db) {
        el.innerHTML = '<li><span class="key">Database unavailable</span></li>';
        return;
    }
    
    el.innerHTML = `
        <li>
            <span class="key">Total Documents</span>
            <span class="value">${formatNumber(db.total_documents)}</span>
        </li>
        <li>
            <span class="key">Total Pages</span>
            <span class="value">${formatNumber(db.total_pages)}</span>
        </li>
        <li>
            <span class="key">Vector Chunks</span>
            <span class="value">${formatNumber(db.vector_chunks)}</span>
        </li>
    `;
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

function formatNumber(num) {
    if (num === null || num === undefined) return '0';
    return num.toLocaleString();
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function truncate(str, len) {
    if (!str) return '';
    return str.length > len ? str.substring(0, len) + '...' : str;
}

function formatTimestamp(iso) {
    if (!iso) return '';
    try {
        const date = new Date(iso);
        return date.toLocaleString([], { 
            month: 'short', 
            day: 'numeric',
            hour: '2-digit', 
            minute: '2-digit',
            second: '2-digit'
        });
    } catch {
        return iso;
    }
}

function formatTimeAgo(iso) {
    if (!iso) return 'Never';
    try {
        const date = new Date(iso);
        const now = new Date();
        const diffMs = now - date;
        const diffSecs = Math.floor(diffMs / 1000);
        const diffMins = Math.floor(diffSecs / 60);
        const diffHours = Math.floor(diffMins / 60);
        const diffDays = Math.floor(diffHours / 24);
        
        if (diffSecs < 60) return 'Just now';
        if (diffMins < 60) return `${diffMins}m ago`;
        if (diffHours < 24) return `${diffHours}h ago`;
        if (diffDays < 7) return `${diffDays}d ago`;
        return date.toLocaleDateString();
    } catch {
        return iso;
    }
}

// ============================================================================
// IP GEOLOCATION AND RECENT REQUESTS
// ============================================================================

function renderRecentRequestsTable(requests) {
    const tbody = document.getElementById('recent-requests-body');
    if (!tbody) return;
    
    if (!requests || requests.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">No recent requests</td></tr>';
        return;
    }
    
    // Render with geo data from backend
    tbody.innerHTML = requests.map((req, i) => {
        const statusClass = req.status_code < 400 ? 'badge-success' : req.status_code < 500 ? 'badge-warning' : 'badge-danger';
        const timeAgo = formatTimeAgo(req.timestamp);
        const uaShort = req.user_agent ? (req.user_agent.length > 40 ? req.user_agent.substring(0, 40) + '...' : req.user_agent) : 'Unknown';
        const pathShort = req.path ? (req.path.length > 30 ? req.path.substring(0, 30) + '...' : req.path) : '/';
        const location = req.geo_location || 'Unknown';
        const isp = req.geo_isp || '';
        
        return `
            <tr>
                <td style="white-space: nowrap; font-size: 0.8rem; color: var(--text-muted);">${timeAgo}</td>
                <td style="font-family: monospace; font-size: 0.85rem;">${escapeHtml(req.client_ip)}</td>
                <td style="font-size: 0.85rem;" title="${escapeHtml(isp)}">${escapeHtml(location)}</td>
                <td title="${escapeHtml(req.path || '')}" style="font-size: 0.85rem;">${escapeHtml(pathShort)}</td>
                <td title="${escapeHtml(req.user_agent || '')}" style="font-size: 0.8rem; color: var(--text-muted); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(uaShort)}</td>
                <td><span class="badge ${statusClass}">${req.status_code || '?'}</span></td>
            </tr>
        `;
    }).join('');
}

function renderTopIpsTable(topIps) {
    const tbody = document.getElementById('top-ips-body');
    if (!tbody) return;
    
    if (!topIps || topIps.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No IP data available</td></tr>';
        return;
    }
    
    // Render with geo data from backend
    tbody.innerHTML = topIps.map(item => {
        const location = item.geo_location || 'Unknown';
        const isp = item.geo_isp || 'Unknown';
        
        return `
            <tr>
                <td style="font-family: monospace; font-size: 0.9rem;">${escapeHtml(item.ip)}</td>
                <td style="font-size: 0.9rem;">${escapeHtml(location)}</td>
                <td style="font-size: 0.85rem; color: var(--text-muted);" title="${escapeHtml(isp)}">${escapeHtml(truncate(isp, 30))}</td>
                <td style="text-align: right;">
                    <span class="badge badge-info">${formatNumber(item.count)}</span>
                </td>
            </tr>
        `;
    }).join('');
}

// ============================================================================
// LOG MANAGEMENT FUNCTIONS
// ============================================================================

async function clearLogs(logType) {
    const logNames = {
        'access': 'Requests (access.log)',
        'audit': 'Search/Documents (audit.log)',
        'security': 'Security (security.log)',
        'error': 'Errors (error.log)',
        'all': 'ALL logs'
    };
    
    const logName = logNames[logType] || logType;
    
    if (!confirm(`Are you sure you want to clear ${logName}?\n\nA backup will be created, but this action cannot be easily undone.`)) {
        return;
    }
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/logs/clear?log_type=${logType}`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to clear logs');
        }
        
        const data = await response.json();
        alert(`✓ ${data.message}\n\nCleared: ${data.cleared.join(', ')}`);
        
        // Refresh system data to update log sizes
        loadSystemData();
        
    } catch (error) {
        console.error('Error clearing logs:', error);
        alert(`Error: ${error.message}`);
    }
}

// ============================================================================
// FEEDBACK FUNCTIONS
// ============================================================================

// Store all feedback data for filtering
let allFeedbackData = [];
let feedbackSelectedIds = new Set();
let feedbackSortField = 'timestamp';
let feedbackSortAsc = false; // newest first by default
let feedbackSearchDebounce = null;

async function loadFeedbackData() {
    try {
        const response = await authFetch(`${API_BASE}/feedback`);
        if (!response.ok) throw new Error('Failed to load feedback');
        const data = await response.json();
        
        allFeedbackData = data.feedback || [];
        
        renderFeedbackStats(data);
        renderFeedbackTypes(data.type_counts);
        applyFeedbackFilterAndSort();
        
        setupFeedbackFilter();
    } catch (error) {
        console.error('Error loading feedback:', error);
        document.getElementById('feedback-stats').innerHTML = 
            '<div class="stat-card danger"><div class="stat-value">Error</div><div class="stat-label">Failed to load feedback</div></div>';
    }
}

function setupFeedbackFilter() {
    const typeFilter = document.getElementById('feedback-type-filter');
    const statusFilter = document.getElementById('feedback-status-filter');
    const searchInput = document.getElementById('feedback-search');
    
    if (typeFilter) {
        typeFilter.removeEventListener('change', applyFeedbackFilterAndSort);
        typeFilter.addEventListener('change', applyFeedbackFilterAndSort);
    }
    if (statusFilter) {
        statusFilter.removeEventListener('change', applyFeedbackFilterAndSort);
        statusFilter.addEventListener('change', applyFeedbackFilterAndSort);
    }
    if (searchInput) {
        searchInput.removeEventListener('input', handleFeedbackSearch);
        searchInput.addEventListener('input', handleFeedbackSearch);
    }
    
    const bulkStatusSelect = document.getElementById('feedback-bulk-status');
    if (bulkStatusSelect) {
        bulkStatusSelect.removeEventListener('change', handleBulkStatusChange);
        bulkStatusSelect.addEventListener('change', handleBulkStatusChange);
    }
}

function handleFeedbackSearch() {
    clearTimeout(feedbackSearchDebounce);
    feedbackSearchDebounce = setTimeout(applyFeedbackFilterAndSort, 250);
}

function applyFeedbackFilterAndSort() {
    const typeFilter = document.getElementById('feedback-type-filter');
    const statusFilter = document.getElementById('feedback-status-filter');
    const searchInput = document.getElementById('feedback-search');
    
    const selectedType = typeFilter ? typeFilter.value : 'all';
    const selectedStatus = statusFilter ? statusFilter.value : 'all';
    const searchTerm = (searchInput ? searchInput.value : '').toLowerCase().trim();
    
    let filtered = allFeedbackData;
    
    if (selectedType !== 'all') {
        filtered = filtered.filter(fb => fb.type === selectedType);
    }
    if (selectedStatus !== 'all') {
        filtered = filtered.filter(fb => (fb.status || 'new') === selectedStatus);
    }
    if (searchTerm) {
        filtered = filtered.filter(fb =>
            (fb.email || '').toLowerCase().includes(searchTerm) ||
            (fb.message || '').toLowerCase().includes(searchTerm)
        );
    }
    
    filtered = sortFeedbackArray(filtered, feedbackSortField, feedbackSortAsc);
    renderFeedbackTable(filtered);
    updateSortIcons();
}

function sortFeedbackArray(arr, field, asc) {
    return [...arr].sort((a, b) => {
        let valA = a[field] || '';
        let valB = b[field] || '';
        if (field === 'status') { valA = valA || 'new'; valB = valB || 'new'; }
        if (typeof valA === 'string') valA = valA.toLowerCase();
        if (typeof valB === 'string') valB = valB.toLowerCase();
        if (valA < valB) return asc ? -1 : 1;
        if (valA > valB) return asc ? 1 : -1;
        return 0;
    });
}

function sortFeedbackBy(field) {
    if (feedbackSortField === field) {
        feedbackSortAsc = !feedbackSortAsc;
    } else {
        feedbackSortField = field;
        feedbackSortAsc = field === 'timestamp' ? false : true;
    }
    applyFeedbackFilterAndSort();
}

function updateSortIcons() {
    ['timestamp', 'type', 'status', 'email'].forEach(f => {
        const icon = document.getElementById(`sort-icon-${f}`);
        if (!icon) return;
        if (f === feedbackSortField) {
            icon.textContent = feedbackSortAsc ? '▲' : '▼';
            icon.style.opacity = '1';
        } else {
            icon.textContent = '▼';
            icon.style.opacity = '0.4';
        }
    });
}

// --- Selection & Bulk ---

function toggleFeedbackCheckbox(id) {
    if (feedbackSelectedIds.has(id)) {
        feedbackSelectedIds.delete(id);
    } else {
        feedbackSelectedIds.add(id);
    }
    updateBulkBar();
    const selectAll = document.getElementById('feedback-select-all');
    if (selectAll) {
        const visibleCheckboxes = document.querySelectorAll('.feedback-row-checkbox');
        selectAll.checked = visibleCheckboxes.length > 0 && feedbackSelectedIds.size >= visibleCheckboxes.length;
    }
}

function toggleAllFeedbackCheckboxes(checked) {
    const checkboxes = document.querySelectorAll('.feedback-row-checkbox');
    checkboxes.forEach(cb => {
        const id = cb.dataset.feedbackId;
        if (checked) {
            feedbackSelectedIds.add(id);
        } else {
            feedbackSelectedIds.delete(id);
        }
        cb.checked = checked;
    });
    updateBulkBar();
}

function clearFeedbackSelection() {
    feedbackSelectedIds.clear();
    document.querySelectorAll('.feedback-row-checkbox').forEach(cb => cb.checked = false);
    const selectAll = document.getElementById('feedback-select-all');
    if (selectAll) selectAll.checked = false;
    updateBulkBar();
}

function updateBulkBar() {
    const bar = document.getElementById('feedback-bulk-bar');
    const countEl = document.getElementById('feedback-selected-count');
    if (!bar) return;
    
    if (feedbackSelectedIds.size > 0) {
        bar.style.display = 'flex';
        countEl.textContent = `${feedbackSelectedIds.size} selected`;
    } else {
        bar.style.display = 'none';
        const bulkStatus = document.getElementById('feedback-bulk-status');
        if (bulkStatus) bulkStatus.value = '';
    }
}

async function handleBulkStatusChange() {
    const select = document.getElementById('feedback-bulk-status');
    const newStatus = select.value;
    if (!newStatus || feedbackSelectedIds.size === 0) return;
    
    const count = feedbackSelectedIds.size;
    if (!confirm(`Set ${count} feedback item(s) to "${newStatus}"?`)) {
        select.value = '';
        return;
    }
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/feedback/bulk/status`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: [...feedbackSelectedIds], status: newStatus })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Bulk status update failed');
        }
        
        feedbackSelectedIds.forEach(id => {
            const fb = allFeedbackData.find(f => f.id === id);
            if (fb) fb.status = newStatus;
        });
        
        clearFeedbackSelection();
        applyFeedbackFilterAndSort();
    } catch (error) {
        console.error('Bulk status error:', error);
        alert(`Error: ${error.message}`);
        loadFeedbackData();
    }
    select.value = '';
}

async function bulkDeleteFeedback() {
    if (feedbackSelectedIds.size === 0) return;
    
    const count = feedbackSelectedIds.size;
    if (!confirm(`Permanently delete ${count} feedback item(s)? This cannot be undone.`)) return;
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/feedback/bulk/delete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: [...feedbackSelectedIds] })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Bulk delete failed');
        }
        
        allFeedbackData = allFeedbackData.filter(fb => !feedbackSelectedIds.has(fb.id));
        clearFeedbackSelection();
        applyFeedbackFilterAndSort();
        loadFeedbackData();
    } catch (error) {
        console.error('Bulk delete error:', error);
        alert(`Error: ${error.message}`);
        loadFeedbackData();
    }
}

async function updateFeedbackStatus(feedbackId, newStatus) {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/feedback/${feedbackId}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: newStatus })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to update status');
        }
        
        const fb = allFeedbackData.find(f => f.id === feedbackId);
        if (fb) fb.status = newStatus;
        
    } catch (error) {
        console.error('Error updating feedback status:', error);
        alert(`Error: ${error.message}`);
        loadFeedbackData();
    }
}

function renderFeedbackStats(data) {
    const typeIcons = {
        'bug': '🐛',
        'feature': '✨',
        'content': '📄',
        'pinned': '📌',
        'other': '💬'
    };
    
    document.getElementById('feedback-stats').innerHTML = `
        <div class="stat-card">
            <div class="stat-value">${formatNumber(data.total_feedback)}</div>
            <div class="stat-label">Total Feedback</div>
        </div>
        <div class="stat-card ${data.type_counts?.bug > 0 ? 'warning' : ''}">
            <div class="stat-value">${formatNumber(data.type_counts?.bug || 0)}</div>
            <div class="stat-label">🐛 Bug Reports</div>
        </div>
        <div class="stat-card info">
            <div class="stat-value">${formatNumber(data.type_counts?.feature || 0)}</div>
            <div class="stat-label">✨ Feature Requests</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${formatNumber(data.type_counts?.content || 0)}</div>
            <div class="stat-label">📄 Content Issues</div>
        </div>
        <div class="stat-card accent">
            <div class="stat-value">${formatNumber(data.type_counts?.pinned || 0)}</div>
            <div class="stat-label">📌 Pinned Suggestions</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${formatNumber(data.type_counts?.other || 0)}</div>
            <div class="stat-label">💬 Other</div>
        </div>
    `;
}

function renderFeedbackTypes(types) {
    const el = document.getElementById('feedback-types');
    if (!el) return;
    
    if (!types || Object.keys(types).length === 0) {
        el.innerHTML = '<li><span class="key">No feedback yet</span></li>';
        return;
    }
    
    const typeLabels = {
        'bug': '🐛 Bug Reports',
        'feature': '✨ Feature Requests',
        'content': '📄 Content Issues',
        'pinned': '📌 Pinned Suggestions',
        'other': '💬 Other Feedback'
    };
    
    const sorted = Object.entries(types).sort((a, b) => b[1] - a[1]);
    el.innerHTML = sorted.map(([type, count]) => {
        const label = typeLabels[type] || type;
        let badgeClass = 'badge-info';
        if (type === 'bug') badgeClass = 'badge-warning';
        if (type === 'feature') badgeClass = 'badge-success';
        if (type === 'content') badgeClass = 'badge-info';
        if (type === 'pinned') badgeClass = 'badge-accent';
        
        return `
            <li>
                <span class="key">${label}</span>
                <span class="badge ${badgeClass}">${formatNumber(count)}</span>
            </li>
        `;
    }).join('');
}

function renderFeedbackTable(feedback) {
    const tbody = document.getElementById('feedback-table-body');
    if (!tbody) return;
    
    if (!feedback || feedback.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: var(--space-xl);">No feedback submissions found</td></tr>';
        return;
    }
    
    const typeLabels = {
        'bug': '<span class="badge badge-warning">🐛 Bug</span>',
        'feature': '<span class="badge badge-success">✨ Feature</span>',
        'content': '<span class="badge badge-info">📄 Content</span>',
        'pinned': '<span class="badge badge-accent">📌 Pinned</span>',
        'other': '<span class="badge">💬 Other</span>'
    };
    
    tbody.innerHTML = feedback.map(fb => {
        const date = fb.timestamp ? new Date(fb.timestamp).toLocaleString([], {
            month: 'short',
            day: 'numeric',
            year: 'numeric',
            hour: '2-digit',
            minute: '2-digit'
        }) : 'Unknown';
        
        const typeLabel = typeLabels[fb.type] || `<span class="badge">${fb.type}</span>`;
        const status = fb.status || 'new';
        const email = fb.email || '<span style="color: var(--text-muted);">—</span>';
        const messagePreview = fb.message ? truncate(fb.message, 60) : '';
        const ip = fb.ip || 'Unknown';
        const isChecked = feedbackSelectedIds.has(fb.id) ? 'checked' : '';
        
        return `
            <tr style="${isChecked ? 'background: var(--accent-glow);' : ''}">
                <td style="text-align: center;"><input type="checkbox" class="feedback-row-checkbox" data-feedback-id="${fb.id}" ${isChecked} onchange="toggleFeedbackCheckbox('${fb.id}')"></td>
                <td style="white-space: nowrap; font-size: 0.85rem; color: var(--text-muted);">${date}</td>
                <td>${typeLabel}</td>
                <td>
                    <select class="feedback-status-select" data-feedback-id="${fb.id}" onchange="updateFeedbackStatus('${fb.id}', this.value)" style="background: var(--bg-tertiary); border: 1px solid var(--border); color: var(--text-primary); padding: 4px 8px; border-radius: 4px; font-size: 0.8rem; cursor: pointer;">
                        <option value="new" ${status === 'new' ? 'selected' : ''}>🔴 New</option>
                        <option value="read" ${status === 'read' ? 'selected' : ''}>🔵 Read</option>
                        <option value="in-progress" ${status === 'in-progress' ? 'selected' : ''}>🟡 In Progress</option>
                        <option value="completed" ${status === 'completed' ? 'selected' : ''}>🟢 Completed</option>
                        <option value="archived" ${status === 'archived' ? 'selected' : ''}>⚫ Archived</option>
                    </select>
                </td>
                <td style="font-size: 0.9rem;">${escapeHtml(typeof email === 'string' ? email : '—')}</td>
                <td>
                    <span class="feedback-message-preview" onclick="openFeedbackModal('${fb.id}')" title="Click to view full message">
                        ${escapeHtml(messagePreview)}
                    </span>
                </td>
                <td style="font-family: monospace; font-size: 0.8rem; color: var(--text-muted);">${escapeHtml(ip)}</td>
                <td>
                    <div style="display: flex; gap: 4px;">
                        <button class="btn-icon" onclick="openFeedbackModal('${fb.id}')" title="View details">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                                <circle cx="12" cy="12" r="3"/>
                            </svg>
                        </button>
                        <button class="btn-icon delete" onclick="deleteFeedback('${fb.id}')" title="Delete">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="3,6 5,6 21,6"/>
                                <path d="M19,6v14a2,2,0,0,1-2,2H7a2,2,0,0,1-2-2V6M8,6V4a2,2,0,0,1,2-2h4a2,2,0,0,1,2,2V6"/>
                            </svg>
                        </button>
                    </div>
                </td>
            </tr>
        `;
    }).join('');
}

function openFeedbackModal(feedbackId) {
    const feedback = allFeedbackData.find(fb => fb.id === feedbackId);
    if (!feedback) return;
    
    const modal = document.getElementById('feedback-modal');
    const body = document.getElementById('feedback-modal-body');
    
    const typeLabels = {
        'bug': '🐛 Bug Report',
        'feature': '✨ Feature Request',
        'content': '📄 Content Issue',
        'pinned': '📌 Pinned Suggestion',
        'other': '💬 Other Feedback'
    };
    
    const date = feedback.timestamp ? new Date(feedback.timestamp).toLocaleString() : 'Unknown';
    const typeLabel = typeLabels[feedback.type] || feedback.type;
    const status = feedback.status || 'new';
    
    body.innerHTML = `
        <div class="feedback-detail">
            <div class="feedback-detail-row">
                <span class="feedback-detail-label">Date</span>
                <span class="feedback-detail-value">${date}</span>
            </div>
            <div class="feedback-detail-row">
                <span class="feedback-detail-label">Type</span>
                <span class="feedback-detail-value">${typeLabel}</span>
            </div>
            <div class="feedback-detail-row">
                <span class="feedback-detail-label">Status</span>
                <span class="feedback-detail-value">
                    <select id="modal-status-select" onchange="updateFeedbackStatusFromModal('${feedback.id}', this.value)" style="background: var(--bg-tertiary); border: 1px solid var(--border); color: var(--text-primary); padding: 6px 12px; border-radius: 4px; cursor: pointer;">
                        <option value="new" ${status === 'new' ? 'selected' : ''}>🔴 New</option>
                        <option value="read" ${status === 'read' ? 'selected' : ''}>🔵 Read</option>
                        <option value="in-progress" ${status === 'in-progress' ? 'selected' : ''}>🟡 In Progress</option>
                        <option value="completed" ${status === 'completed' ? 'selected' : ''}>🟢 Completed</option>
                        <option value="archived" ${status === 'archived' ? 'selected' : ''}>⚫ Archived</option>
                    </select>
                </span>
            </div>
            <div class="feedback-detail-row">
                <span class="feedback-detail-label">Email</span>
                <span class="feedback-detail-value">${escapeHtml(feedback.email || 'Not provided')}</span>
            </div>
            <div class="feedback-detail-row">
                <span class="feedback-detail-label">IP</span>
                <span class="feedback-detail-value" style="font-family: var(--font-mono);">${escapeHtml(feedback.ip || 'Unknown')}</span>
            </div>
            <div class="feedback-detail-row">
                <span class="feedback-detail-label">ID</span>
                <span class="feedback-detail-value" style="font-family: var(--font-mono); font-size: 0.85rem; color: var(--text-muted);">${escapeHtml(feedback.id)}</span>
            </div>
            <div style="margin-top: var(--space-md);">
                <span class="feedback-detail-label" style="display: block; margin-bottom: var(--space-sm);">Message</span>
                <div class="feedback-message-full">${escapeHtml(feedback.message || 'No message')}</div>
            </div>
            <div style="margin-top: var(--space-lg); display: flex; gap: var(--space-md); justify-content: flex-end;">
                <button class="btn btn-danger" onclick="deleteFeedback('${feedback.id}'); closeFeedbackModal();">
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="3,6 5,6 21,6"/>
                        <path d="M19,6v14a2,2,0,0,1-2,2H7a2,2,0,0,1-2-2V6M8,6V4a2,2,0,0,1,2-2h4a2,2,0,0,1,2,2V6"/>
                    </svg>
                    Delete Feedback
                </button>
                <button class="btn" onclick="closeFeedbackModal()">Close</button>
            </div>
        </div>
    `;
    
    modal.style.display = 'flex';
    
    // Close on escape key
    document.addEventListener('keydown', handleModalEscape);
    
    // Close on overlay click
    modal.addEventListener('click', handleModalOverlayClick);
}

async function updateFeedbackStatusFromModal(feedbackId, newStatus) {
    await updateFeedbackStatus(feedbackId, newStatus);
    applyFeedbackFilterAndSort();
}

function closeFeedbackModal() {
    const modal = document.getElementById('feedback-modal');
    modal.style.display = 'none';
    document.removeEventListener('keydown', handleModalEscape);
    modal.removeEventListener('click', handleModalOverlayClick);
}

function handleModalEscape(e) {
    if (e.key === 'Escape') {
        closeFeedbackModal();
    }
}

function handleModalOverlayClick(e) {
    if (e.target.classList.contains('modal-overlay')) {
        closeFeedbackModal();
    }
}

async function deleteFeedback(feedbackId) {
    if (!confirm('Are you sure you want to delete this feedback? This action cannot be undone.')) {
        return;
    }
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/feedback/${feedbackId}`, {
            method: 'DELETE'
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to delete feedback');
        }
        
        allFeedbackData = allFeedbackData.filter(fb => fb.id !== feedbackId);
        feedbackSelectedIds.delete(feedbackId);
        applyFeedbackFilterAndSort();
        loadFeedbackData();
        
    } catch (error) {
        console.error('Error deleting feedback:', error);
        alert(`Error: ${error.message}`);
    }
}

// ============================================================================
// DOCUMENT MANAGEMENT FUNCTIONS (Re-download / Re-extract)
// ============================================================================

let _selectedMgmtDoc = null;
let _mgmtSearchResults = {};

async function searchDocumentForManagement() {
    const input = document.getElementById('doc-mgmt-search');
    const query = input ? input.value.trim() : '';
    const resultsEl = document.getElementById('doc-mgmt-results');
    const detailEl = document.getElementById('doc-mgmt-detail');
    if (!resultsEl) return;

    if (!query) {
        resultsEl.style.display = 'none';
        detailEl.style.display = 'none';
        _selectedMgmtDoc = null;
        _mgmtSearchResults = {};
        return;
    }

    resultsEl.style.display = 'block';
    resultsEl.innerHTML = '<div style="padding: var(--space-md); text-align: center;"><span class="spinner"></span> Searching...</div>';
    detailEl.style.display = 'none';
    _selectedMgmtDoc = null;
    _mgmtSearchResults = {};

    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents-visibility?search=${encodeURIComponent(query)}&limit=20`);
        if (!response.ok) throw new Error('Search failed');
        const data = await response.json();

        if (!data.documents || data.documents.length === 0) {
            resultsEl.innerHTML = '<div style="padding: var(--space-md); color: var(--text-muted); text-align: center;">No documents found</div>';
            return;
        }

        data.documents.forEach(doc => { _mgmtSearchResults[doc.id] = doc; });

        resultsEl.innerHTML = data.documents.map(doc => `
            <div onclick="selectMgmtDocument('${escapeHtml(doc.id)}')"
                 style="padding: var(--space-sm) var(--space-md); border-bottom: 1px solid var(--border); cursor: pointer; transition: background 0.15s;"
                 onmouseenter="this.style.background='var(--bg-tertiary)'" onmouseleave="this.style.background=''">
                <div style="font-family: var(--font-mono); font-size: 0.85rem; color: var(--text-primary);">
                    ${doc.is_hidden === 1 ? '🔒 ' : ''}${escapeHtml(doc.filename)}
                </div>
                <div style="font-size: 0.75rem; color: var(--text-muted);">
                    ${escapeHtml(doc.category || 'Unknown')} &bull; ${escapeHtml(doc.subcategory || '')} &bull; ${escapeHtml(doc.file_type || '')}
                </div>
            </div>
        `).join('');
    } catch (error) {
        console.error('Error searching documents:', error);
        resultsEl.innerHTML = '<div style="padding: var(--space-md); color: var(--danger); text-align: center;">Search failed</div>';
    }
}

function selectMgmtDocument(docId) {
    const doc = _mgmtSearchResults[docId] || _selectedMgmtDoc;
    if (!doc) return;
    _selectedMgmtDoc = doc;

    const resultsEl = document.getElementById('doc-mgmt-results');
    const detailEl = document.getElementById('doc-mgmt-detail');
    const infoEl = document.getElementById('doc-mgmt-info');
    const feedbackEl = document.getElementById('doc-mgmt-feedback');

    if (resultsEl) resultsEl.style.display = 'none';
    if (feedbackEl) feedbackEl.style.display = 'none';

    const isEfta = doc.filename && doc.filename.toUpperCase().startsWith('EFTA');
    const redownloadBtn = document.getElementById('doc-mgmt-redownload-btn');
    if (redownloadBtn) redownloadBtn.style.display = isEfta ? '' : 'none';

    infoEl.innerHTML = `
        <div style="font-family: var(--font-mono); font-size: 1rem; font-weight: 500; margin-bottom: var(--space-xs);">
            ${doc.is_hidden === 1 ? '🔒 ' : ''}${escapeHtml(doc.filename)}
        </div>
        <div style="font-size: 0.85rem; color: var(--text-muted); display: flex; flex-wrap: wrap; gap: var(--space-sm) var(--space-lg);">
            <span><strong>Category:</strong> ${escapeHtml(doc.category || 'Unknown')}</span>
            <span><strong>Subcategory:</strong> ${escapeHtml(doc.subcategory || '-')}</span>
            <span><strong>Type:</strong> ${escapeHtml(doc.file_type || '-')}</span>
            <span><strong>Pages:</strong> ${doc.page_count ?? '-'}</span>
            <span><strong>Characters:</strong> ${(doc.char_count ?? 0).toLocaleString()}</span>
            <span><strong>ID:</strong> <code style="font-size: 0.75rem;">${escapeHtml(doc.id)}</code></span>
        </div>
    `;
    detailEl.style.display = 'block';
}

function _showMgmtFeedback(message, isError) {
    const el = document.getElementById('doc-mgmt-feedback');
    if (!el) return;
    el.style.display = 'block';
    el.style.background = isError ? 'var(--danger-dim)' : 'var(--success-dim, var(--accent-glow))';
    el.style.color = isError ? 'var(--danger)' : 'var(--success, var(--accent))';
    el.style.border = `1px solid ${isError ? 'var(--danger)' : 'var(--success, var(--accent))'}`;
    el.textContent = message;
}

async function redownloadDocument() {
    if (!_selectedMgmtDoc) return;
    const docId = _selectedMgmtDoc.id;
    const btn = document.getElementById('doc-mgmt-redownload-btn');
    const origHtml = btn.innerHTML;

    if (!confirm(`Re-download ${_selectedMgmtDoc.filename} from the DOJ website?\n\nThis will replace the local file with the latest version.`)) return;

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;"></span> Downloading...';
    document.getElementById('doc-mgmt-feedback').style.display = 'none';

    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents/${encodeURIComponent(docId)}/redownload`, {
            method: 'POST'
        });

        if (!response.ok) {
            const msg = await safeErrorMessage(response, 'Re-download failed');
            throw new Error(msg);
        }

        const data = await response.json();
        const oldKB = (data.old_size / 1024).toFixed(1);
        const newKB = (data.new_size / 1024).toFixed(1);
        const changed = data.size_changed ? ` (size changed: ${oldKB} KB → ${newKB} KB)` : ` (size unchanged: ${newKB} KB)`;
        _showMgmtFeedback(`Successfully re-downloaded ${data.filename}${changed}`, false);
    } catch (error) {
        console.error('Error re-downloading document:', error);
        _showMgmtFeedback('Error: ' + error.message, true);
    } finally {
        btn.disabled = false;
        btn.innerHTML = origHtml;
    }
}

async function reExtractDocument() {
    if (!_selectedMgmtDoc) return;
    const docId = _selectedMgmtDoc.id;
    const btn = document.getElementById('doc-mgmt-reextract-btn');
    const origHtml = btn.innerHTML;

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;"></span> Extracting...';
    document.getElementById('doc-mgmt-feedback').style.display = 'none';

    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents/${encodeURIComponent(docId)}/re-extract`, {
            method: 'POST'
        });

        if (!response.ok) {
            const msg = await safeErrorMessage(response, 'Re-extraction failed');
            throw new Error(msg);
        }

        const data = await response.json();
        const vectorMsg = data.vector_updated ? ', search index updated' : ', search index unchanged';
        _showMgmtFeedback(
            `Successfully re-extracted ${data.filename}: ${data.char_count.toLocaleString()} characters, ${data.page_count} pages${vectorMsg}`,
            false
        );

        _selectedMgmtDoc.char_count = data.char_count;
        _selectedMgmtDoc.page_count = data.page_count;
        selectMgmtDocument(_selectedMgmtDoc.id);
    } catch (error) {
        console.error('Error re-extracting document:', error);
        _showMgmtFeedback('Error: ' + error.message, true);
    } finally {
        btn.disabled = false;
        btn.innerHTML = origHtml;
    }
}

// ============================================================================
// BULK DOCUMENT MANAGEMENT FUNCTIONS (Re-download / Re-extract)
// ============================================================================

function updateBulkMgmtCount() {
    const textarea = document.getElementById('bulk-mgmt-textarea');
    const countEl = document.getElementById('bulk-mgmt-count');
    if (!textarea || !countEl) return;
    const filenames = parseCsvFilenames(textarea.value);
    countEl.textContent = filenames.length > 0 ? `${filenames.length} filename(s) detected` : '';
}

function _renderBulkMgmtResults(data, operationLabel) {
    const resultsEl = document.getElementById('bulk-mgmt-results');
    if (!resultsEl) return;

    let html = '<div style="padding: var(--space-md); font-size: 0.85rem; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-tertiary);">';
    html += `<div style="color: var(--success, #10b981); font-weight: 500; margin-bottom: var(--space-sm);">${operationLabel} complete: ${data.processed} succeeded`;
    if (data.failed > 0) html += `, <span style="color: var(--danger);">${data.failed} failed</span>`;
    html += '</div>';

    if (data.skipped && data.skipped.length > 0) {
        html += `<details style="margin-bottom: var(--space-xs);"><summary style="color: var(--text-muted); cursor: pointer;">${data.skipped.length} file(s) skipped (unsupported type)</summary>`;
        html += '<div style="max-height: 120px; overflow-y: auto; margin-top: var(--space-xs); padding: var(--space-xs); background: var(--bg-secondary); border-radius: 4px; font-family: var(--font-mono); font-size: 0.75rem;">';
        html += data.skipped.map(f => escapeHtml(f)).join('<br>');
        html += '</div></details>';
    }

    if (data.not_found && data.not_found.length > 0) {
        html += `<details style="margin-bottom: var(--space-xs);"><summary style="color: var(--warning); cursor: pointer;">${data.not_found.length} filename(s) not found in database</summary>`;
        html += '<div style="max-height: 120px; overflow-y: auto; margin-top: var(--space-xs); padding: var(--space-xs); background: var(--bg-secondary); border-radius: 4px; font-family: var(--font-mono); font-size: 0.75rem;">';
        html += data.not_found.map(f => escapeHtml(f)).join('<br>');
        html += '</div></details>';
    }

    const successes = (data.results || []).filter(r => r.success);
    const failures = (data.results || []).filter(r => !r.success);

    if (successes.length > 0) {
        html += `<details style="margin-bottom: var(--space-xs);"><summary style="color: var(--success, #10b981); cursor: pointer;">${successes.length} successful</summary>`;
        html += '<div style="max-height: 200px; overflow-y: auto; margin-top: var(--space-xs); padding: var(--space-xs); background: var(--bg-secondary); border-radius: 4px; font-family: var(--font-mono); font-size: 0.75rem;">';
        for (const r of successes) {
            let detail = escapeHtml(r.filename);
            if (r.char_count !== undefined) detail += ` — ${r.char_count.toLocaleString()} chars, ${r.page_count} pages`;
            if (r.old_size !== undefined) {
                const oldKB = (r.old_size / 1024).toFixed(1);
                const newKB = (r.new_size / 1024).toFixed(1);
                detail += r.size_changed ? ` — ${oldKB} KB → ${newKB} KB` : ` — ${newKB} KB (unchanged)`;
            }
            html += detail + '<br>';
        }
        html += '</div></details>';
    }

    if (failures.length > 0) {
        html += `<details open style="margin-bottom: var(--space-xs);"><summary style="color: var(--danger); cursor: pointer;">${failures.length} failed</summary>`;
        html += '<div style="max-height: 200px; overflow-y: auto; margin-top: var(--space-xs); padding: var(--space-xs); background: var(--bg-secondary); border-radius: 4px; font-family: var(--font-mono); font-size: 0.75rem;">';
        for (const r of failures) {
            html += `<span style="color:var(--danger);">${escapeHtml(r.filename)}</span>: ${escapeHtml(r.error || 'Unknown error')}<br>`;
        }
        html += '</div></details>';
    }

    html += '</div>';
    resultsEl.style.display = 'block';
    resultsEl.innerHTML = html;
}

function _setBulkMgmtBusy(busy) {
    const progressEl = document.getElementById('bulk-mgmt-progress');
    const redownloadBtn = document.getElementById('bulk-redownload-btn');
    const reextractBtn = document.getElementById('bulk-reextract-btn');

    if (progressEl) progressEl.style.display = busy ? 'block' : 'none';
    if (redownloadBtn) redownloadBtn.disabled = busy;
    if (reextractBtn) reextractBtn.disabled = busy;
}

function _updateBulkProgress(current, total, filename) {
    const pct = total > 0 ? Math.round((current / total) * 100) : 0;
    const textEl = document.getElementById('bulk-mgmt-progress-text');
    const barEl = document.getElementById('bulk-mgmt-progress-bar');
    const detailEl = document.getElementById('bulk-mgmt-progress-detail');

    if (textEl) textEl.textContent = `Processing ${current} of ${total} (${pct}%)`;
    if (barEl) barEl.style.width = `${pct}%`;
    if (detailEl) detailEl.textContent = filename || '';
}

async function _resolveFilenames(filenames) {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents/resolve-filenames`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filenames })
        });
        if (response.ok) return await response.json();
    } catch (e) { /* fall through to fallback */ }
    return await _resolveFilenamesFallback(filenames);
}

async function _resolveFilenamesFallback(filenames) {
    const found = {};
    const not_found = [];
    for (const fname of filenames) {
        try {
            const resp = await authFetch(
                `${window.location.origin}/api/admin/documents-visibility?search=${encodeURIComponent(fname)}&limit=1`
            );
            if (resp.ok) {
                const data = await resp.json();
                const match = (data.documents || []).find(d => d.filename === fname);
                if (match) { found[fname] = match; } else { not_found.push(fname); }
            } else { not_found.push(fname); }
        } catch (e) { not_found.push(fname); }
    }
    return { found, not_found };
}

function _validateBulkInput(textarea, resultsEl) {
    const filenames = parseCsvFilenames(textarea.value);
    if (filenames.length === 0) {
        if (resultsEl) {
            resultsEl.style.display = 'block';
            resultsEl.innerHTML = '<div style="padding: var(--space-sm); color: var(--warning); font-size: 0.85rem;">No valid filenames found. Paste filenames one per line or comma-separated.</div>';
        }
        return null;
    }
    if (filenames.length > 500) {
        if (resultsEl) {
            resultsEl.style.display = 'block';
            resultsEl.innerHTML = '<div style="padding: var(--space-sm); color: var(--danger); font-size: 0.85rem;">Maximum 500 filenames per bulk operation. You have ' + filenames.length + '.</div>';
        }
        return null;
    }
    return filenames;
}

async function executeBulkRedownload() {
    const textarea = document.getElementById('bulk-mgmt-textarea');
    const resultsEl = document.getElementById('bulk-mgmt-results');
    if (!textarea) return;

    const filenames = _validateBulkInput(textarea, resultsEl);
    if (!filenames) return;

    const preview = filenames.slice(0, 5).join('\n') + (filenames.length > 5 ? '\n...' : '');
    if (!confirm(`Re-download ${filenames.length} file(s) from the DOJ website?\n\nNon-EFTA files will be skipped.\n\nFirst files:\n${preview}`)) return;

    _setBulkMgmtBusy(true);
    _updateBulkProgress(0, filenames.length, 'Resolving filenames...');
    if (resultsEl) resultsEl.style.display = 'none';

    const results = [];
    const skipped = [];
    let processed = 0;
    let failed = 0;
    let notFound = [];

    try {
        const resolved = await _resolveFilenames(filenames);
        notFound = resolved.not_found || [];
        const found = resolved.found || {};
        const docsToProcess = [];

        for (const fname of filenames) {
            if (!found[fname]) continue;
            const doc = found[fname];
            const isEfta = doc.filename && doc.filename.toUpperCase().startsWith('EFTA');
            if (!isEfta) {
                skipped.push(doc.filename || fname);
                continue;
            }
            docsToProcess.push({ fname, doc });
        }

        const total = docsToProcess.length;
        for (let i = 0; i < total; i++) {
            const { fname, doc } = docsToProcess[i];
            _updateBulkProgress(i + 1, total, doc.filename || fname);

            try {
                const resp = await authFetch(`${window.location.origin}/api/admin/documents/${encodeURIComponent(doc.id)}/redownload`, {
                    method: 'POST'
                });
                if (!resp.ok) {
                    const msg = await safeErrorMessage(resp, 'Download failed');
                    results.push({ filename: doc.filename || fname, success: false, error: msg });
                    failed++;
                } else {
                    const data = await resp.json();
                    results.push({
                        filename: data.filename || fname,
                        success: true,
                        old_size: data.old_size,
                        new_size: data.new_size,
                        size_changed: data.size_changed,
                    });
                    processed++;
                }
            } catch (err) {
                results.push({ filename: doc.filename || fname, success: false, error: err.message });
                failed++;
            }
        }

        _renderBulkMgmtResults({ processed, failed, skipped, not_found: notFound, results }, 'Bulk re-download');
    } catch (error) {
        console.error('Bulk redownload error:', error);
        if (resultsEl) {
            resultsEl.style.display = 'block';
            resultsEl.innerHTML = `<div style="padding: var(--space-sm); color: var(--danger); font-size: 0.85rem;">Error: ${escapeHtml(error.message)}</div>`;
        }
    } finally {
        _setBulkMgmtBusy(false);
    }
}

async function executeBulkReExtract() {
    const textarea = document.getElementById('bulk-mgmt-textarea');
    const resultsEl = document.getElementById('bulk-mgmt-results');
    if (!textarea) return;

    const filenames = _validateBulkInput(textarea, resultsEl);
    if (!filenames) return;

    const preview = filenames.slice(0, 5).join('\n') + (filenames.length > 5 ? '\n...' : '');
    if (!confirm(`Re-extract transcripts for ${filenames.length} file(s)?\n\nUnsupported file types will be skipped.\n\nFirst files:\n${preview}`)) return;

    _setBulkMgmtBusy(true);
    _updateBulkProgress(0, filenames.length, 'Resolving filenames...');
    if (resultsEl) resultsEl.style.display = 'none';

    const results = [];
    const skipped = [];
    let processed = 0;
    let failed = 0;
    let notFound = [];

    try {
        const resolved = await _resolveFilenames(filenames);
        notFound = resolved.not_found || [];
        const found = resolved.found || {};
        const docsToProcess = [];

        const supportedExts = ['.pdf', '.jpg', '.jpeg', '.png', '.gif', '.tiff', '.bmp', '.mp3', '.mp4', '.wav', '.m4a', '.avi', '.mov', '.wmv'];
        for (const fname of filenames) {
            if (!found[fname]) continue;
            const doc = found[fname];
            const ext = (doc.filename || fname).toLowerCase().match(/\.[^.]+$/);
            if (!ext || !supportedExts.includes(ext[0])) {
                skipped.push(doc.filename || fname);
                continue;
            }
            docsToProcess.push({ fname, doc });
        }

        const total = docsToProcess.length;
        for (let i = 0; i < total; i++) {
            const { fname, doc } = docsToProcess[i];
            _updateBulkProgress(i + 1, total, doc.filename || fname);

            try {
                const resp = await authFetch(`${window.location.origin}/api/admin/documents/${encodeURIComponent(doc.id)}/re-extract`, {
                    method: 'POST'
                });
                if (!resp.ok) {
                    const msg = await safeErrorMessage(resp, 'Extraction failed');
                    results.push({ filename: doc.filename || fname, success: false, error: msg });
                    failed++;
                } else {
                    const data = await resp.json();
                    results.push({
                        filename: data.filename || fname,
                        success: true,
                        char_count: data.char_count,
                        page_count: data.page_count,
                        vector_updated: data.vector_updated,
                    });
                    processed++;
                }
            } catch (err) {
                results.push({ filename: doc.filename || fname, success: false, error: err.message });
                failed++;
            }
        }

        _renderBulkMgmtResults({ processed, failed, skipped, not_found: notFound, results }, 'Bulk re-extract');
    } catch (error) {
        console.error('Bulk re-extract error:', error);
        if (resultsEl) {
            resultsEl.style.display = 'block';
            resultsEl.innerHTML = `<div style="padding: var(--space-sm); color: var(--danger); font-size: 0.85rem;">Error: ${escapeHtml(error.message)}</div>`;
        }
    } finally {
        _setBulkMgmtBusy(false);
    }
}

// ============================================================================
// CONTENT MANAGEMENT FUNCTIONS (Ask AI Toggle + Pinned Documents)
// ============================================================================

async function loadContentData() {
    await Promise.all([
        loadStatusPageSettings(),
        loadAskAIStatus(),
        loadPinnedDocsStatus(),
        loadAdsStatus(),
        loadAffiliateStatus(),
        loadPinnedDocuments(),
        loadKeywords(),
        loadDojCompleteness(),
        loadHiddenDocuments(),
        loadCategoryVisibility()
    ]);
}

// =============================================================================
// STATUS PAGE MANAGEMENT FUNCTIONS
// =============================================================================

// Status page message templates
const STATUS_TEMPLATES = {
    maintenance: {
        title: "Under Maintenance",
        message: "We're performing scheduled maintenance to improve your experience. The archive will be back online shortly."
    },
    technical: {
        title: "Technical Difficulties",
        message: "We're experiencing technical difficulties and are working to resolve them as quickly as possible. We apologize for any inconvenience."
    },
    upgrade: {
        title: "System Upgrade in Progress",
        message: "We're upgrading our systems with new features and improvements. The archive will be back online once the upgrade is complete."
    },
    server: {
        title: "Server Unavailable",
        message: "Our servers are temporarily unavailable due to unexpected issues. Our team is working to restore service. Thank you for your patience."
    }
};

async function loadStatusPageSettings() {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/status-page`);
        if (!response.ok) throw new Error('Failed to load status page settings');
        const data = await response.json();
        
        const toggle = document.getElementById('status-page-toggle');
        const form = document.getElementById('status-page-form');
        const indicator = document.getElementById('status-page-indicator');
        const infoEl = document.getElementById('status-page-info');
        
        // Update toggle state
        if (toggle) toggle.checked = data.enabled;
        
        // Show/hide form based on state
        if (form) form.style.display = data.enabled ? 'block' : 'none';
        
        // Update indicator badge
        if (indicator) {
            if (data.indexing_active) {
                indicator.className = 'badge badge-warning';
                indicator.textContent = '⏳ Indexing Active';
            } else if (data.enabled) {
                indicator.className = 'badge badge-danger';
                indicator.textContent = '🚧 Status Page Active';
            } else {
                indicator.className = 'badge badge-success';
                indicator.textContent = '✓ Site Online';
            }
        }
        
        // Populate form fields
        document.getElementById('status-page-title').value = data.title || 'Under Maintenance';
        document.getElementById('status-page-message').value = data.message || '';
        document.getElementById('status-page-timeline').value = data.timeline || '';
        
        // Update info panel
        if (infoEl) {
            if (data.indexing_active) {
                infoEl.innerHTML = `
                    <div style="color: var(--warning);">
                        <strong>⏳ Indexing in Progress</strong><br>
                        <span style="font-size: 0.85rem;">The site is currently showing the indexing progress page. 
                        The custom status page will not be shown until indexing completes.</span>
                    </div>
                `;
            } else if (data.enabled) {
                const startedText = data.started ? `Started: ${new Date(data.started).toLocaleString()}` : '';
                infoEl.innerHTML = `
                    <div style="color: var(--danger);">
                        <strong>🚧 Status Page is LIVE</strong><br>
                        <span style="font-size: 0.85rem;">Visitors are currently seeing the maintenance page instead of the archive.</span>
                        ${startedText ? `<br><span style="font-size: 0.8rem; color: var(--text-muted);">${startedText}</span>` : ''}
                    </div>
                `;
            } else {
                infoEl.innerHTML = `
                    <div style="color: var(--success);">
                        <strong>✓ Site is Online</strong><br>
                        <span style="font-size: 0.85rem;">The archive is accessible to visitors. Enable the status page when you need to take the site down for maintenance.</span>
                    </div>
                `;
            }
        }
        
    } catch (error) {
        console.error('Error loading status page settings:', error);
        const infoEl = document.getElementById('status-page-info');
        if (infoEl) infoEl.innerHTML = '<span style="color: var(--danger);">Error loading status page settings</span>';
    }
}

async function toggleStatusPage(enabled) {
    const toggle = document.getElementById('status-page-toggle');
    const form = document.getElementById('status-page-form');
    
    // Show/hide form
    if (form) form.style.display = enabled ? 'block' : 'none';
    
    if (enabled) {
        // When enabling, just show the form - user needs to click Save
        // Pre-populate with defaults if empty
        const titleEl = document.getElementById('status-page-title');
        const messageEl = document.getElementById('status-page-message');
        
        if (!titleEl.value) titleEl.value = 'Under Maintenance';
        if (!messageEl.value) messageEl.value = STATUS_TEMPLATES.maintenance.message;
        
    } else {
        // When disabling, immediately disable the status page
        if (!confirm('Are you sure you want to disable the status page? The site will be accessible to visitors again.')) {
            if (toggle) toggle.checked = true;
            if (form) form.style.display = 'block';
            return;
        }
        
        try {
            const response = await authFetch(`${window.location.origin}/api/admin/status-page/disable`, {
                method: 'POST'
            });
            
            if (!response.ok) throw new Error('Failed to disable status page');
            
            // Reload settings to update UI
            await loadStatusPageSettings();
            
        } catch (error) {
            console.error('Error disabling status page:', error);
            alert('Error: ' + error.message);
            // Revert toggle
            if (toggle) toggle.checked = true;
            if (form) form.style.display = 'block';
        }
    }
}

function applyStatusTemplate(templateName) {
    const template = STATUS_TEMPLATES[templateName];
    if (!template) return;
    
    document.getElementById('status-page-title').value = template.title;
    document.getElementById('status-page-message').value = template.message;
}

async function saveStatusPageSettings() {
    const enabled = document.getElementById('status-page-toggle').checked;
    const title = document.getElementById('status-page-title').value.trim();
    const message = document.getElementById('status-page-message').value.trim();
    const timeline = document.getElementById('status-page-timeline').value.trim();
    
    if (!title) {
        alert('Please enter a title for the status page');
        return;
    }
    
    if (!message) {
        alert('Please enter a message for the status page');
        return;
    }
    
    // Confirm if enabling
    if (enabled) {
        const confirmMsg = `Are you sure you want to enable the status page?\n\nThis will immediately take the site offline for visitors.\n\nTitle: ${title}\nMessage: ${message}\n${timeline ? 'Timeline: ' + timeline : ''}`;
        if (!confirm(confirmMsg)) {
            return;
        }
    }
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/status-page`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                enabled: enabled,
                title: title,
                message: message,
                timeline: timeline
            })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to save status page settings');
        }
        
        alert(enabled ? '✓ Status page is now LIVE. Visitors will see the maintenance page.' : '✓ Settings saved.');
        
        // Reload settings to update UI
        await loadStatusPageSettings();
        
    } catch (error) {
        console.error('Error saving status page settings:', error);
        alert('Error: ' + error.message);
    }
}

async function loadAskAIStatus() {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/settings`);
        if (!response.ok) throw new Error('Failed to load settings');
        const settings = await response.json();
        
        const isEnabled = settings.ask_ai_enabled !== 'false';
        const toggle = document.getElementById('ask-ai-toggle');
        const statusEl = document.getElementById('ask-ai-status');
        
        if (toggle) toggle.checked = isEnabled;
        if (statusEl) {
            statusEl.innerHTML = isEnabled 
                ? '<span style="color: var(--success);">✓ Ask AI is currently <strong>visible</strong> on the frontend</span>'
                : '<span style="color: var(--warning);">⚠️ Ask AI is currently <strong>hidden</strong> from the frontend</span>';
        }
    } catch (error) {
        console.error('Error loading Ask AI status:', error);
        const statusEl = document.getElementById('ask-ai-status');
        if (statusEl) statusEl.innerHTML = '<span style="color: var(--danger);">Error loading status</span>';
    }
}

async function toggleAskAI(enabled) {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: 'ask_ai_enabled', value: enabled ? 'true' : 'false' })
        });
        
        if (!response.ok) throw new Error('Failed to update setting');
        
        const statusEl = document.getElementById('ask-ai-status');
        if (statusEl) {
            statusEl.innerHTML = enabled 
                ? '<span style="color: var(--success);">✓ Ask AI is now <strong>visible</strong> on the frontend</span>'
                : '<span style="color: var(--warning);">⚠️ Ask AI is now <strong>hidden</strong> from the frontend</span>';
        }
    } catch (error) {
        console.error('Error toggling Ask AI:', error);
        alert('Error updating setting: ' + error.message);
        // Revert toggle
        const toggle = document.getElementById('ask-ai-toggle');
        if (toggle) toggle.checked = !enabled;
    }
}

async function loadPinnedDocsStatus() {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/settings`);
        if (!response.ok) throw new Error('Failed to load settings');
        const settings = await response.json();
        
        const isEnabled = settings.pinned_documents_enabled !== 'false';
        const toggle = document.getElementById('pinned-docs-toggle');
        const statusEl = document.getElementById('pinned-docs-status');
        
        if (toggle) toggle.checked = isEnabled;
        if (statusEl) {
            statusEl.innerHTML = isEnabled 
                ? '<span style="color: var(--success);">✓ Featured Documents bar is currently <strong>visible</strong> on the frontend</span>'
                : '<span style="color: var(--warning);">⚠️ Featured Documents bar is currently <strong>hidden</strong> from the frontend</span>';
        }
    } catch (error) {
        console.error('Error loading Pinned Docs status:', error);
        const statusEl = document.getElementById('pinned-docs-status');
        if (statusEl) statusEl.innerHTML = '<span style="color: var(--danger);">Error loading status</span>';
    }
}

async function togglePinnedDocs(enabled) {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: 'pinned_documents_enabled', value: enabled ? 'true' : 'false' })
        });
        
        if (!response.ok) throw new Error('Failed to update setting');
        
        const statusEl = document.getElementById('pinned-docs-status');
        if (statusEl) {
            statusEl.innerHTML = enabled 
                ? '<span style="color: var(--success);">✓ Featured Documents bar is now <strong>visible</strong> on the frontend</span>'
                : '<span style="color: var(--warning);">⚠️ Featured Documents bar is now <strong>hidden</strong> from the frontend</span>';
        }
    } catch (error) {
        console.error('Error toggling Pinned Docs:', error);
        alert('Error updating setting: ' + error.message);
        // Revert toggle
        const toggle = document.getElementById('pinned-docs-toggle');
        if (toggle) toggle.checked = !enabled;
    }
}

async function loadAdsStatus() {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/settings`);
        if (!response.ok) throw new Error('Failed to load settings');
        const settings = await response.json();

        // Default OFF: only enable after an ad network is wired up in app.js AD_CONFIG.
        const isEnabled = settings.ads_enabled === 'true';
        const toggle = document.getElementById('ads-toggle');
        const statusEl = document.getElementById('ads-status');

        if (toggle) toggle.checked = isEnabled;
        if (statusEl) {
            statusEl.innerHTML = isEnabled
                ? '<span style="color: var(--success);">✓ Display ads are currently <strong>enabled</strong> on the frontend</span>'
                : '<span style="color: var(--text-muted);">Display ads are currently <strong>disabled</strong></span>';
        }
    } catch (error) {
        console.error('Error loading Ads status:', error);
        const statusEl = document.getElementById('ads-status');
        if (statusEl) statusEl.innerHTML = '<span style="color: var(--danger);">Error loading status</span>';
    }
}

async function toggleAds(enabled) {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: 'ads_enabled', value: enabled ? 'true' : 'false' })
        });

        if (!response.ok) throw new Error('Failed to update setting');

        const statusEl = document.getElementById('ads-status');
        if (statusEl) {
            statusEl.innerHTML = enabled
                ? '<span style="color: var(--success);">✓ Display ads are now <strong>enabled</strong></span>'
                : '<span style="color: var(--text-muted);">Display ads are now <strong>disabled</strong></span>';
        }
    } catch (error) {
        console.error('Error toggling Ads:', error);
        alert('Error updating setting: ' + error.message);
        const toggle = document.getElementById('ads-toggle');
        if (toggle) toggle.checked = !enabled;
    }
}

async function loadAffiliateStatus() {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/settings`);
        if (!response.ok) throw new Error('Failed to load settings');
        const settings = await response.json();

        // Default ON; strip stays hidden anyway until AFFILIATE_TAG is set in app.js.
        const isEnabled = settings.affiliate_enabled !== 'false';
        const toggle = document.getElementById('affiliate-toggle');
        const statusEl = document.getElementById('affiliate-status');

        if (toggle) toggle.checked = isEnabled;
        if (statusEl) {
            statusEl.innerHTML = isEnabled
                ? '<span style="color: var(--success);">✓ Affiliate strip is currently <strong>visible</strong> (if a tag is configured)</span>'
                : '<span style="color: var(--warning);">⚠️ Affiliate strip is currently <strong>hidden</strong> from the frontend</span>';
        }
    } catch (error) {
        console.error('Error loading Affiliate status:', error);
        const statusEl = document.getElementById('affiliate-status');
        if (statusEl) statusEl.innerHTML = '<span style="color: var(--danger);">Error loading status</span>';
    }
}

async function toggleAffiliate(enabled) {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: 'affiliate_enabled', value: enabled ? 'true' : 'false' })
        });

        if (!response.ok) throw new Error('Failed to update setting');

        const statusEl = document.getElementById('affiliate-status');
        if (statusEl) {
            statusEl.innerHTML = enabled
                ? '<span style="color: var(--success);">✓ Affiliate strip is now <strong>visible</strong></span>'
                : '<span style="color: var(--warning);">⚠️ Affiliate strip is now <strong>hidden</strong></span>';
        }
    } catch (error) {
        console.error('Error toggling Affiliate:', error);
        alert('Error updating setting: ' + error.message);
        const toggle = document.getElementById('affiliate-toggle');
        if (toggle) toggle.checked = !enabled;
    }
}

// Store pinned documents data
let pinnedDocsData = [];

async function loadPinnedDocuments() {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/pinned-documents`);
        if (!response.ok) throw new Error('Failed to load pinned documents');
        const data = await response.json();
        
        pinnedDocsData = data.pinned_documents || [];
        renderPinnedDocuments(pinnedDocsData);
    } catch (error) {
        console.error('Error loading pinned documents:', error);
        const el = document.getElementById('pinned-docs-list');
        if (el) el.innerHTML = '<div style="color: var(--danger);">Error loading pinned documents</div>';
    }
}

function renderPinnedDocuments(docs) {
    const el = document.getElementById('pinned-docs-list');
    if (!el) return;
    
    if (!docs || docs.length === 0) {
        el.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
                </svg>
                <p>No documents pinned yet</p>
                <p style="font-size: 0.85rem;">Click "Pin Document" to add featured documents to the homepage</p>
            </div>
        `;
        return;
    }
    
    el.innerHTML = docs.map((doc, index) => `
        <div class="pinned-doc-item" data-id="${escapeHtml(doc.document_id)}">
            <div class="pinned-doc-order">${doc.display_order || index + 1}</div>
            <div class="pinned-doc-info">
                <div class="pinned-doc-filename" title="${escapeHtml(doc.filename)}">${escapeHtml(doc.filename)}</div>
                ${doc.reason ? `<div class="pinned-doc-reason">"${escapeHtml(doc.reason)}"</div>` : ''}
                <div class="pinned-doc-meta">${escapeHtml(doc.category || '')} • Pinned ${formatTimeAgo(doc.pinned_at)}</div>
            </div>
            <div class="pinned-doc-actions">
                <button class="btn-icon" onclick="editPinnedDocument('${escapeHtml(doc.document_id)}')" title="Edit">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                        <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                    </svg>
                </button>
                <button class="btn-icon delete" onclick="unpinDocument('${escapeHtml(doc.document_id)}')" title="Unpin">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="3,6 5,6 21,6"/>
                        <path d="M19,6v14a2,2,0,0,1-2,2H7a2,2,0,0,1-2-2V6M8,6V4a2,2,0,0,1,2-2h4a2,2,0,0,1,2,2V6"/>
                    </svg>
                </button>
            </div>
        </div>
    `).join('');
}

// Document search for pin modal
let pinSearchTimeout = null;

function openPinDocumentModal() {
    const modal = document.getElementById('pin-document-modal');
    if (modal) {
        modal.style.display = 'flex';
        // Reset form
        document.getElementById('pin-doc-search').value = '';
        document.getElementById('pin-doc-reason').value = '';
        document.getElementById('pin-doc-order').value = '0';
        document.getElementById('pin-doc-selected').style.display = 'none';
        document.getElementById('pin-doc-results').innerHTML = 
            '<div style="padding: var(--space-md); color: var(--text-muted); text-align: center;">Enter a search term to find documents</div>';
    }
}

function closePinDocumentModal() {
    const modal = document.getElementById('pin-document-modal');
    if (modal) modal.style.display = 'none';
}

async function searchDocumentsForPin(query) {
    if (pinSearchTimeout) clearTimeout(pinSearchTimeout);
    
    const resultsEl = document.getElementById('pin-doc-results');
    
    if (!query || query.length < 2) {
        resultsEl.innerHTML = '<div style="padding: var(--space-md); color: var(--text-muted); text-align: center;">Enter at least 2 characters to search</div>';
        return;
    }
    
    // Debounce search
    pinSearchTimeout = setTimeout(async () => {
        resultsEl.innerHTML = '<div style="padding: var(--space-md); text-align: center;"><span class="spinner"></span> Searching...</div>';
        
        try {
            // Use search parameter which searches both filename AND subcategory
            const response = await fetch(`${window.location.origin}/api/documents?search=${encodeURIComponent(query)}&limit=100`);
            if (!response.ok) throw new Error('Search failed');
            const data = await response.json();
            
            if (!data.documents || data.documents.length === 0) {
                resultsEl.innerHTML = '<div style="padding: var(--space-md); color: var(--text-muted); text-align: center;">No documents found</div>';
                return;
            }
            
            resultsEl.innerHTML = data.documents.map(doc => {
                const categoryInfo = [doc.category, doc.subcategory].filter(Boolean).join(' › ');
                return `
                <div class="pin-search-item" onclick="selectDocumentForPin('${escapeHtml(doc.id)}', '${escapeHtml(doc.filename.replace(/'/g, "\\'"))}')">
                    <div class="pin-search-filename">${escapeHtml(doc.filename)}</div>
                    <div class="pin-search-meta">${escapeHtml(categoryInfo || 'Unknown')} • ${doc.page_count || 0} pages</div>
                </div>
            `;
            }).join('');
        } catch (error) {
            console.error('Error searching documents:', error);
            resultsEl.innerHTML = '<div style="padding: var(--space-md); color: var(--danger); text-align: center;">Error searching documents</div>';
        }
    }, 300);
}

function selectDocumentForPin(docId, filename) {
    const selectedEl = document.getElementById('pin-doc-selected');
    const selectedNameEl = document.getElementById('pin-doc-selected-name');
    const selectedIdEl = document.getElementById('pin-doc-selected-id');
    
    selectedIdEl.value = docId;
    selectedNameEl.textContent = filename;
    selectedEl.style.display = 'block';
    
    // Highlight selected item
    document.querySelectorAll('.pin-search-item').forEach(el => el.classList.remove('selected'));
    document.querySelector(`.pin-search-item[onclick*="${docId}"]`)?.classList.add('selected');
}

async function submitPinDocument() {
    const docId = document.getElementById('pin-doc-selected-id').value;
    const reason = document.getElementById('pin-doc-reason').value.trim();
    const order = parseInt(document.getElementById('pin-doc-order').value) || 0;
    
    if (!docId) {
        alert('Please select a document first');
        return;
    }
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/pinned-documents`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                document_id: docId,
                reason: reason || null,
                display_order: order
            })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to pin document');
        }
        
        closePinDocumentModal();
        loadPinnedDocuments();
        
    } catch (error) {
        console.error('Error pinning document:', error);
        alert('Error: ' + error.message);
    }
}

async function unpinDocument(docId) {
    if (!confirm('Are you sure you want to unpin this document?')) return;
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/pinned-documents/${docId}`, {
            method: 'DELETE'
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to unpin document');
        }
        
        loadPinnedDocuments();
        
    } catch (error) {
        console.error('Error unpinning document:', error);
        alert('Error: ' + error.message);
    }
}

function editPinnedDocument(docId) {
    const doc = pinnedDocsData.find(d => d.document_id === docId);
    if (!doc) return;
    
    // Open the edit modal
    const modal = document.getElementById('edit-pinned-modal');
    if (!modal) return;
    
    // Populate the modal with current values
    document.getElementById('edit-pinned-doc-id').value = docId;
    document.getElementById('edit-pinned-doc-name').textContent = doc.filename || docId;
    document.getElementById('edit-pinned-reason').value = doc.reason || '';
    document.getElementById('edit-pinned-order').value = doc.display_order || 0;
    
    modal.style.display = 'flex';
}

function closeEditPinnedModal() {
    const modal = document.getElementById('edit-pinned-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}

async function submitEditPinned() {
    const docId = document.getElementById('edit-pinned-doc-id').value;
    const reason = document.getElementById('edit-pinned-reason').value.trim();
    const order = parseInt(document.getElementById('edit-pinned-order').value) || 0;
    
    if (!docId) return;
    
    await updatePinnedDocument(docId, reason, order);
    closeEditPinnedModal();
}

async function updatePinnedDocument(docId, reason, order) {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/pinned-documents/${docId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason, display_order: order })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to update pinned document');
        }
        
        loadPinnedDocuments();
        
    } catch (error) {
        console.error('Error updating pinned document:', error);
        alert('Error: ' + error.message);
    }
}

// =============================================================================
// Keywords/Topics Management
// =============================================================================

let keywordsData = [];

async function loadKeywords() {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/keywords`);
        if (!response.ok) throw new Error('Failed to load keywords');
        const data = await response.json();
        
        keywordsData = data.keywords || [];
        renderKeywords(keywordsData);
    } catch (error) {
        console.error('Error loading keywords:', error);
        const el = document.getElementById('keywords-list');
        if (el) el.innerHTML = '<div style="color: var(--danger);">Error loading keywords</div>';
    }
}

function renderKeywords(keywords) {
    const el = document.getElementById('keywords-list');
    if (!el) return;
    
    if (!keywords || keywords.length === 0) {
        el.innerHTML = `
            <div style="text-align: center; padding: var(--space-xl); color: var(--text-muted);">
                <p style="margin-bottom: var(--space-md);">No keywords configured yet.</p>
                <button class="btn btn-primary" onclick="seedKeywords()">
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M12 2v20M2 12h20"/>
                    </svg>
                    Seed Default Keywords
                </button>
            </div>
        `;
        return;
    }
    
    // Group by category
    const grouped = {};
    keywords.forEach(kw => {
        if (!grouped[kw.category]) grouped[kw.category] = [];
        grouped[kw.category].push(kw);
    });
    
    // Category icons
    const categoryIcons = {
        'People': '👤',
        'Locations': '📍',
        'Topics': '📋'
    };
    
    let html = '';
    for (const [category, items] of Object.entries(grouped)) {
        const icon = categoryIcons[category] || '🏷️';
        html += `
            <div style="margin-bottom: var(--space-lg);">
                <h4 style="margin-bottom: var(--space-sm); color: var(--text-primary);">${icon} ${category}</h4>
                <div class="data-table-container">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Search Term</th>
                                <th>Doc Count</th>
                                <th>Order</th>
                                <th>Status</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
        `;
        
        items.forEach(kw => {
            const statusBadge = kw.is_active 
                ? '<span style="color: var(--success); font-weight: 500;">Active</span>'
                : '<span style="color: var(--text-muted);">Inactive</span>';
            
            html += `
                <tr>
                    <td style="font-weight: 500;">${escapeHtml(kw.name)}</td>
                    <td><code style="background: var(--bg-tertiary); padding: 2px 6px; border-radius: 3px;">${escapeHtml(kw.search_term)}</code></td>
                    <td>${formatNumber(kw.document_count)}</td>
                    <td>${kw.display_order}</td>
                    <td>${statusBadge}</td>
                    <td>
                        <button class="btn btn-sm" onclick="editKeyword(${kw.id})" title="Edit">
                            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>
                            </svg>
                        </button>
                        <button class="btn btn-sm btn-danger" onclick="deleteKeyword(${kw.id}, '${escapeHtml(kw.name)}')" title="Delete">
                            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="3,6 5,6 21,6"/>
                                <path d="M19,6v14a2,2,0,0,1-2,2H7a2,2,0,0,1-2-2V6M8,6V4a2,2,0,0,1,2-2h4a2,2,0,0,1,2,2V6"/>
                            </svg>
                        </button>
                    </td>
                </tr>
            `;
        });
        
        html += `
                        </tbody>
                    </table>
                </div>
            </div>
        `;
    }
    
    el.innerHTML = html;
}

function openAddKeywordModal() {
    document.getElementById('keyword-modal-title').textContent = 'Add Keyword';
    document.getElementById('keyword-edit-id').value = '';
    document.getElementById('keyword-name').value = '';
    document.getElementById('keyword-search-term').value = '';
    document.getElementById('keyword-category').value = 'People';
    document.getElementById('keyword-order').value = '0';
    document.getElementById('keyword-active').value = '1';
    document.getElementById('keyword-modal').style.display = 'flex';
}

function editKeyword(keywordId) {
    const kw = keywordsData.find(k => k.id === keywordId);
    if (!kw) return;
    
    document.getElementById('keyword-modal-title').textContent = 'Edit Keyword';
    document.getElementById('keyword-edit-id').value = kw.id;
    document.getElementById('keyword-name').value = kw.name;
    document.getElementById('keyword-search-term').value = kw.search_term;
    document.getElementById('keyword-category').value = kw.category;
    document.getElementById('keyword-order').value = kw.display_order;
    document.getElementById('keyword-active').value = kw.is_active ? '1' : '0';
    document.getElementById('keyword-modal').style.display = 'flex';
}

function closeKeywordModal() {
    document.getElementById('keyword-modal').style.display = 'none';
}

async function submitKeyword() {
    const editId = document.getElementById('keyword-edit-id').value;
    const name = document.getElementById('keyword-name').value.trim();
    const searchTerm = document.getElementById('keyword-search-term').value.trim();
    const category = document.getElementById('keyword-category').value;
    const displayOrder = parseInt(document.getElementById('keyword-order').value) || 0;
    const isActive = document.getElementById('keyword-active').value === '1';
    
    if (!name || !searchTerm) {
        alert('Please fill in both Name and Search Term');
        return;
    }
    
    try {
        const isEdit = !!editId;
        const url = isEdit 
            ? `${window.location.origin}/api/admin/keywords/${editId}`
            : `${window.location.origin}/api/admin/keywords`;
        
        const response = await authFetch(url, {
            method: isEdit ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name,
                search_term: searchTerm,
                category,
                display_order: displayOrder,
                is_active: isActive
            })
        });
        
        if (!response.ok) {
            let errorMessage = 'Failed to save keyword';
            const text = await response.text();
            try {
                const data = JSON.parse(text);
                errorMessage = data.detail || errorMessage;
            } catch (e) {
                errorMessage = text || `Server error (${response.status})`;
            }
            throw new Error(errorMessage);
        }
        
        closeKeywordModal();
        loadKeywords();
        
    } catch (error) {
        console.error('Error saving keyword:', error);
        alert('Error: ' + error.message);
    }
}

async function deleteKeyword(keywordId, keywordName) {
    if (!confirm(`Are you sure you want to delete the keyword "${keywordName}"?`)) {
        return;
    }
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/keywords/${keywordId}`, {
            method: 'DELETE'
        });
        
        if (!response.ok) {
            let errorMessage = 'Failed to delete keyword';
            const text = await response.text();
            try {
                const data = JSON.parse(text);
                errorMessage = data.detail || errorMessage;
            } catch (e) {
                // Response wasn't valid JSON
                errorMessage = text || `Server error (${response.status})`;
            }
            throw new Error(errorMessage);
        }
        
        loadKeywords();
        
    } catch (error) {
        console.error('Error deleting keyword:', error);
        alert('Error: ' + error.message);
    }
}

async function recountKeywords() {
    if (!confirm('This will scan all documents to recount keyword matches. This may take a while. Continue?')) {
        return;
    }
    
    const el = document.getElementById('keywords-list');
    if (el) el.innerHTML = '<div class="loading"><span class="spinner"></span>Recounting keywords...</div>';
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/keywords/recount`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to recount keywords');
        }
        
        const data = await response.json();
        alert(`Successfully recounted ${Object.keys(data.counts).length} keywords!`);
        loadKeywords();
        
    } catch (error) {
        console.error('Error recounting keywords:', error);
        alert('Error: ' + error.message);
        loadKeywords();
    }
}

async function seedKeywords() {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/keywords/seed`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to seed keywords');
        }
        
        const data = await response.json();
        if (data.keywords_added > 0) {
            alert(`Successfully added ${data.keywords_added} default keywords!`);
        } else {
            alert('Keywords already exist. No new keywords added.');
        }
        loadKeywords();
        
    } catch (error) {
        console.error('Error seeding keywords:', error);
        alert('Error: ' + error.message);
    }
}

// Global refresh function
window.refreshAll = refreshAll;
window.clearLogs = clearLogs;
window.openFeedbackModal = openFeedbackModal;
window.closeFeedbackModal = closeFeedbackModal;
window.deleteFeedback = deleteFeedback;
window.toggleAskAI = toggleAskAI;
window.togglePinnedDocs = togglePinnedDocs;
window.openPinDocumentModal = openPinDocumentModal;
window.closePinDocumentModal = closePinDocumentModal;
window.searchDocumentsForPin = searchDocumentsForPin;
window.selectDocumentForPin = selectDocumentForPin;
window.submitPinDocument = submitPinDocument;
window.unpinDocument = unpinDocument;
window.editPinnedDocument = editPinnedDocument;
window.closeEditPinnedModal = closeEditPinnedModal;
window.submitEditPinned = submitEditPinned;
window.loadKeywords = loadKeywords;
window.openAddKeywordModal = openAddKeywordModal;
window.editKeyword = editKeyword;
window.closeKeywordModal = closeKeywordModal;
window.submitKeyword = submitKeyword;
window.deleteKeyword = deleteKeyword;
window.recountKeywords = recountKeywords;
window.seedKeywords = seedKeywords;

// =============================================================================
// DOJ Download Completeness Functions
// =============================================================================

let dojCompletenessData = null;
let missingDocumentsData = [];

async function loadDojCompleteness() {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/doj-completeness`);
        if (!response.ok) throw new Error('Failed to load completeness data');
        dojCompletenessData = await response.json();
        renderDojCompletenessStats();
        await loadMissingDocuments();
    } catch (error) {
        console.error('Error loading DOJ completeness:', error);
        const statsEl = document.getElementById('doj-completeness-stats');
        if (statsEl) {
            statsEl.innerHTML = '<div style="color: var(--danger);">Error loading completeness data. Make sure you have run the download script first.</div>';
        }
    }
}

function renderDojCompletenessStats() {
    const statsEl = document.getElementById('doj-completeness-stats');
    if (!statsEl || !dojCompletenessData) return;
    
    const manifest = dojCompletenessData.manifest || {};
    const missing = dojCompletenessData.missing || {};
    
    // Build dataset cards
    let html = '<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: var(--space-md); margin-bottom: var(--space-lg);">';
    
    // Overall stats card
    html += `
        <div style="background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 8px; padding: var(--space-md);">
            <div style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: var(--space-xs);">Total Files Tracked</div>
            <div style="font-size: 1.5rem; font-weight: 600; color: var(--accent);">${formatNumber(manifest.total || 0)}</div>
        </div>
    `;
    
    // Missing count card
    html += `
        <div style="background: var(--danger-dim); border: 1px solid var(--danger); border-radius: 8px; padding: var(--space-md);">
            <div style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: var(--space-xs);">Missing (404)</div>
            <div style="font-size: 1.5rem; font-weight: 600; color: var(--danger);">${formatNumber(missing.total || 0)}</div>
        </div>
    `;
    
    html += '</div>';
    
    // Per-dataset breakdown
    if (manifest.by_dataset && Object.keys(manifest.by_dataset).length > 0) {
        html += '<h4 style="margin-bottom: var(--space-md); color: var(--text-secondary);">Per-Dataset Status</h4>';
        html += '<div style="overflow-x: auto;"><table style="width: 100%; border-collapse: collapse;">';
        html += `<thead>
            <tr style="border-bottom: 1px solid var(--border);">
                <th style="text-align: left; padding: var(--space-sm); color: var(--text-muted);">Dataset</th>
                <th style="text-align: right; padding: var(--space-sm); color: var(--text-muted);">Total Found</th>
                <th style="text-align: right; padding: var(--space-sm); color: var(--success);">Downloaded</th>
                <th style="text-align: right; padding: var(--space-sm); color: var(--danger);">404</th>
                <th style="text-align: right; padding: var(--space-sm); color: var(--warning);">Failed</th>
                <th style="text-align: right; padding: var(--space-sm); color: var(--text-muted);">Completion</th>
            </tr>
        </thead><tbody>`;
        
        for (const [dataset, stats] of Object.entries(manifest.by_dataset)) {
            const total = stats.total || 0;
            const downloaded = stats.downloaded || 0;
            const notFound = stats['404'] || 0;
            const failed = stats.failed || 0;
            const completionPct = total > 0 ? ((downloaded / total) * 100).toFixed(1) : '0.0';
            
            html += `<tr style="border-bottom: 1px solid var(--border);">
                <td style="padding: var(--space-sm);">Data Set ${dataset}</td>
                <td style="text-align: right; padding: var(--space-sm);">${formatNumber(total)}</td>
                <td style="text-align: right; padding: var(--space-sm); color: var(--success);">${formatNumber(downloaded)}</td>
                <td style="text-align: right; padding: var(--space-sm); color: var(--danger);">${formatNumber(notFound)}</td>
                <td style="text-align: right; padding: var(--space-sm); color: var(--warning);">${formatNumber(failed)}</td>
                <td style="text-align: right; padding: var(--space-sm);">
                    <span style="color: ${parseFloat(completionPct) > 95 ? 'var(--success)' : 'var(--warning)'};">${completionPct}%</span>
                </td>
            </tr>`;
        }
        
        html += '</tbody></table></div>';
    } else {
        html += '<p style="color: var(--text-muted);">No manifest data available. Run the download script to start tracking files.</p>';
    }
    
    statsEl.innerHTML = html;
}

async function loadMissingDocuments() {
    const datasetFilter = document.getElementById('doj-dataset-filter');
    const dataset = datasetFilter ? datasetFilter.value : '';
    
    try {
        let url = `${window.location.origin}/api/admin/missing-documents`;
        if (dataset) {
            url += `?dataset=${dataset}`;
        }
        
        const response = await authFetch(url);
        if (!response.ok) throw new Error('Failed to load missing documents');
        const data = await response.json();
        missingDocumentsData = data.missing_documents || [];
        renderMissingDocuments();
    } catch (error) {
        console.error('Error loading missing documents:', error);
        const listEl = document.getElementById('missing-docs-list');
        if (listEl) {
            listEl.innerHTML = '<div style="color: var(--text-muted);">No missing documents found.</div>';
        }
    }
}

function renderMissingDocuments() {
    const listEl = document.getElementById('missing-docs-list');
    if (!listEl) return;
    
    if (!missingDocumentsData || missingDocumentsData.length === 0) {
        listEl.innerHTML = '<div style="text-align: center; padding: var(--space-lg); color: var(--text-muted);">No missing documents (404s) found.</div>';
        return;
    }
    
    let html = '<table style="width: 100%; border-collapse: collapse;">';
    html += `<thead>
        <tr style="border-bottom: 1px solid var(--border);">
            <th style="text-align: left; padding: var(--space-sm); color: var(--text-muted);">Filename</th>
            <th style="text-align: left; padding: var(--space-sm); color: var(--text-muted);">Dataset</th>
            <th style="text-align: left; padding: var(--space-sm); color: var(--text-muted);">Page</th>
            <th style="text-align: left; padding: var(--space-sm); color: var(--text-muted);">First Seen</th>
            <th style="text-align: right; padding: var(--space-sm); color: var(--text-muted);">Checks</th>
            <th style="text-align: center; padding: var(--space-sm); color: var(--text-muted);">Link</th>
        </tr>
    </thead><tbody>`;
    
    for (const doc of missingDocumentsData) {
        const firstSeen = doc.first_seen ? new Date(doc.first_seen).toLocaleDateString() : 'Unknown';
        
        html += `<tr style="border-bottom: 1px solid var(--border);">
            <td style="padding: var(--space-sm); font-family: var(--font-mono); font-size: 0.85rem;">${escapeHtml(doc.filename)}</td>
            <td style="padding: var(--space-sm);">Set ${doc.dataset_num}</td>
            <td style="padding: var(--space-sm);">${doc.page_found_on !== null ? doc.page_found_on + 1 : '-'}</td>
            <td style="padding: var(--space-sm);">${firstSeen}</td>
            <td style="text-align: right; padding: var(--space-sm);">${doc.check_count || 1}</td>
            <td style="text-align: center; padding: var(--space-sm);">
                <a href="${escapeHtml(doc.url)}" target="_blank" rel="noopener" style="color: var(--accent);" title="Open on DOJ">
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
                        <polyline points="15 3 21 3 21 9"/>
                        <line x1="10" y1="14" x2="21" y2="3"/>
                    </svg>
                </a>
            </td>
        </tr>`;
    }
    
    html += '</tbody></table>';
    listEl.innerHTML = html;
}

function filterDojData() {
    loadMissingDocuments();
}

async function refreshDojCompleteness() {
    await loadDojCompleteness();
}

function exportMissingDocs() {
    if (!missingDocumentsData || missingDocumentsData.length === 0) {
        alert('No missing documents to export.');
        return;
    }
    
    // Create CSV content
    let csv = 'Filename,URL,Dataset,Page Found,First Seen,Last Checked,Check Count\n';
    for (const doc of missingDocumentsData) {
        csv += `"${doc.filename}","${doc.url}",${doc.dataset_num},${doc.page_found_on || ''},"${doc.first_seen || ''}","${doc.last_checked || ''}",${doc.check_count || 1}\n`;
    }
    
    // Download
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `missing_documents_${new Date().toISOString().split('T')[0]}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;')
              .replace(/</g, '&lt;')
              .replace(/>/g, '&gt;')
              .replace(/"/g, '&quot;')
              .replace(/'/g, '&#039;');
}

// Export Status Page functions
window.loadStatusPageSettings = loadStatusPageSettings;
window.toggleStatusPage = toggleStatusPage;
window.applyStatusTemplate = applyStatusTemplate;
window.saveStatusPageSettings = saveStatusPageSettings;

// Export DOJ functions
window.loadDojCompleteness = loadDojCompleteness;
window.refreshDojCompleteness = refreshDojCompleteness;
window.filterDojData = filterDojData;
window.exportMissingDocs = exportMissingDocs;

// =============================================================================
// Document Visibility Management
// =============================================================================

let hiddenDocumentsData = [];
let categoryVisibilityData = [];

// Bulk selection state
let selectedSearchDocIds = new Set();
let selectedHiddenDocIds = new Set();
let lastSearchResults = [];

function updateBulkActionBar() {
    const bar = document.getElementById('visibility-bulk-actions');
    if (!bar) return;
    
    const count = selectedSearchDocIds.size;
    if (count === 0) {
        bar.style.display = 'none';
        return;
    }
    
    // Count how many selected are hidden vs visible
    const selectedHiddenCount = lastSearchResults.filter(d => selectedSearchDocIds.has(d.id) && d.is_hidden === 1).length;
    const selectedVisibleCount = count - selectedHiddenCount;
    
    bar.style.display = 'flex';
    bar.innerHTML = `
        <span style="font-size: 0.85rem; font-weight: 500;">${count} selected</span>
        <div style="display: flex; gap: var(--space-xs);">
            ${selectedVisibleCount > 0 ? `
                <button class="btn btn-sm btn-danger" onclick="bulkHideSelected()">
                    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>
                        <line x1="1" y1="1" x2="23" y2="23"/>
                    </svg>
                    Hide Selected (${selectedVisibleCount})
                </button>
            ` : ''}
            ${selectedHiddenCount > 0 ? `
                <button class="btn btn-sm" onclick="bulkUnhideSelected()">
                    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                        <circle cx="12" cy="12" r="3"/>
                    </svg>
                    Unhide Selected (${selectedHiddenCount})
                </button>
            ` : ''}
        </div>
    `;
}

function toggleSearchDocSelection(docId) {
    if (selectedSearchDocIds.has(docId)) {
        selectedSearchDocIds.delete(docId);
    } else {
        selectedSearchDocIds.add(docId);
    }
    updateBulkActionBar();
    // Update checkbox visual state without re-rendering entire list
    const cb = document.getElementById(`search-cb-${docId}`);
    if (cb) cb.checked = selectedSearchDocIds.has(docId);
}

function toggleSelectAllSearch() {
    const allCheckbox = document.getElementById('search-select-all');
    if (!allCheckbox) return;
    
    if (allCheckbox.checked) {
        lastSearchResults.forEach(doc => selectedSearchDocIds.add(doc.id));
    } else {
        lastSearchResults.forEach(doc => selectedSearchDocIds.delete(doc.id));
    }
    // Update individual checkboxes
    lastSearchResults.forEach(doc => {
        const cb = document.getElementById(`search-cb-${doc.id}`);
        if (cb) cb.checked = selectedSearchDocIds.has(doc.id);
    });
    updateBulkActionBar();
}

async function bulkHideSelected() {
    const visibleIds = lastSearchResults
        .filter(d => selectedSearchDocIds.has(d.id) && d.is_hidden !== 1)
        .map(d => d.id);
    
    if (visibleIds.length === 0) return;
    
    if (!confirm(`Are you sure you want to hide ${visibleIds.length} document(s)?\n\nThese documents will become inaccessible to the public.`)) {
        return;
    }
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents/bulk-hide`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ document_ids: visibleIds })
        });
        
        if (!response.ok) {
            const msg = await safeErrorMessage(response, 'Bulk hide failed');
            throw new Error(msg);
        }
        
        const result = await response.json();
        selectedSearchDocIds.clear();
        updateBulkActionBar();
        
        // Show result feedback
        showBulkFeedback(`Successfully hidden ${result.hidden_count} document(s)`);
        
        // Refresh both lists
        searchDocumentsForVisibility();
        loadHiddenDocuments();
    } catch (error) {
        console.error('Error bulk hiding documents:', error);
        alert('Error: ' + error.message);
    }
}

async function bulkUnhideSelected() {
    const hiddenIds = lastSearchResults
        .filter(d => selectedSearchDocIds.has(d.id) && d.is_hidden === 1)
        .map(d => d.id);
    
    if (hiddenIds.length === 0) return;
    
    if (!confirm(`Are you sure you want to unhide ${hiddenIds.length} document(s)?`)) {
        return;
    }
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents/bulk-unhide`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ document_ids: hiddenIds })
        });
        
        if (!response.ok) {
            const msg = await safeErrorMessage(response, 'Bulk unhide failed');
            throw new Error(msg);
        }
        
        const result = await response.json();
        selectedSearchDocIds.clear();
        updateBulkActionBar();
        
        showBulkFeedback(`Successfully unhidden ${result.unhidden_count} document(s)`);
        
        searchDocumentsForVisibility();
        loadHiddenDocuments();
    } catch (error) {
        console.error('Error bulk unhiding documents:', error);
        alert('Error: ' + error.message);
    }
}

function showBulkFeedback(message) {
    const feedbackEl = document.getElementById('visibility-bulk-feedback');
    if (!feedbackEl) return;
    
    feedbackEl.textContent = message;
    feedbackEl.style.display = 'block';
    setTimeout(() => { feedbackEl.style.display = 'none'; }, 5000);
}

// --- Hidden Documents List with Bulk Unhide ---

function updateHiddenBulkBar() {
    const bar = document.getElementById('hidden-bulk-actions');
    if (!bar) return;
    
    const count = selectedHiddenDocIds.size;
    if (count === 0) {
        bar.style.display = 'none';
        return;
    }
    
    bar.style.display = 'flex';
    bar.innerHTML = `
        <span style="font-size: 0.85rem; font-weight: 500;">${count} selected</span>
        <button class="btn btn-sm" onclick="bulkUnhideFromHiddenList()">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                <circle cx="12" cy="12" r="3"/>
            </svg>
            Unhide Selected (${count})
        </button>
    `;
}

function toggleHiddenDocSelection(docId) {
    if (selectedHiddenDocIds.has(docId)) {
        selectedHiddenDocIds.delete(docId);
    } else {
        selectedHiddenDocIds.add(docId);
    }
    updateHiddenBulkBar();
    const cb = document.getElementById(`hidden-cb-${docId}`);
    if (cb) cb.checked = selectedHiddenDocIds.has(docId);
}

function toggleSelectAllHidden() {
    const allCheckbox = document.getElementById('hidden-select-all');
    if (!allCheckbox) return;
    
    if (allCheckbox.checked) {
        hiddenDocumentsData.forEach(doc => selectedHiddenDocIds.add(doc.id));
    } else {
        selectedHiddenDocIds.clear();
    }
    hiddenDocumentsData.forEach(doc => {
        const cb = document.getElementById(`hidden-cb-${doc.id}`);
        if (cb) cb.checked = selectedHiddenDocIds.has(doc.id);
    });
    updateHiddenBulkBar();
}

async function bulkUnhideFromHiddenList() {
    const ids = Array.from(selectedHiddenDocIds);
    if (ids.length === 0) return;
    
    if (!confirm(`Are you sure you want to unhide ${ids.length} document(s)?`)) {
        return;
    }
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents/bulk-unhide`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ document_ids: ids })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Bulk unhide failed');
        }
        
        const result = await response.json();
        selectedHiddenDocIds.clear();
        updateHiddenBulkBar();
        
        showBulkFeedback(`Successfully unhidden ${result.unhidden_count} document(s)`);
        loadHiddenDocuments();
        searchDocumentsForVisibility();
    } catch (error) {
        console.error('Error bulk unhiding documents:', error);
        alert('Error: ' + error.message);
    }
}

// --- Bulk Hide by Filename Pattern ---

async function previewPatternHide() {
    const input = document.getElementById('visibility-pattern-input');
    const pattern = input ? input.value.trim() : '';
    const resultsEl = document.getElementById('pattern-preview-results');
    if (!resultsEl) return;
    
    if (!pattern) {
        resultsEl.innerHTML = '<div style="padding: var(--space-sm); color: var(--text-muted); text-align: center; font-size: 0.85rem;">Enter a filename pattern to preview matches</div>';
        return;
    }
    
    resultsEl.innerHTML = '<div class="loading"><span class="spinner"></span>Searching...</div>';
    
    try {
        // Use existing search endpoint to preview
        const searchTerm = pattern.replace(/\*/g, '');
        const response = await authFetch(`${window.location.origin}/api/admin/documents-visibility?search=${encodeURIComponent(searchTerm)}&limit=100`);
        if (!response.ok) throw new Error('Preview failed');
        const data = await response.json();
        
        const docs = data.documents || [];
        const visibleDocs = docs.filter(d => d.is_hidden !== 1);
        
        if (docs.length === 0) {
            resultsEl.innerHTML = '<div style="padding: var(--space-sm); color: var(--text-muted); text-align: center; font-size: 0.85rem;">No documents match this pattern</div>';
            return;
        }
        
        resultsEl.innerHTML = `
            <div style="padding: var(--space-sm) var(--space-md); font-size: 0.85rem; color: var(--text-muted); border-bottom: 1px solid var(--border);">
                ${docs.length} match${docs.length !== 1 ? 'es' : ''} found (${visibleDocs.length} currently visible)
            </div>
            <div style="max-height: 200px; overflow-y: auto;">
                ${docs.map(doc => `
                    <div style="padding: var(--space-xs) var(--space-md); border-bottom: 1px solid var(--border); font-size: 0.8rem; ${doc.is_hidden === 1 ? 'background: var(--danger-dim); opacity: 0.7;' : ''}">
                        <span style="font-family: var(--font-mono);">${doc.is_hidden === 1 ? '🔒 ' : ''}${escapeHtml(doc.filename)}</span>
                        <span style="color: var(--text-muted);"> - ${escapeHtml(doc.category || '')}</span>
                    </div>
                `).join('')}
            </div>
            ${visibleDocs.length > 0 ? `
                <div style="padding: var(--space-sm) var(--space-md); border-top: 1px solid var(--border);">
                    <button class="btn btn-sm btn-danger" onclick="executePatternHide()">
                        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>
                            <line x1="1" y1="1" x2="23" y2="23"/>
                        </svg>
                        Hide All ${visibleDocs.length} Matching Document(s)
                    </button>
                </div>
            ` : ''}
        `;
    } catch (error) {
        console.error('Error previewing pattern:', error);
        resultsEl.innerHTML = '<div style="padding: var(--space-sm); color: var(--danger); text-align: center; font-size: 0.85rem;">Preview failed</div>';
    }
}

async function executePatternHide() {
    const input = document.getElementById('visibility-pattern-input');
    const pattern = input ? input.value.trim() : '';
    
    if (!pattern) return;
    
    if (!confirm(`Are you sure you want to hide all documents matching "${pattern}"?\n\nMatching documents will become inaccessible to the public.`)) {
        return;
    }
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents/bulk-hide-by-pattern`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename_pattern: pattern })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Bulk hide by pattern failed');
        }
        
        const result = await response.json();
        showBulkFeedback(`Successfully hidden ${result.hidden_count} document(s) matching "${pattern}"`);
        
        // Refresh
        previewPatternHide();
        loadHiddenDocuments();
        searchDocumentsForVisibility();
    } catch (error) {
        console.error('Error hiding by pattern:', error);
        alert('Error: ' + error.message);
    }
}

// --- CSV Paste / Upload Bulk Actions ---

function parseCsvFilenames(text) {
    // Split into rows by newlines first (preserves CSV column structure)
    const rows = text.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    const filenames = [];
    
    for (const row of rows) {
        // Check if this row has commas (CSV format) or is a plain filename
        let filename;
        if (row.includes(',')) {
            // CSV row: extract the first column only, ignore Category/Subcategory/DOJ URL etc.
            filename = row.split(',')[0].trim().replace(/^["']|["']$/g, '');
        } else if (row.includes(';')) {
            // Semicolon-delimited: take first column
            filename = row.split(';')[0].trim().replace(/^["']|["']$/g, '');
        } else {
            // Plain filename (one per line)
            filename = row.trim().replace(/^["']|["']$/g, '');
        }
        
        if (!filename) continue;
        // Skip header rows
        if (/^(filename|file_name|file|name|document|id)$/i.test(filename)) continue;
        filenames.push(filename);
    }
    
    return [...new Set(filenames)]; // Deduplicate
}

function handleCsvFileUpload(input) {
    const file = input.files[0];
    if (!file) return;
    
    const reader = new FileReader();
    reader.onload = function(e) {
        const text = e.target.result;
        const textarea = document.getElementById('csv-bulk-textarea');
        if (textarea) {
            textarea.value = text;
        }
        updateCsvCount();
    };
    reader.readAsText(file);
    // Reset input so same file can be re-selected
    input.value = '';
}

function updateCsvCount() {
    const textarea = document.getElementById('csv-bulk-textarea');
    const countEl = document.getElementById('csv-bulk-count');
    if (!textarea || !countEl) return;
    
    const filenames = parseCsvFilenames(textarea.value);
    countEl.textContent = filenames.length > 0 ? `${filenames.length} filename(s) detected` : '';
}

async function executeCsvBulkAction() {
    const textarea = document.getElementById('csv-bulk-textarea');
    const actionSelect = document.getElementById('csv-bulk-action');
    const resultsEl = document.getElementById('csv-bulk-results');
    
    if (!textarea || !actionSelect) return;
    
    const filenames = parseCsvFilenames(textarea.value);
    const action = actionSelect.value;
    
    if (filenames.length === 0) {
        if (resultsEl) resultsEl.innerHTML = '<div style="padding: var(--space-sm); color: var(--warning); font-size: 0.85rem;">No valid filenames found. Paste filenames one per line or comma-separated.</div>';
        return;
    }
    
    const actionLabel = action === 'hide' ? 'hide' : 'unhide';
    if (!confirm(`Are you sure you want to ${actionLabel} ${filenames.length} document(s)?\n\nFirst few filenames:\n${filenames.slice(0, 5).join('\n')}${filenames.length > 5 ? '\n...' : ''}`)) {
        return;
    }
    
    if (resultsEl) resultsEl.innerHTML = '<div class="loading"><span class="spinner"></span>Processing...</div>';
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents/bulk-hide-by-filenames`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filenames, action })
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || `Bulk ${actionLabel} failed`);
        }
        
        const result = await response.json();
        const affectedCount = result.hidden_count || result.unhidden_count || 0;
        const notFound = result.not_found || [];
        const alreadyState = result.already_hidden || result.already_visible || [];
        
        let html = `<div style="padding: var(--space-sm); font-size: 0.85rem; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-tertiary);">`;
        html += `<div style="color: var(--success, #10b981); font-weight: 500; margin-bottom: var(--space-xs);">Successfully ${action === 'hide' ? 'hidden' : 'unhidden'} ${affectedCount} document(s)</div>`;
        
        if (alreadyState.length > 0) {
            html += `<div style="color: var(--text-muted); margin-bottom: var(--space-xs);">${alreadyState.length} already ${action === 'hide' ? 'hidden' : 'visible'}</div>`;
        }
        
        if (notFound.length > 0) {
            html += `<details style="margin-top: var(--space-xs);">`;
            html += `<summary style="color: var(--warning); cursor: pointer;">${notFound.length} filename(s) not found in database</summary>`;
            html += `<div style="max-height: 150px; overflow-y: auto; margin-top: var(--space-xs); padding: var(--space-xs); background: var(--bg-secondary); border-radius: 4px; font-family: var(--font-mono); font-size: 0.75rem;">`;
            html += notFound.map(f => escapeHtml(f)).join('<br>');
            html += `</div></details>`;
        }
        
        html += `</div>`;
        if (resultsEl) resultsEl.innerHTML = html;
        
        showBulkFeedback(`${action === 'hide' ? 'Hidden' : 'Unhidden'} ${affectedCount} document(s) from CSV`);
        loadHiddenDocuments();
        searchDocumentsForVisibility();
    } catch (error) {
        console.error(`Error bulk ${actionLabel}:`, error);
        if (resultsEl) resultsEl.innerHTML = `<div style="padding: var(--space-sm); color: var(--danger); font-size: 0.85rem;">Error: ${escapeHtml(error.message)}</div>`;
    }
}

// Update count as user types in the textarea
document.addEventListener('DOMContentLoaded', function() {
    const textarea = document.getElementById('csv-bulk-textarea');
    if (textarea) {
        textarea.addEventListener('input', updateCsvCount);
    }
});

// --- Core Load/Render Functions ---

async function loadHiddenDocuments() {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/hidden-documents?limit=500`);
        if (!response.ok) throw new Error('Failed to load hidden documents');
        const data = await response.json();
        
        hiddenDocumentsData = data.hidden_documents || [];
        
        // Update count
        const countEl = document.getElementById('hidden-docs-count');
        if (countEl) countEl.textContent = data.total || 0;
        
        // Clear selection for docs no longer in the list
        const currentIds = new Set(hiddenDocumentsData.map(d => d.id));
        for (const id of selectedHiddenDocIds) {
            if (!currentIds.has(id)) selectedHiddenDocIds.delete(id);
        }
        
        renderHiddenDocuments();
    } catch (error) {
        console.error('Error loading hidden documents:', error);
        const el = document.getElementById('hidden-docs-list');
        if (el) el.innerHTML = '<div style="color: var(--danger);">Error loading hidden documents</div>';
    }
}

function renderHiddenDocuments() {
    const el = document.getElementById('hidden-docs-list');
    if (!el) return;
    
    if (!hiddenDocumentsData || hiddenDocumentsData.length === 0) {
        el.innerHTML = `
            <div style="padding: var(--space-md); text-align: center; color: var(--text-muted);">
                <svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="2" style="margin-bottom: var(--space-sm); opacity: 0.5;">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                    <circle cx="12" cy="12" r="3"/>
                </svg>
                <p>No documents are currently hidden</p>
            </div>
        `;
        updateHiddenBulkBar();
        return;
    }
    
    const allSelected = hiddenDocumentsData.length > 0 && hiddenDocumentsData.every(d => selectedHiddenDocIds.has(d.id));
    
    el.innerHTML = `
        <div style="display: flex; align-items: center; gap: var(--space-sm); padding: var(--space-sm) var(--space-md); border-bottom: 1px solid var(--border); background: var(--bg-tertiary);">
            <input type="checkbox" id="hidden-select-all" onchange="toggleSelectAllHidden()" ${allSelected ? 'checked' : ''} style="cursor: pointer;">
            <label for="hidden-select-all" style="font-size: 0.8rem; color: var(--text-muted); cursor: pointer;">Select All (${hiddenDocumentsData.length})</label>
        </div>
        <div style="max-height: 300px; overflow-y: auto;">
            ${hiddenDocumentsData.map(doc => `
                <div style="display: flex; align-items: center; gap: var(--space-sm); padding: var(--space-sm) var(--space-md); border-bottom: 1px solid var(--border);">
                    <input type="checkbox" id="hidden-cb-${escapeHtml(doc.id)}" onchange="toggleHiddenDocSelection('${escapeHtml(doc.id)}')" ${selectedHiddenDocIds.has(doc.id) ? 'checked' : ''} style="cursor: pointer; flex-shrink: 0;">
                    <div style="flex: 1; min-width: 0;">
                        <div style="font-family: var(--font-mono); font-size: 0.85rem; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(doc.filename)}">
                            ${escapeHtml(doc.filename)}
                        </div>
                        <div style="font-size: 0.75rem; color: var(--text-muted);">
                            ${escapeHtml(doc.category || 'Unknown')} • ${escapeHtml(doc.subcategory || '')}
                        </div>
                    </div>
                    <button class="btn btn-sm" onclick="unhideDocument('${escapeHtml(doc.id)}')" style="flex-shrink: 0;">
                        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                            <circle cx="12" cy="12" r="3"/>
                        </svg>
                        Unhide
                    </button>
                </div>
            `).join('')}
        </div>
    `;
    updateHiddenBulkBar();
}

async function searchDocumentsForVisibility() {
    const searchInput = document.getElementById('visibility-doc-search');
    const query = searchInput ? searchInput.value.trim() : '';
    
    const resultsEl = document.getElementById('visibility-search-results');
    if (!resultsEl) return;
    
    if (!query) {
        lastSearchResults = [];
        selectedSearchDocIds.clear();
        updateBulkActionBar();
        resultsEl.innerHTML = '<div style="padding: var(--space-md); color: var(--text-muted); text-align: center;">Search for documents to manage their visibility</div>';
        return;
    }
    
    resultsEl.innerHTML = '<div class="loading"><span class="spinner"></span>Searching...</div>';
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents-visibility?search=${encodeURIComponent(query)}&limit=200`);
        if (!response.ok) throw new Error('Search failed');
        const data = await response.json();
        
        if (!data.documents || data.documents.length === 0) {
            lastSearchResults = [];
            selectedSearchDocIds.clear();
            updateBulkActionBar();
            resultsEl.innerHTML = '<div style="padding: var(--space-md); color: var(--text-muted); text-align: center;">No documents found</div>';
            return;
        }
        
        lastSearchResults = data.documents;
        
        // Prune selection: remove IDs no longer in results
        const resultIds = new Set(data.documents.map(d => d.id));
        for (const id of selectedSearchDocIds) {
            if (!resultIds.has(id)) selectedSearchDocIds.delete(id);
        }
        
        const allSelected = lastSearchResults.length > 0 && lastSearchResults.every(d => selectedSearchDocIds.has(d.id));
        
        resultsEl.innerHTML = `
            <div style="display: flex; align-items: center; gap: var(--space-sm); padding: var(--space-sm) var(--space-md); border-bottom: 1px solid var(--border); background: var(--bg-tertiary); position: sticky; top: 0; z-index: 1;">
                <input type="checkbox" id="search-select-all" onchange="toggleSelectAllSearch()" ${allSelected ? 'checked' : ''} style="cursor: pointer;">
                <label for="search-select-all" style="font-size: 0.8rem; color: var(--text-muted); cursor: pointer;">Select All (${data.documents.length})</label>
            </div>
        ` + data.documents.map(doc => {
            const isHidden = doc.is_hidden === 1;
            const isSelected = selectedSearchDocIds.has(doc.id);
            return `
                <div style="display: flex; align-items: center; gap: var(--space-sm); padding: var(--space-sm) var(--space-md); border-bottom: 1px solid var(--border); ${isHidden ? 'background: var(--danger-dim);' : ''}">
                    <input type="checkbox" id="search-cb-${escapeHtml(doc.id)}" onchange="toggleSearchDocSelection('${escapeHtml(doc.id)}')" ${isSelected ? 'checked' : ''} style="cursor: pointer; flex-shrink: 0;">
                    <div style="flex: 1; min-width: 0;">
                        <div style="font-family: var(--font-mono); font-size: 0.85rem; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(doc.filename)}">
                            ${isHidden ? '🔒 ' : ''}${escapeHtml(doc.filename)}
                        </div>
                        <div style="font-size: 0.75rem; color: var(--text-muted);">
                            ${escapeHtml(doc.category || 'Unknown')} • ${escapeHtml(doc.subcategory || '')}
                        </div>
                    </div>
                    ${isHidden ? `
                        <button class="btn btn-sm" onclick="unhideDocument('${escapeHtml(doc.id)}'); searchDocumentsForVisibility();">
                            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                                <circle cx="12" cy="12" r="3"/>
                            </svg>
                            Unhide
                        </button>
                    ` : `
                        <button class="btn btn-sm btn-danger" onclick="hideDocument('${escapeHtml(doc.id)}'); searchDocumentsForVisibility();">
                            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>
                                <line x1="1" y1="1" x2="23" y2="23"/>
                            </svg>
                            Hide
                        </button>
                    `}
                </div>
            `;
        }).join('');
        
        updateBulkActionBar();
        
    } catch (error) {
        console.error('Error searching documents:', error);
        resultsEl.innerHTML = '<div style="padding: var(--space-md); color: var(--danger); text-align: center;">Search failed</div>';
    }
}

async function hideDocument(docId) {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents/${encodeURIComponent(docId)}/hide`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            const msg = await safeErrorMessage(response, 'Failed to hide document');
            throw new Error(msg);
        }
        
        loadHiddenDocuments();
    } catch (error) {
        console.error('Error hiding document:', error);
        alert('Error: ' + error.message);
    }
}

async function unhideDocument(docId) {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents/${encodeURIComponent(docId)}/unhide`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            const msg = await safeErrorMessage(response, 'Failed to unhide document');
            throw new Error(msg);
        }
        
        loadHiddenDocuments();
    } catch (error) {
        console.error('Error unhiding document:', error);
        alert('Error: ' + error.message);
    }
}

// =============================================================================
// Category Visibility Management
// =============================================================================

async function loadCategoryVisibility() {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/categories-visibility`);
        if (!response.ok) throw new Error('Failed to load categories');
        const data = await response.json();
        
        categoryVisibilityData = data.categories || [];
        renderCategoryVisibility();
    } catch (error) {
        console.error('Error loading category visibility:', error);
        const el = document.getElementById('category-visibility-list');
        if (el) el.innerHTML = '<div style="color: var(--danger);">Error loading categories</div>';
    }
}

function renderCategoryVisibility() {
    const el = document.getElementById('category-visibility-list');
    if (!el) return;
    
    if (!categoryVisibilityData || categoryVisibilityData.length === 0) {
        el.innerHTML = '<div style="padding: var(--space-md); text-align: center; color: var(--text-muted);">No categories found</div>';
        return;
    }
    
    el.innerHTML = `
        <div style="display: grid; gap: var(--space-sm);">
            ${categoryVisibilityData.map(cat => {
                const isHidden = cat.is_hidden === 1;
                return `
                    <div style="display: flex; align-items: center; justify-content: space-between; padding: var(--space-md); background: ${isHidden ? 'var(--danger-dim)' : 'var(--bg-tertiary)'}; border: 1px solid ${isHidden ? 'var(--danger)' : 'var(--border)'}; border-radius: 6px;">
                        <div>
                            <div style="font-weight: 500; color: var(--text-primary);">
                                ${isHidden ? '🔒 ' : ''}${escapeHtml(cat.category)}
                            </div>
                            <div style="font-size: 0.8rem; color: var(--text-muted);">
                                ${formatNumber(cat.document_count)} documents
                            </div>
                        </div>
                        ${isHidden ? `
                            <button class="btn btn-sm" onclick="unhideCategory('${escapeHtml(cat.category)}')">
                                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                                    <circle cx="12" cy="12" r="3"/>
                                </svg>
                                Show Category
                            </button>
                        ` : `
                            <button class="btn btn-sm btn-danger" onclick="hideCategory('${escapeHtml(cat.category)}')">
                                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                                    <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>
                                    <line x1="1" y1="1" x2="23" y2="23"/>
                                </svg>
                                Hide Category
                            </button>
                        `}
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

async function hideCategory(category) {
    if (!confirm(`Are you sure you want to hide the "${category}" category?\n\nAll ${categoryVisibilityData.find(c => c.category === category)?.document_count || 0} documents in this category will become inaccessible to the public.`)) {
        return;
    }
    
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/categories/${encodeURIComponent(category)}/hide`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to hide category');
        }
        
        loadCategoryVisibility();
    } catch (error) {
        console.error('Error hiding category:', error);
        alert('Error: ' + error.message);
    }
}

async function unhideCategory(category) {
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/categories/${encodeURIComponent(category)}/unhide`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || 'Failed to unhide category');
        }
        
        loadCategoryVisibility();
    } catch (error) {
        console.error('Error unhiding category:', error);
        alert('Error: ' + error.message);
    }
}

// Export Visibility Management functions
window.loadHiddenDocuments = loadHiddenDocuments;
window.searchDocumentsForVisibility = searchDocumentsForVisibility;
window.hideDocument = hideDocument;
window.unhideDocument = unhideDocument;
window.toggleSearchDocSelection = toggleSearchDocSelection;
window.toggleSelectAllSearch = toggleSelectAllSearch;
window.bulkHideSelected = bulkHideSelected;
window.bulkUnhideSelected = bulkUnhideSelected;
window.toggleHiddenDocSelection = toggleHiddenDocSelection;
window.toggleSelectAllHidden = toggleSelectAllHidden;
window.bulkUnhideFromHiddenList = bulkUnhideFromHiddenList;
window.previewPatternHide = previewPatternHide;
window.executePatternHide = executePatternHide;
window.handleCsvFileUpload = handleCsvFileUpload;
window.executeCsvBulkAction = executeCsvBulkAction;
window.loadCategoryVisibility = loadCategoryVisibility;
window.hideCategory = hideCategory;
window.unhideCategory = unhideCategory;

// =============================================================================
// File Type Reclassification
// =============================================================================

let _reclassifySelectedIds = new Set();

function _fileTypeBadgeColor(ft) {
    const colors = { pdf: '#6366f1', document: '#22c55e', image: '#f59e0b', audio: '#06b6d4', video: '#ec4899' };
    return colors[ft] || '#888';
}

async function searchDocsForReclassify() {
    const query = (document.getElementById('reclassify-search')?.value || '').trim();
    const typeFilter = document.getElementById('reclassify-type-filter')?.value || '';
    const resultsEl = document.getElementById('reclassify-results');
    if (!resultsEl) return;

    if (!query && !typeFilter) {
        resultsEl.innerHTML = '<div style="padding: var(--space-md); color: var(--text-muted); text-align: center; font-size: 0.85rem;">Enter a search term or select a file type filter</div>';
        return;
    }

    resultsEl.innerHTML = '<div class="loading"><span class="spinner"></span>Searching...</div>';
    _reclassifySelectedIds.clear();
    _updateReclassifyBulkBar();

    try {
        let url = `${window.location.origin}/api/admin/documents-visibility?limit=200`;
        if (query) url += `&search=${encodeURIComponent(query)}`;
        if (typeFilter) url += `&file_type=${encodeURIComponent(typeFilter)}`;

        const response = await authFetch(url);
        if (!response.ok) throw new Error('Search failed');
        const data = await response.json();
        const docs = data.documents || [];

        if (docs.length === 0) {
            resultsEl.innerHTML = '<div style="padding: var(--space-md); color: var(--text-muted); text-align: center;">No documents found</div>';
            return;
        }

        let html = `<div style="padding: 6px 12px; background: var(--bg-elevated); border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: var(--space-sm); font-size: 0.8rem; color: var(--text-muted);">
            <label style="cursor:pointer;"><input type="checkbox" onchange="toggleSelectAllReclassify(this.checked)" style="margin-right:4px;">Select all</label>
            <span style="margin-left:auto;">${docs.length} result${docs.length !== 1 ? 's' : ''}</span>
        </div>`;

        for (const doc of docs) {
            const badgeColor = _fileTypeBadgeColor(doc.file_type);
            html += `
            <div style="display: flex; align-items: center; gap: var(--space-sm); padding: 8px 12px; border-bottom: 1px solid var(--border); font-size: 0.85rem;" data-reclassify-id="${escapeHtml(doc.id)}">
                <input type="checkbox" onchange="toggleReclassifySelection('${escapeHtml(doc.id)}', this.checked)" ${_reclassifySelectedIds.has(doc.id) ? 'checked' : ''}>
                <div style="flex:1; min-width:0;">
                    <div style="font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(doc.filename)}</div>
                    <div style="font-size:0.75rem; color:var(--text-muted);">${escapeHtml(doc.category || '')} ${doc.subcategory ? '› ' + escapeHtml(doc.subcategory) : ''} · ${doc.char_count ?? 0} chars</div>
                </div>
                <span style="padding:2px 8px; border-radius:4px; font-size:0.75rem; font-weight:600; color:#fff; background:${badgeColor}; white-space:nowrap;">${escapeHtml(doc.file_type || 'unknown')}</span>
                <select onchange="reclassifySingleDoc('${escapeHtml(doc.id)}', this.value, this)" style="padding:4px 6px; border-radius:4px; border:1px solid var(--border); background:var(--bg-elevated); color:var(--text); font-size:0.8rem; min-width:90px;">
                    <option value="">Change…</option>
                    ${['pdf','document','image','audio','video'].filter(t => t !== doc.file_type).map(t => `<option value="${t}">${t}</option>`).join('')}
                </select>
            </div>`;
        }
        resultsEl.innerHTML = html;
    } catch (error) {
        console.error('Reclassify search error:', error);
        resultsEl.innerHTML = `<div style="padding: var(--space-md); color: var(--danger); text-align: center;">Error: ${escapeHtml(error.message)}</div>`;
    }
}

function toggleReclassifySelection(docId, checked) {
    if (checked) _reclassifySelectedIds.add(docId);
    else _reclassifySelectedIds.delete(docId);
    _updateReclassifyBulkBar();
}

function toggleSelectAllReclassify(checked) {
    document.querySelectorAll('#reclassify-results input[type="checkbox"][onchange*="toggleReclassifySelection"]').forEach(cb => {
        cb.checked = checked;
        const id = cb.getAttribute('onchange').match(/'([^']+)'/)?.[1];
        if (id) { if (checked) _reclassifySelectedIds.add(id); else _reclassifySelectedIds.delete(id); }
    });
    _updateReclassifyBulkBar();
}

function _updateReclassifyBulkBar() {
    const bar = document.getElementById('reclassify-bulk-bar');
    const countEl = document.getElementById('reclassify-selected-count');
    if (!bar) return;
    const n = _reclassifySelectedIds.size;
    bar.style.display = n > 0 ? 'flex' : 'none';
    if (countEl) countEl.textContent = `${n} selected`;
}

async function reclassifySingleDoc(docId, newType, selectEl) {
    if (!newType) return;
    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents/reclassify`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ document_ids: [docId], file_type: newType })
        });
        if (!response.ok) { const d = await response.json(); throw new Error(d.detail || 'Failed'); }

        // Update the badge in-place
        const row = document.querySelector(`[data-reclassify-id="${docId}"]`);
        if (row) {
            const badge = row.querySelector('span[style*="border-radius:4px"]');
            if (badge) { badge.textContent = newType; badge.style.background = _fileTypeBadgeColor(newType); }
            // Rebuild the dropdown to exclude the new type
            const sel = row.querySelector('select');
            if (sel) {
                sel.innerHTML = `<option value="">Change…</option>` +
                    ['pdf','document','image','audio','video'].filter(t => t !== newType).map(t => `<option value="${t}">${t}</option>`).join('');
            }
        }
    } catch (error) {
        console.error('Reclassify error:', error);
        alert('Error: ' + error.message);
    }
    if (selectEl) selectEl.value = '';
}

async function bulkReclassifySelected() {
    const n = _reclassifySelectedIds.size;
    if (n === 0) return;
    const targetType = document.getElementById('reclassify-bulk-type')?.value;
    if (!targetType) { alert('Select a target file type'); return; }
    if (!confirm(`Reclassify ${n} document(s) as "${targetType}"?`)) return;

    try {
        const response = await authFetch(`${window.location.origin}/api/admin/documents/reclassify`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ document_ids: Array.from(_reclassifySelectedIds), file_type: targetType })
        });
        if (!response.ok) { const d = await response.json(); throw new Error(d.detail || 'Failed'); }
        const result = await response.json();
        alert(`Reclassified ${result.updated_count} document(s) as "${targetType}"`);
        _reclassifySelectedIds.clear();
        _updateReclassifyBulkBar();
        searchDocsForReclassify();
    } catch (error) {
        console.error('Bulk reclassify error:', error);
        alert('Error: ' + error.message);
    }
}

// Export File Type Reclassification functions
window.searchDocsForReclassify = searchDocsForReclassify;
window.toggleReclassifySelection = toggleReclassifySelection;
window.toggleSelectAllReclassify = toggleSelectAllReclassify;
window.reclassifySingleDoc = reclassifySingleDoc;
window.bulkReclassifySelected = bulkReclassifySelected;

