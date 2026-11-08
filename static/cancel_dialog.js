// cancel_dialog.js
// =====================================================
// "Why is this being cancelled?" — SHARED by the POS (/) and the waiter page.
// Reason buttons come from /config (cancel_reasons): "Yanlış Ürün",
// "Soğuk/Eksik", "Diğer" (Diğer needs a typed reason). When a line has more
// than one unit, staff choose how many to cancel (default: all).
//
//   CancelDialog.init(cfg)                       // once, with the /config JSON
//   CancelDialog.open({ title, maxQty, confirmText })
//       → Promise resolving to {qty, reason, note}, or null if dismissed.
// The server validates everything again; this only makes it quick and clear.
// =====================================================
window.CancelDialog = (function () {
    let reasons = ["Yanlış Ürün", "Soğuk/Eksik", "Diğer"], other = "Diğer";
    let el = null, state = null;

    function esc(s) {
        return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    function init(cfg) {
        if (cfg && Array.isArray(cfg.cancel_reasons) && cfg.cancel_reasons.length) reasons = cfg.cancel_reasons;
        if (cfg && cfg.cancel_reason_other) other = cfg.cancel_reason_other;
    }

    function build() {
        el = document.createElement("div");
        el.id = "cancelModal";
        el.className = "modal-overlay";
        el.style.display = "none";
        el.innerHTML = `
            <div class="modal-box cancel-box" role="dialog" aria-modal="true">
                <h3 id="cxTitle"></h3>
                <p class="cx-sub">İptal nedeni seçin</p>
                <div class="cx-reasons" id="cxReasons"></div>
                <input id="cxNote" class="cx-note" maxlength="120" placeholder="İptal nedenini yazın…" style="display:none;">
                <div class="cx-qty" id="cxQtyRow" style="display:none;">
                    <span>Kaç adet iptal edilsin?</span>
                    <div class="cx-stepper">
                        <button type="button" id="cxMinus" aria-label="azalt">−</button>
                        <span id="cxQty">1</span>
                        <button type="button" id="cxPlus" aria-label="artır">+</button>
                    </div>
                    <span class="cx-of" id="cxOf"></span>
                </div>
                <div class="cx-error" id="cxError"></div>
                <div class="modal-btn-row">
                    <button type="button" class="modal-btn cx-confirm" id="cxConfirm" disabled>İptal Et</button>
                    <button type="button" class="modal-btn secondary" id="cxCancel">Vazgeç</button>
                </div>
            </div>`;
        document.body.appendChild(el);
        el.querySelector("#cxCancel").onclick = () => close(null);
        el.addEventListener("click", e => { if (e.target === el) close(null); });     // tap outside
        el.querySelector("#cxMinus").onclick = () => setQty(state.qty - 1);
        el.querySelector("#cxPlus").onclick = () => setQty(state.qty + 1);
        el.querySelector("#cxNote").addEventListener("input", refresh);
        el.querySelector("#cxNote").addEventListener("keydown", e => { if (e.key === "Enter") confirm(); });
        el.querySelector("#cxConfirm").onclick = confirm;
    }

    function setQty(q) {
        state.qty = Math.min(state.max, Math.max(1, q));
        el.querySelector("#cxQty").textContent = state.qty;
        el.querySelector("#cxMinus").disabled = state.qty <= 1;
        el.querySelector("#cxPlus").disabled = state.qty >= state.max;
    }

    function pick(r) {
        state.reason = r;
        el.querySelectorAll(".cx-reason").forEach(b => b.classList.toggle("on", b.dataset.r === r));
        const note = el.querySelector("#cxNote");
        note.style.display = r === other ? "block" : "none";
        if (r === other) note.focus();
        refresh();
    }

    function valid() {
        if (!state || !state.reason) return false;
        if (state.reason === other && !el.querySelector("#cxNote").value.trim()) return false;
        return true;
    }

    function refresh() {
        el.querySelector("#cxConfirm").disabled = !valid();
        el.querySelector("#cxError").textContent = "";
    }

    function confirm() {
        if (!valid()) {
            el.querySelector("#cxError").textContent =
                state && state.reason === other ? "Lütfen iptal nedenini yazın." : "Lütfen bir neden seçin.";
            return;
        }
        const note = state.reason === other ? el.querySelector("#cxNote").value.trim() : null;
        close({ qty: state.qty, reason: state.reason, note });
    }

    function close(result) {
        if (el) el.style.display = "none";
        const r = state && state.resolve;
        state = null;
        if (r) r(result);
    }

    function open({ title = "İptal", maxQty = 1, confirmText = "İptal Et" } = {}) {
        if (!el) build();
        if (state) close(null);
        const max = Math.max(1, Math.floor(Number(maxQty) || 1));
        return new Promise(resolve => {
            state = { reason: null, qty: max, max, resolve };
            el.querySelector("#cxTitle").textContent = title;
            el.querySelector("#cxReasons").innerHTML = reasons.map(r =>
                `<button type="button" class="cx-reason" data-r="${esc(r)}">${esc(r)}</button>`).join("");
            el.querySelectorAll(".cx-reason").forEach(b => b.onclick = () => pick(b.dataset.r));
            const note = el.querySelector("#cxNote"); note.value = ""; note.style.display = "none";
            el.querySelector("#cxQtyRow").style.display = max > 1 ? "flex" : "none";
            el.querySelector("#cxOf").textContent = max > 1 ? `/ ${max}` : "";
            el.querySelector("#cxConfirm").textContent = confirmText;
            setQty(max);
            refresh();
            el.style.display = "flex";
        });
    }

    return { init, open };
})();
