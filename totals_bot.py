# =====================================================================
#  TOTALS BOT — Telegram-бот для пошуку матчів з високою ймовірністю
#  тоталу голів (ТБ 1.5 / ТБ 2.5)
#
#  Джерело даних: "Free API Live Football Data" (RapidAPI, автор Smart API)
#  Безкоштовний план: обмежена к-сть запитів/день — скрипт сам стежить
#  за лічильником і зупиняється, щоб не перевищити ліміт.
# =====================================================================
#
#  Що робить бот:
#   1) Раз на CHECK_PREMATCH_EVERY_HOURS годин бере сьогоднішні матчі,
#      які ще не почались. Для кожної унікальної ліги один раз завантажує
#      турнірну таблицю (де вже є "голів забито - голів пропущено" за
#      сезон) і рахує середній тотал голів команди: (забито+пропущено)/
#      зіграно. Якщо в обох команд матчу середній тотал > порогу —
#      надсилає сповіщення (пре-матч сигнал).
#   2) Кожні CHECK_LIVE_EVERY_MINUTES хвилин перевіряє LIVE-матчі: якщо
#      хвилина 25–35, рахунок 0:0, а сумарна к-сть ударів (shots) висока
#      — надсилає сповіщення (live-сигнал).
#
# =====================================================================

import requests
import time
import json
import os
from datetime import datetime, date

# =====================================================================
#  1. НАЛАШТУВАННЯ
# =====================================================================

TELEGRAM_BOT_TOKEN = "8987703737:AAFB1b_t5SJy-ChhliZACjRi3dCbTguEfPA"
TELEGRAM_CHAT_ID = "6028507442"

# Ключ RapidAPI для "Free API Live Football Data"
RAPIDAPI_KEY = "a00a1680d9msh3298193c9e76b9ap166841jsn8848ae9991c0"

# =====================================================================
#  2. ПАРАМЕТРИ СТРАТЕГІЇ (можна змінювати під себе)
# =====================================================================

AVG_GOALS_THRESHOLD = 2.5      # поріг середньої к-сті голів для пре-матч сигналу
MIN_PLAYED_FOR_PREMATCH = 3    # мінімум зіграних матчів у команди, щоб довіряти статистиці

LIVE_MINUTE_FROM = 25          # від якої хвилини шукати live-сигнал
LIVE_MINUTE_TO = 35            # до якої хвилини
LIVE_TOTAL_SHOTS_THRESHOLD = 10   # сумарна к-сть ударів (обидві команди), що вважається "багато"

CHECK_LIVE_EVERY_MINUTES = 20     # як часто перевіряти live-матчі
CHECK_PREMATCH_EVERY_HOURS = 12   # як часто оновлювати пре-матч аналіз

MAX_REQUESTS_PER_DAY = 95         # запобіжник проти денного ліміту
MAX_LEAGUES_PER_PREMATCH_RUN = 15 # скільки різних ліг максимум перевіряти за один прогін

# =====================================================================
#  3. ТЕХНІЧНІ РЕЧІ
# =====================================================================

API_HOST = "free-api-live-football-data.p.rapidapi.com"
API_BASE = f"https://{API_HOST}"
HEADERS = {
    "x-rapidapi-key": RAPIDAPI_KEY,
    "x-rapidapi-host": API_HOST,
}

STATE_FILE = "bot_state.json"

# ---------------------------------------------------------------------
#  Стан бота (щоб не дублювати сповіщення і не перевищувати ліміт)
# ---------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "date": str(date.today()),
        "requests_used": 0,
        "notified_prematch_ids": [],
        "notified_live_ids": [],
    }

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def reset_state_if_new_day(state):
    today = str(date.today())
    if state.get("date") != today:
        print(f"[INFO] Новий день ({today}) — скидаю лічильник запитів і список сповіщень.")
        state["date"] = today
        state["requests_used"] = 0
        state["notified_prematch_ids"] = []
        state["notified_live_ids"] = []
    return state

# ---------------------------------------------------------------------
#  Запити до API з лічильником
# ---------------------------------------------------------------------

