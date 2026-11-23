#!/usr/bin/env python3
"""Публікація графіків ГПВ (підчерги N.2) у Telegram-канал.

Джерела: Telegram-канал Черкасиобленерго (основне, виходить раніше) і сайт (запасне).
Одна й та сама версія графіка публікується один раз, хто б із джерел її не показав першим.

Змінні середовища:
  BOT_TOKEN  токен бота від @BotFather
  CHAT_ID    @username каналу або числовий ID (-100...)
  SEED=1     лише запам'ятати поточні пости, нічого не публікувати
  DRY_RUN=1  показати повідомлення в консолі, не надсилати й не зберігати стан
"""
import os
import re
import json
import html
import hashlib
import time
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

# ---------------- налаштування ----------------
LIST_URL = "https://www.cherkasyoblenergo.com/media?category=news&lang=uk"
TG_NAME = "pat_cherkasyoblenergo"
TG_URL = f"https://t.me/s/{TG_NAME}"
TOKEN = os.environ.get("BOT_TOKEN")
CHAT = os.environ.get("CHAT_ID")
SEED = os.environ.get("SEED") == "1"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-bot)"}
TZ = ZoneInfo("Europe/Kyiv")

SHOW_SOURCE = False          # True = додавати рядок «Джерело: Черкасиобленерго»
PARSER_VERSION = 2          # змінюйте при зміні алгоритму розбору: збережені розклади скинуться
MIN_ROWS = 6                 # мінімум розпізнаних рядків N.M, щоб довіряти розбору
INV = "\u2800"               # «невидимий» символ для інтервалів між рядками

FOOTER = [
    INV,
    "#Графіки_Чигирин",
    INV,
    '<a href="https://t.me/+q6hgQ6Yf4TsxMDRi">Канал "Чигиринщина"</a>'
    ' | <a href="https://www.facebook.com/groups/chygyrynschyna">Ми у Facebook</a>',
    INV,
    '✅ <a href="https://t.me/Chyhyryn_bot">Повідомити новину</a>',
]

MONTHS = {"січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5,
          "червня": 6, "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10,
          "листопада": 11, "грудня": 12}
TITLE_RE = re.compile(r"погодинних\s+відключень|\bГПВ\b", re.I)
DATE_RE = re.compile(rf"\bна\s+(\d{{1,2}}\s+(?:{'|'.join(MONTHS)}))", re.I)
T = r"\d{1,2}\s*:\s*\d{2}"
ROW_RE = re.compile(rf"\b([1-6])\.([12])\s+((?:{T}\s*[-–—]\s*{T}[\s,]*)+)")
SLOT_RE = re.compile(rf"({T})\s*[-–—]\s*({T})")


# ---------------- час ----------------
def to_min(s):
    h, m = re.sub(r"\s", "", s).split(":")
    return int(h) * 60 + int(m)


def fmt(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def span(a, b):
    return f"{fmt(a)} - {fmt(b)}"


def day_start(date, now):
    d, mon = date.lower().split()
    cands = []
    for y in (now.year - 1, now.year, now.year + 1):
        try:
            cands.append(datetime(y, MONTHS[mon], int(d), tzinfo=TZ))
        except ValueError:
            pass
    return min(cands, key=lambda x: abs(x - now))


# ---------------- стан ----------------
KEEP_DATES = 10              # скільки останніх дат тримати в стані
KEEP_VERSIONS = 12           # скільки версій графіка на дату пам'ятати


def log(*a):
    print(*a, flush=True)


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if isinstance(d, list):                       # старий формат
        d = {"seen": d}
    st = {"seen": set(d.get("seen", [])),
          "schedules": d.get("schedules", {}),    # дата -> останній графік
          "versions": d.get("versions", {}),      # дата -> відбитки вже оброблених версій
          "times": d.get("times", {}),            # дата -> час публікації останньої версії (UTC)
          "tg_ready": bool(d.get("tg_ready", False))}
    if d.get("parser") != PARSER_VERSION:         # розклади збережені старим алгоритмом
        st["schedules"], st["versions"], st["times"] = {}, {}, {}   # seen лишається
    return st


def save_state(st):
    scheds = st["schedules"]
    while len(scheds) > KEEP_DATES:               # лишаємо останні дати
        scheds.pop(next(iter(scheds)))
    for k in ("versions", "times"):
        for date in [x for x in st[k] if x not in scheds]:
            del st[k][date]
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"seen": sorted(st["seen"]), "schedules": scheds,
                   "versions": st["versions"], "times": st["times"],
                   "tg_ready": st["tg_ready"], "parser": PARSER_VERSION},
                  f, ensure_ascii=False)


