/* ─────────────────────────────────────────────────────────────────────────
   Hot Wheels Hunt — Frontend JavaScript
───────────────────────────────────────────────────────────────────────── */

const API = '';

// ── Utility ──────────────────────────────────────────────────────────────────

function fmtTime(isoStr) {
    if (!isoStr) return '—';
    const d = new Date(isoStr + 'Z');
    return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: true });
}

function fmtTimeShort(isoStr) {
    if (!isoStr) return '—';
    const d = new Date(isoStr + 'Z');
    return d.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: true });
}

function fmtAgo(isoStr) {
    if (!isoStr) return 'not yet checked';
    const ms = Date.now() - new Date(isoStr + 'Z').getTime();
    if (ms < 0 || ms < 1000) return 'just now';
    const s = Math.floor(ms / 1000);
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    return `${h}h ago`;
}

function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

let _toastTimer;
function showToast(msg, type = 'info') {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.className = `toast ${type}`;
    el.classList.remove('hidden');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => el.classList.add('hidden'), 4000);
}

// ── State ─────────────────────────────────────────────────────────────────────

let allProducts = [];
let allEvents = [];
let logFilter = 'all';
let _knownEventIds = new Set();
let _eventsPrimed = false;
const NEW_BADGE_MINUTES = 10;   // show NEW badge for products added within N minutes

// ── Browser Notifications ─────────────────────────────────────────────────────

function updateNotifBanner() {
    const banner = document.getElementById('notif-banner');
    if (!banner) return;
    if (!('Notification' in window)) { banner.style.display = 'none'; return; }
    if (Notification.permission === 'granted') {
        banner.style.display = 'none';
    } else if (Notification.permission === 'denied') {
        banner.innerHTML = '🔕 Notifications blocked. <a href="javascript:void(0)" onclick="showNotifHelp()">How to re-enable</a>';
        banner.className = 'notif-banner notif-denied';
        banner.style.display = 'flex';
    } else {
        banner.innerHTML = '🔔 Enable notifications to get instant alerts when new Hot Wheels are available. <button onclick="askNotifPermission()" class="btn btn-primary" style="padding:4px 12px;font-size:.8rem">Enable Notifications</button>';
        banner.className = 'notif-banner notif-prompt';
        banner.style.display = 'flex';
    }
}

window.askNotifPermission = async function () {
    if (!('Notification' in window)) return;
    const result = await Notification.requestPermission();
    updateNotifBanner();
    if (result === 'granted') showToast('Notifications enabled! 🎉', 'success');
};

window.showNotifHelp = function () {
    showToast('Click the 🔒 lock icon in the address bar → Reset Notifications → Reload', 'info');
};

function sendBrowserNotification(product, eventType = 'NEW') {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    const price = product.price ? ` — ₹${Math.round(product.price)}` : '';
    const titles = {
        NEW: '🚗 New Hot Wheels!',
        RESTOCK: '🔄 Hot Wheels Restocked!',
        OOS: '❌ Hot Wheels Sold Out',
        PRICE_DROP: '💸 Hot Wheels Price Drop!',
        FAVORITE: '⭐ Favorite is IN STOCK!',
    };
    const n = new Notification(titles[eventType] || '🚗 Hot Wheels Change', {
        body: `${product.name}${price}`,
        icon: product.image_url || undefined,
        tag: `${eventType}-${product.product_id}`,
        requireInteraction: true,
    });
    n.onclick = () => {
        if (product.url) window.open(product.url, '_blank');
        n.close();
    };
}

// ── API calls ─────────────────────────────────────────────────────────────────

async function apiFetch(path, options = {}) {
    const resp = await fetch(API + path, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
    });
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    return resp.json();
}

// ── Stats ─────────────────────────────────────────────────────────────────────

async function loadStats() {
    try {
        const s = await apiFetch('/api/stats');
        document.getElementById('stat-total').textContent = s.total_products;
        document.getElementById('stat-instock').textContent = s.in_stock;
        document.getElementById('stat-oos').textContent = s.out_of_stock;
        document.getElementById('stat-deliverable').textContent = s.deliverable;
        document.getElementById('stat-last-check').textContent = s.last_check
            ? new Date(s.last_check + 'Z').toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: true, hour: '2-digit', minute: '2-digit' })
            : '—';
        // Update countdown target from live backend data
        if (s.next_check) {
            const t = new Date(s.next_check);
            // Only update if it's in the future (don't regress to stale value)
            if (t > Date.now()) _nextCheckAt = t;
        }
        _updateCountdown();
    } catch (e) { console.error('Stats load failed:', e); }
}

