/* ===== SESSION KEY PROXY DASHBOARD JS ===== */

const BASE = window.location.origin;
let adminToken = localStorage.getItem('adminToken') || '';

// ── DOM refs ──────────────────────────────────────────────────────────────
const loginScreen   = document.getElementById('login-screen');
const dashboardScreen = document.getElementById('dashboard-screen');
const tokenInput    = document.getElementById('admin-token');
const loginForm     = document.getElementById('login-form');
const loginError    = document.getElementById('login-error');
const logoutBtn     = document.getElementById('logout-btn');
const healthBadge  = document.getElementById('health-badge');
const serviceUrl   = document.getElementById('service-url');
const statStatus   = document.getElementById('stat-status');
const statVersion  = document.getElementById('stat-version');
const statTime     = document.getElementById('stat-time');
const statAvailable = document.getElementById('stat-available');
const statBound     = document.getElementById('stat-bound');
const statTotal     = document.getElementById('stat-total');
const refreshAll   = document.getElementById('refresh-all');
const testProxy    = document.getElementById('test-proxy');
const keysList     = document.getElementById('keys-list');
const sessionsList = document.getElementById('sessions-list');
const reloadKeys   = document.getElementById('reload-keys');
const actionResult = document.getElementById('action-result');

// Add key
const btnAddKey    = document.getElementById('btn-add-key');
const addResult    = document.getElementById('add-result');
const newKeyId     = document.getElementById('new-key-id');
const newKeyLabel  = document.getElementById('new-key-label');
const newKeySecret = document.getElementById('new-key-secret');

// Bot connection
const botBase      = document.getElementById('bot-base');
const botToken     = document.getElementById('bot-token');
const btnToggle    = document.getElementById('btn-toggle-token');
const btnCopy      = document.getElementById('btn-copy-token');
const curlExample  = document.getElementById('curl-example');

// Modal
const editModal    = document.getElementById('edit-modal');
const editKeyTitle = document.getElementById('edit-key-title');
const editKeyId    = document.getElementById('edit-key-id');
const editKeyLabel = document.getElementById('edit-key-label');
const editKeySecret = document.getElementById('edit-key-secret');
const btnEditSave  = document.getElementById('btn-edit-save');
const btnEditCancel= document.getElementById('btn-edit-cancel');
const editResult   = document.getElementById('edit-result');

// API Tester
const apiMethod    = document.getElementById('api-method');
const apiUrl       = document.getElementById('api-url');
const apiBody      = document.getElementById('api-body');
const apiSend      = document.getElementById('api-send');
const apiResult    = document.getElementById('api-result');

// Quick actions
const btnResetKey  = document.getElementById('btn-reset-key');
const btnUnbind    = document.getElementById('btn-unbind');
const btnSetState  = document.getElementById('btn-set-state');
const btnSyncKeys  = document.getElementById('btn-sync-keys-json');

// DeepSeek API Settings
const btnSaveApiKey = document.getElementById('btn-save-api-key');
const btnPreviewKeys = document.getElementById('btn-preview-keys');
const apiSaveResult = document.getElementById('api-save-result');
const apiEmail     = document.getElementById('api-email');
const apiPassword  = document.getElementById('api-password');
const apiKeyIdInput = document.getElementById('api-key-id');
const apiKeySecretInput = document.getElementById('api-key-secret');
const apiKeyLabelInput = document.getElementById('api-key-label');

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
    botBase.textContent = BASE;
    botToken.textContent = adminToken;
    curlExample.textContent =
        `# اطلب حالة الخدمة\ncurl -sS ${BASE}/admin/status \\\n  -H "X-Admin-Token: $ADMIN_TOKEN"`;
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

let tokenHidden = true;

