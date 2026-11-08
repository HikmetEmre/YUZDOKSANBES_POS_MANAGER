// duration.js
// =====================================================
// How long a table has been open — SHARED by the POS (/) and the waiter page.
//   long:  "1 dakikadan az" · "3 dakika" · "32 dakika" · "1 saat" · "1 saat 7 dakika"
//   short: "<1 dk" · "3 dk" · "32 dk" · "1 sa" · "1 sa 7 dk"   (under table buttons)
// Seconds come from the SERVER (/table/{n}, /tables-status → open_seconds), so a
// phone or tablet with a wrong clock still shows the right duration; between
// server updates we only add the time elapsed on this device.
// =====================================================
window.Duration = (function () {
    function minutes(sec) { return Math.floor(Math.max(0, Number(sec) || 0) / 60); }

    function long(sec) {
        const m = minutes(sec);
        if (m < 1) return '1 dakikadan az';
        const h = Math.floor(m / 60), r = m % 60;
        if (!h) return `${m} dakika`;
        return r ? `${h} saat ${r} dakika` : `${h} saat`;
    }

    function short(sec) {
        const m = minutes(sec);
        if (m < 1) return '<1 dk';
        const h = Math.floor(m / 60), r = m % 60;
        if (!h) return `${m} dk`;
        return r ? `${h} sa ${r} dk` : `${h} sa`;
    }

    function clock(iso) {                      // "14:05" for the tooltip
        const d = new Date(iso);
        if (isNaN(d)) return '';
        const p = n => String(n).padStart(2, '0');
        return `${p(d.getHours())}:${p(d.getMinutes())}`;
    }

    // ---- order-list header of the selected table (ticks every 30 s) ----
    let hdr = { el: null, sec: 0, at: 0, timer: null };
    function paintHeader() {
        if (!hdr.el) return;
        const now = hdr.sec + (Date.now() - hdr.at) / 1000;
        hdr.el.textContent = '⏱ ' + long(now);
    }
    function showHeader(el, openSeconds, createdAt) {
        clearHeader(el);
        if (!el || openSeconds == null) return;
        hdr = { el, sec: Number(openSeconds) || 0, at: Date.now(), timer: null };
        el.title = createdAt ? 'Açılış: ' + clock(createdAt) : '';
        paintHeader();
        hdr.timer = setInterval(paintHeader, 30000);
    }
    function clearHeader(el) {
        if (hdr.timer) clearInterval(hdr.timer);
        hdr = { el: null, sec: 0, at: 0, timer: null };
        if (el) { el.textContent = ''; el.title = ''; }
    }

    // ---- small duration under every occupied table button ----
    // openSeconds: {"12": 1920, ...} from /tables-status; tables: all table numbers.
    function paintButtons(openSeconds, tables) {
        const map = openSeconds || {};
        (tables || []).forEach(n => {
            const btn = document.getElementById('t' + n);
            if (!btn) return;
            let tag = btn.querySelector('.t-dur');
            const sec = map[String(n)];
            if (sec == null) { if (tag) tag.remove(); return; }
            if (!tag) { tag = document.createElement('span'); tag.className = 't-dur'; btn.appendChild(tag); }
            tag.textContent = short(sec);
        });
    }

    return { long, short, showHeader, clearHeader, paintButtons };
})();

// =====================================================
// Table names — every table has an internal number (orders, QR links, the
// button id "t12") and a display name set in Ayarlar → Masalar.
//   label(12)   → "Masa 12"  (digits-only name)   · "Toplantı Masası" (text name)
//   short(12)   → "12"                             · "Toplantı Masası"   (buttons)
// set() takes /config → table_names ({"12": "12", "1001": "Toplantı Masası"}).
// =====================================================
window.TableNames = (function () {
    let names = {};
    function set(map) { names = map || {}; }
    function short(no) { const n = names[String(no)]; return n == null || n === '' ? String(no) : String(n); }
    function label(no) { const s = short(no); return /^\d+$/.test(s) ? 'Masa ' + s : s; }
    function isNamed(no) { return !/^\d+$/.test(short(no)); }
    return { set, short, label, isNamed };
})();