def digest(rows):
    """Відбиток версії графіка: однакові дані з різних джерел дають однаковий відбиток."""
    return hashlib.md5(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:12]


# ---------------- сайт ----------------
def get_soup(url):
    """Завантажує сторінку; при збої мережі робить до 3 спроб."""
    for attempt in (1, 2, 3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except requests.RequestException as e:
            if attempt == 3:
                raise
            log(f"Помилка завантаження ({e.__class__.__name__}), спроба {attempt}/3, повтор")
            time.sleep(2 * attempt)


def fetch_list():
    """Новини про графіки: у заголовку є і «ГПВ/погодинних відключень», і дата."""
    soup = get_soup(LIST_URL)
    items = {}
    for a in soup.select('a[href*="/media/"]'):
        url = urljoin(LIST_URL, a["href"])
        img = a.find("img")
        title = a.get_text(strip=True) or (img.get("alt", "") if img else "")
        e = items.setdefault(url, {"url": url, "title": ""})
        if title and not e["title"]:
            e["title"] = title
    return [v for v in items.values()
            if TITLE_RE.search(v["title"]) and DATE_RE.search(v["title"])]


PUB_RE = re.compile(r"(?<![\d.])(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})")


def parse_published(text):
    """Дата публікації зі сторінки новини («21.11.2026 23:02») за київським часом."""
    m = PUB_RE.search(text)
    if not m:
        return None
    d, mo, y, h, mi = map(int, m.groups())
    try:
        return datetime(y, mo, d, h, mi, tzinfo=TZ)
    except ValueError:
        return None


def parse_rows(url):
    """({'1.2': [[960, 1080], ...], ...}, ok, час публікації) зі сторінки новини"""
    text = get_soup(url).get_text("\n")
    rows, ok = parse_text_checked(text)
    return rows, ok, parse_published(text)


GLUE_RE = re.compile(r"(?<![\d.])(\d)\s+(\d)\s*:\s*(\d{2})")
LABEL_RE = re.compile(r"(?<![\d.])([1-6])\.([12])(?=\s+\d)")


def normalize(text):
    """Склеює розірваний час: '0 0 :00' -> '00:00', '10 :00' -> '10:00'."""
    text = text.replace("\xa0", " ")
    return GLUE_RE.sub(r"\1\2:\3", text)


def parse_text(text):
    rows, _ = parse_text_checked(text)
    return rows


HEADER_RE = re.compile(r"Години\s+відсутності", re.I)
# Початок ДРУГОГО списку (окремі графіки для філій) або кінець блоку з графіком.
# Перший список (по області) береться повністю, усе, що йде за цими ознаками, ігнорується.
END_RE = re.compile(
    r"Тимчасові\s+індивідуальні"       # заголовок другого списку
    r"|індивідуальні\s+графіки"         # запасне формулювання
    r"|Години\s+відсутності"            # повторний заголовок = другий список
    r"|Свою\s+чергу",                   # кінець блоку в старому форматі
    re.I)


def first_list(text):
    """Лишає лише перший список графіка (по області).
    Старий формат (один список) повертається без змін."""
    m = HEADER_RE.search(text)
    if m:
        text = text[m.start():]
        e = END_RE.search(text, m.end() - m.start())   # шукаємо після першого заголовка
    else:
        e = END_RE.search(text)
    return text[:e.start()] if e else text


def parse_text_checked(text):
    """Повертає (rows, ok). ok=False, якщо на сторінці є рядки N.M,
    які не вдалося розібрати (тоді графік публікувати не можна)."""
    text = first_list(normalize(text))
    rows = {}
    for m in ROW_RE.finditer(text):
        key = f"{m.group(1)}.{m.group(2)}"
        if key in rows:        # мітка повторилась: далі йде інший список
            break
        rows[key] = sorted(
            [to_min(a), to_min(b)] for a, b in SLOT_RE.findall(m.group(3)))
    labels = {f"{q}.{m}" for q, m in LABEL_RE.findall(text)}
    return rows, labels <= set(rows)


# ---------------- Telegram-канал Черкасиобленерго ----------------
TG_DATE_RE = re.compile(
    rf"(\d{{1,2}}\s+(?:{'|'.join(MONTHS)}))\s+з\s+\d{{1,2}}\s*:\s*\d{{2}}", re.I)


def fetch_tg():
    """Останні пости каналу з веб-перегляду t.me/s/<канал> (потрібен відкритий перегляд)."""
    soup = get_soup(TG_URL)
    msgs = soup.select("div.tgme_widget_message[data-post]")
    if not msgs:
        raise RuntimeError("на сторінці Telegram немає повідомлень "
                           "(змінилася розмітка або веб-перегляд каналу вимкнено)")
    out = []
    for w in msgs:
        node = w.select_one("div.tgme_widget_message_text")
        if node is None:                          # фото/відео без тексту
            continue
        when = None
        t = w.select_one("a.tgme_widget_message_date time")
        if t is not None and t.get("datetime"):
            try:
                when = datetime.fromisoformat(t["datetime"])
            except ValueError:
                pass
        post = w["data-post"]
        out.append({"key": f"tg:{post}", "url": f"https://t.me/{post}",
                    "text": node.get_text("\n"), "time": when})
    return out


def tg_post(m, now):
    """Пост Telegram -> пост для обробки; None, якщо це не графік ГПВ (наприклад, ГОП)."""
    head = m["text"][:500]
    if not TITLE_RE.search(head):
        return None
    if not ROW_RE.search(normalize(m["text"])):   # немає рядків N.M: інформаційний пост
        return None
    d = DATE_RE.search(head) or TG_DATE_RE.search(head)
    if not d:
        log(f"[telegram] {m['url']}: це ГПВ, але дату не знайдено, пропускаю")
        return None
    rows, ok = parse_text_checked(m["text"])
    first = m["text"].strip().split("\n")[0][:120]
    return {"src": "telegram", "key": m["key"], "url": m["url"], "title": first,
            "date": d.group(1).lower(), "rows": rows, "ok": ok,
            "time": m["time"] or now}


# ---------------- порівняння ----------------
def period(start):
    return ("ранковий" if start < 12 * 60
            else "денний" if start < 16 * 60
            else "вечірній")


def hours_phrase(d):
    """d — хвилини: 60 -> 'одну годину', 120 -> '2 години', 300 -> '5 годин'."""
    if d % 60:
        return f"{d} хвилин"
    h = d // 60
    if h == 1:
        return "одну годину"
    if h % 10 == 1 and h != 11:
        return f"{h} годину"
    if h % 10 in (2, 3, 4) and h not in (12, 13, 14):
        return f"{h} години"
    return f"{h} годин"


def describe_change(r, a):
    p = period(a[0])
    dr, da = r[1] - r[0], a[1] - a[0]
    tail = f"(триватиме з {fmt(a[0])} до {fmt(a[1])} замість {span(*r)})"
    if (a[0] == r[0] or a[1] == r[1]) and da != dr:   # змінилася одна межа
        verb = "продовжено" if da > dr else "скорочено"
        return f"{p} інтервал {verb} на {hours_phrase(abs(da - dr))} {tail}"
    return f"{p} інтервал змінено {tail}"             # зсув або зміна обох меж


def diff_queue(old, new, ds, now):
    old = [tuple(x) for x in old]
    new = [tuple(x) for x in new]
    removed = [x for x in old if x not in new]
    added = [x for x in new if x not in old]
    notes = []

    def passed(*ends):                    # чи вже минув час зміни
        return ds + timedelta(minutes=max(ends)) <= now

    for r in removed[:]:                  # інтервали, що перекриваються = «зміна»
        m = next((a for a in added if a[0] < r[1] and r[0] < a[1]), None)
        if m:
            removed.remove(r)
            added.remove(m)
            if not passed(r[1], m[1]):
                notes.append(describe_change(r, m))
    for r in removed:
        if not passed(r[1]):
            notes.append(f"скасовано вимкнення з {fmt(r[0])} до {fmt(r[1])}")
    for a in added:
        if not passed(a[1]):
            notes.append(f"додано вимкнення з {fmt(a[0])} до {fmt(a[1])}")
    return notes


# ---------------- повідомлення ----------------
def list_lines(rows):
    out = []
    for q in range(1, 7):
        sl = rows.get(f"{q}.2")
        if sl:                            # черг без даних не виводимо
            out += [f"<b>{q} черга:</b>",
                    "🚫 " + ", ".join(span(a, b) for a, b in sl), INV]
    return out


def src_lines(url):
    if not SHOW_SOURCE:
        return []
    return [f'<a href="{url}">Джерело: Черкасиобленерго</a>']


def build_full(item, date, rows):
    head = f"<b>⚡ Графік погодинних вимкнень електроенергії на {date}</b>"
    parts = [head, INV] + list_lines(rows)
    if SHOW_SOURCE:
        parts += src_lines(item["url"]) + [INV]
    return "\n".join(parts + FOOTER)


def build_update(item, date, old, new, now=None):
    now = now or datetime.now(TZ)
    ds = day_start(date, now)
    changes = []
    for q in range(1, 7):
        k = f"{q}.2"
        notes = diff_queue(old.get(k, []), new.get(k, []), ds, now)
        if notes:
            changes.append(f"✅ <b>{q} черга:</b> " + ", ".join(notes) + ".")
    if not changes:
        return None
    head = (f"<b>❗️ Зміни до графіка погодинних вимкнень електроенергії "
            f"на {date}</b>")
    sub = (f"<b>Що змінилося порівняно з попереднім графіком "
           f"(актуальні зміни станом на {now:%H:%M}):</b>")
    body = f"\n{INV}\n".join(changes)
    parts = [head, INV] + list_lines(new) + [sub, INV, body, INV]
    if SHOW_SOURCE:
        parts += src_lines(item["url"]) + [INV]
    return "\n".join(parts + FOOTER)


# ---------------- Telegram ----------------
def send(text):
    if DRY_RUN:
        print(text)
        print("\n" + "=" * 60 + "\n")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT, "text": text, "parse_mode": "HTML",
            "link_preview_options": json.dumps({"is_disabled": True})}
    for attempt in (1, 2, 3):
        try:
            r = requests.post(url, data=data, timeout=30)
        except requests.exceptions.ConnectionError as e:   # збій з'єднання (повідомлення не доставлено)
            if attempt == 3:
                raise
            log(f"Telegram: помилка з'єднання ({e.__class__.__name__}), спроба {attempt}/3, повтор")
            time.sleep(3 * attempt)
            continue
        if attempt < 3 and (r.status_code == 429 or r.status_code >= 500):
            try:
                wait = int(r.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                wait = 5
            log(f"Telegram: відповідь {r.status_code}, спроба {attempt}/3, повтор через {min(wait, 30)} с")
            time.sleep(min(wait, 30))
            continue
        if not r.ok:
            print("Помилка Telegram:", r.status_code, r.text)
            r.raise_for_status()
        return


# ---------------- головна логіка ----------------
def process(st, p, now):
    """Один пост (із сайту або Telegram): опублікувати повний графік чи зміни."""
    date, rows, when = p["date"], p["rows"], p["time"]
    lag = max(0, int((now - when).total_seconds() // 60))
    tag = f"[{p['src']}] {p['title']} ({date}), пост від {when.astimezone(TZ):%d.%m %H:%M}, виявлено через {lag} хв"
    if len(rows) < MIN_ROWS or not p["ok"]:
        if p["src"] == "site" and not SEED:       # формат змінився: не мовчимо
            send(f"⚡ <b>{html.escape(p['title'])}</b>\n"
                 f"Не вдалося автоматично розібрати графік.\n"
                 f'<a href="{p["url"]}">Відкрити</a>')
            log(tag + " -> не розібрано, надіслано попередження")
        else:
            log(tag + " -> не розібрано, пропущено (сайт покриє)")
        return
    h = digest(rows)
    versions = st["versions"].setdefault(date, [])
    last = st["times"].get(date)
    if h in versions:
        log(tag + " -> ця версія вже оброблена, пропущено")
        return
    if last and when < datetime.fromisoformat(last) - timedelta(minutes=2):
        log(tag + " -> старіша за вже оброблену, пропущено")
        return
    old = st["schedules"].get(date)
    sent = False
    if not SEED:
        msg = (build_full(p, date, rows) if old is None
               else build_update(p, date, old, rows))
        if msg:
            send(msg)
            sent = True
    st["schedules"][date] = rows                  # нова база для порівнянь
    versions.append(h)
    del versions[:-KEEP_VERSIONS]
    st["times"][date] = when.astimezone(timezone.utc).isoformat()
    log(tag + (" -> опубліковано" if sent else
               " -> запам'ятовано" if SEED else " -> змін, які варто публікувати, немає"))


def main():
    if not DRY_RUN and not (TOKEN and CHAT):
        raise SystemExit("Задайте змінні середовища BOT_TOKEN і CHAT_ID")
    st = load_state()
    seen = st["seen"]
    now = datetime.now(TZ)
    posts, ok_sources, dirty = [], 0, False

    try:                                          # сайт (від старих до нових)
        for item in reversed(fetch_list()):
            if item["url"] in seen:
                continue
            rows, ok, when = parse_rows(item["url"])
            posts.append({"src": "site", "key": item["url"], "url": item["url"],
                          "title": item["title"],
                          "date": DATE_RE.search(item["title"]).group(1).lower(),
                          "rows": rows, "ok": ok, "time": when or now})
        ok_sources += 1
    except Exception as e:
        log(f"[сайт] недоступний: {e!r}")

    try:                                          # Telegram-канал
        tg = fetch_tg()
        ok_sources += 1
        if not st["tg_ready"] and not DRY_RUN:
            # перший запуск: лише запам'ятати наявні пости, щоб не публікувати старе
            seen.update(m["key"] for m in tg)
            st["tg_ready"] = dirty = True
            log(f"[telegram] перший запуск: запам'ятовано {len(tg)} постів, нічого не опубліковано")
        else:
            for m in tg:
                if m["key"] in seen:
                    continue
                p = tg_post(m, now)
                if p is None:
                    seen.add(m["key"])            # не графік ГПВ
                    dirty = True
                else:
                    posts.append(p)
    except Exception as e:
        log(f"[telegram] недоступний: {e!r}")

    if ok_sources == 0:
        raise SystemExit("Жодне з джерел не відповіло")

    posts.sort(key=lambda x: x["time"])           # від старих до нових
    for p in posts:
        process(st, p, now)
        seen.add(p["key"])
        if not DRY_RUN:
            save_state(st)
            dirty = False
    if dirty and not DRY_RUN:
        save_state(st)


if __name__ == "__main__":
    main()