def api_get(state, endpoint, params=None):
    if state["requests_used"] >= MAX_REQUESTS_PER_DAY:
        print("[WARN] Досягнуто денний ліміт запитів — пропускаю запит.")
        return None
    try:
        resp = requests.get(f"{API_BASE}{endpoint}", headers=HEADERS, params=params, timeout=15)
        state["requests_used"] += 1
        save_state(state)
        if resp.status_code != 200:
            print(f"[ERROR] API повернув статус {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        if data.get("status") != "success":
            print(f"[WARN] API відповів без success: {str(data)[:200]}")
            return None
        return data.get("response")
    except Exception as e:
        print(f"[ERROR] Помилка запиту до API: {e}")
        return None

# ---------------------------------------------------------------------
#  Telegram
# ---------------------------------------------------------------------

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] Telegram не прийняв повідомлення: {r.text[:200]}")
    except Exception as e:
        print(f"[ERROR] Не вдалося надіслати повідомлення в Telegram: {e}")

# ---------------------------------------------------------------------
#  ЛОГІКА 1: Пре-матч аналіз через турнірну таблицю ліги
# ---------------------------------------------------------------------

def get_matches_by_date(state, yyyymmdd):
    resp = api_get(state, "/football-get-matches-by-date", params={"date": yyyymmdd})
    if not resp:
        return []
    return resp.get("matches", [])

def get_league_table(state, league_id):
    """Повертає {team_id: {"name":..., "avg_goals":..., "played":...}, ...} для ліги."""
    resp = api_get(state, "/football-get-list-all-team", params={"leagueid": league_id})
    if not resp:
        return {}

    table = {}
    for team in resp.get("list", []):
        played = team.get("played") or 0
        scores_str = team.get("scoresStr")  # напр. "10-1" (забито-пропущено)
        if not played or not scores_str or "-" not in scores_str:
            continue
        try:
            scored_str, conceded_str = scores_str.split("-")
            scored = int(scored_str.strip())
            conceded = int(conceded_str.strip())
        except ValueError:
            continue

        if played < MIN_PLAYED_FOR_PREMATCH:
            continue

        avg_goals = (scored + conceded) / played
        table[team.get("id")] = {
            "name": team.get("name"),
            "avg_goals": avg_goals,
            "played": played,
        }
    return table

def check_prematch_signals(state):
    print("[INFO] Запускаю пре-матч аналіз сьогоднішніх матчів...")
    today_str = date.today().strftime("%Y%m%d")
    matches = get_matches_by_date(state, today_str)
    if not matches:
        print("[WARN] Не вдалося отримати сьогоднішні матчі.")
        return

    # Залишаємо тільки матчі, які ще не почались і не скасовані
    upcoming = []
    for m in matches:
        status = m.get("status", {})
        if status.get("started") is False and status.get("cancelled") is False:
            upcoming.append(m)

    print(f"[INFO] Знайдено {len(upcoming)} матчів, що ще не почались (з {len(matches)} всього).")

    # Групуємо за лігою, щоб один запит покривав усі матчі цієї ліги
    leagues_needed = {}
    for m in upcoming:
        leagues_needed.setdefault(m.get("leagueId"), []).append(m)

    league_ids = list(leagues_needed.keys())[:MAX_LEAGUES_PER_PREMATCH_RUN]

    for league_id in league_ids:
        table = get_league_table(state, league_id)
        if not table:
            continue

        for m in leagues_needed[league_id]:
            fixture_id = m.get("id")
            if fixture_id in state["notified_prematch_ids"]:
                continue

            home = m.get("home", {})
            away = m.get("away", {})
            home_stats = table.get(home.get("id"))
            away_stats = table.get(away.get("id"))

            if not home_stats or not away_stats:
                continue

            if home_stats["avg_goals"] > AVG_GOALS_THRESHOLD and away_stats["avg_goals"] > AVG_GOALS_THRESHOLD:
                text = (
                    f"⚽️ <b>Пре-матч сигнал (тотал голів)</b>\n\n"
                    f"🆚 {home.get('name')} — {away.get('name')}\n"
                    f"🕒 Початок: {m.get('time')}\n\n"
                    f"📊 Середній тотал за сезон:\n"
                    f"   {home.get('name')}: {home_stats['avg_goals']:.2f} ({home_stats['played']} матчів)\n"
                    f"   {away.get('name')}: {away_stats['avg_goals']:.2f} ({away_stats['played']} матчів)\n\n"
                    f"✅ Рекомендація: Ставка ТБ 2.5"
                )
                send_telegram_message(text)
                state["notified_prematch_ids"].append(fixture_id)
                save_state(state)
                print(f"[INFO] Надіслано пре-матч сигнал: {home.get('name')} — {away.get('name')}")

