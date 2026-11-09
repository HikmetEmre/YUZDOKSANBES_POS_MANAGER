// ── SHARED HELPERS ──
        // Format an ISO timestamp as 'DD-MM-YYYY HH:MM'. Storage stays ISO;
        // this is display-only so date math/sorting never breaks.
        function fmtDate(iso) {
            const d = new Date(iso);
            if(isNaN(d)) return '';
            const p = n => String(n).padStart(2, '0');
            return `${p(d.getDate())}-${p(d.getMonth()+1)}-${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
        }
        function fmtTime(iso) {
            const d = new Date(iso);
            if(isNaN(d)) return '';
            const p = n => String(n).padStart(2, '0');
            return `${p(d.getHours())}:${p(d.getMinutes())}`;
        }
        // Escape user/menu text before inserting via innerHTML (prevents XSS).
        function esc(s) {
            return String(s ?? '').replace(/[&<>"']/g, c => (
                {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
            ));
        }

        // ── STATE ──
        let AREAS      = [];           // loaded from /config
        let ALL_TABLES = [];           // flat list of all table numbers
        let CURRENCY    = "₺";
        let MENU        = {};
        let currentTable   = null;
        let cart           = [];   // {product_name, quantity, price, created_at, isNew}
        let pendingItem    = null;
        let pendingPayType = null;
        let pendingPayLines = [];
        let pendingPayTotal = 0;
        let currentQty     = 1;
        let currentOrderId       = null;
        let currentBaristaStatus = null;

        // ════════════ PIN AUTH (verified SERVER-side) ════════════

        // Token + role from /auth. authFetch() attaches the token to every
        // request so the server can enforce permissions.
        let AUTH_TOKEN = sessionStorage.getItem('pos_token') || null;
        let MY_ROLE    = sessionStorage.getItem('pos_role')  || null;

        function logout() {
            if(!confirm('Çıkış yapılsın mı?')) return;
            sessionStorage.removeItem('pos_token');
            sessionStorage.removeItem('pos_role');
            sessionStorage.removeItem('pos_auth');
            AUTH_TOKEN = null; MY_ROLE = null;
            location.reload();   // returns to the PIN screen
        }

        function authFetch(url, opts = {}) {
            opts.headers = opts.headers || {};
            const sent = AUTH_TOKEN;
            if(sent) opts.headers['Authorization'] = 'Bearer ' + sent;
            return fetch(url, opts).then(res => {
                // 401 = session no longer valid (passwords changed at install, or
                // 24 h expiry) → back to the password screen instead of errors.
                if(res.status === 401 && sent) { sessionExpired(); return new Promise(() => {}); }  // page reloads; callers never see the 401
                return res;
            });
        }
        let _expiring = false;
        function sessionExpired() {
            if(_expiring) return; _expiring = true;
            ['pos_token', 'pos_role', 'pos_auth'].forEach(k => sessionStorage.removeItem(k));
            AUTH_TOKEN = null; MY_ROLE = null;
            alert('Oturum sona erdi (şifre değişmiş olabilir). Lütfen yeniden giriş yapın.');
            location.reload();
        }

        async function checkPin() {
            OrderAlerts.unlockAudio();   // unlock the bell inside this click, before any await
            const val = document.getElementById('pinInput').value;
            try {
                const res = await fetch('/auth', {
                    method:  'POST',
                    headers: {'Content-Type': 'application/json'},
                    body:    JSON.stringify({pin: val})
                });
                if(res.ok) {
                    const data = await res.json();
                    // The cash desk is admin-only (payments, cancels, revenue).
                    // The waiter password is for /waiter — same rule as the Panel.
                    if(data.role !== 'checkout') {
                        document.getElementById('pinError').innerText = 'Yönetici yetkisi gerekli.';
                        document.getElementById('pinInput').value = '';
                        return;
                    }
                    AUTH_TOKEN = data.token;
                    MY_ROLE    = data.role;
                    sessionStorage.setItem('pos_token', AUTH_TOKEN);
                    sessionStorage.setItem('pos_role', MY_ROLE);
                    sessionStorage.setItem('pos_auth', '1');
                    document.getElementById('pinModal').style.display = 'none';
                    init();
                } else {
                    document.getElementById('pinError').innerText = 'Hatalı şifre, tekrar deneyin.';
                    document.getElementById('pinInput').value = '';
                }
            } catch(e) {
                document.getElementById('pinError').innerText = 'Sunucuya ulaşılamadı.';
            }
        }

        // ════════════ CONFIG + DYNAMIC MENU/TABLES ════════════

        async function loadConfig() {
            const res = await fetch('/config');
            const cfg = await res.json();
            AREAS    = cfg.areas;
            ALL_TABLES = AREAS.flatMap(a => a.tables);   // every table number, for status refresh
            CURRENCY = cfg.currency;
            MENU     = cfg.menu;
            TableNames.set(cfg.table_names);             // "12" / "Toplantı Masası" (Ayarlar → Masalar)
            CancelDialog.init(cfg);                      // cancel reasons (Yanlış Ürün, Soğuk/Eksik, Diğer)
            buildTables();
            buildMenu();
        }

        // Build one tab per area + one table grid per area (only active area shown).
        function buildTables() {
            const tabsEl = document.getElementById('areaTabs');
            const c      = document.getElementById('tablesContainer');
            let tabs = '', grids = '';
            AREAS.forEach((area, idx) => {
                tabs += `<button class="area-tab ${idx === 0 ? 'active' : ''}"
                                 id="areaTab_${area.id}"
                                 onclick="showArea('${area.id}', this)">${area.name}</button>`;
                grids += `<div class="tables area-grid" id="areaGrid_${area.id}"
                               style="display:${idx === 0 ? 'flex' : 'none'};">`;
                area.tables.forEach(n => {
                    const named = TableNames.isNamed(n);
                    grids += `<button class="table-btn${named ? ' t-named' : ''}" onclick="selectTable(${n})" id="t${n}"
                                      title="${esc(TableNames.label(n))}">${esc(TableNames.short(n))}</button>`;
                });
                grids += `</div>`;
            });
            tabsEl.innerHTML = tabs;
            c.innerHTML = grids;
        }

        function showArea(areaId, btn) {
            document.querySelectorAll('.area-grid').forEach(g => g.style.display = 'none');
            document.querySelectorAll('.area-tab').forEach(t => t.classList.remove('active'));
            document.getElementById('areaGrid_' + areaId).style.display = 'flex';
            btn.classList.add('active');
        }

        function buildMenu() {
            const cats = document.getElementById('categoriesContainer');
            const menu = document.getElementById('menuContainer');
            let catBtns = '', sections = '';
            for(const [category, items] of Object.entries(MENU)) {
                const safeId = category.replace(/ /g, '_');
                catBtns += `<button class="cat-btn" onclick="showCategory('${safeId}', this)">${esc(category)}</button>`;
                sections += `<div class="menu-grid" id="${safeId}" style="display:none;">`;
                items.forEach(it => {
                    const safeName = it.name.replace(/'/g, "\\'");
                    sections += `<button class="menu-item-btn" onclick="addItem('${safeName}', ${it.price})">
                        <span class="item-name">${esc(it.name)}</span>
                        <span class="item-price">${it.price} ${CURRENCY}</span>
                    </button>`;
                });
                sections += `</div>`;
            }
            cats.innerHTML = catBtns;
            menu.innerHTML = sections;
        }

        // ════════════ INIT ════════════

        async function init() {
            await loadConfig();
            await loadAllTables();
            // Shared new-QR-order alert (same as the waiter page, see
            // order_alerts.js): bell + OS notification within ~8 s, then refresh
            // tables (red table + area signal + badge) and the Bekleyen list.
            OrderAlerts.start({
                authFetch, isLoggedIn: () => !!AUTH_TOKEN,
                onNewOrder: () => {
                    showToast('🔔 Yeni sipariş geldi!', 'success');
                    loadAllTables();
                    if(document.getElementById('pendingView').style.display === 'block') loadPending();
                }
            });
        }

        async function loadAllTables() {
            try {
                const res  = await authFetch('/tables-status');
                const data = await res.json();
                ALL_TABLES.forEach(n => {
                    const el = document.getElementById('t' + n);
                    if(el) { el.classList.remove('red'); el.classList.remove('pending'); }
                });
                data.open_tables.forEach(n => {
                    const el = document.getElementById('t' + n);
                    if(el) el.classList.add('red');
                });
                (data.pending_tables || []).forEach(n => {
                    const el = document.getElementById('t' + n);
                    if(el) el.classList.add('pending');
                });
                // Red signal on the area tab holding a table with a new QR order.
                const pendSet = new Set((data.pending_tables || []).map(String));
                AREAS.forEach(a => {
                    const tab = document.getElementById('areaTab_' + a.id);
                    if(tab) tab.classList.toggle('alert', a.tables.some(n => pendSet.has(String(n))));
                });
                // Open-duration under every occupied table ("32 dk").
                Duration.paintButtons(data.open_seconds, ALL_TABLES);
                // Update the Bekleyen tab badge from the same data.
                const badge = document.getElementById('pendingBadge');
                const np = (data.pending_tables || []).length;
                if(np > 0) { badge.innerText = np; badge.style.display = 'inline-block'; }
                else       { badge.style.display = 'none'; }

            } catch(e) {
                showToast('Masa durumu yüklenemedi!', 'error');
            }
        }

        
        async function deleteSalesLog(id) {
            if(!confirm('Bu satış kaydını silmek istiyor musunuz?')) return;
            showLoading();
            try {
                const res = await authFetch('/sales-log/' + id, {method: 'DELETE'});
                if(!res.ok) throw new Error();
                cart.forEach(i => i.isNew = false);
                document.getElementById('t' + currentTable).classList.add('red');
                const r = await authFetch('/table/' + currentTable);
                const d = await r.json();
                if(d.status === 'open') {
                    currentOrderId       = d.order_id;
                    currentBaristaStatus = d.barista_status;
                }
                renderCart();
                showToast('Sipariş kaydedildi ✓', 'success');
                loadSales();
            } catch(e) {
                showToast('Silme hatası!', 'error');
            } finally {
                hideLoading();
            }
        }



        // ════════════ TABS ════════════

        function showTab(tab) {
            document.getElementById('posView').style.display     = tab === 'pos'     ? 'block' : 'none';
            document.getElementById('pendingView').style.display = tab === 'pending' ? 'block' : 'none';
            document.getElementById('salesView').style.display   = tab === 'sales'   ? 'block' : 'none';
            document.getElementById('cancelView').style.display  = tab === 'cancels' ? 'block' : 'none';
            document.getElementById('tabPos').classList.toggle('active',     tab === 'pos');
            document.getElementById('tabPending').classList.toggle('active', tab === 'pending');
            document.getElementById('tabSales').classList.toggle('active',   tab === 'sales');
            document.getElementById('tabCancels').classList.toggle('active', tab === 'cancels');
            if(tab === 'sales')   loadSales();
            if(tab === 'pending') loadPending();
            if(tab === 'cancels') loadCancels();
        }

        // ════════════ PENDING (Bekleyen) — QR customer orders ════════════

        async function loadPending() {
            try {
                const data = await (await authFetch('/pending')).json();
                const el = document.getElementById('pendingList');
                if(!data.tables || data.tables.length === 0) {
                    el.innerHTML = '<div style="text-align:center;color:#bbb;padding:30px;">Bekleyen sipariş yok</div>';
                    return;
                }
                let html = '';
                data.tables.forEach(t => {
                    let items = '', total = 0;
                    t.items.forEach(i => {
                        total += i.quantity * i.price;
                        items += `<div class="pend-item"><span>${esc(i.product_name)} ×${i.quantity}</span>
                                  <span>${(i.quantity*i.price).toFixed(0)} ₺</span></div>`;
                    });
                    html += `<div class="pend-card">
                        <div class="pend-head">
                            <span>${esc(TableNames.label(t.table_no))}</span>
                            <span>${total.toFixed(0)} ₺</span>
                        </div>
                        ${items}
                        <div class="pend-actions">
                            <button class="pend-refuse" onclick="refusePending(${t.table_no})">✕ Reddet</button>
                            <button class="pend-accept" onclick="acceptPending(${t.table_no})">✓ Onayla</button>
                        </div>
                    </div>`;
                });
                el.innerHTML = html;
            } catch(e) {}
        }

        async function acceptPending(tableNo) {
            showLoading();
            try {
                const res = await authFetch('/accept-order', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({table_no: tableNo})
                });
                if(!res.ok) throw new Error();
                showToast(TableNames.label(tableNo) + ' onaylandı ✓', 'success');
                loadPending();
                loadAllTables();
                // If the accepted table is currently open in POS, refresh its cart.
                if(currentTable === tableNo) selectTable(tableNo);
            } catch(e) { showToast('Hata!', 'error'); }
            finally { hideLoading(); }
        }

        async function refusePending(tableNo) {
            if(!confirm(TableNames.label(tableNo) + ' müşteri siparişini reddet?')) return;
            showLoading();
            try {
                const res = await authFetch('/refuse-order', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({table_no: tableNo})
                });
                if(!res.ok) throw new Error();
                showToast(TableNames.label(tableNo) + ' reddedildi', 'success');
                loadPending();
                loadAllTables();
                if(currentTable === tableNo) selectTable(tableNo);
            } catch(e) { showToast('Hata!', 'error'); }
            finally { hideLoading(); }
        }
        // Live pending badge on the tab.
        async function refreshPendingBadge() {
            try {
                const data = await authFetch('/tables-status').then(r => r.json());
                const n = (data.pending_tables || []).length;
                const badge = document.getElementById('pendingBadge');
                if(n > 0) { badge.innerText = n; badge.style.display = 'inline-block'; }
                else      { badge.style.display = 'none'; }
            } catch(e) {}
        }

        // ════════════ TABLE SELECTION ════════════

        async function selectTable(n) {
            if(currentTable) document.getElementById('t' + currentTable).classList.remove('selected');
            currentTable = n;
            document.getElementById('t' + n).classList.add('selected');
            document.getElementById('selectedTable').innerText = TableNames.label(n);
            Duration.clearHeader(document.getElementById('orderTime'));

            showLoading();
            try {
                const res  = await authFetch('/table/' + n);
                const data = await res.json();
                cart = [];

                if(data.status === 'open') {
                    currentOrderId       = data.order_id;
                    currentBaristaStatus = data.barista_status;
                    data.items.forEach(i => {
                        // Pending QR items are NOT part of the order list yet — they
                        // live only in the Bekleyen tab until staff accept them.
                        if(i.item_status === 'pending') return;
                        cart.push({
                            id:           i.id,
                            product_name: i.product_name,
                            quantity:     i.quantity,
                            price:        i.price,
                            created_at:   i.created_at,
                            paid_qty:     i.paid_qty || 0,
                            isNew:        false
                        });
                    });
                    // How long the table has been open (server clock), ticking.
                    Duration.showHeader(document.getElementById('orderTime'),
                                        data.open_seconds, data.created_at);
                }
                renderCart();
            } catch(e) {
                showToast('Masa bilgisi alınamadı!', 'error');
            } finally {
                hideLoading();
            }
        }

        // ════════════ CATEGORIES ════════════

        function showCategory(catId, btn) {
            document.querySelectorAll('.menu-grid').forEach(el => el.style.display = 'none');
            document.querySelectorAll('.cat-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(catId).style.display = 'grid';
            btn.classList.add('active');
        }

        // ════════════ ADD ITEM — QTY MODAL ════════════

        function addItem(name, price) {
            if(!currentTable) {
                showToast('Önce bir masa seçin!', 'error');
                return;
            }
            pendingItem = {name, price};
            currentQty  = 1;
            document.getElementById('qtyModalTitle').innerText = name + '  —  ' + price + ' ₺';
            document.getElementById('qtyDisplay').innerText    = '1';
            document.getElementById('qtyModal').style.display  = 'flex';
        }

        function changeQty(delta) {
            currentQty = Math.max(1, currentQty + delta);
            document.getElementById('qtyDisplay').innerText = currentQty;
        }

        function confirmQty() {
            cart.push({
                product_name: pendingItem.name,
                quantity:     currentQty,
                price:        pendingItem.price,
                created_at:   new Date().toISOString(),
                isNew:        true
            });
            closeQtyModal();
            renderCart();
        }

        function closeQtyModal() {
            document.getElementById('qtyModal').style.display = 'none';
            pendingItem = null;
        }

        // ════════════ CART ════════════

        function getTotal() {
            return cart.reduce((s, i) => s + i.quantity * i.price, 0);
        }

        function renderCart() {
            const total = getTotal();
            let html = '';

            if(cart.length === 0) {
                html = '<div class="cart-empty">Sipariş listesi boş</div>';
            } else {
                html = `<table class="cart-table">
                    <thead><tr>
                        <th>ÜRÜN</th><th>ADET</th><th>ZAMAN</th><th>TUTAR</th><th></th>
                    </tr></thead><tbody>`;

                cart.forEach((item, idx) => {
                    const t      = fmtTime(item.created_at);
                    const paidQ  = item.paid_qty || 0;
                    const fullyPaid   = !item.isNew && paidQ >= item.quantity;
                    const partlyPaid  = !item.isNew && paidQ > 0 && paidQ < item.quantity;
                    const badge = item.isNew ? '<span class="new-badge">YENİ</span>'
                                : fullyPaid  ? '<span class="paid-badge">ÖDENDİ</span>'
                                : partlyPaid ? `<span class="paid-badge">${paidQ.toFixed(0)}/${item.quantity.toFixed(0)} ÖDENDİ</span>` : '';
                    const rowClass = item.isNew ? 'new-row' : (fullyPaid ? 'paid-row' : '');
                    // Quantity is locked once any unit is paid (keeps accounting honest).
                    const qtyLocked = !item.isNew && paidQ > 0;
                    const qtyCell = qtyLocked
                        ? `<span style="color:#aaa;">${item.quantity}</span>`
                        : `<input type="number" value="${item.quantity}" min="1" onchange="updateQty(${idx}, this.value)">`;
                    const actionCell = (item.isNew || paidQ === 0)
                        ? `<button class="remove-btn" onclick="removeItem(${idx})">✕</button>`
                        : '';
                    html += `<tr class="${rowClass}">
                        <td>${esc(item.product_name)}${badge}</td>
                        <td>${qtyCell}</td>
                        <td style="color:#888;font-size:13px;">${t}</td>
                        <td style="font-weight:bold;">${(item.quantity * item.price).toFixed(0)} ₺</td>
                        <td>${actionCell}</td>
                    </tr>`;
                });

                html += '</tbody></table>';
            }

            document.getElementById('cart').innerHTML = html;
            document.getElementById('total').innerText = total.toFixed(0);

            // ÖDENEN = sum of already-paid units × price; KALAN = what's left.
            const paid      = cart.reduce((s, i) => s + (i.paid_qty || 0) * i.price, 0);
            const remaining = Math.max(0, total - paid);
            document.getElementById('totalPaid').innerText      = paid.toFixed(0);
            document.getElementById('totalRemaining').innerText = remaining.toFixed(0);

            renderBaristaStatus();
        }

        function updateQty(idx, val) {
            val = parseInt(val);
            if(isNaN(val) || val < 1) return;
            cart[idx].quantity = val;
            renderCart();
        }

        async function removeItem(idx) {
            const item = cart[idx];
            if(item.isNew) { cart.splice(idx, 1); renderCart(); return; }   // not saved yet → no reason needed
            const choice = await CancelDialog.open({
                title: `${item.product_name} ×${item.quantity} — ${TableNames.label(currentTable)}`,
                maxQty: item.quantity
            });
            if(!choice) return;                                              // Vazgeç
            showLoading();
            try {
                const res = await authFetch('/remove-item', {
                    method:  'POST',
                    headers: {'Content-Type': 'application/json'},
                    body:    JSON.stringify({table_no: currentTable, item_id: item.id,
                                             qty: choice.qty, reason: choice.reason, note: choice.note})
                });
                if(!res.ok) {
                    const d = await res.json().catch(() => ({}));
                    showToast(d.detail || 'Silinemedi!', 'error');
                    return;
                }
                const unsaved = cart.filter(i => i.isNew);   // keep anything not yet saved
                await selectTable(currentTable);
                cart.push(...unsaved); renderCart();
                loadAllTables();                             // table turns free if now empty
                showToast(choice.qty < item.quantity ? `${choice.qty} adet iptal edildi` : 'Ürün iptal edildi', 'success');
                refreshCancelsIfOpen();
            } catch(e) {
                showToast('İptal edilemedi!', 'error');
            } finally {
                hideLoading();
            }
        }

        
        function renderBaristaStatus() {
            const el = document.getElementById('baristaStatus');
            if(!el) return;
            if(!currentOrderId) { el.innerHTML = ''; return; }
            const isH = currentBaristaStatus === 'hazirlaniyor';
            el.innerHTML = `
                <button onclick="setBaristaStatus('hazirlaniyor')"
                    style="padding:4px 10px;font-size:11px;font-weight:bold;border-radius:5px;cursor:pointer;border:2px solid #3d8bfd;
                        background:${isH ? '#3d8bfd' : 'transparent'};color:${isH ? 'white' : '#3d8bfd'};">
                    🔥 Hazırlanıyor</button>
                <button onclick="setBaristaStatus('hazir')"
                    style="padding:4px 10px;font-size:11px;font-weight:bold;border-radius:5px;cursor:pointer;border:2px solid #20c997;
                        background:${!isH ? '#20c997' : 'transparent'};color:${!isH ? 'white' : '#20c997'};">
                    ✅ Hazır</button>`;
        }

        async function setBaristaStatus(status) {
            if(!currentOrderId) return;
            currentBaristaStatus = status;
            try {
                await authFetch('/update-order-status', {
                    method:  'POST',
                    headers: {'Content-Type': 'application/json'},
                    body:    JSON.stringify({order_id: currentOrderId, status: status})
                });
            } catch(e) { showToast('Durum güncellenemedi!', 'error'); }
            renderBaristaStatus();
        }


        // ════════════ İPTALLER (cancellation log tab) ════════════

        function refreshCancelsIfOpen() {
            if(document.getElementById('cancelView').style.display === 'block') loadCancels();
        }

        async function loadCancels() {
            const dEl = document.getElementById('cancelDate');
            if(!dEl.value) { const t = new Date(); const p = n => String(n).padStart(2, '0');
                             dEl.value = `${t.getFullYear()}-${p(t.getMonth()+1)}-${p(t.getDate())}`; }
            const body = document.getElementById('cancelTableBody');
            try {
                const res = await authFetch(`/cancellations?start=${dEl.value}&end=${dEl.value}`);
                if(!res.ok) throw new Error();
                const d = await res.json();
                document.getElementById('cancelChips').innerHTML =
                    `<span class="cx-chip">Toplam: <b>${d.totals.count}</b> · ${d.totals.amount.toFixed(0)} ₺</span>` +
                    d.by_reason.map(b => `<span class="cx-chip">${esc(b.reason)}: <b>${b.count}</b> · ${b.amount.toFixed(0)} ₺</span>`).join('');
                body.innerHTML = d.rows.length ? d.rows.map(r => `
                    <tr>
                        <td>${fmtTime(r.cancelled_at)}</td>
                        <td><b>${esc(TableNames.short(r.table_no))}</b></td>
                        <td>${esc(r.product_name)}</td>
                        <td>${+r.quantity.toFixed(2)}</td>
                        <td>${r.amount.toFixed(0)} ₺</td>
                        <td>${esc(r.reason)}${r.note ? `: <i>${esc(r.note)}</i>` : ''}</td>
                        <td>${r.kind === 'order' ? 'Adisyon' : 'Ürün'}</td>
                        <td>${r.cancelled_by === 'waiter' ? 'Garson' : 'Kasa'}</td>
                    </tr>`).join('')
                    : '<tr><td colspan="8" style="text-align:center;padding:24px;color:#bbb;">Bu tarihte iptal yok</td></tr>';
            } catch(e) {
                body.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:24px;color:#bbb;">Yüklenemedi</td></tr>';
            }
        }

        // ════════════ CANCEL ════════════

        async function cancelOrder() {
            if(!currentTable) { showToast('Masa seçin!', 'error'); return; }
            if(!cart.some(i => !i.isNew)) {                 // nothing saved on this table
                if(cart.length && confirm('Kaydedilmemiş ürünler silinsin mi?')) { cart = []; renderCart(); }
                else if(!cart.length) showToast('Açık adisyon yok', 'error');
                return;
            }
            const choice = await CancelDialog.open({
                title: `${TableNames.label(currentTable)} — tüm adisyon iptal edilecek`,
                maxQty: 1, confirmText: 'Adisyonu İptal Et'
            });
            if(!choice) return;

            showLoading();
            try {
                const res = await authFetch('/cancel-order', {
                    method:  'POST',
                    headers: {'Content-Type': 'application/json'},
                    body:    JSON.stringify({table_no: currentTable, reason: choice.reason, note: choice.note})
                });
                if(!res.ok) {
                    const d = await res.json().catch(() => ({}));
                    showToast(d.detail || 'İptal hatası!', 'error');
                    return;
                }
                document.getElementById('t' + currentTable).classList.remove('red');
                Duration.clearHeader(document.getElementById('orderTime'));
                cart = [];
                renderCart();
                loadSales();
                loadAllTables();
                showToast('Adisyon iptal edildi', 'success');
                refreshCancelsIfOpen();
            } catch(e) {
                showToast('İptal hatası!', 'error');
            } finally {
                hideLoading();
            }
        }

        // ════════════ SAVE (only new items) ════════════

        async function saveOrder() {
            if(!currentTable) { showToast('Masa seçin!', 'error'); return; }
            const newItems = cart.filter(i => i.isNew);
            if(newItems.length === 0) { showToast('Kaydedilecek yeni ürün yok', 'error'); return; }

            showLoading();
            try {
                const res = await authFetch('/create-order', {
                    method:  'POST',
                    headers: {'Content-Type': 'application/json'},
                    body:    JSON.stringify({table_no: currentTable, items: newItems})
                });
                if(!res.ok) throw new Error();
                // Reload the order from the server so saved items get their real
                // database ids (needed for per-item split payment). Without this,
                // new items have no id and /pay-items would 422.
                document.getElementById('t' + currentTable).classList.add('red');
                await selectTable(currentTable);
                showToast('Sipariş kaydedildi ✓', 'success');
            } catch(e) {
                showToast('Kayıt hatası!', 'error');
            } finally {
                hideLoading();
            }
        }

        // ════════════ PAY ════════════

        // Quick pay: settle ALL remaining units with a single method (cash or card).
        function pay(type) {
            if(!currentTable) { showToast('Masa seçin!', 'error'); return; }
            if(cart.some(i => i.isNew)) {
                showToast('Önce siparişi kaydedin!', 'error'); return;
            }
            const lines = cart
                .filter(i => !i.isNew)
                .map(i => ({...i, remaining: i.quantity - (i.paid_qty || 0)}))
                .filter(i => i.remaining > 0.0001);
            if(lines.length === 0) { showToast('Ödenecek ürün yok!', 'error'); return; }
            const total = lines.reduce((s, i) => s + i.remaining * i.price, 0);

            pendingPayType  = type;
            pendingPayLines = lines.map(i => ({item_id: i.id, qty: i.remaining}));
            pendingPayTotal = total;
            const label = type === 'cash' ? '💵 Nakit' : '💳 Kart';
            document.getElementById('payModalText').innerHTML =
                `<b>${esc(TableNames.label(currentTable))}</b><br>
                 <span style="font-size:26px;font-weight:bold;">${total.toFixed(0)} ₺</span><br>
                 <span style="font-size:13px;color:#777;">${label} ile tahsil edilecek</span>`;
            document.getElementById('payModal').style.display = 'flex';
        }

        async function confirmPay() {
            const lines  = pendingPayLines;
            const total  = pendingPayTotal;
            const method = pendingPayType;
            closePayModal();
            showLoading();
            try {
                const res = await authFetch('/pay-items', {
                    method:  'POST',
                    headers: {'Content-Type': 'application/json'},
                    body:    JSON.stringify({
                        table_no: currentTable,
                        lines:    lines,
                        splits:   [{method: method, amount: total}]
                    })
                });
                if(!res.ok) throw new Error();
                const data = await res.json();
                await afterPayment(data);
                showToast('Ödeme tamamlandı ✓', 'success');
            } catch(e) {
                showToast('Ödeme hatası!', 'error');
            } finally {
                hideLoading();
            }
        }

        // Shared post-payment handling: refresh table; clear if fully settled.
        async function afterPayment(data) {
            if(data.order_closed) {
                document.getElementById('t' + currentTable).classList.remove('red');
                Duration.clearHeader(document.getElementById('orderTime'));
                cart = [];
                renderCart();
            } else {
                const r = await authFetch('/table/' + currentTable);
                const d = await r.json();
                if(d.status === 'open') {
                    cart = d.items.filter(i => i.item_status !== 'pending').map(i => ({...i, isNew: false}));
                    renderCart();
                }
            }
            loadSales();
        }

        function closePayModal() {
            document.getElementById('payModal').style.display = 'none';
            pendingPayType = null;
        }

        // ════════════ SPLIT PAYMENT (per-unit quantity steppers) ════════════

        let splitQty = {};   // { item_id: qty_being_paid_now }

        function unpaidLines() {
            // Lines with units still owed. remaining = quantity - paid_qty.
            return cart
                .filter(i => !i.isNew)
                .map(i => ({...i, remaining: i.quantity - (i.paid_qty || 0)}))
                .filter(i => i.remaining > 0.0001);
        }

        function openSplitModal() {
            if(!currentTable) { showToast('Masa seçin!', 'error'); return; }
            if(cart.some(i => i.isNew)) { showToast('Önce siparişi kaydedin!', 'error'); return; }
            const lines = unpaidLines();
            if(lines.length === 0) { showToast('Ödenecek ürün yok!', 'error'); return; }

            // Default: select ALL remaining units (covers "pay whole check" in one tap).
            splitQty = {};
            lines.forEach(l => { splitQty[l.id] = l.remaining; });
            document.getElementById('splitCash').value = '';
            renderSplitModal();
            document.getElementById('splitModal').style.display = 'flex';
        }

        function stepQty(id, delta) {
            const line = unpaidLines().find(l => l.id === id);
            if(!line) return;
            let q = (splitQty[id] || 0) + delta;
            q = Math.max(0, Math.min(line.remaining, q));   // clamp 0..remaining
            splitQty[id] = q;
            renderSplitModal();
        }

        function setQtyAll(id) {
            const line = unpaidLines().find(l => l.id === id);
            if(!line) return;
            // Toggle: if already all, drop to 0; else set to remaining.
            splitQty[id] = (splitQty[id] === line.remaining) ? 0 : line.remaining;
            renderSplitModal();
        }

        function selectAllUnits()  { unpaidLines().forEach(l => splitQty[l.id] = l.remaining); renderSplitModal(); }
        function clearAllUnits()   { unpaidLines().forEach(l => splitQty[l.id] = 0);            renderSplitModal(); }

        function splitSelectedTotal() {
            return unpaidLines().reduce((s, l) => s + (splitQty[l.id] || 0) * l.price, 0);
        }

        function renderSplitModal() {
            const lines = unpaidLines();
            let rows = '';
            lines.forEach(l => {
                const chosen = splitQty[l.id] || 0;
                const paidQ  = l.paid_qty || 0;
                // Per-line progress: how many of the original quantity are paid.
                const progress = paidQ > 0
                    ? `<span class="split-progress">${paidQ.toFixed(0)}/${l.quantity.toFixed(0)} ödendi</span>` : '';
                rows += `<div class="split-line ${chosen > 0 ? 'on' : ''}">
                    <div class="split-line-info">
                        <span class="split-name">${esc(l.product_name)}</span>
                        <span class="split-sub">${l.price.toFixed(0)} ₺ × kalan ${l.remaining.toFixed(0)} ${progress}</span>
                    </div>
                    <div class="split-stepper">
                        <button class="step-btn" onclick="stepQty(${l.id}, -1)">−</button>
                        <span class="step-val" onclick="setQtyAll(${l.id})" title="Tümü">${chosen.toFixed(0)}</span>
                        <button class="step-btn" onclick="stepQty(${l.id}, 1)">+</button>
                    </div>
                    <span class="split-line-amt">${(chosen * l.price).toFixed(0)} ₺</span>
                </div>`;
            });
            document.getElementById('splitItems').innerHTML = rows;
            document.getElementById('splitTotal').innerText = splitSelectedTotal().toFixed(0);

            // Overall remaining on the table AFTER this payment goes through.
            const tableRemaining = lines.reduce((s, l) => s + l.remaining * l.price, 0);
            const afterPay = tableRemaining - splitSelectedTotal();
            document.getElementById('splitRemaining').innerText = afterPay.toFixed(0);

            updateSplitRemainder();
        }

        // Cash drives; card auto-fills the remainder so the split always balances.
        function updateSplitRemainder() {
            const total = splitSelectedTotal();
            const cash  = parseFloat(document.getElementById('splitCash').value) || 0;
            const card  = Math.max(0, total - cash);
            document.getElementById('splitCard').value = total > 0 ? card.toFixed(0) : '';
            const ok = total > 0 && Math.abs((cash + card) - total) < 0.01;
            const btn = document.getElementById('splitConfirmBtn');
            btn.disabled = !ok;
            btn.style.opacity = ok ? '1' : '0.5';
        }

        async function confirmSplit() {
            const lines = unpaidLines()
                .filter(l => (splitQty[l.id] || 0) > 0)
                .map(l => ({item_id: l.id, qty: splitQty[l.id]}));
            if(lines.length === 0) { showToast('Adet seçin!', 'error'); return; }

            const cash = parseFloat(document.getElementById('splitCash').value) || 0;
            const card = parseFloat(document.getElementById('splitCard').value) || 0;
            const splits = [];
            if(cash > 0) splits.push({method: 'cash', amount: cash});
            if(card > 0) splits.push({method: 'card', amount: card});
            if(splits.length === 0) { showToast('Tutar girin!', 'error'); return; }

            closeSplitModal();
            showLoading();
            try {
                const res = await authFetch('/pay-items', {
                    method:  'POST',
                    headers: {'Content-Type': 'application/json'},
                    body:    JSON.stringify({table_no: currentTable, lines: lines, splits: splits})
                });
                if(!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    const msg = typeof err.detail === 'string' ? err.detail : 'Geçersiz ödeme';
                    throw new Error(msg);
                }
                const data = await res.json();
                await afterPayment(data);
                showToast(data.order_closed ? 'Masa kapatıldı ✓' : 'Ödeme alındı ✓', 'success');
            } catch(e) {
                showToast('Ödeme hatası: ' + e.message, 'error');
            } finally {
                hideLoading();
            }
        }

        function closeSplitModal() {
            document.getElementById('splitModal').style.display = 'none';
            document.getElementById('splitCash').value = '';
        }

        // ════════════ SALES DASHBOARD ════════════

        async function loadSales() {
            showLoading();
            try {
                const res  = await authFetch('/sales-summary');
                const data = await res.json();

                document.getElementById('todayTotal').innerText = data.today.total.toFixed(0) + ' ₺';
                document.getElementById('todayCash').innerText  = data.today.cash.toFixed(0)  + ' ₺';
                document.getElementById('todayCard').innerText  = data.today.card.toFixed(0)  + ' ₺';
                document.getElementById('todayCount').innerText = data.today.count;

                let html = '';
                if(data.recent.length === 0) {
                    html = '<tr><td colspan="4" style="text-align:center;padding:28px;color:#bbb;">Henüz satış kaydı yok</td></tr>';
                } else {
                    data.recent.forEach(r => {
                        const str   = fmtDate(r.paid_at);
                        const badge = r.payment_type === 'cash'
                            ? '<span class="badge cash">Nakit</span>'
                            : '<span class="badge card">Kart</span>';
                        html += `<tr>
                            <td style="font-weight:bold;">${esc(r.table_label || TableNames.label(r.table_no))}</td>
                            <td style="font-weight:bold;">${r.total_amount.toFixed(0)} ₺</td>
                            <td>${badge}</td>
                            <td style="color:#999;font-size:13px;">${str}</td>
                            <td><button class="sales-del-btn" onclick="deleteSalesLog(${r.id})">✕</button></td>
                        </tr>`;
                    });
                }
                document.getElementById('salesTableBody').innerHTML = html;
            } catch(e) {
                showToast('Satış verileri yüklenemedi!', 'error');
            } finally {
                hideLoading();
            }
        }

        // ════════════ UI HELPERS ════════════

        function showLoading() { document.getElementById('loadingOverlay').style.display = 'flex'; }
        function hideLoading() { document.getElementById('loadingOverlay').style.display = 'none'; }

        let toastTimer = null;
        function showToast(msg, type = 'success') {
            const t = document.getElementById('toast');
            t.innerText = msg;
            t.className = 'show ' + type;
            clearTimeout(toastTimer);   // a newer message gets its full time
            toastTimer = setTimeout(() => t.className = '', 2600);
        }

        // ════════════ MIDNIGHT REFRESH ════════════

        function scheduleMidnightRefresh() {
            const now       = new Date();
            const midnight  = new Date(now);
            midnight.setHours(24, 0, 0, 0);
            const msUntil   = midnight - now;
            setTimeout(() => {
                loadAllTables();
                loadSales();
                scheduleMidnightRefresh();
            }, msUntil);
        }

        // ════════════ BOOT ════════════

        window.onload = function() {
            document.getElementById('pinInput').addEventListener('keydown', e => {
                if(e.key === 'Enter') checkPin();
            });
            // A session saved by an older version with the waiter password must
            // not reopen the cash desk: drop it and ask for the admin password.
            if(sessionStorage.getItem('pos_auth') === '1' && sessionStorage.getItem('pos_role') !== 'checkout') {
                ['pos_token', 'pos_role', 'pos_auth'].forEach(k => sessionStorage.removeItem(k));
                AUTH_TOKEN = null; MY_ROLE = null;
            }
            if(sessionStorage.getItem('pos_auth') === '1') {
                document.getElementById('pinModal').style.display = 'none';
                init();
            }
            scheduleMidnightRefresh();

            setInterval(loadAllTables, 20000);
            setInterval(async () => {
                if(!currentTable || !currentOrderId) return;
                try {
                    const res  = await authFetch('/table/' + currentTable);
                    const data = await res.json();
                    if(data.status === 'open') {
                        currentOrderId = data.order_id;
                        const prevStatus = currentBaristaStatus;
                        currentBaristaStatus = data.barista_status;
                        const itemCount = data.items.length;
                        if(cart.filter(i => !i.isNew).length < itemCount) {
                            cart = data.items.filter(i => i.item_status !== 'pending').map(i => ({...i, isNew: false}));
                            renderCart();
                        } else {
                            if(prevStatus !== null && prevStatus !== currentBaristaStatus) renderBaristaStatus();
                        }
                    }
                } catch(e) {}
            }, 30000);

            let lastOpenCount = 0;
            setInterval(async () => {
                try {
                    const res    = await authFetch('/tables-status');
                    const data   = await res.json();
                    const count  = data.open_tables.length;
                    if(lastOpenCount > 0 && count > lastOpenCount) {
                        showToast('🔔 Yeni sipariş eklendi!', 'success');
                    }
                    lastOpenCount = count;
                } catch(e) {}
            }, 30000);
        };