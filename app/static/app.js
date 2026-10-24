/* ===== SESSION KEY PROXY DASHBOARD JS ===== */

const BASE = window.location.origin;
let adminToken = localStorage.getItem('adminToken') || '';

// ── DOM refs ──────────────────────────────────────────────────────────────
const loginScreen  = document.getElementById('login-screen');
const dashboardScreen = document.getElementById('dashboard-screen');
const tokenInput   = document.getElementById('admin-token');
const loginForm    = document.getElementById('login-form');
const loginError   = document.getElementById('login-error');
const logoutBtn    = document.getElementById('logout-btn');
const healthBadge  = document.getElementById('health-badge');
const serviceUrl   = document.getElementById('service-url');
const statStatus   = document.getElementById('stat-status');
const statVersion  = document.getElementById('stat-version');
const statTime     = document.getElementById('stat-time');
const refreshAll   = document.getElementById('refresh-all');
const testProxy    = document.getElementById('test-proxy');
const keysList     = document.getElementById('keys-list');
const sessionsList = document.getElementById('sessions-list');
const reloadKeys   = document.getElementById('reload-keys');
const actionResult = document.getElementById('action-result');

// API Tester
const apiMethod    = document.getElementById('api-method');
const apiUrl       = document.getElementById('api-url');
const apiBody      = document.getElementById('api-body');
const apiSend      = document.getElementById('api-send');
const apiResult    = document.getElementById('api-result');

// Quick actions
const btnResetKey  = document.getElementById('btn-reset-key');
const btnReassign  = document.getElementById('btn-reassign');
const btnSetState  = document.getElementById('btn-set-state');

// ── Init ──────────────────────────────────────────────────────────────────
if (adminToken) {
    validateToken().then(ok => {
        if (ok) showDashboard();
        else showLogin();
    });
} else {
    showLogin();
}

// ── Auth ──────────────────────────────────────────────────────────────────
loginForm.addEventListener('submit', async e => {
    e.preventDefault();
    adminToken = tokenInput.value.trim();
    const ok = await validateToken();
    if (ok) {
        localStorage.setItem('adminToken', adminToken);
        showDashboard();
    } else {
        loginError.textContent = '❌ Token غير صحيح أو الخدمة لا تستجيب';
        loginError.classList.remove('hidden');
        adminToken = '';
    }
});

logoutBtn.addEventListener('click', () => {
    adminToken = '';
    localStorage.removeItem('adminToken');
    showLogin();
});

async function validateToken() {
    if (!adminToken) return false;
    try {
        const r = await fetch(`${BASE}/admin/status`, {
            headers: { 'X-Admin-Token': adminToken }
        });
        return r.ok;
    } catch { return false; }
}

// ── Screens ───────────────────────────────────────────────────────────────
function showLogin() {
    loginScreen.classList.remove('hidden');
    dashboardScreen.classList.add('hidden');
    tokenInput.value = '';
    loginError.classList.add('hidden');
}

function showDashboard() {
    loginScreen.classList.add('hidden');
    dashboardScreen.classList.remove('hidden');
    serviceUrl.textContent = BASE;
    refreshAllData();
}

// ── API Helper ────────────────────────────────────────────────────────────
async function api(method, path, body = null) {
    const opts = {
        method,
        headers: {
            'X-Admin-Token': adminToken,
            'Accept': 'application/json'
        }
    };
    if (body) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    const r = await fetch(`${BASE}${path}`, opts);
    const text = await r.text();
    let json;
    try { json = JSON.parse(text); } catch { json = text; }
    return { ok: r.ok, status: r.status, data: json };
}

// ── Refresh All ───────────────────────────────────────────────────────────
refreshAll.addEventListener('click', refreshAllData);
testProxy.addEventListener('click', testProxyEndpoint);
reloadKeys.addEventListener('click', loadStatus);

async function refreshAllData() {
    healthBadge.textContent = 'جاري التحميل...';
    healthBadge.className = 'badge gray';

    // Health
    try {
        const h = await fetch(`${BASE}/healthz`);
        const hjson = await h.json();
        healthBadge.textContent = '🟢 نشطة';
        healthBadge.className = 'badge green';
        statStatus.textContent = ' працює';
        statVersion.textContent = hjson.version || hjson.status || '—';
        statTime.textContent = new Date().toLocaleTimeString('ar-SA');
    } catch {
        healthBadge.textContent = '🔴 معطّلة';
        healthBadge.className = 'badge red';
        statStatus.textContent = 'خطأ';
    }

    await loadStatus();
}

