"""
Бот-проверяльщик профилей Telegram.
Принимает юзернеймы (текстом или файлом .txt/.csv), оценивает профили
по набору признаков и возвращает только тех, у кого рейтинг >= порога.

Установка:  pip install telethon
Запуск:     python profile_checker_bot.py
"""
import asyncio
import re
import unicodedata
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    User, UserStatusOnline, UserStatusOffline, UserStatusRecently,
    UserStatusLastWeek, UserStatusLastMonth,
)

# ====== НАСТРОЙКИ ======
API_ID = 12345                    # https://my.telegram.org -> API development tools
API_HASH = "your_api_hash"
BOT_TOKEN = "123456:ABC..."       # токен от @BotFather
DEFAULT_MIN_SCORE = 70            # порог по умолчанию (меняется командой /min 80)
DELAY = 1.5                       # пауза между запросами (сек), защита от флуд-лимитов
MAX_USERNAMES = 300               # максимум юзернеймов за один запуск
# =======================

# Бот-интерфейс + пользовательская сессия: Bot API не умеет получать
# данные профиля произвольного пользователя по @username, MTProto — умеет.
user_client = TelegramClient("user_session", API_ID, API_HASH)
bot = TelegramClient("bot_session", API_ID, API_HASH)

min_score_by_chat: dict[int, int] = {}

# Примерная привязка ID аккаунта -> дата регистрации (приблизительно, можно уточнять)
ID_DATES = [
    (0, "2013-08"), (100_000_000, "2015-06"), (300_000_000, "2017-01"),
    (600_000_000, "2018-07"), (1_000_000_000, "2019-12"), (1_500_000_000, "2020-09"),
    (2_000_000_000, "2021-04"), (5_000_000_000, "2022-06"), (6_000_000_000, "2023-01"),
    (7_000_000_000, "2024-02"), (7_700_000_000, "2024-12"), (8_300_000_000, "2025-06"),
]


def estimate_account_age_years(user_id: int) -> float:
    date_str = ID_DATES[0][1]
    for threshold, d in ID_DATES:
        if user_id >= threshold:
            date_str = d
    created = datetime.strptime(date_str, "%Y-%m").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).days / 365


LEET = str.maketrans("", "", "")
LEET_RE = re.compile(r"[a-z]+[0-9@$]+[a-z]+|[a-z]*[430@$][a-z]+[1370][a-z]*", re.I)


