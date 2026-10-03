"""bsf_fetch -- Live-Datenbezug aus Home Assistant (REST + WebSocket).

Nur stdlib: urllib fuer REST, ein minimaler RFC-6455-WebSocket-Client fuer
recorder/statistics_during_period (das gibt es nicht per REST).

Schreibt die 5 Dateien im hadata/-Format in ein Zielverzeichnis, damit die
Analyse (bsf_checks) live und offline identisch laeuft.

WICHTIG (Operator-Setup): --ha-url als IP angeben (http://10.102.10.11:8123);
der Hostname 'hass' loest auf manchen Clients nicht auf.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import json
import os
import socket
import ssl
import struct
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from zoneinfo import ZoneInfo

from bsf_data import UTC

DOMAIN = "balcony_solar_forecast"

def resolve_sources(entries: list[dict], registry: list[dict], states: list[dict],
                    entry_id: str | None = None) -> tuple[str, dict[str, str], list[str]]:
    """Resolve one site through registry identity, including renamed entities.

    Source attributes belong to the selected entry's DC/AC aggregate entities;
    entity names and another site's diagnostics never establish membership.
    """
    ours = {e['entry_id'] for e in entries if e.get('domain') == DOMAIN}
    if entry_id is None:
        if len(ours) != 1:
            raise FetchError('Select --entry-id: no unique balcony-solar config entry')
        entry_id = next(iter(ours))
    if entry_id not in ours:
        raise FetchError('Selected entry does not belong to balcony_solar_forecast')
    roles = {}
    for row in registry:
        if row.get('config_entry_id') != entry_id or row.get('disabled_by'):
            continue
        unique = row.get('unique_id', '')
        prefix = entry_id + '_'
        if row.get('platform') == DOMAIN and unique.startswith(prefix):
            roles[unique[len(prefix):]] = row['entity_id']
    by_id = {s['entity_id']: s for s in states}
    sources = []
    for key, attr in [('measured_dc_power_total', 'sources'), ('measured_ac_power', 'source')]:
        attrs = by_id.get(roles.get(key), {}).get('attributes') or {}
        value = attrs.get(attr, [])
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise FetchError('Invalid measurement-source metadata')
        sources.extend(values)
    return entry_id, roles, list(dict.fromkeys(sources))


class FetchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


class Rest:
    def __init__(self, base_url: str, token: str, timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, body: Any | None = None) -> Any:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            with contextlib.suppress(Exception):
                detail = e.read().decode(errors="replace")[:300]
            if e.code == 401:
                raise FetchError(
                    "401 Unauthorized - Long-Lived-Token pruefen (HA-Profil > "
                    "Sicherheit > Langlebige Zugriffstoken)."
                ) from e
            raise FetchError(f"HTTP {e.code} fuer {path}: {detail}") from e
        except urllib.error.URLError as e:
            raise FetchError(
                f"Verbindung zu {self.base} fehlgeschlagen ({e.reason}). "
                "IP statt Hostname verwenden, z.B. http://10.102.10.11:8123."
            ) from e
        if not raw:
            return None
        return json.loads(raw)

    def get(self, path: str) -> Any:
        return self._req("GET", path)

    def post(self, path: str, body: Any) -> Any:
        return self._req("POST", path, body)


# ---------------------------------------------------------------------------
# Minimaler WebSocket-Client (RFC 6455, Client-Frames maskiert)
# ---------------------------------------------------------------------------


class MiniWS:
    def __init__(self, ha_url: str, token: str, timeout: float = 90.0):
        u = urllib.parse.urlparse(ha_url)
        self.host = u.hostname or "localhost"
        self.tls = u.scheme == "https"
        self.port = u.port or (443 if self.tls else 80)
        self.token = token
        self.timeout = timeout
        self._id = 0
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.tls:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /api/websocket HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        raw.sendall(req.encode())
        hdr = b""
        while b"\r\n\r\n" not in hdr:
            chunk = raw.recv(4096)
            if not chunk:
                raise FetchError("WebSocket-Handshake: Verbindung geschlossen.")
            hdr += chunk
        head, _, rest = hdr.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise FetchError(
                "WebSocket-Upgrade abgelehnt: " + head.decode(errors="replace")[:200]
            )
        self._buf = rest
        # Auth-Flow
        msg = json.loads(self._recv_message())
        if msg.get("type") != "auth_required":
            raise FetchError(f"Unerwartete WS-Begruessung: {msg}")
        self._send_text(json.dumps({"type": "auth", "access_token": self.token}))
        msg = json.loads(self._recv_message())
        if msg.get("type") != "auth_ok":
            raise FetchError(f"WS-Auth fehlgeschlagen: {msg}")

    # --- frames ---
    def _read_exact(self, n: int) -> bytes:
        assert self.sock is not None
        while len(self._buf) < n:
            chunk = self.sock.recv(min(65536, n - len(self._buf) + 65536))
            if not chunk:
                raise FetchError("WebSocket: Verbindung geschlossen.")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_text(self, text: str) -> None:
        assert self.sock is not None
        payload = text.encode()
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def _send_pong(self, payload: bytes) -> None:
        assert self.sock is not None
        mask = os.urandom(4)
        head = struct.pack("!BB", 0x8A, 0x80 | len(payload))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def _recv_message(self) -> str:
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            masked, ln = b2 & 0x80, b2 & 0x7F
            if ln == 126:
                (ln,) = struct.unpack("!H", self._read_exact(2))
            elif ln == 127:
                (ln,) = struct.unpack("!Q", self._read_exact(8))
            mkey = self._read_exact(4) if masked else b""
            payload = self._read_exact(ln)
            if mkey:
                payload = bytes(b ^ mkey[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:  # ping
                self._send_pong(payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:
                raise FetchError("WebSocket: Server hat geschlossen.")
            parts.append(payload)
            if fin:
                return b"".join(parts).decode()

    def call(self, payload: dict) -> Any:
        self._id += 1
        payload = {"id": self._id, **payload}
        self._send_text(json.dumps(payload))
        while True:
            msg = json.loads(self._recv_message())
            if msg.get("id") == self._id and msg.get("type") == "result":
                if not msg.get("success"):
                    raise FetchError(f"WS-Fehler: {msg.get('error')}")
                return msg.get("result")
            # Events / fremde ids ueberspringen

    def close(self) -> None:
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.close()


# ---------------------------------------------------------------------------
# Fetch-Orchestrierung
# ---------------------------------------------------------------------------


def _iso(d: dt.datetime) -> str:
    return d.astimezone(UTC).isoformat()


def fetch_all(
    ha_url: str,
    token: str,
    out_dir: str,
    days: int = 8,
    entry_id: str | None = None,
    log=print,
) -> None:
    os.makedirs(out_dir, exist_ok=True)
    rest = Rest(ha_url, token)
    now = dt.datetime.now(UTC)
    entries = rest.get(f"/api/config/config_entries/entry?domain={DOMAIN}")
    if isinstance(entries, dict):
        entries = entries.get("entries") or entries.get("result") or []
    states = rest.get("/api/states")
    ws = MiniWS(ha_url, token)
    ws.connect()
    try:
        registry = ws.call({"type": "config/entity_registry/list"})
    finally:
        ws.close()
    entry_id, roles, sources = resolve_sources(entries, registry, states, entry_id)
    timezone = rest.get("/api/config")["time_zone"]
    loc = ZoneInfo(timezone)
    power_keys = ("power_production_now", "power_production_now_dc",
                  "measured_ac_power", "measured_dc_power_total")
    hourly_ids = list(dict.fromkeys(sources + [roles[k] for k in power_keys if k in roles]))
    five_ids = [roles[k] for k in (*power_keys, "intraday_scalar") if k in roles]
    history_minimal = [value for key, value in roles.items() if key.startswith("energy_production_")]
    history_attrs = [value for key, value in roles.items() if key.endswith("_status") or key == "daily_kwh_mae"]
    start_local = dt.datetime.combine(
        (now.astimezone(loc) - dt.timedelta(days=days)).date(), dt.time(0), tzinfo=loc
    )
    start = start_local.astimezone(UTC)
    log(f"Fenster: {start.isoformat()} .. {now.isoformat()} (UTC)")

    # 1) Entities-Snapshot -------------------------------------------------
    log("1/5 Entities-Snapshot (REST /api/states) ...")
    ents = {
        s["entity_id"]: {
            "state": s.get("state"),
            "last_changed": s.get("last_changed"),
            "last_updated": s.get("last_updated"),
            "attributes": s.get("attributes") or {},
        }
        for s in states
        if s.get("entity_id") in roles.values()
    }
    _write(out_dir, "entities_now.json", ents)
    log(f"    {len(ents)} Entities.")

    # 2) Recorder-Statistiken (WS) ----------------------------------------
    log("2/5 recorder/statistics_during_period (WebSocket) ...")
    ws = MiniWS(ha_url, token)
    ws.connect()
    try:
        hourly = ws.call(
            {
                "type": "recorder/statistics_during_period",
                "start_time": _iso(start),
                "end_time": _iso(now),
                "statistic_ids": hourly_ids,
                "period": "hour",
                "types": ["mean"],
                "units": {"power": "W"},
            }
        )
        _write(
            out_dir,
            "actuals_hourly_stats.json",
            {
                "stats": hourly,
                "stat_ids": [
                    {"id": s, "mean": True, "sum": False} for s in hourly_ids
                ],
            },
        )
        log(f"    hourly: {sum(len(v) for v in hourly.values())} Zeilen.")
        five = ws.call(
            {
                "type": "recorder/statistics_during_period",
                "start_time": _iso(start),
                "end_time": _iso(now),
                "statistic_ids": five_ids,
                "period": "5minute",
                "types": ["mean"],
                "units": {"power": "W"},
            }
        )
        _write(
            out_dir,
            "fiveminute_stats.json",
            {"counts": {k: len(v) for k, v in five.items()}, "stats": five},
        )
        log(f"    5minute: {sum(len(v) for v in five.values())} Zeilen.")
    finally:
        ws.close()

    # 3) History (REST) ----------------------------------------------------
    log("3/5 history/period (REST) ...")
    q_start = urllib.parse.quote(_iso(start))
    q_end = urllib.parse.quote(_iso(now))

    def _hist(eids: list[str], minimal: bool) -> list:
        if not eids:
            return []
        flt = ",".join(eids)
        url = (
            f"/api/history/period/{q_start}?end_time={q_end}"
            f"&filter_entity_id={flt}"
        )
        if minimal:
            url += "&minimal_response"
        res = rest.get(url)
        return res if isinstance(res, list) else []

    hist = {
        "minimal": _hist(history_minimal, True),
        "with_attrs": _hist(history_attrs, False),
    }
    _write(out_dir, "forecast_sensor_history.json", hist)
    log(
        f"    minimal: {len(hist['minimal'])} Entities, "
        f"with_attrs: {len(hist['with_attrs'])} Entities."
    )

    # 4) get_issued_forecast pro Tag (REST mit return_response) ------------
    log("4/5 get_issued_forecast pro Tag ...")
    issued: dict[str, Any] = {}
    today_local = now.astimezone(loc).date()
    for i in range(days, -1, -1):
        day = today_local - dt.timedelta(days=i)
        body: dict[str, Any] = {"date": day.isoformat()}
        if entry_id:
            body["entry_id"] = entry_id
        try:
            resp = rest.post(
                f"/api/services/{DOMAIN}/get_issued_forecast?return_response", body
            )
            sr = resp.get("service_response") if isinstance(resp, dict) else None
            issued[day.isoformat()] = {"result": sr if sr is not None else resp}
        except FetchError as e:
            issued[day.isoformat()] = {"error": str(e)}
            log(f"    {day}: FEHLER {e}")

    # 5) Diagnostics (optional, non-fatal) ---------------------------------
    log("5/5 Diagnostics (optional) ...")
    diagnostics = None
    try:
        eid = entry_id
        if eid:
            diagnostics = rest.get(f"/api/diagnostics/config_entry/{eid}")
            # HA verpackt Diagnostics teils als {"data": {...}} auf oberster Ebene
            if (
                isinstance(diagnostics, dict)
                and "data" in diagnostics
                and "integration_manifest" not in diagnostics
                and isinstance(diagnostics["data"], dict)
                and "integration_manifest" in diagnostics["data"]
            ):
                diagnostics = diagnostics["data"]
        else:
            log("    Kein Config-Entry gefunden - Diagnostics uebersprungen.")
    except FetchError as e:
        log(f"    Diagnostics nicht verfuegbar ({e}) - Checks laufen ohne.")

    _write(
        out_dir,
        "issued_forecasts_and_diag.json",
        {"issued": issued, "diagnostics": diagnostics},
    )
    names = ["entities_now.json", "actuals_hourly_stats.json", "fiveminute_stats.json",
             "forecast_sensor_history.json", "issued_forecasts_and_diag.json"]
    import hashlib
    hashes = {}
    for name in names:
        with open(os.path.join(out_dir, name), "rb") as captured:
            hashes[name] = hashlib.sha256(captured.read()).hexdigest()
    _write(out_dir, "capture_manifest.json", {
        "schema_version": 1, "captured_at": _iso(now), "entry_id": entry_id,
        "timezone": timezone, "roles": roles, "measurement_sources": sources,
        "statistics_units": {"power": "W"}, "files_sha256": hashes,
        "sharing": "private operator capture; review and anonymize before publication",
    })
    log(f"Fertig. Daten in {out_dir}")


def _write(out_dir: str, name: str, obj: Any) -> None:
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
