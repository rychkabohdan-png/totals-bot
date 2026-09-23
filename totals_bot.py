# =====================================================================
#  TOTALS BOT — Telegram-бот для щоденного пре-матч аналізу:
#  - Тотал голів > 2.5 (ставка ТБ 2.5)
#  - Найбільш ймовірний фаворит матчу
#
#  Джерело даних: football-data.org (офіційний, легальний, безкоштовний
#  API). Ліміт: 10 запитів/хвилину, БЕЗ місячних/денних квот — це
#  найстабільніший варіант з усіх, що ми пробували.
#
#  Покриття: топові ліги (АПЛ, Ла Ліга, Бундесліга, Серія А, Ліга 1,
#  Чемпіонат Португалії, Ліга чемпіонів, Чемпіоншип, ЧС, Єредівізі,
#  Бразильська Серія A, Євро) — усі ліги, доступні на безкоштовному плані.
#
#  ЯК ЦЕ ПРАЦЮЄ (раз на день, не постійний моніторинг):
#   1) Бере всі сьогоднішні матчі одним запитом.
#   2) Для кожної унікальної ліги, де сьогодні є матчі, один раз
#      завантажує турнірну таблицю (очки, зіграно, голи за/проти).
#   3) ТОТАЛ: рахує середній тотал голів команди (голи за+проти)/зіграно.
#      Сигнал — якщо ОБИДВІ команди матчу > порогу, І сума ≥ порогу суми.
#   4) ФАВОРИТ: порівнює очки-за-гру (points/played) обох команд.
#      Якщо різниця істотна — це орієнтовний фаворит (НЕ гарантія,
#      а орієнтир на основі статистики сезону).
# =====================================================================

import requests
import time
import json
import os
from datetime import date

# =====================================================================
#  1. НАЛАШТУВАННЯ
# =====================================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FOOTBALL_DATA_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "4340397fcd2a4a43bf4aa0545d8e1518")

# =====================================================================
#  2. ПАРАМЕТРИ СТРАТЕГІЇ
# =====================================================================

AVG_GOALS_THRESHOLD = 2.5           # поріг середньої к-сті голів КОЖНОЇ з команд
COMBINED_AVG_GOALS_THRESHOLD = 6.0  # І сума середніх обох команд має бути не нижче цього
MIN_PLAYED_FOR_SIGNAL = 5           # мінімум зіграних матчів у сезоні, щоб довіряти статистиці

FAVORITE_PPG_GAP = 0.7   # мінімальна різниця "очок за гру", щоб вважати команду фаворитом

CHECK_INTERVAL_HOURS = 24  # аналіз запускається раз на добу
REQUEST_DELAY_SECONDS = 6.5  # пауза між запитами (ліміт 10/хв на безкоштовному плані)

# =====================================================================
#  3. ТЕХНІЧНІ РЕЧІ
# =====================================================================

API_BASE = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": FOOTBALL_DATA_TOKEN}

STATE_FILE = "bot_state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_run_date": None, "notified_ids": []}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def api_get(endpoint, params=None):
    try:
        resp = requests.get(f"{API_BASE}{endpoint}", headers=HEADERS, params=params, timeout=15)
        if resp.status_code == 429:
            print("[WARN] football-data.org: 429 — забагато запитів за хвилину, чекаю...")
            time.sleep(65)
            return api_get(endpoint, params)
        if resp.status_code != 200:
            print(f"[ERROR] API повернув статус {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.json()
    except Exception as e:
        print(f"[ERROR] Помилка запиту до API: {e}")
        return None

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERROR] TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID не задані.")
        return False
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
#  Отримання турнірної таблиці конкретної ліги (з кешем на один прогін)
# ---------------------------------------------------------------------

def get_standings_table(competition_code, cache):
    if competition_code in cache:
        return cache[competition_code]

    time.sleep(REQUEST_DELAY_SECONDS)
    data = api_get(f"/competitions/{competition_code}/standings")
    if not data:
        cache[competition_code] = {}
        return {}

    table = {}
    for group in data.get("standings", []):
        if group.get("type") != "TOTAL":
            continue
        for row in group.get("table", []):
            played = row.get("playedGames", 0)
            if played < MIN_PLAYED_FOR_SIGNAL:
                continue
            goals_for = row.get("goalsFor", 0)
            goals_against = row.get("goalsAgainst", 0)
            points = row.get("points", 0)
            team_id = row.get("team", {}).get("id")
            table[team_id] = {
                "name": row.get("team", {}).get("name"),
                "avg_goals": (goals_for + goals_against) / played,
                "ppg": points / played,
                "played": played,
            }
    cache[competition_code] = table
    return table

