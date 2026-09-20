# =====================================================================
#  TOTALS BOT — Telegram-бот для пошуку матчів з високою ймовірністю
#  тоталу голів (ТБ 1.5 / ТБ 2.5)
#
#  Джерело даних: API-FOOTBALL (офіційно, напряму через api-football.com,
#  БЕЗ RapidAPI). Безкоштовний план: 100 запитів/день, ліміт скидається
#  о 00:00 UTC. Скрипт сам стежить за лічильником, щоб не перевищити його.
# =====================================================================
#
#  ВАЖЛИВО ПРО ЛІМІТ: 100 запитів/день — це небагато, тому:
#   - Live-матчі перевіряються НЕ надто часто (за замовчуванням раз на
#     30 хвилин), щоб залишити запас на все інше.
#   - Статистика конкретного матчу (удари в створ тощо) запитується
#     ЛИШЕ для матчів, які вже підходять за хвилиною і рахунком —
#     тобто це рідкісні, "точкові" запити, а не масові.
#   - Пре-матч аналіз запускається раз на день і кешує вже перевірені
#     команди, щоб не питати статистику однієї й тієї ж команди двічі.
#
#  Стратегія (сильні сигнали — тобто зменшена кількість, але надійніші):
#   1) ПРЕ-МАТЧ: команда вважається "результативною", якщо середня
#      кількість голів за матч (забито+пропущено) > AVG_GOALS_THRESHOLD.
#      Сигнал надсилається, тільки якщо ОБИДВІ команди матчу відповідають
#      цій умові, І сума їх середніх ≥ COMBINED_AVG_GOALS_THRESHOLD.
#   2) LIVE: рахунок 0:0, хвилина в діапазоні LIVE_MINUTE_FROM..TO.
#      Дивимось на удари в створ і загальну кількість ударів та кутові —
#      сигнал надсилається, тільки якщо мінімум LIVE_MIN_CRITERIA_MATCHED
#      з 3 умов виконались одночасно.
# =====================================================================

import requests
import time
import json
import os
from datetime import datetime, date

# =====================================================================
#  1. НАЛАШТУВАННЯ
# =====================================================================

TELEGRAM_BOT_TOKEN = "8972182844:AAHevRhJ9TQP3Ujw-nzoJ1wINqBK0QFcdZI"
TELEGRAM_CHAT_ID = "6028507442"

# Ключ з api-football.com (Account -> My Access)
APIFOOTBALL_KEY = "b45a7e58d179e26b5da36615e79a8559"

# =====================================================================
#  2. ПАРАМЕТРИ СТРАТЕГІЇ
# =====================================================================

AVG_GOALS_THRESHOLD = 2.5           # поріг середньої к-сті голів КОЖНОЇ з команд
COMBINED_AVG_GOALS_THRESHOLD = 6.0  # І сума середніх обох команд має бути не нижче цього
MIN_FIXTURES_FOR_PREMATCH = 5       # мінімум зіграних матчів команди в сезоні, щоб довіряти статистиці

LIVE_MINUTE_FROM = 20
LIVE_MINUTE_TO = 40

LIVE_SHOTS_ON_TARGET_THRESHOLD = 5   # сумарно ударів у створ ворін
LIVE_TOTAL_SHOTS_THRESHOLD = 14      # сумарно всіх ударів (вищий поріг, бо менш якісний показник)
LIVE_CORNERS_THRESHOLD = 8           # сумарно кутових ударів
LIVE_MIN_CRITERIA_MATCHED = 2        # скільки з 3 умов має спрацювати одночасно

CHECK_LIVE_EVERY_MINUTES = 30        # частота перевірки live-матчів (бережемо ліміт запитів)
CHECK_PREMATCH_ONCE_PER_DAY = True   # пре-матч аналіз — 1 раз на день

MAX_REQUESTS_PER_DAY = 90            # запобіжник (справжній ліміт 100, лишаємо запас)
MAX_TEAM_STATS_LOOKUPS_PER_DAY = 40  # скільки запитів на статистику команд максимум за день
MAX_FIXTURES_TO_SCAN_PREMATCH = 20   # скільки сьогоднішніх матчів максимум розглядати

# =====================================================================
#  3. ТЕХНІЧНІ РЕЧІ
# =====================================================================

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": APIFOOTBALL_KEY}

STATE_FILE = "bot_state.json"

# ---------------------------------------------------------------------
#  Стан бота
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
        "team_stats_lookups_used": 0,
        "notified_prematch_ids": [],
        "notified_live_ids": [],
        "prematch_done_today": False,
        # кеш середніх голів команди за сезон: "team_id:league_id:season" -> avg_goals
        "team_avg_cache": {},
    }

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def reset_state_if_new_day(state):
    today = str(date.today())
    if state.get("date") != today:
        print(f"[INFO] Новий день ({today}) — скидаю лічильники і кеш.")
        state["date"] = today
        state["requests_used"] = 0
        state["team_stats_lookups_used"] = 0
        state["notified_prematch_ids"] = []
        state["notified_live_ids"] = []
        state["prematch_done_today"] = False
        state["team_avg_cache"] = {}
    return state

