#!/usr/bin/env python3
"""Публікація графіків ГПВ (підчерги N.2) із сайту Черкасиобленерго в Telegram-канал.

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
import requests
from datetime import datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

# ---------------- налаштування ----------------
LIST_URL = "https://www.cherkasyoblenergo.com/media?category=news&lang=uk"
TOKEN = os.environ.get("BOT_TOKEN")
CHAT = os.environ.get("CHAT_ID")
SEED = os.environ.get("SEED") == "1"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (outage-bot)"}
TZ = ZoneInfo("Europe/Kyiv")

SHOW_SOURCE = False          # True = додавати рядок «Джерело: Черкасиобленерго»
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
def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if isinstance(d, list):                       # старий формат
        d = {"seen": d}
    return set(d.get("seen", [])), d.get("schedules", {})


def save_state(seen, scheds):
    while len(scheds) > 10:                       # лишаємо останні дати
        scheds.pop(next(iter(scheds)))
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"seen": sorted(seen), "schedules": scheds},
                  f, ensure_ascii=False)


# ---------------- сайт ----------------
def get_soup(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


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


def parse_rows(url):
    """{'1.2': [[960, 1080], [1320, 1440]], ...} для всіх знайдених N.M"""
    text = get_soup(url).get_text("\n")
    return parse_text_checked(text)


GLUE_RE = re.compile(r"(?<![\d.])(\d)\s+(\d)\s*:\s*(\d{2})")
LABEL_RE = re.compile(r"(?<![\d.])([1-6])\.([12])(?=\s+\d)")


def normalize(text):
    """Склеює розірваний час: '0 0 :00' -> '00:00', '10 :00' -> '10:00'."""
    text = text.replace("\xa0", " ")
    return GLUE_RE.sub(r"\1\2:\3", text)


def parse_text(text):
    rows, _ = parse_text_checked(text)
    return rows


def parse_text_checked(text):
    """Повертає (rows, ok). ok=False, якщо на сторінці є рядки N.M,
    які не вдалося розібрати (тоді графік публікувати не можна)."""
    text = normalize(text)
    s = text.find("Години відсутності")
    if s != -1:
        text = text[s:]
    e = text.find("Свою чергу")
    if e != -1:
        text = text[:e]
    rows = {}
    for q, sub, times in ROW_RE.findall(text):
        rows[f"{q}.{sub}"] = sorted(
            [to_min(a), to_min(b)] for a, b in SLOT_RE.findall(times))
    labels = {f"{q}.{m}" for q, m in LABEL_RE.findall(text)}
    return rows, labels <= set(rows)


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
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text, "parse_mode": "HTML",
              "link_preview_options": json.dumps({"is_disabled": True})},
        timeout=30)
    if not r.ok:
        print("Помилка Telegram:", r.status_code, r.text)
        r.raise_for_status()


# ---------------- головна логіка ----------------
def main():
    if not DRY_RUN and not (TOKEN and CHAT):
        raise SystemExit("Задайте змінні середовища BOT_TOKEN і CHAT_ID")
    seen, scheds = load_state()
    for item in reversed(fetch_list()):           # від старих до нових
        url = item["url"]
        if url in seen:
            continue
        date = DATE_RE.search(item["title"]).group(1).lower()
        rows, ok = parse_rows(url)
        if len(rows) < MIN_ROWS or not ok:        # формат змінився: не мовчимо
            if not SEED:
                send(f"⚡ <b>{html.escape(item['title'])}</b>\n"
                     f"Не вдалося автоматично розібрати графік.\n"
                     f'<a href="{url}">Відкрити</a>')
        else:
            old = scheds.get(date)
            if not SEED:
                msg = (build_full(item, date, rows) if old is None
                       else build_update(item, date, old, rows))
                if msg:
                    send(msg)
            scheds[date] = rows                   # нова база для порівнянь
        seen.add(url)
        if not DRY_RUN:
            save_state(seen, scheds)


if __name__ == "__main__":
    main()
