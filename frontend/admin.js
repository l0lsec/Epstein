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

async function loadFeedbackData() {
    try {
        const response = await authFetch(`${API_BASE}/feedback`);
        if (!response.ok) throw new Error('Failed to load feedback');
        const data = await response.json();
        
        allFeedbackData = data.feedback || [];
        
        renderFeedbackStats(data);
        renderFeedbackTypes(data.type_counts);
        renderFeedbackTable(allFeedbackData);
        
        // Setup filter event listener
        setupFeedbackFilter();
    } catch (error) {
        console.error('Error loading feedback:', error);
        document.getElementById('feedback-stats').innerHTML = 
            '<div class="stat-card danger"><div class="stat-value">Error</div><div class="stat-label">Failed to load feedback</div></div>';
    }
}

function setupFeedbackFilter() {
    const filter = document.getElementById('feedback-type-filter');
    if (!filter) return;
    
    // Remove existing listener if any
    filter.removeEventListener('change', handleFeedbackFilter);
    filter.addEventListener('change', handleFeedbackFilter);
}

function handleFeedbackFilter() {
    const filter = document.getElementById('feedback-type-filter');
    const selectedType = filter.value;
    
    if (selectedType === 'all') {
        renderFeedbackTable(allFeedbackData);
    } else {
        const filtered = allFeedbackData.filter(fb => fb.type === selectedType);
        renderFeedbackTable(filtered);
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
        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: var(--space-xl);">No feedback submissions yet</td></tr>';
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
        const email = fb.email || '<span style="color: var(--text-muted);">—</span>';
        const messagePreview = fb.message ? truncate(fb.message, 60) : '';
        const ip = fb.ip || 'Unknown';
        
        return `
            <tr>
                <td style="white-space: nowrap; font-size: 0.85rem; color: var(--text-muted);">${date}</td>
                <td>${typeLabel}</td>
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
        
        // Remove from local data
        allFeedbackData = allFeedbackData.filter(fb => fb.id !== feedbackId);
        
        // Re-render with current filter
        handleFeedbackFilter();
        
        // Reload full data to update stats
        loadFeedbackData();
        
    } catch (error) {
        console.error('Error deleting feedback:', error);
        alert(`Error: ${error.message}`);
    }
}

// ============================================================================
// CONTENT MANAGEMENT FUNCTIONS (Ask AI Toggle + Pinned Documents)
// ============================================================================

async function loadContentData() {
    await Promise.all([
        loadAskAIStatus(),
        loadPinnedDocuments()
    ]);
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
            // Use the documents API with filename filter
            const response = await fetch(`${window.location.origin}/api/documents?filename=${encodeURIComponent(query)}&limit=20`);
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

// Global refresh function
window.refreshAll = refreshAll;
window.clearLogs = clearLogs;
window.openFeedbackModal = openFeedbackModal;
window.closeFeedbackModal = closeFeedbackModal;
window.deleteFeedback = deleteFeedback;
window.toggleAskAI = toggleAskAI;
window.openPinDocumentModal = openPinDocumentModal;
window.closePinDocumentModal = closePinDocumentModal;
window.searchDocumentsForPin = searchDocumentsForPin;
window.selectDocumentForPin = selectDocumentForPin;
window.submitPinDocument = submitPinDocument;
window.unpinDocument = unpinDocument;
window.editPinnedDocument = editPinnedDocument;
window.closeEditPinnedModal = closeEditPinnedModal;
window.submitEditPinned = submitEditPinned;

