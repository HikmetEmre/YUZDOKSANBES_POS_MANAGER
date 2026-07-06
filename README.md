# YUZDOKSANBES POS — Step 1 (Hardened)

## Run
```bash
pip install fastapi uvicorn pydantic
cd pos
python3 order_server.py
```
Server runs on `0.0.0.0:8000`. Tablets/phones on the same Wi‑Fi open
`http://<MAIN_COMPUTER_IP>:8000` (find IP with `ipconfig` / `ip addr`).

## Structure
- `config.py`     — menu, table count, PIN, port. Edit here only.
- `order_server.py` — backend API + DB. No HTML inside anymore.
- `static/`       — frontend (index.html, style.css, app.js), served by the server.

## What changed in Step 1
- SQLite **WAL mode** + busy timeout → many tablets can read/write without "database is locked".
- **Connection context-manager** → connections always close; rollback on error.
- **Server-side totals** → client-sent prices are ignored; server charges the real menu price. Tamper-proof.
- **Server-side PIN auth** (`POST /auth`) → PIN no longer shipped to the browser.
- **Input validation** (Pydantic) + clean JSON errors.
- **Frontend decoupled** → menu/tables built from `GET /config`.

## Next
- Step 2: split-by-item payment (per-item paid flag).