async function refreshAllData() {
    healthBadge.textContent = 'جاري التحميل...';
    healthBadge.className = 'badge gray';

    // Health
    try {
        const h = await fetch(`${BASE}/healthz`);
        const hjson = await h.json();
        healthBadge.textContent = '🟢 نشطة';
        healthBadge.className = 'badge green';
        statStatus.textContent = 'يعمل';
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
    sessionsList.innerHTML = '<p class="loading">جاري التحميل</p>';

    const { ok, data } = await api('GET', '/admin/status');
    if (!ok) {
        keysList.innerHTML = '<p class="loading">❌ فشل التحميل</p>';
        sessionsList.innerHTML = '<p class="loading">❌ فشل التحميل</p>';
        return;
    }

    // Counts
    const counts = data.counts || {};
    statAvailable.textContent = counts.available ?? '—';
    statBound.textContent = counts.bound ?? '—';
    statTotal.textContent = data.keys ? data.keys.length : '—';

    // Keys
    const keys = data.keys || [];
    if (!keys.length) {
        keysList.innerHTML = '<p class="loading">لا توجد مفاتيح، أضف من النموذج أعلاه</p>';
    } else {
        keysList.innerHTML = keys.map(k => {
            const sessions = (k.sessions || []).map(s => s.session_id).join(', ');
            return `
            <div class="key-item">
                <div class="key-info">
                    <span class="key-id">${escHtml(k.id)}</span>
                    <span class="key-label">${escHtml(k.label || k.id)}</span>
                    ${sessions ? `<span class="key-label" style="display:block;font-size:11px">🔗 ${sessions.map(escHtml).join(', ')}</span>` : ''}
                </div>
                <div style="text-align:left">
                    <span class="key-status status-${escHtml(k.status)}">${stateLabel(k.status)}</span>
                    <div class="key-actions">
                        <button class="btn-outline small-btn" onclick="openEdit('${escHtml(k.id)}','${escHtml(k.label || '')}')">✏️ تعديل</button>
                        <button class="btn-danger small-btn" onclick="deleteKey('${escHtml(k.id)}')">🗑️ حذف</button>
                    </div>
                </div>
            </div>`;
        }).join('');
    }

    // Sessions (from bindings)
    const bindings = data.bindings || [];
    if (!bindings.length) {
        sessionsList.innerHTML = '<p class="loading">لا توجد جلسات مرتبطة حالياً</p>';
    } else {
        sessionsList.innerHTML = bindings.map(b => {
            const boundAt = b.bound_at ? Math.round(b.bound_at * 1000) : null;
            return `
            <div class="session-item">
                <div class="session-info">
                    <span class="session-id">${escHtml(b.session_id)}</span>
                    <span class="session-label">${boundAt ? 'مُنذ ' + relativeTime(boundAt) : ''}</span>
                </div>
                <span class="key-status status-bound">${escHtml(b.key_id)}</span>
            </div>`;
        }).join('');
    }
}

// ── Test Proxy ────────────────────────────────────────────────────────────
async function testProxyEndpoint() {
    const { ok, data } = await api('GET', '/admin/status');
    showActionResult(ok ? 'success' : 'error',
        ok ? `✅ Proxy يعمل! عدد المفاتيح: ${data.keys ? data.keys.length : 0}` : `❌ خطأ: ${JSON.stringify(data)}`);
}

// ── Add Key ───────────────────────────────────────────────────────────────
btnAddKey.addEventListener('click', async () => {
    const keyId = newKeyId.value.trim();
    const secret = newKeySecret.value.trim();
    const label = newKeyLabel.value.trim();
    if (!keyId || !secret) {
        addResult.className = 'action-result error';
        addResult.textContent = '❌ أدخل معرّف المفتاح والمفتاح السري';
        addResult.classList.remove('hidden');
        return;
    }
    const { ok, status, data } = await api('POST', '/admin/add-key', { key_id: keyId, secret, label });
    showAddResult(ok, ok ? `✅ تمت إضافة ${keyId}` : (data && data.detail ? `❌ ${data.detail}` : `❌ HTTP ${status}`));
    if (ok) {
        newKeyId.value = '';
        newKeySecret.value = '';
        newKeyLabel.value = '';
        loadStatus();
    }
});

function showAddResult(ok, msg) {
    addResult.className = `action-result ${ok ? 'success' : 'error'}`;
    addResult.textContent = msg;
    addResult.classList.remove('hidden');
    setTimeout(() => addResult.classList.add('hidden'), 8000);
}

// ── Edit Modal ────────────────────────────────────────────────────────────
window.openEdit = function(keyId, label) {
    editKeyTitle.textContent = keyId;
    editKeyId.value = keyId;
    editKeyLabel.value = label || '';
    editKeySecret.value = '';
    editResult.classList.add('hidden');
    editModal.classList.remove('hidden');
};

btnEditCancel.addEventListener('click', () => {
    editModal.classList.add('hidden');
});

btnEditSave.addEventListener('click', async () => {
    const keyId = editKeyId.value.trim();
    const payload = { label: editKeyLabel.value.trim() };
    if (editKeySecret.value.trim()) payload.secret = editKeySecret.value.trim();
    const { ok, data } = await api('PUT', `/admin/update-key/${encodeURIComponent(keyId)}`, payload);
    editResult.className = `action-result ${ok ? 'success' : 'error'}`;
    editResult.textContent = ok ? '✅ تم الحفظ' : `❌ ${data && data.detail ? data.detail : 'فشل'}`;
    editResult.classList.remove('hidden');
    if (ok) {
        setTimeout(() => editModal.classList.add('hidden'), 800);
        loadStatus();
    }
});

// ── Delete Key ────────────────────────────────────────────────────────────
window.deleteKey = async function(keyId) {
    if (!confirm(`هل أنت متأكد من حذف المفتاح "${keyId}"؟ ستفقد كل حوال إن كان مرتبط.`)) return;
    const { ok, data } = await api('DELETE', `/admin/delete-key/${encodeURIComponent(keyId)}`);
    showActionResult(ok ? 'success' : 'error', ok ? `🗑️ تم حذف ${keyId}` : `❌ ${data && data.detail ? data.detail : 'فشل'}`);
    if (ok) loadStatus();
};

// ── Sync keys.json ────────────────────────────────────────────────────────
btnSyncKeys.addEventListener('click', async () => {
    const { ok, data } = await api('POST', '/admin/reload-keys');
    showActionResult(ok ? 'success' : 'error', ok ? `✅ ${data && data.message ? data.message : 'تمت المزامنة'}` : `❌ ${JSON.stringify(data)}`);
    if (ok) loadStatus();
});

// ── Bot Connection ────────────────────────────────────────────────────────
btnToggle.addEventListener('click', () => {
    tokenHidden = !tokenHidden;
    botToken.style.filter = tokenHidden ? 'blur(4px)' : 'none';
    btnToggle.textContent = tokenHidden ? 'إظهار' : 'إخفاء';
});

btnCopy.addEventListener('click', async () => {
    try {
        await navigator.clipboard.writeText(adminToken);
        btnCopy.textContent = '✓ تم';
        setTimeout(() => btnCopy.textContent = 'نسخ', 1500);
    } catch {
        // fallback
        const ta = document.createElement('textarea');
        ta.value = adminToken;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        btnCopy.textContent = '✓ تم';
        setTimeout(() => btnCopy.textContent = 'نسخ', 1500);
    }
});

// ── API Tester ────────────────────────────────────────────────────────────
apiMethod.addEventListener('change', () => {
    apiBody.classList.toggle('hidden', apiMethod.value === 'GET');
});

apiSend.addEventListener('click', async () => {
    const method = apiMethod.value;
    const path = apiUrl.value.trim();
    const body = apiBody.value.trim();
    apiResult.classList.remove('hidden');
    apiResult.textContent = 'جاري...';
    try {
        const parsed = body ? JSON.parse(body) : null;
        const { ok, status, data } = await api(method, path, (method === 'GET' || !body) ? null : parsed);
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
    showActionResult(ok ? 'success' : 'error', ok ? `✅ ${JSON.stringify(data)}` : `❌ ${data && data.detail ? data.detail : JSON.stringify(data)}`);
    if (ok) loadStatus();
});

btnUnbind.addEventListener('click', async () => {
    const sessionId = document.getElementById('unbind-session').value.trim();
    if (!sessionId) { showActionResult('error', '❌ أدخل session-id'); return; }
    const { ok, data } = await api('POST', `/admin/unbind/${encodeURIComponent(sessionId)}`);
    showActionResult(ok ? 'success' : 'error', ok ? `✅ تم فك ربط ${sessionId}` : `❌ ${data && data.detail ? data.detail : JSON.stringify(data)}`);
    if (ok) loadStatus();
});

btnSetState.addEventListener('click', async () => {
    const keyId = document.getElementById('set-key-id').value.trim();
    const state = document.getElementById('set-state').value;
    if (!keyId) { showActionResult('error', '❌ أدخل key-id'); return; }
    const { ok, data } = await api('POST', '/admin/set-key-state', { key_id: keyId, state });
    showActionResult(ok ? 'success' : 'error', ok ? `✅ ${JSON.stringify(data)}` : `❌ ${data && data.detail ? data.detail : JSON.stringify(data)}`);
    if (ok) loadStatus();
});

// ── DeepSeek API Settings ─────────────────────────────────────────────────
btnSaveApiKey.addEventListener('click', async () => {
    const keyId = apiKeyIdInput.value.trim();
    const secret = apiKeySecretInput.value.trim();
    const label = apiKeyLabelInput.value.trim();
    if (!keyId || !secret) {
        apiSaveResult.className = 'action-result error';
        apiSaveResult.textContent = '❌ أدخل معرّف المفتاح والمفتاح السري (sk-...)';
        apiSaveResult.classList.remove('hidden');
        return;
    }
    // If email and password provided, append them to label for reference
    const emailVal = apiEmail.value.trim();
    const passVal = apiPassword.value.trim();
    let finalLabel = label;
    if (emailVal) finalLabel = finalLabel ? `${finalLabel} | 📧 ${emailVal}` : `📧 ${emailVal}`;
    if (passVal) finalLabel = finalLabel ? `${finalLabel} | 🔐` : `🔐`;
    const { ok, status: st, data } = await api('POST', '/admin/add-key', {
        key_id: keyId,
        secret: secret,
        label: finalLabel || keyId,
    });
    apiSaveResult.className = `action-result ${ok ? 'success' : 'error'}`;
    if (ok) {
        apiSaveResult.textContent = `✅ تم حفظ مفتاح "${keyId}" في الخدمة مؤقتاً! (للحفظ الدائم حدّث keys.json)`;
        apiEmail.value = '';
        apiPassword.value = '';
        apiKeyIdInput.value = '';
        apiKeySecretInput.value = '';
        apiKeyLabelInput.value = '';
        loadStatus();
    } else {
        apiSaveResult.textContent = `❌ ${data && data.detail ? data.detail : `HTTP ${st}`}`;
    }
    apiSaveResult.classList.remove('hidden');
    setTimeout(() => apiSaveResult.classList.add('hidden'), 10000);
});

btnPreviewKeys.addEventListener('click', async () => {
    const { ok, data } = await api('GET', '/admin/status');
    if (!ok) {
        alert('❌ فشل تحميل المفاتيح');
        return;
    }
    const keys = data.keys || [];
    if (!keys.length) {
        alert('لا توجد مفاتيح حالياً');
        return;
    }
    const lines = keys.map(k =>
        `🔑 ${k.id}\n   Label: ${k.label || '—'}\n   Status: ${stateLabel(k.status)}\n   Secret: ${k.masked}`
    ).join('\n\n');
    alert(`📋 المفاتيح الحالية (مُقنّعة):\n\n${lines}\n\n⚠️ للماحظة: secrets الحقيقية لا تظهر — راجع keys.json للحصول عليها.`);
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
    return map[s] || s || '—';
}

function relativeTime(ms) {
    if (!ms) return '—';
    const diff = Date.now() - ms;
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

// Init token display blur
if (adminToken) {
    botToken.textContent = adminToken;
    tokenHidden = true;
    botToken.style.filter = 'blur(4px)';
}
