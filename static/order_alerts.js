// order_alerts.js
// =====================================================
// New-QR-order alert, SHARED by the waiter page (/waiter) and the POS (/).
// Rings static/order_bell.MP3, vibrates (Android) and shows an OS notification
// (when allowed) the moment a new unaccepted QR item appears, then lets the
// page refresh itself (table colours, area signal, Bekleyen list).
//
// Usage (each page passes its own token-aware fetch):
//   OrderAlerts.start({ authFetch, isLoggedIn: () => !!AUTH_TOKEN,
//                       onNewOrder: tables => { ...refresh UI... } });
//   OrderAlerts.unlockAudio()   // call inside the login tap, before any await
// =====================================================
window.OrderAlerts = (function () {
    const bell = new Audio('/order_bell.MP3');
    bell.preload = 'auto';
    let audioUnlocked = false;
    let audioUnlocking = false;   // a muted unlock play() is in flight
    let knownPendingIds = null;   // set of pending item ids from the last poll
    let pendingPrimed = false;    // true after the FIRST poll (no ring on load)
    let opts = null;

    function unlockAudio() {
        if (audioUnlocked || audioUnlocking) return;
        audioUnlocking = true;
        // SILENT unlock (muted + volume 0) so opening the page / tapping never
        // makes a sound. This just satisfies the autoplay policy so the poll
        // timer is allowed to play the bell later. audioUnlocked is only set
        // once play() actually SUCCEEDS — if the browser rejects it, the next
        // tap/click/key retries the unlock.
        bell.muted = true; bell.volume = 0;
        const restore = () => { bell.muted = false; bell.volume = 1; audioUnlocking = false; };
        const ok = () => { bell.pause(); bell.currentTime = 0; audioUnlocked = true; restore(); };
        let p;
        try { p = bell.play(); } catch (e) { restore(); p = null; }
        if (p && p.then) p.then(ok).catch(restore);
        else if (p !== null) ok();
        // Ask for OS notification permission (only granted on HTTPS/localhost).
        try { if (window.Notification && Notification.permission === 'default') Notification.requestPermission(); } catch (e) {}
        keepAwake();
    }
    // The first user gesture (tap/click/key) unlocks the bell — also covers an
    // already-logged-in session that reopens without a login tap.
    ['touchend', 'click', 'keydown'].forEach(ev =>
        document.addEventListener(ev, unlockAudio, { passive: true }));

    // Screen Wake Lock keeps the device awake while the page is in the
    // foreground (so timers/audio aren't suspended). Needs HTTPS/localhost;
    // fails silently over plain http:// on the LAN.
    async function keepAwake() {
        try {
            if ('wakeLock' in navigator && document.visibilityState === 'visible')
                await navigator.wakeLock.request('screen');
        } catch (e) {}
    }
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') keepAwake(); });

    function notify(tables) {
        // 1) Sound (works while the page is in the foreground).
        try { bell.currentTime = 0; const p = bell.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {}
        // 2) Vibrate (Android foreground).
        try { if (navigator.vibrate) navigator.vibrate([250, 120, 250]); } catch (e) {}
        // 3) OS notification — the closest to a real background alert the web
        //    platform allows. Delivered in the background only over HTTPS/PWA.
        try {
            if (window.Notification && Notification.permission === 'granted') {
                const n = new Notification('🔔 Yeni sipariş!', {
                    body: tables ? tables : 'Yeni QR siparişi bekliyor',
                    tag: 'yeni-siparis', renotify: true, icon: '/favicon.ico'
                });
                n.onclick = () => { window.focus(); n.close(); };
            }
        } catch (e) {}
    }

    async function check() {
        if (!opts || !opts.isLoggedIn()) return;
        try {
            const res = await opts.authFetch('/pending');
            if (!res.ok) return;
            const d = await res.json();
            const ids = new Set();
            const newTables = new Set();
            const labels = new Map();                     // table_no -> "Masa 12" / "Toplantı Masası"
            (d.tables || []).forEach(t => (t.items || []).forEach(it => {
                ids.add(it.id);
                if (pendingPrimed && knownPendingIds && !knownPendingIds.has(it.id)) {
                    newTables.add(t.table_no);
                    labels.set(t.table_no, t.table_label || ('Masa ' + t.table_no));
                }
            }));
            if (pendingPrimed && newTables.size) {        // never fires on the first poll
                const list = [...newTables].join(', ');
                notify([...labels.values()].join(', '));
                try { opts.onNewOrder(list); } catch (e) {}
            }
            knownPendingIds = ids;
            pendingPrimed = true;
        } catch (e) {}
    }

    function start(o) {
        if (opts) return;                       // already running
        opts = o;
        check();
        keepAwake();
        setInterval(check, o.intervalMs || 8000);
    }

    return { start, unlockAudio, keepAwake };
})();