// ── Products ──────────────────────────────────────────────────────────────────

async function loadProducts() {
    try {
        const fresh = await apiFetch('/api/products');

        allProducts = fresh;
        await loadSeries();
        renderProducts();
    } catch (e) { console.error('Products load failed:', e); }
}

async function loadSeries() {
    try {
        const list = await apiFetch('/api/products/series/list');
        const sel = document.getElementById('filter-series');
        const current = sel.value;
        sel.innerHTML = '<option value="all">All Series</option>';
        list.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s; opt.textContent = s;
            if (s === current) opt.selected = true;
            sel.appendChild(opt);
        });
    } catch (e) { /* ignore */ }
}

function renderProducts() {
    const statusFilter = document.getElementById('filter-status').value;
    const seriesFilter = document.getElementById('filter-series').value;
    const searchVal = document.getElementById('filter-search').value.toLowerCase();

    const filtered = allProducts.filter(p => {
        if (statusFilter === 'available' && !p.is_available) return false;
        if (statusFilter === 'oos' && p.is_available) return false;
        if (statusFilter === 'deliverable' && !p.pincode_deliverable) return false;
        if (seriesFilter !== 'all' && p.series !== seriesFilter) return false;
        if (searchVal && !p.name.toLowerCase().includes(searchVal)) return false;
        return true;
    });

    const grid = document.getElementById('products-grid');
    const empty = document.getElementById('no-products');

    if (!filtered.length) {
        grid.innerHTML = '';
        empty.classList.remove('hidden');
        return;
    }
    empty.classList.add('hidden');

    grid.innerHTML = filtered.map(p => {
        const oos = !p.is_available;

        // Discount ribbon
        let ribbon = '';
        const isNew = p.first_seen && (Date.now() - new Date(p.first_seen + 'Z')) < NEW_BADGE_MINUTES * 60_000;
        if (isNew) {
            ribbon = '<div class="product-ribbon product-ribbon-new">🆕 NEW</div>';
        } else if (p.price && p.original_price && p.original_price > p.price) {
            const disc = Math.round((1 - p.price / p.original_price) * 100);
            ribbon = `<div class="product-ribbon">−${disc}%</div>`;
        }

        // Image / placeholder
        const imgHtml = p.image_url
            ? `<img src="${escHtml(p.image_url)}" alt="${escHtml(p.name)}" loading="lazy" />`
            : '<span class="no-img">🚗</span>';

        const oosOverlay = oos ? '<div class="product-oos-overlay">Out of Stock</div>' : '';

        // Series label
        const seriesHtml = p.series
            ? `<div class="product-series">${escHtml(p.series)}</div>` : '';

        // Price
        let priceHtml = '—';
        if (p.price) {
            priceHtml = `<span class="price-sale">₹${Math.round(p.price)}</span>`;
            if (p.original_price && p.original_price > p.price) {
                priceHtml += ` <span class="price-original">₹${Math.round(p.original_price)}</span>`;
            }
        }

        // Status badges
        const stockBadge = oos
            ? '<span class="badge badge-oos">Out of Stock</span>'
            : '<span class="badge badge-available">In Stock</span>';

        const delivBadge = p.pincode_deliverable === true
            ? '<span class="badge badge-deliverable">✓ Confirmed</span>'
            : ''; // don't show if unverified

        return `
<a class="product-card${oos ? ' oos' : ''}" href="${escHtml(p.url)}" target="_blank" rel="noopener">
  <div class="product-image">
    ${imgHtml}${ribbon}${oosOverlay}
    <div class="product-img-footer">
      <div class="product-price-overlay">${priceHtml}</div>
    </div>
  </div>
  <div class="product-body">
    ${seriesHtml}
    <div class="product-name">${escHtml(p.name)}</div>
    <div class="product-status-row">${stockBadge}${delivBadge}</div>
  </div>
  <div class="product-footer">
    <span class="product-link">FirstCry →</span>
    <span class="product-time">${fmtTimeShort(p.last_checked)}</span>
  </div>
</a>`;
    }).join('');
}

// ── Events ────────────────────────────────────────────────────────────────────

