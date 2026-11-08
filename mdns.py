# mdns.py
# =====================================================
# Announces this PC on the café WiFi as  <config.MDNS_NAME>.local
# (default: siparis.local) using mDNS / "Bonjour", so phones reach the POS at
# http://siparis.local — no router setup, and printed QR codes keep working
# even when the router gives this PC a new IP address.
#
# Supported by iPhone/iPad, macOS, Windows 10+, and Android 12+.
#
# Safety: like backup.py, a failure here only logs a warning. The POS keeps
# running and QR codes fall back to the plain IP address (see order_server).
# =====================================================

import logging
import socket
import threading

import config

log = logging.getLogger("mdns")

_zc = None              # zeroconf.Zeroconf instance while running
_info = None            # the registered ServiceInfo
_ip = None              # IPv4 currently announced
_stop = threading.Event()


def lan_ip() -> str:
    """This PC's LAN IPv4 — the address of the interface that routes outward
    (no packet is sent). Virtual adapters (Hyper-V, VPN…) are thereby skipped."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def hostname() -> str:
    return f"{config.MDNS_NAME}.local"


def is_running() -> bool:
    return _zc is not None


def _make_info(ip: str, port: int):
    from zeroconf import ServiceInfo
    return ServiceInfo(
        "_http._tcp.local.",
        "YUZDOKSANBES POS._http._tcp.local.",
        addresses=[socket.inet_aton(ip)],
        port=port,
        server=hostname() + ".",          # ← answers A-record lookups for siparis.local
        properties={"path": "/"},
    )


def _in_own_thread(fn, *args, timeout=15):
    """Run fn on a fresh thread and wait for it. zeroconf's blocking API must
    NOT be called from inside a running asyncio loop (the web server's startup
    hook is one): there it would attach to that loop and deadlock. On a plain
    thread it creates and owns its own background loop instead."""
    out = {}
    t = threading.Thread(target=lambda: out.update(r=fn(*args)), daemon=True, name="mdns")
    t.start()
    t.join(timeout)
    return out.get("r", False)


def start(port: int) -> bool:
    """Register siparis.local → this PC's LAN IP. Returns True on success."""
    return _in_own_thread(_start, port)


def stop() -> None:
    _in_own_thread(_stop_now, timeout=5)


def _start(port: int) -> bool:
    global _zc, _info, _ip
    if not getattr(config, "MDNS_ENABLED", True):
        return False
    try:
        from zeroconf import Zeroconf, IPVersion
        ip = lan_ip()
        if ip.startswith("127."):
            log.warning("mDNS not started: no LAN connection found")
            return False
        _zc = Zeroconf(interfaces=[ip], ip_version=IPVersion.V4Only)
        _info = _make_info(ip, port)
        _zc.register_service(_info, allow_name_change=True)
        _ip = ip
        log.info("mDNS: http://%s → %s:%s", hostname(), ip, port)
        _stop.clear()
        threading.Thread(target=_watch_ip, args=(port,), daemon=True, name="mdns-ip-watch").start()
        return True
    except Exception as e:
        log.warning("mDNS could not start (QR codes will use the IP address): %r", e)
        _stop_now()
        return False


def _watch_ip(port: int) -> None:
    """If DHCP gives this PC a new IP, re-announce siparis.local on it."""
    while not _stop.wait(60):
        try:
            ip = lan_ip()
            if ip != _ip and not ip.startswith("127."):
                log.info("mDNS: LAN IP changed %s → %s, re-announcing", _ip, ip)
                _stop_now()
                _start(port)      # this watcher thread has no asyncio loop — safe
                return            # _start() spawned a fresh watcher
        except Exception as e:
            log.warning("mDNS IP check failed: %s", e)


def _stop_now() -> None:
    global _zc, _info, _ip
    _stop.set()
    zc, info = _zc, _info
    _zc = _info = _ip = None
    try:
        if zc and info:
            zc.unregister_service(info)
    except Exception:
        pass
    try:
        if zc:
            zc.close()
    except Exception:
        pass