def looks_random(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 4:
        return False
    scripts = {unicodedata.name(c, "").split()[0] for c in letters}
    if len(scripts) > 1:  # смесь алфавитов (latin + cyrillic и т.п.)
        return True
    vowels = sum(c.lower() in "aeiouyаеёиоуыэюя" for c in letters)
    return vowels / len(letters) < 0.2


def has_weird_symbols(text: str) -> bool:
    return any(
        unicodedata.category(c).startswith(("S", "C", "M")) and not c.isspace()
        for c in text
    )


async def analyze(username: str) -> dict:
    entity = await user_client.get_entity(username)
    if not isinstance(entity, User):
        return {"username": username, "error": "это не пользователь (канал/группа)"}
    if entity.bot:
        return {"username": username, "error": "это бот"}

    full = await user_client(GetFullUserRequest(entity))
    bio = (full.full_user.about or "").strip()
    photos = await user_client.get_profile_photos(entity, limit=5)
    name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()

    score, notes = 100, []

    def penalty(points, text):
        nonlocal score
        score -= points
        notes.append(f"-{points} {text}")

    # возраст аккаунта
    age = estimate_account_age_years(entity.id)
    if age < 1:
        penalty(15, f"аккаунт младше года (~{age:.1f} г.)")
    elif age < 2:
        penalty(8, f"аккаунт ~{age:.1f} г.")

    # аватарка
    if not photos:
        penalty(20, "нет аватарки")
    else:
        if len(photos) == 1:
            penalty(5, "аватарка никогда не менялась")
        days = (datetime.now(timezone.utc) - photos[0].date).days
        if days < 3:
            penalty(3, "аватарка поставлена только что")

    # имя / юзернейм
    if re.search(r"\d", name) or re.search(r"\d", entity.username or ""):
        penalty(8, "цифры в имени/юзернейме")
    if looks_random(name):
        penalty(10, "имя выглядит сгенерированным")
    if has_weird_symbols(name):
        penalty(6, "странные символы в имени")
    if LEET_RE.search(entity.username or ""):
        penalty(8, "leet-speak в юзернейме")

    # описание
    if not bio:
        penalty(7, "пустое описание")
    elif re.search(r"(https?://|t\.me/|@\w{4,})", bio, re.I):
        penalty(10, "ссылка/упоминание в описании")

    # последний онлайн
    st = entity.status
    if isinstance(st, (UserStatusLastMonth, type(None))):
        penalty(5, "давно не был в сети / скрыт")
    elif isinstance(st, UserStatusLastWeek):
        penalty(2, "был в сети на прошлой неделе")

    # плюсы
    if entity.premium:
        score += 10
        notes.append("+10 Premium")
    if entity.verified:
        score += 10
        notes.append("+10 verified")

    score = max(0, min(100, score))
    return {"username": entity.username or username, "score": score,
            "name": name, "notes": notes, "premium": bool(entity.premium)}


def parse_usernames(text: str) -> list[str]:
    found = re.findall(r"(?:t\.me/|@|\b)([A-Za-z][A-Za-z0-9_]{4,31})\b", text)
    seen, result = set(), []
    for u in found:
        if u.lower() not in seen:
            seen.add(u.lower())
            result.append(u)
    return result


@bot.on(events.NewMessage(pattern=r"^/start"))
async def start(event):
    await event.reply(
        "Пришли юзернеймы сообщением или файлом .txt/.csv.\n"
        "Я верну только тех, у кого рейтинг >= порога.\n\n"
        f"Порог по умолчанию: {DEFAULT_MIN_SCORE}. Изменить: /min 80"
    )


@bot.on(events.NewMessage(pattern=r"^/min\s+(\d{1,3})"))
async def set_min(event):
    value = max(0, min(100, int(event.pattern_match.group(1))))
    min_score_by_chat[event.chat_id] = value
    await event.reply(f"Порог установлен: {value}")


@bot.on(events.NewMessage(func=lambda e: not (e.raw_text or "").startswith("/") or e.file))
async def handle(event):
    text = event.raw_text or ""
    if event.file:
        data = await event.download_media(file=bytes)
        text += "\n" + data.decode("utf-8", errors="ignore")

    usernames = parse_usernames(text)[:MAX_USERNAMES]
    if not usernames:
        return await event.reply("Не нашёл юзернеймов.")

    threshold = min_score_by_chat.get(event.chat_id, DEFAULT_MIN_SCORE)
    status = await event.reply(f"Проверяю {len(usernames)} аккаунтов, порог {threshold}...")

    good, skipped = [], []
    for i, uname in enumerate(usernames, 1):
        try:
            res = await analyze(uname)
        except FloodWaitError as e:
            await status.edit(f"Флуд-лимит Telegram, жду {e.seconds} сек...")
            await asyncio.sleep(e.seconds + 1)
            try:
                res = await analyze(uname)
            except Exception:
                skipped.append(uname)
                continue
        except (UsernameInvalidError, UsernameNotOccupiedError, ValueError):
            skipped.append(uname)
            continue
        except Exception:
            skipped.append(uname)
            continue

        if "score" in res and res["score"] >= threshold:
            good.append(res)

        if i % 10 == 0:
            await status.edit(f"Проверено {i}/{len(usernames)}, подходящих: {len(good)}")
        await asyncio.sleep(DELAY)

    good.sort(key=lambda r: r["score"], reverse=True)
    lines = [f"✅ @{r['username']} — {r['score']}" + (" ⭐" if r["premium"] else "") for r in good]
    summary = f"Готово. Подходящих (>= {threshold}): {len(good)} из {len(usernames)}."
    if skipped:
        summary += f"\nНе удалось проверить: {len(skipped)}"

    await status.edit(summary)
    if lines:
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > 3800:
                await event.respond(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await event.respond(chunk)


async def main():
    await user_client.start()          # при первом запуске спросит номер и код
    await bot.start(bot_token=BOT_TOKEN)
    print("Бот запущен")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
