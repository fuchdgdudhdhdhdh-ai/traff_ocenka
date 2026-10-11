"""
Бот-проверяльщик профилей Telegram (один файл).

Вход в аккаунт-проверяльщик происходит прямо в боте: номер телефона
прописан в коде, а код из Telegram вводится кнопками-цифрами.

Установка:  pip install telethon
Запуск:     python profile_checker_bot.py
"""
import asyncio
import os
import re
import unicodedata
from datetime import datetime, timezone

from telethon import Button, TelegramClient, events
from telethon.errors import (
    FloodWaitError, PhoneCodeExpiredError, PhoneCodeInvalidError,
    SessionPasswordNeededError, UsernameInvalidError, UsernameNotOccupiedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import User, UserStatusLastMonth, UserStatusLastWeek

# ====================== НАСТРОЙКИ ======================
API_ID = 35981014                    # https://my.telegram.org -> API development tools
API_HASH = "4e788ed1a686308838891734a4173c48"
BOT_TOKEN = "8936797539:AAFef7YisHtaqFykqFvrcZBxwH9SVa06T5I"   # от @BotFather
PHONE = os.environ.get("PHONE", "+17313936771")           # номер аккаунта-проверяльщика
ADMIN_ID = int(os.environ.get("ADMIN_ID", 8504594395))              # твой Telegram ID (узнать: @userinfobot)
TWO_FA_PASSWORD = os.environ.get("TWO_FA_PASSWORD", "Fiksik2009")    # облачный пароль, если включён (иначе бот спросит)

DEFAULT_MIN_SCORE = 70      # порог по умолчанию (меняется командой /min 80)
DELAY = 1.5                 # пауза между запросами, защита от флуд-лимитов
MAX_USERNAMES = 300         # максимум юзернеймов за один запуск
SESSION_FILE = "user_session.txt"
# ========================================================

user_client = TelegramClient(StringSession(), API_ID, API_HASH)
bot = TelegramClient(StringSession(), API_ID, API_HASH)

ready = False                                   # авторизован ли аккаунт-проверяльщик
login: dict = {}                                # состояние входа
min_score_by_chat: dict[int, int] = {}

# ---------------------- оценка профилей ----------------------
# Примерная привязка ID аккаунта -> дата регистрации (приблизительно)
ID_DATES = [
    (0, "2013-08"), (100_000_000, "2015-06"), (300_000_000, "2017-01"),
    (600_000_000, "2018-07"), (1_000_000_000, "2019-12"), (1_500_000_000, "2020-09"),
    (2_000_000_000, "2021-04"), (5_000_000_000, "2022-06"), (6_000_000_000, "2023-01"),
    (7_000_000_000, "2024-02"), (7_700_000_000, "2024-12"), (8_300_000_000, "2025-06"),
]
LEET_RE = re.compile(r"[a-z]+[0-9@$]+[a-z]+|[a-z]*[430@$][a-z]+[1370][a-z]*", re.I)


def estimate_account_age_years(user_id: int) -> float:
    date_str = ID_DATES[0][1]
    for threshold, d in ID_DATES:
        if user_id >= threshold:
            date_str = d
    created = datetime.strptime(date_str, "%Y-%m").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).days / 365


