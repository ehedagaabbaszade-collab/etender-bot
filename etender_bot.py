#!/usr/bin/env python3
"""
etender.gov.az monitor: tenderləri çəkir -> OpenAI ilə təhlil edir -> Telegram-a göndərir.

İstifadə:
  python etender_bot.py --probe     # saytın cavabını göstərir (ilk dəfə mütləq işlət)
  python etender_bot.py --once      # bir dəfə yoxla və çıx (cron / GitHub Actions üçün)
  python etender_bot.py             # daimi dövr (hər CHECK_INTERVAL_MIN dəqiqədən bir)

Məxfi açarlar .env faylından / mühit dəyişənlərindən oxunur (kodun içinə yazma!):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY
"""
import argparse
import html
import json
import logging
import os
import sqlite3
import sys
import time

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ----------------------------- Ayarlar -----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "15"))
DB_PATH = os.getenv("DB_PATH", "seen_tenders.db")

# Sayt strukturu dəyişərsə YALNIZ bu hissəni düzəlt (.env-də də verə bilərsən).
# --probe rejimi real cavabı göstərir.
LIST_URL = os.getenv("ETENDER_LIST_URL", "https://etender.gov.az/api/events")
LIST_PARAMS = json.loads(os.getenv(
    "ETENDER_LIST_PARAMS",
    '{"EventType": 2, "PageSize": 50, "PageNumber": 1, "EventStatus": 1}',
))
DETAIL_URL = os.getenv("ETENDER_DETAIL_URL", "https://etender.gov.az/api/events/{id}")
PUBLIC_URL = os.getenv("ETENDER_PUBLIC_URL", "https://etender.gov.az/main/competition/detail/{id}")

ID_KEYS = ("eventId", "id", "Id", "ID", "tenderId", "number")
TITLE_KEYS = ("eventName", "name", "title", "Name", "Title", "subject")

MAX_CONTENT_CHARS = 7000
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "az,en;q=0.8",
}

SYSTEM_PROMPT = """Sən Azərbaycanda fəaliyyət göstərən "ECO TRANSPORT SOLUTIONS" MMC şirkəti üçün tender filtrləmə köməkçisisən.

Şirkətin fəaliyyət sahəsi: ağır yük daşımaları, qazma-daşıma işləri, tikinti söküntü işləri, torpaq, qum, çınqıl və sənaye tipli tikinti materiallarının daşınması və nəqli xidmətləri (özüboşaldan, ekskavator, buldozer, ağır texnika icarəsi/xidməti və s.).

UYĞUN SAYILMAYANLAR: məişət, mebel, ev/ofis köçürülməsi daşımaları, sərnişin daşımaları, avtomobil/texnika ALQI-satqısı (xidmət deyil), ərzaq, kompüter, tibb, təmir-təchizat və şirkətin sahəsinə aid olmayan hər şey.

Tenderin başlığına və məzmununa (texniki şərtlərə) baxıb qərar ver. Yalnız aydın şəkildə şirkətin sahəsinə aid olanları uyğun say. Şübhəli hallarda relevant=false de.
Cavabı YALNIZ bu JSON formatında ver:
{"relevant": true|false, "reason": "Azərbaycan dilində 1-2 cümlə izah"}"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("etender-bot")

session = requests.Session()
session.headers.update(HEADERS)


# ----------------------------- Yaddaş (SQLite) -----------------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, relevant INTEGER, ts REAL)"
    )
    conn.commit()
    return conn


def is_seen(conn, tid):
    return conn.execute("SELECT 1 FROM seen WHERE id=?", (tid,)).fetchone() is not None


def mark_seen(conn, tid, relevant):
    conn.execute(
        "INSERT OR REPLACE INTO seen (id, relevant, ts) VALUES (?,?,?)",
        (tid, int(relevant), time.time()),
    )
    conn.commit()


# ----------------------------- Saytdan məlumat çəkmə -----------------------------
def http_get(url, params=None, retries=3):
    last = None
    for i in range(retries):
        try:
            r = session.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def _find_list_of_dicts(data):
    """JSON cavabında tenderlərin siyahısını tapır (cavab strukturu fərqli ola bilər)."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in data.values():
            if isinstance(v, dict):
                found = _find_list_of_dicts(v)
                if found:
                    return found
    return []