async function loadStatus() {
    keysList.innerHTML = '<p class="loading">جاري التحميل</p>';
    sessionsList.innerHTML = '<p class="loading>جاري التحميل</p>';

    const { ok, data } = await api('GET', '/admin/status');
    if (!ok) {
        keysList.innerHTML = '<p class="loading">❌ فشل التحميل</p>';
        sessionsList.innerHTML = '<p class="loading">❌ فشل التحميل</p>';
        return;
    }

    // Keys
    const keys = data.keys || [];
    if (!keys.length) {
        keysList.innerHTML = '<p class="loading">لا توجد مفاتيح</p>';
    } else {
        keysList.innerHTML = keys.map(k => `
            <div class="key-item">
                <div class="key-info">
                    <span class="key-id">${escHtml(k.id)}</span>
                    <span class="key-label">${escHtml(k.label || k.id)}</span>
                </div>
                <div>
                    <span class="key-status status-${k.state}">${stateLabel(k.state)}</span>
                    ${k.session_id ? `<span class="key-label" style="margin-top:4px;display:block;text-align:right;font-size:11px">🔗 ${escHtml(k.session_id)}</span>` : ''}
                </div>
            </div>
        `).join('');
    }

    // Sessions
    const sessions = data.sessions || [];
    if (!sessions.length) {
        sessionsList.innerHTML = '<p class="loading">لا توجد جلسات</p>';
    } else {
        sessionsList.innerHTML = sessions.map(s => `
            <div class="session-item">
                <div class="session-info">
                    <span class="session-id">${escHtml(s.session_id)}</span>
                    <span class="session-label">مُنذ ${relativeTime(s.bound_at)}</span>
                </div>
                <span class="key-status status-${s.key_state}">${stateLabel(s.key_state)} ${s.key_id ? '('+escHtml(s.key_id)+')' : ''}</span>
            </div>
        `).join('');
    }
}

// ── Test Proxy ────────────────────────────────────────────────────────────
async function testProxyEndpoint() {
    const { ok, data } = await api('GET', '/admin/status');
    showActionResult(ok ? 'success' : 'error',
        ok ? `✅ Proxy يعمل! الحالة: ${JSON.stringify(data, null, 2)}` : `❌ خطأ: ${JSON.stringify(data)}`);
}

// ── API Tester ────────────────────────────────────────────────────────────
apiMethod.addEventListener('change', () => {
    apiBody.classList.toggle('hidden', apiMethod.value === 'GET');
});

apiSend.addEventListener('click', async () => {
    const method = apiMethod.value;
    const path = apiUrl.value.trim();
    const body = method === 'POST' ? apiBody.value.trim() : null;
    apiResult.classList.remove('hidden');
    apiResult.textContent = 'جاري...';
    try {
        const parsed = body ? JSON.parse(body) : null;
        const { ok, status, data } = await api(method, path, parsed);
        apiResult.textContent = `HTTP ${status} ${ok ? '✅' : '❌'}\n\n${JSON.stringify(data, null, 2)}`;
        apiResult.style.color = ok ? 'var(--green)' : 'var(--red)';
    } catch (e) {
        apiResult.textContent = `❌ خطأ: ${e.message}`;
        apiResult.style.color = 'var(--red)';
    }
});

// ── Quick Actions ─────────────────────────────────────────────────────────
btnResetKey.addEventListener('click', async () => {
    const keyId = document.getElementById('reset-key-id').value.trim();
    const target = document.getElementById('reset-target').value;
    if (!keyId) { showActionResult('error', '❌ أدخل key-id'); return; }
    const { ok, data } = await api('POST', '/admin/reset-key', { key_id: keyId, target_state: target });
    showActionResult(ok ? 'success' : 'error', ok ? `✅ ${JSON.stringify(data)}` : `❌ ${JSON.stringify(data)}`);
    if (ok) loadStatus();
});

btnReassign.addEventListener('click', async () => {
    const sessionId = document.getElementById('reassign-session').value.trim();
    const newKeyId = document.getElementById('reassign-key').value.trim();
    if (!sessionId || !newKeyId) { showActionResult('error', '❌ أدخل session-id و key-id'); return; }
    const { ok, data } = await api('POST', `/admin/reassign-session/${encodeURIComponent(sessionId)}`, { key_id: newKeyId });
    showActionResult(ok ? 'success' : 'error', ok ? `✅ ${JSON.stringify(data)}` : `❌ ${JSON.stringify(data)}`);
    if (ok) loadStatus();
});

btnSetState.addEventListener('click', async () => {
    const keyId = document.getElementById('set-key-id').value.trim();
    const state = document.getElementById('set-state').value;
    if (!keyId) { showActionResult('error', '❌ أدخل key-id'); return; }
    const { ok, data } = await api('POST', '/admin/set-key-state', { key_id: keyId, state });
    showActionResult(ok ? 'success' : 'error', ok ? `✅ ${JSON.stringify(data)}` : `❌ ${JSON.stringify(data)}`);
    if (ok) loadStatus();
});

// ── Helpers ───────────────────────────────────────────────────────────────
function escHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function stateLabel(s) {
    const map = {
        available: 'متاح',
        bound: 'مُربوط',
        cooldown: 'انتظار',
        exhausted: 'منتهي',
        invalid: 'غير صالح'
    };
    return map[s] || s;
}

function relativeTime(iso) {
    if (!iso) return '—';
    const diff = Date.now() - new Date(iso).getTime();
    const m = Math.floor(diff / 60000);
    if (m < 1) return 'الآن';
    if (m < 60) return `منذ ${m} دقيقة`;
    const h = Math.floor(m / 60);
    if (h < 24) return `منذ ${h} ساعة`;
    return `منذ ${Math.floor(h/24)} يوم`;
}

function showActionResult(type, msg) {
    actionResult.className = `action-result ${type}`;
    actionResult.textContent = msg;
    actionResult.classList.remove('hidden');
    setTimeout(() => actionResult.classList.add('hidden'), 8000);
}