# ---------------------------------------------------------------------
#  Запити до API-FOOTBALL з лічильником
# ---------------------------------------------------------------------

def api_get(state, endpoint, params=None):
    if state["requests_used"] >= MAX_REQUESTS_PER_DAY:
        print("[WARN] Досягнуто денний ліміт запитів — пропускаю запит.")
        return None
    try:
        resp = requests.get(f"{API_BASE}{endpoint}", headers=HEADERS, params=params, timeout=15)
        state["requests_used"] += 1
        save_state(state)

        if resp.status_code == 429:
            print("[WARN] API-FOOTBALL: 429 — ліміт запитів вичерпано на сьогодні.")
            state["requests_used"] = MAX_REQUESTS_PER_DAY  # більше не пробуємо сьогодні
            save_state(state)
            return None
        if resp.status_code != 200:
            print(f"[ERROR] API повернув статус {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        if data.get("errors"):
            print(f"[WARN] API-FOOTBALL повернув помилку: {data['errors']}")
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
            return False
        return True
    except Exception as e:
        print(f"[ERROR] Не вдалося надіслати повідомлення в Telegram: {e}")
        return False

# ---------------------------------------------------------------------
#  ЛОГІКА 1: Пре-матч аналіз
# ---------------------------------------------------------------------

def guess_season_for_date(d):
    """Європейський сезон зазвичай позначається роком його старту (напр. 2026 для сезону 2026/27)."""
    return d.year if d.month >= 7 else d.year - 1

def get_team_avg_goals(state, team_id, league_id, season):
    """Повертає середню к-сть голів (забито+пропущено) команди за сезон, з кешем на день."""
    cache_key = f"{team_id}:{league_id}:{season}"
    if cache_key in state["team_avg_cache"]:
        return state["team_avg_cache"][cache_key]

    if state["team_stats_lookups_used"] >= MAX_TEAM_STATS_LOOKUPS_PER_DAY:
        return None

    resp = api_get(state, "/teams/statistics", params={
        "team": team_id, "league": league_id, "season": season
    })
    state["team_stats_lookups_used"] += 1
    save_state(state)

    if not resp:
        return None

    goals = resp.get("goals", {})
    try:
        avg_for = float(goals.get("for", {}).get("average", {}).get("total", 0) or 0)
        avg_against = float(goals.get("against", {}).get("average", {}).get("total", 0) or 0)
        played = resp.get("fixtures", {}).get("played", {}).get("total", 0) or 0
    except (ValueError, TypeError):
        return None

    if played < MIN_FIXTURES_FOR_PREMATCH:
        return None

    avg_total = avg_for + avg_against
    state["team_avg_cache"][cache_key] = avg_total
    save_state(state)
    return avg_total

def check_prematch_signals(state):
    print("[INFO] Запускаю пре-матч аналіз сьогоднішніх матчів...")
    today_str = date.today().strftime("%Y-%m-%d")
    matches = api_get(state, "/fixtures", params={"date": today_str})
    if not matches:
        print("[WARN] Не вдалося отримати сьогоднішні матчі.")
        return

    upcoming = [m for m in matches if m.get("fixture", {}).get("status", {}).get("short") == "NS"]
    upcoming = upcoming[:MAX_FIXTURES_TO_SCAN_PREMATCH]
    print(f"[INFO] Знайдено {len(upcoming)} матчів для пре-матч перевірки (з {len(matches)} всього).")

    for m in upcoming:
        fixture_id = m["fixture"]["id"]
        if fixture_id in state["notified_prematch_ids"]:
            continue

        league = m.get("league", {})
        league_id = league.get("id")
        season = league.get("season")
        home = m["teams"]["home"]
        away = m["teams"]["away"]

        if not league_id or not season:
            continue

        home_avg = get_team_avg_goals(state, home["id"], league_id, season)
        away_avg = get_team_avg_goals(state, away["id"], league_id, season)

        if home_avg is None or away_avg is None:
            continue

        both_above = home_avg > AVG_GOALS_THRESHOLD and away_avg > AVG_GOALS_THRESHOLD
        combined = home_avg + away_avg
        combined_above = combined >= COMBINED_AVG_GOALS_THRESHOLD

        if both_above and combined_above:
            text = (
                f"⚽️ <b>Пре-матч сигнал (тотал голів)</b>\n\n"
                f"🏆 {league.get('name')}\n"
                f"🆚 {home['name']} — {away['name']}\n"
                f"🕒 Початок: {m['fixture']['date']}\n\n"
                f"📊 Середній тотал за сезон:\n"
                f"   {home['name']}: {home_avg:.2f}\n"
                f"   {away['name']}: {away_avg:.2f}\n"
                f"   Сума середніх: {combined:.2f} (поріг {COMBINED_AVG_GOALS_THRESHOLD})\n\n"
                f"✅ Рекомендація: Ставка ТБ 2.5"
            )
            if send_telegram_message(text):
                state["notified_prematch_ids"].append(fixture_id)
                save_state(state)
                print(f"[INFO] Надіслано пре-матч сигнал: {home['name']} — {away['name']}")

# ---------------------------------------------------------------------
#  ЛОГІКА 2: Live-аналіз
# ---------------------------------------------------------------------

def get_live_matches(state):
    resp = api_get(state, "/fixtures", params={"live": "all"})
    return resp or []

def extract_stat(stats_response, stat_type):
    """stats_response — це response з /fixtures/statistics (список по 2 команди)."""
    values = []
    for team_block in stats_response:
        for stat in team_block.get("statistics", []):
            if stat.get("type") == stat_type:
                v = stat.get("value")
                try:
                    values.append(int(v) if v is not None else 0)
                except (ValueError, TypeError):
                    values.append(0)
    return values if len(values) == 2 else None

def check_live_signals(state):
    print("[INFO] Перевіряю live-матчі...")
    live_matches = get_live_matches(state)
    print(f"[INFO] Зараз {len(live_matches)} live-матчів.")

    for m in live_matches:
        fixture_id = m["fixture"]["id"]
        if fixture_id in state["notified_live_ids"]:
            continue

        minute = m["fixture"]["status"].get("elapsed")
        goals = m.get("goals", {})
        home_score = goals.get("home")
        away_score = goals.get("away")

        if minute is None or home_score is None or away_score is None:
            continue
        if not (LIVE_MINUTE_FROM <= minute <= LIVE_MINUTE_TO):
            continue
        if not (home_score == 0 and away_score == 0):
            continue

        stats_resp = api_get(state, "/fixtures/statistics", params={"fixture": fixture_id})
        if not stats_resp:
            continue

        sot = extract_stat(stats_resp, "Shots on Goal")
        total_shots = extract_stat(stats_resp, "Total Shots")
        corners = extract_stat(stats_resp, "Corner Kicks")

        triggered = []
        if sot and sum(sot) >= LIVE_SHOTS_ON_TARGET_THRESHOLD:
            triggered.append(f"удари в створ {sum(sot)} ≥ {LIVE_SHOTS_ON_TARGET_THRESHOLD}")
        if total_shots and sum(total_shots) >= LIVE_TOTAL_SHOTS_THRESHOLD:
            triggered.append(f"всього ударів {sum(total_shots)} ≥ {LIVE_TOTAL_SHOTS_THRESHOLD}")
        if corners and sum(corners) >= LIVE_CORNERS_THRESHOLD:
            triggered.append(f"кутових {sum(corners)} ≥ {LIVE_CORNERS_THRESHOLD}")

        if len(triggered) < LIVE_MIN_CRITERIA_MATCHED:
            continue

        home = m["teams"]["home"]["name"]
        away = m["teams"]["away"]["name"]
        league_name = m.get("league", {}).get("name", "")

        stats_lines = []
        if sot:
            stats_lines.append(f"   Удари в створ: {sot[0]}:{sot[1]}")
        if total_shots:
            stats_lines.append(f"   Всього ударів: {total_shots[0]}:{total_shots[1]}")
        if corners:
            stats_lines.append(f"   Кутові: {corners[0]}:{corners[1]}")

        text = (
            f"🔴 <b>СИЛЬНИЙ LIVE сигнал (тотал голів)</b>\n\n"
            f"🏆 {league_name}\n"
            f"🆚 {home} — {away}\n"
            f"⏱ Хвилина: {minute}'\n"
            f"⚽️ Рахунок: {home_score}:{away_score}\n\n"
            f"📊 Статистика:\n" + "\n".join(stats_lines) + "\n\n"
            f"✅ Виконано {len(triggered)}/3 критеріїв: {', '.join(triggered)}\n"
            f"✅ Рекомендація: Ставка ТБ 1.5 або ТБ 0.5"
        )
        if send_telegram_message(text):
            state["notified_live_ids"].append(fixture_id)
            save_state(state)
            print(f"[INFO] Надіслано live-сигнал: {home} — {away}")

# ---------------------------------------------------------------------
#  ГОЛОВНИЙ ЦИКЛ
# ---------------------------------------------------------------------

def main():
    print("[INFO] Бот запущено.")
    send_telegram_message("🤖 Бот аналізу тоталів запущено і почав роботу.")

    state = load_state()
    state = reset_state_if_new_day(state)
    save_state(state)

    while True:
        try:
            state = reset_state_if_new_day(state)

            if not state["prematch_done_today"] and CHECK_PREMATCH_ONCE_PER_DAY:
                check_prematch_signals(state)
                state["prematch_done_today"] = True
                save_state(state)

            if state["requests_used"] < MAX_REQUESTS_PER_DAY:
                check_live_signals(state)
            else:
                print("[INFO] Ліміт запитів на сьогодні вичерпано — чекаю до завтра.")

        except Exception as e:
            print(f"[ERROR] Несподівана помилка в головному циклі: {e}")

        print(f"[INFO] Використано запитів сьогодні: {state['requests_used']}/{MAX_REQUESTS_PER_DAY}. "
              f"Чекаю {CHECK_LIVE_EVERY_MINUTES} хв...")
        time.sleep(CHECK_LIVE_EVERY_MINUTES * 60)

if __name__ == "__main__":
    main()