async function loadEvents() {
    try {
        const freshEvents = await apiFetch('/api/events?limit=50');
        if (!_eventsPrimed) {
            freshEvents.forEach(e => _knownEventIds.add(e.id));
            _eventsPrimed = true;
        } else {
            const productsById = new Map(allProducts.map(p => [p.product_id, p]));
            const unseen = freshEvents.filter(e => !_knownEventIds.has(e.id));
            unseen.reverse().forEach(e => {
                const product = productsById.get(e.product_id) || {
                    product_id: e.product_id,
                    name: e.product_name || e.product_id,
                    price: e.price,
                    url: '',
                };
                sendBrowserNotification(product, e.event_type);
                showToast(`${e.event_type.replace('_', ' ')}: ${(e.product_name || e.product_id).slice(0, 42)}`, e.event_type === 'OOS' ? 'error' : 'success');
            });
            freshEvents.forEach(e => _knownEventIds.add(e.id));
        }
        allEvents = freshEvents;
        renderEvents();
    } catch (e) { console.error('Events load failed:', e); }
}

function renderEvents() {
    const list = document.getElementById('events-list');
    const noEvt = document.getElementById('no-events');
    const events = logFilter === 'all'
        ? allEvents
        : allEvents.filter(e => e.event_type === logFilter);

    if (!events.length) {
        list.innerHTML = '';
        noEvt.classList.remove('hidden');
        return;
    }
    noEvt.classList.add('hidden');

    list.innerHTML = events.map(e => {
        const price = e.price ? ` · ₹${Math.round(e.price)}` : '';
        const label = e.event_type.replace('_', ' ');
        const time = new Date(e.timestamp + 'Z').toLocaleString('en-IN', {
            timeZone: 'Asia/Kolkata', hour12: true,
            month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit',
        });
        return `
<div class="event-row">
  <span class="event-badge event-${escHtml(e.event_type)}">${label}</span>
  <span class="event-name" title="${escHtml(e.product_name || e.product_id)}">${escHtml(e.product_name || e.product_id)}${escHtml(price)}</span>
  <span class="event-time">${time}</span>
</div>`;
    }).join('');
}

// ── Live Feed ─────────────────────────────────────────────────────────────────

let _liveFeedTimer = null;

async function loadLiveFeed() {
    try {
        const feed = await apiFetch('/api/live');
        const container = document.getElementById('live-feed');
        const noLive = document.getElementById('no-live');
        if (!feed.length) {
            container.innerHTML = '';
            noLive.classList.remove('hidden');
            return;
        }
        noLive.classList.add('hidden');
        container.innerHTML = feed.map(e => {
            const t = new Date(e.time + 'Z').toLocaleTimeString('en-IN', {
                timeZone: 'Asia/Kolkata', hour12: true, hour: '2-digit', minute: '2-digit', second: '2-digit',
            });
            const hasActivity = e.new || e.restock || e.oos || e.price_drop;
            const tags = [
                e.new ? `<span class="live-tag tag-new">+${e.new} new</span>` : '',
                e.restock ? `<span class="live-tag tag-restock">↑${e.restock} restock</span>` : '',
                e.oos ? `<span class="live-tag tag-oos">✕${e.oos} OOS</span>` : '',
                e.price_drop ? `<span class="live-tag tag-price">↓${e.price_drop} price</span>` : '',
            ].filter(Boolean).join('');
            return `
<div class="live-row${hasActivity ? ' live-row-active' : ''}">
  <span class="live-time">${t}</span>
  <span class="live-products">${e.products_found} products</span>
  <span class="live-tags">${tags || '<span class="live-quiet">no changes</span>'}</span>
</div>`;
        }).join('');
    } catch (e) { console.error('Live feed failed:', e); }
}

function startLivePolling() {
    if (_liveFeedTimer) clearInterval(_liveFeedTimer);
    // Poll live feed every 10s when on Live tab
    _liveFeedTimer = setInterval(() => {
        const liveTab = document.getElementById('tab-live');
        if (liveTab && liveTab.classList.contains('active')) loadLiveFeed();
    }, 10_000);
}

let _eventPollTimer = null;

function startEventPolling() {
    if (_eventPollTimer) clearInterval(_eventPollTimer);
    _eventPollTimer = setInterval(loadEvents, 1_000);
}

// ── Favorites (priority watchlist) ────────────────────────────────────────────

let allFavorites = [];
let _favAvailability = new Map();   // item id -> last known is_available, to detect "just became available"