def looks_random(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 4:
        return False
    scripts = {unicodedata.name(c, "").split()[0] for c in letters}
    if len(scripts) > 1:
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
        return {"username": username, "error": "не пользователь"}
    if entity.bot:
        return {"username": username, "error": "бот"}

    full = await user_client(GetFullUserRequest(entity))
    bio = (full.full_user.about or "").strip()
    photos = await user_client.get_profile_photos(entity, limit=5)
    name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()

    score, notes = 100, []

    def penalty(points, text):
        nonlocal score
        score -= points
        notes.append(f"-{points} {text}")

    age = estimate_account_age_years(entity.id)
    if age < 1:
        penalty(15, f"аккаунт младше года (~{age:.1f} г.)")
    elif age < 2:
        penalty(8, f"аккаунт ~{age:.1f} г.")

    if not photos:
        penalty(20, "нет аватарки")
    else:
        if len(photos) == 1:
            penalty(5, "аватарка никогда не менялась")
        if (datetime.now(timezone.utc) - photos[0].date).days < 3:
            penalty(3, "аватарка поставлена только что")

    if re.search(r"\d", name) or re.search(r"\d", entity.username or ""):
        penalty(8, "цифры в имени/юзернейме")
    if looks_random(name):
        penalty(10, "имя выглядит сгенерированным")
    if has_weird_symbols(name):
        penalty(6, "странные символы в имени")
    if LEET_RE.search(entity.username or ""):
        penalty(8, "leet-speak в юзернейме")

    if not bio:
        penalty(7, "пустое описание")
    elif re.search(r"(https?://|t\.me/|@\w{4,})", bio, re.I):
        penalty(10, "ссылка/упоминание в описании")

    st = entity.status
    if isinstance(st, UserStatusLastMonth) or st is None:
        penalty(5, "давно не был в сети / скрыт")
    elif isinstance(st, UserStatusLastWeek):
        penalty(2, "был в сети на прошлой неделе")

    if entity.premium:
        score += 10
        notes.append("+10 Premium")
    if entity.verified:
        score += 10
        notes.append("+10 verified")

    return {"username": entity.username or username, "score": max(0, min(100, score)),
            "name": name, "notes": notes, "premium": bool(entity.premium)}


def parse_usernames(text: str) -> list[str]:
    found = re.findall(r"(?:t\.me/|@|\b)([A-Za-z][A-Za-z0-9_]{4,31})\b", text)
    seen, result = set(), []
    for u in found:
        if u.lower() not in seen:
            seen.add(u.lower())
            result.append(u)
    return result


# ---------------------- вход через кнопки ----------------------
def keypad():
    return [
        [Button.inline(str(n), f"k_{n}".encode()) for n in (1, 2, 3)],
        [Button.inline(str(n), f"k_{n}".encode()) for n in (4, 5, 6)],
        [Button.inline(str(n), f"k_{n}".encode()) for n in (7, 8, 9)],
        [Button.inline("⌫", b"k_del"), Button.inline("0", b"k_0"), Button.inline("✅", b"k_ok")],
    ]


def code_text(digits: str, length: int) -> str:
    shown = " ".join(digits) + (" " if digits else "") + " ".join("_" * (length - len(digits)))
    return f"Введи код, который пришёл в Telegram на {PHONE}:\n\n🔢 {shown.strip()}"


async def save_session_and_notify(chat_id: int):
    global ready
    ready = True
    session_str = user_client.session.save()
    try:
        with open(SESSION_FILE, "w") as f:
            f.write(session_str)
    except OSError:
        pass
    login.clear()
    await bot.send_message(
        chat_id,
        "✅ Вход выполнен. Можно присылать юзернеймы.\n\n"
        "⚠️ Чтобы не входить заново после перезапуска сервера, сохрани строку ниже "
        "в переменную окружения USER_SESSION и удали это сообщение. "
        "Никому её не показывай.\n\n" + session_str,
    )


async def begin_login(chat_id: int):
    await user_client.connect()
    if await user_client.is_user_authorized():
        return await save_session_and_notify(chat_id)
    sent = await user_client.send_code_request(PHONE)
    length = getattr(sent.type, "length", 5) or 5
    msg = await bot.send_message(chat_id, code_text("", length), buttons=keypad())
    login.update(hash=sent.phone_code_hash, digits="", length=length,
                 msg=msg, chat=chat_id, need_password=False)


async def try_sign_in(event):
    digits = login["digits"]
    try:
        await user_client.sign_in(PHONE, digits, phone_code_hash=login["hash"])
    except SessionPasswordNeededError:
        if TWO_FA_PASSWORD:
            await user_client.sign_in(password=TWO_FA_PASSWORD)
        else:
            login["need_password"] = True
            return await event.edit(
                "🔐 Включён облачный пароль (2FA). Отправь его сообщением, "
                "я сразу удалю это сообщение."
            )
    except PhoneCodeInvalidError:
        login["digits"] = ""
        return await event.edit("❌ Неверный код, введи ещё раз.\n\n" +
                                code_text("", login["length"]), buttons=keypad())
    except PhoneCodeExpiredError:
        login.clear()
        return await event.edit("⌛ Код устарел. Нажми /login, чтобы получить новый.")
    except FloodWaitError as e:
        login.clear()
        return await event.edit(f"Слишком много попыток, подожди {e.seconds} сек и нажми /login.")
    await event.edit("Проверяю...")
    await save_session_and_notify(login.get("chat", event.chat_id))


@bot.on(events.CallbackQuery(pattern=rb"k_(\d|del|ok)"))
async def on_key(event):
    if event.sender_id != ADMIN_ID or not login or login.get("need_password"):
        return await event.answer()
    key = event.pattern_match.group(1).decode()
    if key == "del":
        login["digits"] = login["digits"][:-1]
    elif key == "ok":
        if login["digits"]:
            await event.answer()
            return await try_sign_in(event)
    elif len(login["digits"]) < login["length"]:
        login["digits"] += key
    await event.answer()
    if len(login["digits"]) >= login["length"]:
        return await try_sign_in(event)
    await event.edit(code_text(login["digits"], login["length"]), buttons=keypad())


# ---------------------- команды и основной обработчик ----------------------
@bot.on(events.NewMessage(pattern=r"^/start"))
async def start(event):
    if event.sender_id != ADMIN_ID:
        return await event.reply(f"Нет доступа. Твой ID: {event.sender_id}")
    if not ready:
        return await begin_login(event.chat_id)
    await event.reply(
        "Пришли юзернеймы сообщением или файлом .txt/.csv.\n"
        "Верну только тех, у кого рейтинг >= порога.\n\n"
        f"Порог по умолчанию: {DEFAULT_MIN_SCORE}. Изменить: /min 80"
    )


@bot.on(events.NewMessage(pattern=r"^/login"))
async def cmd_login(event):
    if event.sender_id == ADMIN_ID:
        await begin_login(event.chat_id)


@bot.on(events.NewMessage(pattern=r"^/min\s+(\d{1,3})"))
async def set_min(event):
    if event.sender_id != ADMIN_ID:
        return
    value = max(0, min(100, int(event.pattern_match.group(1))))
    min_score_by_chat[event.chat_id] = value
    await event.reply(f"Порог установлен: {value}")


@bot.on(events.NewMessage(func=lambda e: not (e.raw_text or "").startswith("/") or e.file))
async def handle(event):
    if event.sender_id != ADMIN_ID:
        return

    # ввод пароля 2FA
    if login.get("need_password"):
        password = event.raw_text
        await event.delete()
        try:
            await user_client.sign_in(password=password)
        except Exception as e:
            return await bot.send_message(event.chat_id, f"❌ Пароль не подошёл ({type(e).__name__}). Отправь ещё раз.")
        return await save_session_and_notify(event.chat_id)

    if not ready:
        return await event.reply("Сначала войди в аккаунт: /login")

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

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3800:
            await event.respond(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await event.respond(chunk)


# ---------------------- запуск ----------------------
async def health_server():
    """Мини-сервер, чтобы Render Web Service видел открытый порт."""
    port = int(os.environ.get("PORT", 0))
    if not port:
        return

    async def handler(reader, writer):
        await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "0.0.0.0", port)
    asyncio.create_task(server.serve_forever())


async def main():
    global ready, user_client
    if not ADMIN_ID:
        raise SystemExit("Укажи ADMIN_ID (твой Telegram ID, узнать можно у @userinfobot)")

    await health_server()

    session_str = os.environ.get("USER_SESSION", "")
    if not session_str and os.path.exists(SESSION_FILE):
        session_str = open(SESSION_FILE).read().strip()
    if session_str:
        user_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)

    await user_client.connect()
    ready = await user_client.is_user_authorized()

    await bot.start(bot_token=BOT_TOKEN)
    print("Бот запущен, аккаунт авторизован:", ready)
    if not ready:
        try:
            await begin_login(ADMIN_ID)   # сработает, если ты уже писал боту /start
        except Exception:
            print("Напиши боту /start, чтобы войти в аккаунт")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
