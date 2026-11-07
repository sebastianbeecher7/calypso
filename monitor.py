"""Monitor de disponibilidad de Trattoria Calypso (Woki).

Consulta la API publica de Woki, y si aparece cupo para PEOPLE personas
que no habia avisado antes, manda un mensaje por WhatsApp (CallMeBot)
y/o una notificacion push (ntfy). Pensado para correr cada 15 min en
GitHub Actions.
"""
import json
import os
import re
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = "https://api.wokiapp.com/geta/v1/availability"
PARTNER = "19ff9be1-b421-4207-b2b2-0fe84d7bb0cb"
PAGE_URL = "https://www.wokiapp.com/restaurante/trattoria-calypso"

PEOPLE = int(os.getenv("PEOPLE", "2"))
HORIZON_DAYS = int(os.getenv("HORIZON_DAYS", "35"))  # cuantos dias hacia adelante revisar
CHUNK_DAYS = 7  # la pagina consulta por tramos cortos; hacemos lo mismo
TZ = ZoneInfo("America/Santiago")
UNAVAILABLE_TAGS = {"FULL", "NOT_AVAILABLE"}
STATE_FILE = Path("state.json")
DIAS = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]

HEADERS = {
    "accept": "application/json",
    "origin": "https://www.wokiapp.com",
    "referer": "https://www.wokiapp.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
    ),
}


def local_midnight_utc(d):
    """Medianoche de Chile para la fecha d, en el formato que usa la API."""
    dt = datetime.combine(d, time(0, 0), TZ).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def api_get(path, params):
    params = {**params, "quantity": PEOPLE, "locale": "es", "partner": PARTNER}
    r = requests.get(f"{BASE}/{path}", params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_days():
    """Devuelve {'2026-11-14': {'enable':..., 'tag':...}, ...} para el horizonte."""
    today = datetime.now(TZ).date()
    end = today + timedelta(days=HORIZON_DAYS)
    days = {}
    start = today
    while start < end:
        stop = min(start + timedelta(days=CHUNK_DAYS), end)
        data = api_get(
            "day-range",
            {"from": local_midnight_utc(start), "to": local_midnight_utc(stop)},
        )
        for key, info in data.items():
            days[key[:10]] = info
        start = stop
    return days


def norm_time(v):
    """'13:30' o un datetime ISO -> 'HH:MM' en hora de Chile; si no, None."""
    if not isinstance(v, str):
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", v):
        return v[:5]
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(TZ)
    return dt.strftime("%H:%M")


TIME_KEYS = {"time", "hour", "start", "starttime", "datetime", "date", "slot"}


def extract_times(node, out):
    """Busca horarios en una respuesta de forma desconocida."""
    if isinstance(node, dict):
        for k, v in node.items():
            t = norm_time(v)
            if t and k.lower() in TIME_KEYS:
                out.append(t)
            elif ":" in k and norm_time(k):
                out.append(norm_time(k))
            else:
                extract_times(v, out)
    elif isinstance(node, list):
        for v in node:
            t = norm_time(v)
            if t:
                out.append(t)
            else:
                extract_times(v, out)


def times_for_day(d):
    data = api_get(
        "bookable-times-for-day",
        {"from": local_midnight_utc(datetime.strptime(d, "%Y-%m-%d").date())},
    )
    found = []
    extract_times(data, found)
    if found:
        return ", ".join(sorted(set(found)))
    return "(formato no reconocido) " + json.dumps(data, ensure_ascii=False)[:200]


def notify(text):
    """Envia por los canales configurados. True si al menos uno funciono."""
    ok = False
    phone, key = os.getenv("CALLMEBOT_PHONE"), os.getenv("CALLMEBOT_APIKEY")
    if phone and key:
        try:
            r = requests.get(
                "https://api.callmebot.com/whatsapp.php",
                params={"phone": phone, "text": text, "apikey": key},
                timeout=30,
            )
            print("WhatsApp (CallMeBot) status:", r.status_code)
            ok = ok or r.ok
        except requests.RequestException as e:
            # No imprimir e: puede incluir la URL con la apikey (logs publicos).
            print("WhatsApp error:", type(e).__name__)
    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                data={"chat_id": tg_chat, "text": text, "disable_web_page_preview": "true"},
                timeout=20,
            )
            print("Telegram status:", r.status_code)
            ok = ok or r.ok
        except requests.RequestException as e:
            # No imprimir e: la URL contiene el token del bot (logs publicos).
            print("Telegram error:", type(e).__name__)
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        try:
            r = requests.post(
                f"https://ntfy.sh/{topic}",
                data=text.encode("utf-8"),
                headers={"Title": "Trattoria Calypso"},
                timeout=20,
            )
            print("ntfy status:", r.status_code)
            ok = ok or r.ok
        except requests.RequestException as e:
            print("ntfy error:", type(e).__name__)
    if not (phone and key) and not (tg_token and tg_chat) and not topic:
        print("No hay ningun canal de aviso configurado (secrets).")
    return ok


def load_state():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def pretty_day(d):
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{DIAS[dt.weekday()]} {dt.day:02d}/{dt.month:02d}"


def main():
    if os.getenv("TEST_NOTIFY") == "1":
        sys.exit(0 if notify("Prueba: el monitor de Trattoria Calypso funciona.") else 1)

    days = fetch_days()
    print("Dias revisados:", len(days), "| tags vistos:", sorted({str(i.get("tag")) for i in days.values()}))

    hits = {
        d: i for d, i in days.items()
        if i.get("enable") or i.get("tag") not in UNAVAILABLE_TAGS
    }
    current = {f"{d}|{i.get('tag')}" for d, i in hits.items()}
    previous = load_state()

    if current - previous:
        lines = []
        for d in sorted(hits):
            try:
                horas = times_for_day(d)
            except requests.RequestException as e:
                horas = f"(no pude leer los horarios: {type(e).__name__})"
            lines.append(f"{pretty_day(d)} [{hits[d].get('tag')}]: {horas}")
        msg = f"Cupo para {PEOPLE} en Trattoria Calypso:\n" + "\n".join(lines) + f"\n{PAGE_URL}"
        print(msg)
        if not notify(msg):
            print("No se pudo avisar; se reintenta en la proxima ejecucion.")
            sys.exit(1)

    if current != previous:
        STATE_FILE.write_text(json.dumps(sorted(current)))


if __name__ == "__main__":
    main()