async function loadFavorites() {
    try {
        const fresh = await apiFetch('/api/watchlist');
        fresh.forEach(item => {
            const wasAvailable = _favAvailability.get(item.id);
            if (item.is_available && wasAvailable !== true) {
                // First sight of this favorite in stock — top priority alert
                sendBrowserNotification(
                    { product_id: item.product_id || String(item.id), name: item.name || item.query, price: item.price, url: item.url, image_url: item.image_url },
                    'FAVORITE'
                );
                showToast(`⭐ Favorite in stock: ${(item.name || item.query).slice(0, 42)}`, 'success');
            }
            _favAvailability.set(item.id, item.is_available);
        });
        allFavorites = fresh;
        renderFavorites();
    } catch (e) { console.error('Favorites load failed:', e); }
}

function renderFavorites() {
    const list = document.getElementById('favorites-list');
    const empty = document.getElementById('no-favorites');
    if (!allFavorites.length) {
        list.innerHTML = '';
        empty.classList.remove('hidden');
        return;
    }
    empty.classList.add('hidden');

    list.innerHTML = allFavorites.map(item => {
        const label = item.name || item.query;
        const statusBadge = item.status === 'available'
            ? '<span class="badge badge-available">✅ In Stock</span>'
            : item.status === 'found'
                ? '<span class="badge badge-oos">🔍 Found — Out of Stock</span>'
                : '<span class="badge badge-watching">👀 Searching…</span>';
        const priceHtml = item.price ? `<span class="favorite-price">₹${Math.round(item.price)}</span>` : '';
        const checkedHtml = `<span class="favorite-checked">Checked ${fmtAgo(item.last_checked)}</span>`;
        const imgHtml = item.image_url
            ? `<img src="${escHtml(item.image_url)}" alt="${escHtml(label)}" loading="lazy" />`
            : '<span class="no-img">⭐</span>';
        const linkHtml = item.url
            ? `<a href="${escHtml(item.url)}" target="_blank" rel="noopener" class="btn btn-secondary">View →</a>`
            : '';

        return `
<div class="favorite-card${item.is_available ? ' favorite-available' : ''}">
  <div class="favorite-image">${imgHtml}</div>
  <div class="favorite-body">
    <div class="favorite-name" title="${escHtml(label)}">${escHtml(label)}</div>
    <div class="favorite-status-row">${statusBadge}${priceHtml}</div>
    <div class="favorite-meta-row">${checkedHtml}<span class="favorite-pulse" title="Actively monitoring every cycle"></span></div>
  </div>
  <div class="favorite-actions">
    ${linkHtml}
    <button class="btn btn-secondary" data-remove-fav="${item.id}">Remove</button>
  </div>
</div>`;
    }).join('');

    list.querySelectorAll('[data-remove-fav]').forEach(btn => {
        btn.addEventListener('click', () => removeFavorite(parseInt(btn.dataset.removeFav)));
    });
}

async function removeFavorite(id) {
    try {
        await apiFetch(`/api/watchlist/${id}`, { method: 'DELETE' });
        _favAvailability.delete(id);
        await loadFavorites();
    } catch (e) { showToast(`Remove failed: ${e.message}`, 'error'); }
}

document.getElementById('favorite-form').addEventListener('submit', async e => {
    e.preventDefault();
    const input = document.getElementById('favorite-input');
    const query = input.value.trim();
    if (!query) return;
    try {
        await apiFetch('/api/watchlist', { method: 'POST', body: JSON.stringify({ query }) });
        input.value = '';
        showToast('Added to favorites! ⭐', 'success');
        await loadFavorites();
    } catch (err) { showToast(`Couldn't add: ${err.message}`, 'error'); }
});

let _favPollTimer = null;
function startFavoritePolling() {
    if (_favPollTimer) clearInterval(_favPollTimer);
    _favPollTimer = setInterval(loadFavorites, 2_000);
}

// ── Tabs ──────────────────────────────────────────────────────────────────────

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        const pane = document.getElementById('tab-' + btn.dataset.tab);
        pane.classList.add('active');
        // Re-fetch data when switching tabs so content is never stale
        if (btn.dataset.tab === 'events') loadEvents();
        if (btn.dataset.tab === 'live') loadLiveFeed();
        if (btn.dataset.tab === 'favorites') loadFavorites();
    });
});

// ── Log filters ───────────────────────────────────────────────────────────────

document.querySelectorAll('.log-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.log-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        logFilter = btn.dataset.filter;
        renderEvents();
    });
});

// ── Settings modal ────────────────────────────────────────────────────────────