def _pick(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def fetch_tender_list():
    """Son elanların siyahısı: [{'id','title','url'}, ...]"""
    r = http_get(LIST_URL, LIST_PARAMS)
    try:
        items = _find_list_of_dicts(r.json())
    except ValueError:
        raise RuntimeError(
            "Siyahı JSON qaytarmadı. 'python etender_bot.py --probe' ilə cavaba bax "
            "və LIST_URL / LIST_PARAMS ayarlarını düzəlt."
        )
    tenders = []
    for it in items:
        tid = _pick(it, ID_KEYS)
        if tid is None:
            continue
        tid = str(tid)
        tenders.append({
            "id": tid,
            "title": str(_pick(it, TITLE_KEYS) or "(başlıq yoxdur)"),
            "url": PUBLIC_URL.format(id=tid),
            "raw": it,
        })
    return tenders


def _flatten_strings(obj, out, depth=0):
    if depth > 8:
        return
    if isinstance(obj, str):
        s = obj.strip()
        if s:
            out.append(s)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _flatten_strings(v, out, depth + 1)
            elif isinstance(v, str) and v.strip():
                out.append(f"{k}: {v.strip()}")
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(f"{k}: {v}")
    elif isinstance(obj, list):
        for v in obj:
            _flatten_strings(v, out, depth + 1)


def fetch_tender_content(tender):
    """Tenderin daxili məzmununu (təsvir, texniki şərtlər, mallar/xidmətlər) mətn kimi qaytarır."""
    parts = []
    try:
        r = http_get(DETAIL_URL.format(id=tender["id"]))
        ctype = r.headers.get("Content-Type", "")
        if "json" in ctype or r.text.lstrip().startswith(("{", "[")):
            _flatten_strings(r.json(), parts)
        else:
            soup = BeautifulSoup(r.text, "html.parser")
            for t in soup(["script", "style", "noscript"]):
                t.decompose()
            parts.append(soup.get_text("\n", strip=True))
    except Exception as e:
        log.warning("Daxili səhifə oxunmadı (%s): %s — siyahı məlumatı ilə davam edilir", tender["id"], e)
        _flatten_strings(tender.get("raw", {}), parts)

    text = "\n".join(parts)
    return text[:MAX_CONTENT_CHARS]


# ----------------------------- OpenAI təhlili -----------------------------
_client = None


def analyze(title, content):
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    user_msg = f"TENDER BAŞLIĞI: {title}\n\nTENDER MƏZMUNU:\n{content}"
    resp = _client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    data = json.loads(resp.choices[0].message.content)
    return bool(data.get("relevant")), str(data.get("reason", "")).strip()


# ----------------------------- Telegram -----------------------------
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram xətası: {r.status_code} {r.text}")


def format_message(t, reason):
    return (
        "🚛 <b>Yeni uyğun tender</b>\n\n"
        f"<b>{html.escape(t['title'])}</b>\n\n"
        f"🤖 {html.escape(reason)}\n\n"
        f"🔗 {html.escape(t['url'])}"
    )


# ----------------------------- Əsas məntiq -----------------------------
def run_cycle(conn):
    tenders = fetch_tender_list()
    log.info("Siyahıda %d tender tapıldı", len(tenders))
    new = [t for t in tenders if not is_seen(conn, t["id"])]
    log.info("Yeni: %d", len(new))
    for t in new:
        try:
            content = fetch_tender_content(t)
            relevant, reason = analyze(t["title"], content)
            if relevant:
                send_telegram(format_message(t, reason))
                log.info("GÖNDƏRİLDİ: %s | %s", t["id"], t["title"])
            else:
                log.info("Uyğun deyil: %s | %s", t["id"], t["title"])
            mark_seen(conn, t["id"], relevant)  # yalnız uğurlu emaldan sonra
        except Exception as e:
            log.error("Tender %s emal olunmadı (növbəti dövrdə yenidən cəhd): %s", t["id"], e)
        time.sleep(1)  # saytı yükləməmək üçün


def probe():
    print("== SİYAHI ==")
    r = http_get(LIST_URL, LIST_PARAMS)
    print("URL:", r.url, "| status:", r.status_code, "|", r.headers.get("Content-Type"))
    print(r.text[:2000])
    try:
        tenders = fetch_tender_list()
        print(f"\nTapılan tender sayı: {len(tenders)}")
        if tenders:
            t = tenders[0]
            print("İlk tender:", t["id"], t["title"], t["url"])
            print("\n== DAXİLİ MƏZMUN (ilk 1500 simvol) ==")
            print(fetch_tender_content(t)[:1500])
    except Exception as e:
        print("XƏTA:", e)


def check_config():
    missing = [k for k, v in {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "OPENAI_API_KEY": OPENAI_API_KEY,
    }.items() if not v]
    if missing:
        sys.exit(f"Çatışmayan ayarlar (.env faylına əlavə et): {', '.join(missing)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if args.probe:
        probe()
        return

    check_config()
    conn = db_connect()
    if args.once:
        run_cycle(conn)
        return

    send_telegram("✅ Tender botu işə düşdü.")
    while True:
        try:
            run_cycle(conn)
        except Exception as e:
            log.error("Dövr xətası: %s", e)
        time.sleep(CHECK_INTERVAL_MIN * 60)


if __name__ == "__main__":
    main()