# ---------------------------------------------------------------------
#  ЛОГІКА 2: Live-аналіз (0:0 на 25–35 хв. і багато ударів)
# ---------------------------------------------------------------------

def get_live_matches(state):
    resp = api_get(state, "/football-current-live")
    if not resp:
        return []
    return resp.get("live", [])

def parse_minute(live_time_short):
    """Перетворює "25'" або "45+2'" на ціле число хвилин, або None."""
    if not live_time_short:
        return None
    text = live_time_short.replace("'", "").strip()
    if "+" in text:
        text = text.split("+")[0]
    try:
        return int(text)
    except ValueError:
        return None

def get_fixture_total_shots(state, event_id):
    """Шукає показник 'Total shots' у статистиці матчу, повертає (home_shots, away_shots) або None."""
    resp = api_get(state, "/football-get-match-event-all-stats", params={"eventid": event_id})
    if not resp:
        return None

    for category in resp.get("stats", []):
        for stat in category.get("stats", []):
            if stat.get("key") == "total_shots" and stat.get("title") == "Total shots":
                values = stat.get("stats", [])
                if len(values) == 2 and values[0] is not None and values[1] is not None:
                    try:
                        return int(values[0]), int(values[1])
                    except (ValueError, TypeError):
                        return None
    return None

def check_live_signals(state):
    print("[INFO] Перевіряю live-матчі...")
    live_matches = get_live_matches(state)
    print(f"[INFO] Зараз {len(live_matches)} live-матчів.")

    for match in live_matches:
        fixture_id = match.get("id")
        if fixture_id in state["notified_live_ids"]:
            continue

        home = match.get("home", {})
        away = match.get("away", {})
        home_score = home.get("score")
        away_score = away.get("score")

        status = match.get("status", {})
        live_time = status.get("liveTime", {})
        minute = parse_minute(live_time.get("short"))

        if minute is None or home_score is None or away_score is None:
            continue
        if not (LIVE_MINUTE_FROM <= minute <= LIVE_MINUTE_TO):
            continue
        if not (home_score == 0 and away_score == 0):
            continue

        shots = get_fixture_total_shots(state, fixture_id)
        if not shots:
            continue
        total_shots = shots[0] + shots[1]
        if total_shots < LIVE_TOTAL_SHOTS_THRESHOLD:
            continue

        text = (
            f"🔴 <b>LIVE сигнал (тотал голів)</b>\n\n"
            f"🆚 {home.get('name')} — {away.get('name')}\n"
            f"⏱ Хвилина: {minute}'\n"
            f"⚽️ Рахунок: {home_score}:{away_score}\n\n"
            f"📊 Сумарна к-сть ударів: {total_shots} ({shots[0]}:{shots[1]})\n\n"
            f"✅ Рекомендація: Ставка ТБ 1.5 або ТБ 0.5 (найближчі 15-20 хв)"
        )
        send_telegram_message(text)
        state["notified_live_ids"].append(fixture_id)
        save_state(state)
        print(f"[INFO] Надіслано live-сигнал: {home.get('name')} — {away.get('name')}")

# ---------------------------------------------------------------------
#  ГОЛОВНИЙ ЦИКЛ
# ---------------------------------------------------------------------

def main():
    print("[INFO] Бот запущено.")
    send_telegram_message("🤖 Бот аналізу тоталів запущено і почав роботу.")

    state = load_state()
    state = reset_state_if_new_day(state)
    save_state(state)

    last_prematch_run = None

    while True:
        try:
            state = reset_state_if_new_day(state)
            now = datetime.now()

            if (last_prematch_run is None or
                    (now - last_prematch_run).total_seconds() >= CHECK_PREMATCH_EVERY_HOURS * 3600):
                check_prematch_signals(state)
                last_prematch_run = now

            check_live_signals(state)

        except Exception as e:
            print(f"[ERROR] Несподівана помилка в головному циклі: {e}")

        print(f"[INFO] Використано запитів сьогодні: {state['requests_used']}/{MAX_REQUESTS_PER_DAY}. "
              f"Чекаю {CHECK_LIVE_EVERY_MINUTES} хв...")
        time.sleep(CHECK_LIVE_EVERY_MINUTES * 60)

if __name__ == "__main__":
    # Якщо ви на Replit — розкоментуйте ці 2 рядки, щоб бот не "засинав".
    # Для Render.com це НЕ потрібно.
    # from keep_alive import keep_alive
    # keep_alive()

    main()