async function openSettings() {
    try {
        const s = await apiFetch('/api/settings');
        document.getElementById('s-pincode').value = s.pincode || '';
        document.getElementById('s-interval').value = s.check_interval_minutes || 5;
        document.getElementById('s-notif-enabled').checked = s.notifications_enabled;
        document.getElementById('s-email-enabled').checked = s.email_enabled;
        document.getElementById('s-email-to').value = s.email_to || '';
        document.getElementById('s-telegram-enabled').checked = s.telegram_enabled;
    } catch (e) { showToast('Failed to load settings.', 'error'); return; }
    document.getElementById('modal-backdrop').classList.remove('hidden');
}

function closeSettings() {
    document.getElementById('modal-backdrop').classList.add('hidden');
}

document.getElementById('settings-form').addEventListener('submit', async e => {
    e.preventDefault();
    const payload = {
        pincode: document.getElementById('s-pincode').value,
        check_interval_minutes: parseInt(document.getElementById('s-interval').value),
        notifications_enabled: document.getElementById('s-notif-enabled').checked,
        email_enabled: document.getElementById('s-email-enabled').checked,
        email_to: document.getElementById('s-email-to').value,
        telegram_enabled: document.getElementById('s-telegram-enabled').checked,
    };
    try {
        await apiFetch('/api/settings', { method: 'PUT', body: JSON.stringify(payload) });
        showToast('Settings saved!', 'success');
        closeSettings();
        await loadAll();
        startPolling(); // restart with new interval
    } catch (err) { showToast(`Save failed: ${err.message}`, 'error'); }
});

// ── Manual check ──────────────────────────────────────────────────────────────

document.getElementById('btn-check').addEventListener('click', async () => {
    const btn = document.getElementById('btn-check');
    const spinner = document.getElementById('spinner');
    btn.disabled = true;
    spinner.classList.remove('hidden');
    try {
        const r = await apiFetch('/api/check', { method: 'POST' });
        showToast(
            `Done! Found ${r.products_found} products. New: ${r.new_products}, Restocked: ${r.restocked}`,
            'success'
        );
        await loadAll();
        startPolling(); // reset countdown after manual check
    } catch (err) { showToast(`Check failed: ${err.message}`, 'error'); }
    finally { btn.disabled = false; spinner.classList.add('hidden'); }
});

// ── Misc event listeners ──────────────────────────────────────────────────────

document.getElementById('btn-settings').addEventListener('click', openSettings);
document.getElementById('btn-modal-close').addEventListener('click', closeSettings);
document.getElementById('btn-cancel').addEventListener('click', closeSettings);
document.getElementById('modal-backdrop').addEventListener('click', e => {
    if (e.target === document.getElementById('modal-backdrop')) closeSettings();
});
['filter-status', 'filter-series'].forEach(id =>
    document.getElementById(id).addEventListener('change', renderProducts));
document.getElementById('filter-search').addEventListener('input', renderProducts);

// ── Boot & polling ────────────────────────────────────────────────────────────

let _pollTimer = null;
let _cdTimer = null;   // countdown tick
let _intervalMs = 2 * 60 * 1000;
let _nextCheckAt = null;   // Date object of next backend scrape

function _updateCountdown() {
    const el = document.getElementById('countdown');
    if (!el) return;
    if (!_nextCheckAt) { el.textContent = ''; return; }
    const secsLeft = Math.round((_nextCheckAt - Date.now()) / 1000);
    if (secsLeft <= 0) {
        el.textContent = 'Checking…';
        return;
    }
    if (secsLeft < 60) {
        el.textContent = `Next in ${secsLeft}s`;
    } else {
        const m = Math.floor(secsLeft / 60);
        const s = secsLeft % 60;
        el.textContent = `Next in ${m}:${String(s).padStart(2, '0')}`;
    }
}

function updateLastRefreshed() {
    const el = document.getElementById('last-refreshed');
    if (el) el.textContent = 'Updated ' + new Date().toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: true, hour: '2-digit', minute: '2-digit' });
}

async function loadAll() {
    await loadStats();
    await loadProducts();
    await loadEvents();
    updateLastRefreshed();
}

async function startPolling() {
    // Poll every 10s to match the scrape interval — every second counts
    if (_pollTimer) clearInterval(_pollTimer);
    _pollTimer = setInterval(loadAll, 10_000);

    if (_cdTimer) clearInterval(_cdTimer);
    _cdTimer = setInterval(_updateCountdown, 1000);
}

loadAll();
loadFavorites();
startPolling();
startLivePolling();
startEventPolling();
startFavoritePolling();
updateNotifBanner();