# ---------------------------------------------------------------------
#  Головний аналіз: раз на день
# ---------------------------------------------------------------------

def run_daily_analysis(state):
    print("[INFO] Запускаю щоденний пре-матч аналіз...")
    today_str = date.today().strftime("%Y-%m-%d")

    matches_data = api_get("/matches", params={"dateFrom": today_str, "dateTo": today_str})
    if not matches_data:
        print("[WARN] Не вдалося отримати сьогоднішні матчі.")
        return

    matches = [m for m in matches_data.get("matches", []) if m.get("status") == "SCHEDULED" or m.get("status") == "TIMED"]
    print(f"[INFO] Знайдено {len(matches)} матчів на сьогодні (з {len(matches_data.get('matches', []))} всього).")

    standings_cache = {}
    signals_sent = 0

    for m in matches:
        match_id = m.get("id")
        if match_id in state["notified_ids"]:
            continue

        competition = m.get("competition", {})
        comp_code = competition.get("code")
        comp_name = competition.get("name")
        home = m.get("homeTeam", {})
        away = m.get("awayTeam", {})
        kickoff = m.get("utcDate", "")

        if not comp_code:
            continue

        table = get_standings_table(comp_code, standings_cache)
        if not table:
            continue

        home_stats = table.get(home.get("id"))
        away_stats = table.get(away.get("id"))
        if not home_stats or not away_stats:
            continue

        message_parts = []

        # --- Критерій 1: тотал голів ---
        both_above = (home_stats["avg_goals"] > AVG_GOALS_THRESHOLD and
                      away_stats["avg_goals"] > AVG_GOALS_THRESHOLD)
        combined = home_stats["avg_goals"] + away_stats["avg_goals"]
        combined_above = combined >= COMBINED_AVG_GOALS_THRESHOLD

        if both_above and combined_above:
            message_parts.append(
                f"⚽️ <b>Тотал голів</b>\n"
                f"   {home_stats['name']}: {home_stats['avg_goals']:.2f} середньо\n"
                f"   {away_stats['name']}: {away_stats['avg_goals']:.2f} середньо\n"
                f"   Сума: {combined:.2f}\n"
                f"   ✅ Рекомендація: Ставка ТБ 2.5"
            )

        # --- Критерій 2: фаворит ---
        ppg_diff = home_stats["ppg"] - away_stats["ppg"]
        if abs(ppg_diff) >= FAVORITE_PPG_GAP:
            favorite = home_stats["name"] if ppg_diff > 0 else away_stats["name"]
            message_parts.append(
                f"🏆 <b>Ймовірний фаворит: {favorite}</b>\n"
                f"   {home_stats['name']}: {home_stats['ppg']:.2f} очок/гру\n"
                f"   {away_stats['name']}: {away_stats['ppg']:.2f} очок/гру\n"
                f"   (орієнтир на основі статистики сезону, не гарантія)"
            )

        if not message_parts:
            continue

        text = (
            f"📅 <b>Пре-матч сигнал</b>\n\n"
            f"🏆 {comp_name}\n"
            f"🆚 {home.get('name')} — {away.get('name')}\n"
            f"🕒 {kickoff}\n\n" + "\n\n".join(message_parts)
        )

        if send_telegram_message(text):
            state["notified_ids"].append(match_id)
            save_state(state)
            signals_sent += 1
            print(f"[INFO] Надіслано сигнал: {home.get('name')} — {away.get('name')}")

    print(f"[INFO] Аналіз завершено. Надіслано сигналів: {signals_sent}.")

    if len(state["notified_ids"]) > 500:
        state["notified_ids"] = state["notified_ids"][-500:]
        save_state(state)

# ---------------------------------------------------------------------
#  ГОЛОВНИЙ ЦИКЛ
# ---------------------------------------------------------------------

def main():
    print("[INFO] Бот запущено.")
    send_telegram_message("🤖 Бот пре-матч аналізу (football-data.org) запущено і почав роботу.")

    state = load_state()

    while True:
        today_str = str(date.today())
        if state.get("last_run_date") != today_str:
            try:
                run_daily_analysis(state)
            except Exception as e:
                print(f"[ERROR] Несподівана помилка під час аналізу: {e}")
            state["last_run_date"] = today_str
            save_state(state)
        else:
            print("[INFO] Аналіз на сьогодні вже виконано.")

        print(f"[INFO] Чекаю {CHECK_INTERVAL_HOURS} год до наступної перевірки...")
        time.sleep(CHECK_INTERVAL_HOURS * 3600)

if __name__ == "__main__":
    main()
