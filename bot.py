"""
DublePost — дублирование постов из ЧУЖОГО канала в ваш.

Обычный бот НЕ может читать чужой канал (Telegram не даёт посты без прав админа).
Поэтому здесь:
  • Telethon (ваш аккаунт) — читает источник и копирует посты
  • Bot API (@dublepostbot) — простой интерфейс: 2 ссылки

Нужно один раз: python login.py  (api_id/api_hash + телефон)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events
from telethon.errors import (
    ChannelPrivateError,
    InviteHashEmptyError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
    UsersTooMuchError,
)
from telethon.tl.custom.message import Message as TlMessage
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import (
    Channel,
    Chat,
    ChatInvite,
    ChatInviteAlready,
    ChatInvitePeek,
    MessageService,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dublepost")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SESSION_PATH = DATA_DIR / "user"
DATA_FILE = Path(os.getenv("DATA_FILE", str(DATA_DIR / "config.json")))

BOT_TOKEN = ""
API_ID = 0
API_HASH = ""
API = ""

# user_id -> setup state
pending: dict[int, dict[str, Any]] = {}

# media group buffer: (source_id, grouped_id) -> list[TlMessage]
album_buf: dict[tuple[int, int], list[TlMessage]] = {}
album_tasks: dict[tuple[int, int], asyncio.Task] = {}

# source chat id -> target chat id (resolved for telethon)
mirrors_runtime: dict[int, int] = {}
client: TelegramClient | None = None
main_loop: asyncio.AbstractEventLoop | None = None


# ---------------------------------------------------------------------------
# env / storage
# ---------------------------------------------------------------------------

def _apply_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ (do not override existing)."""
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_dotenv_file() -> None:
    """Читаем секреты из нескольких файлов — хостинги часто выкидывают `.env` из zip."""
    global BOT_TOKEN, API_ID, API_HASH, API, DATA_FILE
    for p in (
        ROOT / ".env",
        ROOT / "env.txt",
        ROOT / "secrets.env",
        ROOT / "data" / "env.txt",
        ROOT / "data" / "bot.env",
    ):
        _apply_env_file(p)
    BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
    API_HASH = os.getenv("API_HASH", "").strip()
    api_id_raw = os.getenv("API_ID", "").strip()
    API_ID = int(api_id_raw) if api_id_raw.isdigit() else 0
    API = f"https://api.telegram.org/bot{BOT_TOKEN}"
    DATA_FILE = Path(os.getenv("DATA_FILE", str(DATA_DIR / "config.json")))



def load_db() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return {"mirrors": {}}
    try:
        raw = DATA_FILE.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {"mirrors": {}}
        data.setdefault("mirrors", {})
        return data
    except Exception as e:
        logger.error("read db: %s", e)
        return {"mirrors": {}}


def save_db(data: dict[str, Any]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


def user_mirrors(data: dict[str, Any], user_id: int) -> list[dict[str, Any]]:
    out = []
    for source_id, cfg in data.get("mirrors", {}).items():
        if int(cfg.get("owner_id", 0)) == user_id:
            out.append({"source_id": source_id, **cfg})
    return out


# ---------------------------------------------------------------------------
# Bot API (UI only)
# ---------------------------------------------------------------------------

def api(method: str, **params: Any) -> dict[str, Any]:
    data = {k: v for k, v in params.items() if v is not None}
    for k, v in list(data.items()):
        if isinstance(v, bool):
            data[k] = "true" if v else "false"
        elif isinstance(v, (dict, list)):
            data[k] = json.dumps(v, ensure_ascii=False)
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        f"{API}/{method}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(err_body)
        except json.JSONDecodeError:
            raise RuntimeError(f"HTTP {e.code}: {err_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e}") from e
    if not payload.get("ok"):
        raise RuntimeError(f"{method} failed: {payload.get('description', payload)}")
    return payload["result"]


def send_message(
    chat_id: int | str,
    text: str,
    reply_markup: dict | None = None,
    parse_mode: str = "HTML",
) -> dict:
    return api(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


def answer_callback(callback_query_id: str, text: str | None = None) -> None:
    try:
        api("answerCallbackQuery", callback_query_id=callback_query_id, text=text)
    except Exception as e:
        logger.warning("answerCallbackQuery: %s", e)


def kb(rows: list[list[tuple[str, str]]]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": t, "callback_data": d} for t, d in row] for row in rows
        ]
    }


def main_kb(has_config: bool) -> dict:
    rows: list[list[tuple[str, str]]] = [[("➕ Настроить зеркало", "setup")]]
    if has_config:
        rows.append([("📋 Мои настройки", "status")])
        rows.append([("🗑 Остановить", "stop")])
    rows.append([("❓ Как это работает", "help")])
    return kb(rows)


def cancel_kb() -> dict:
    return kb([[("❌ Отмена", "cancel")]])


# ---------------------------------------------------------------------------
# Channel refs
# ---------------------------------------------------------------------------

def parse_channel_ref(text: str) -> str | None:
    """
    Возвращает нормализованную ссылку:
      @publicname | -100id | invite:HASH
    """
    text = (text or "").strip()
    if not text:
        return None

    # Приватные invite: t.me/+HASH  |  t.me/joinchat/HASH  |  tg://join?invite=HASH
    m = re.search(
        r"(?:t\.me|telegram\.me)/\+([A-Za-z0-9_-]+)",
        text,
        re.I,
    )
    if m:
        return f"invite:{m.group(1)}"

    m = re.search(
        r"(?:t\.me|telegram\.me)/joinchat/([A-Za-z0-9_-]+)",
        text,
        re.I,
    )
    if m:
        return f"invite:{m.group(1)}"

    m = re.search(r"(?:tg://join\?invite=|invite=)([A-Za-z0-9_-]+)", text, re.I)
    if m:
        return f"invite:{m.group(1)}"

    # Только hash вида +XXXX (пользователь вставил без t.me)
    m = re.fullmatch(r"\+([A-Za-z0-9_-]{8,})", text)
    if m:
        return f"invite:{m.group(1)}"

    # Приватный канал по внутренней ссылке t.me/c/1234567890/...
    m = re.search(r"(?:t\.me|telegram\.me)/c/(\d+)", text, re.I)
    if m:
        return f"-100{m.group(1)}"

    # Публичный username
    m = re.search(
        r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})",
        text,
        re.I,
    )
    if m:
        name = m.group(1)
        if name.lower() in {
            "c",
            "joinchat",
            "addstickers",
            "share",
            "s",
            "proxy",
            "socks",
            "iv",
        }:
            return None
        return f"@{name}"

    m = re.fullmatch(r"@([A-Za-z0-9_]{4,})", text)
    if m:
        return f"@{m.group(1)}"

    m = re.fullmatch(r"([A-Za-z0-9_]{4,})", text)
    if m:
        return f"@{m.group(1)}"

    m = re.fullmatch(r"-100\d+", text)
    if m:
        return text

    return None


def entity_to_id_title(ent: Any, fallback: str = "") -> tuple[int, str]:
    """Channel/Chat → (-100…, title)."""
    title = (
        getattr(ent, "title", None)
        or getattr(ent, "username", None)
        or fallback
        or "channel"
    )
    cid = int(getattr(ent, "id", 0))
    if isinstance(ent, Channel) or getattr(ent, "broadcast", False) or getattr(
        ent, "megagroup", False
    ):
        full_id = int(f"-100{cid}") if cid > 0 else cid
    elif isinstance(ent, Chat):
        full_id = -cid if cid > 0 else cid
    else:
        full_id = cid if cid < 0 else int(f"-100{cid}")
    return full_id, str(title)


HELP_TEXT = """\
📖 <b>Как это работает (чужие каналы)</b>

Обычный бот <b>не может</b> читать чужой канал — Telegram не присылает посты, если бот не админ.

<b>DublePost</b> читает источник через <b>ваш аккаунт</b> (userbot):
• публичный: https://t.me/name или @name
• приватный: пригласительная ссылка
  <code>https://t.me/+XXXX</code> или <code>https://t.me/joinchat/XXXX</code>
• в <b>ваш</b> канал — ваш аккаунт должен уметь туда писать (вы админ)

Если аккаунт ещё не в приватном канале — бот <b>вступит по ссылке</b> (если ссылка живая).

<b>Настройка:</b>
1. Один раз: <code>python login.py</code>
2. В боте: «Настроить зеркало» → 2 ссылки
3. В свой канал — ваш аккаунт админом

Копируются только <b>новые</b> посты после настройки.
"""


# ---------------------------------------------------------------------------
# Telethon: resolve + copy
# ---------------------------------------------------------------------------

async def resolve_invite(hash_: str, join_if_needed: bool = True) -> Any:
    """
    Разрешает invite-hash в entity канала.
    Если ещё не участник — вступает (ImportChatInvite).
    """
    assert client is not None
    try:
        invite = await client(CheckChatInviteRequest(hash_))
    except InviteHashExpiredError as e:
        raise RuntimeError("Пригласительная ссылка истекла") from e
    except InviteHashInvalidError as e:
        raise RuntimeError("Недействительная пригласительная ссылка") from e
    except InviteHashEmptyError as e:
        raise RuntimeError("Пустая пригласительная ссылка") from e
    except Exception as e:
        logger.warning("CheckChatInvite(%s): %s", hash_[:8], e)
        raise RuntimeError(
            f"Не удалось проверить invite-ссылку: {e}"
        ) from e

    # Уже состоим в канале/чате
    if isinstance(invite, ChatInviteAlready):
        return invite.chat

    # Видим превью, но не состоим — вступаем
    if isinstance(invite, (ChatInvite, ChatInvitePeek)):
        title = getattr(invite, "title", "?")
        if not join_if_needed:
            raise RuntimeError(
                f"Аккаунт не состоит в «{title}». Нужна рабочая invite-ссылка."
            )
        try:
            updates = await client(ImportChatInviteRequest(hash_))
        except UserAlreadyParticipantError:
            again = await client(CheckChatInviteRequest(hash_))
            if isinstance(again, ChatInviteAlready):
                return again.chat
            raise RuntimeError("Уже участник, но канал не прочитан — попробуйте снова")
        except InviteRequestSentError as e:
            raise RuntimeError(
                f"«{title}» — вступление только по заявке. "
                "Одобрите заявку вашего аккаунта в канале и пришлите ссылку снова."
            ) from e
        except InviteHashExpiredError as e:
            raise RuntimeError("Пригласительная ссылка истекла") from e
        except InviteHashInvalidError as e:
            raise RuntimeError("Недействительная пригласительная ссылка") from e
        except UsersTooMuchError as e:
            raise RuntimeError("Канал переполнен, вступить нельзя") from e
        except ChannelPrivateError as e:
            raise RuntimeError(
                "Канал приватный и недоступен по этой ссылке"
            ) from e
        except Exception as e:
            logger.warning("ImportChatInvite: %s", e)
            raise RuntimeError(f"Не удалось вступить: {e}") from e

        chats = getattr(updates, "chats", None) or []
        if chats:
            return chats[0]
        again = await client(CheckChatInviteRequest(hash_))
        if isinstance(again, ChatInviteAlready):
            return again.chat
        raise RuntimeError("Вступили, но канал не найден — попробуйте ещё раз")

    raise RuntimeError("Неизвестный ответ Telegram на invite-ссылку")

async def resolve_entity(ref: str):
    assert client is not None
    if ref.startswith("invite:"):
        return await resolve_invite(ref.split(":", 1)[1], join_if_needed=True)
    if ref.startswith("-100"):
        return await client.get_entity(int(ref))
    if ref.startswith("@"):
        return await client.get_entity(ref)
    return await client.get_entity(ref)


async def resolve_channel_info(ref: str) -> tuple[int, str] | None:
    try:
        ent = await resolve_entity(ref)
        full_id, title = entity_to_id_title(ent, fallback=ref)
        try:
            peer = await client.get_input_entity(ent)
            ch_id = getattr(peer, "channel_id", None)
            if ch_id:
                full_id = int(f"-100{ch_id}")
        except Exception:
            pass
        # кэшируем entity в telethon session
        try:
            await client.get_entity(full_id)
        except Exception:
            pass
        return full_id, title
    except RuntimeError as e:
        logger.warning("resolve %s: %s", ref, e)
        # пробрасываем текст в UI через спец-атрибут
        resolve_channel_info.last_error = str(e)  # type: ignore[attr-defined]
        return None
    except Exception as e:
        logger.warning("resolve %s: %s", ref, e)
        resolve_channel_info.last_error = str(e)  # type: ignore[attr-defined]
        return None


resolve_channel_info.last_error = ""  # type: ignore[attr-defined]


async def can_post_to(target_id: int) -> bool:
    """Проверяем, что аккаунт может писать в целевой канал."""
    assert client is not None
    try:
        perms = await client.get_permissions(target_id, "me")
        if perms is None:
            return False
        # creator / admin with post, or open group
        if getattr(perms, "is_creator", False):
            return True
        if getattr(perms, "post_messages", None) is True:
            return True
        if getattr(perms, "send_messages", None) is True:
            return True
        # some channels: is_admin
        if getattr(perms, "is_admin", False):
            return True
        return False
    except Exception as e:
        logger.warning("can_post_to %s: %s", target_id, e)
        return False


async def copy_one(msg: TlMessage, target_id: int) -> None:
    assert client is not None
    if isinstance(msg, MessageService) or msg.action:
        return
    text = msg.message or ""
    entities = msg.entities
    if msg.media:
        await client.send_file(
            target_id,
            file=msg.media,
            caption=text or None,
            formatting_entities=entities if text else None,
            silent=bool(msg.silent),
        )
    else:
        if not text:
            return
        await client.send_message(
            target_id,
            text,
            formatting_entities=entities,
            silent=bool(msg.silent),
            link_preview=True,
        )


async def copy_album(messages: list[TlMessage], target_id: int) -> None:
    assert client is not None
    messages = sorted(messages, key=lambda m: m.id)
    files = []
    caption = None
    entities = None
    for i, m in enumerate(messages):
        if m.media:
            files.append(m.media)
        if m.message and caption is None:
            caption = m.message
            entities = m.entities
    if not files:
        return
    await client.send_file(
        target_id,
        file=files,
        caption=caption,
        formatting_entities=entities if caption else None,
    )


async def flush_album(key: tuple[int, int], target_id: int) -> None:
    await asyncio.sleep(1.2)
    msgs = album_buf.pop(key, [])
    album_tasks.pop(key, None)
    if not msgs:
        return
    try:
        await copy_album(msgs, target_id)
        logger.info("Album %s msgs from %s -> %s", len(msgs), key[0], target_id)
    except Exception as e:
        logger.error("album copy failed: %s", e)
        # fallback one by one
        for m in msgs:
            try:
                await copy_one(m, target_id)
            except Exception as e2:
                logger.error("fallback copy: %s", e2)


def _lookup_target(source_id: int) -> int | None:
    if source_id in mirrors_runtime:
        return mirrors_runtime[source_id]
    abs_id = abs(source_id)
    # -100XXXXXXXXXX vs raw channel id
    for k, v in mirrors_runtime.items():
        if k == source_id:
            return v
        if abs(k) == abs_id:
            return v
        sk, ss = str(abs(k)), str(abs_id)
        if sk.endswith(ss) or ss.endswith(sk):
            return v
    return None


async def on_new_message(event: events.NewMessage.Event) -> None:
    """Копируем новые посты из источников.

    Важно: НЕ игнорируем msg.out / outgoing.
    Если вы сами пишете в канал-источник тем же аккаунтом (login),
    у сообщений out=True — раньше из‑за этого зеркало «молчало».
    """
    msg: TlMessage = event.message
    if not msg:
        return
    # служебные (вход/выход и т.п.) не копируем
    if isinstance(msg, MessageService) or msg.action:
        return

    source_id = int(event.chat_id)
    target_id = _lookup_target(source_id)
    if target_id is None:
        return

    # не зеркалим сами в себя
    if int(target_id) == int(source_id):
        return

    if msg.grouped_id:
        key = (int(source_id), int(msg.grouped_id))
        album_buf.setdefault(key, []).append(msg)
        old = album_tasks.get(key)
        if old and not old.done():
            old.cancel()
        album_tasks[key] = asyncio.create_task(flush_album(key, target_id))
        return

    try:
        await copy_one(msg, target_id)
        logger.info(
            "Copied msg %s: %s -> %s (out=%s)",
            msg.id,
            source_id,
            target_id,
            getattr(msg, "out", None),
        )
    except Exception as e:
        logger.error("copy failed %s -> %s: %s", source_id, target_id, e)
        db = load_db()
        cfg = db.get("mirrors", {}).get(str(source_id)) or {}
        # ключ в db может быть -100…, event.chat_id — тоже; подстрахуемся
        if not cfg:
            for k, v in db.get("mirrors", {}).items():
                if _lookup_target(int(k)) == target_id:
                    cfg = v
                    break
        owner = cfg.get("owner_id")
        if owner and BOT_TOKEN:
            try:
                send_message(
                    owner,
                    "⚠️ Не удалось скопировать пост.\n"
                    f"Ошибка: <code>{e}</code>\n"
                    "Проверьте, что ваш аккаунт — админ в целевом канале.",
                )
            except Exception:
                pass



async def reload_mirrors() -> None:
    """Перечитать config и подписаться на источники."""
    global mirrors_runtime
    assert client is not None
    db = load_db()
    new_map: dict[int, int] = {}
    for source_key, cfg in db.get("mirrors", {}).items():
        try:
            sid = int(source_key)
            tid = int(cfg["target_id"])
            # ensure entities cached
            try:
                await client.get_entity(sid)
            except Exception:
                ref = cfg.get("source_ref")
                if ref:
                    ent = await resolve_entity(ref)
                    sid = (await resolve_channel_info(ref) or (sid, ""))[0]
            try:
                await client.get_entity(tid)
            except Exception:
                ref = cfg.get("target_ref")
                if ref:
                    info = await resolve_channel_info(ref)
                    if info:
                        tid = info[0]
            new_map[sid] = tid
            # дублируем ключи, чтобы совпало с event.chat_id
            if sid < 0 and str(sid).startswith("-100"):
                raw = int(str(sid)[4:])
                new_map[raw] = tid
            logger.info("Mirror active: %s -> %s (%s)", sid, tid, cfg.get("source_title"))
        except Exception as e:
            logger.error("load mirror %s: %s", source_key, e)
    mirrors_runtime = new_map


# ---------------------------------------------------------------------------
# Bot UI handlers
# ---------------------------------------------------------------------------

def handle_start(chat_id: int, user_id: int) -> None:
    pending.pop(user_id, None)
    data = load_db()
    has = bool(user_mirrors(data, user_id))
    send_message(
        chat_id,
        "👋 <b>DublePost</b>\n"
        "Бот: @dublepostbot\n\n"
        "Дублирую посты <b>из чужого канала</b> в ваш.\n\n"
        "1️⃣ ссылка канала-источника (чужой)\n"
        "2️⃣ ссылка вашего канала\n\n"
        "⚠️ Один раз на ПК/хостинге нужен вход аккаунта:\n"
        "<code>python login.py</code>\n\n"
        "Нажмите кнопку ниже 👇",
        reply_markup=main_kb(has),
    )


def handle_help(chat_id: int) -> None:
    send_message(chat_id, HELP_TEXT)


def handle_setup(chat_id: int, user_id: int) -> None:
    if client is None or not client.is_connected():
        send_message(
            chat_id,
            "❌ Userbot не запущен.\n"
            "На сервере выполните <code>python login.py</code>, "
            "затем снова <code>python bot.py</code>.",
        )
        return
    pending[user_id] = {"step": "source"}
    send_message(
        chat_id,
        "📡 <b>Шаг 1/2</b>\n\n"
        "Пришлите ссылку <b>канала-источника</b>.\n\n"
        "Подходят:\n"
        "• https://t.me/name — публичный\n"
        "• https://t.me/+XXXX — <b>пригласительная</b> (приватный)\n"
        "• https://t.me/joinchat/XXXX — то же\n"
        "• @name\n\n"
        "Для приватного: если аккаунт ещё не внутри — "
        "бот вступит по ссылке автоматически.",
        reply_markup=cancel_kb(),
    )


def handle_status(chat_id: int, user_id: int) -> None:
    items = user_mirrors(load_db(), user_id)
    if not items:
        send_message(
            chat_id,
            "У вас пока нет настроенных зеркал.",
            reply_markup=main_kb(False),
        )
        return
    lines = ["📋 <b>Ваши зеркала:</b>\n"]
    for i, m in enumerate(items, 1):
        lines.append(
            f"{i}. 📡 {m.get('source_title', m['source_id'])}\n"
            f"   → 📤 {m.get('target_title', m.get('target_id'))}"
        )
    send_message(chat_id, "\n".join(lines), reply_markup=main_kb(True))


def handle_stop(chat_id: int, user_id: int) -> None:
    db = load_db()
    mirrors = db.get("mirrors", {})
    for sid in [s for s, c in mirrors.items() if int(c.get("owner_id", 0)) == user_id]:
        del mirrors[sid]
    save_db(db)
    pending.pop(user_id, None)
    if client and main_loop:
        asyncio.run_coroutine_threadsafe(reload_mirrors(), main_loop)
    send_message(
        chat_id,
        "🗑 Зеркало остановлено. Посты больше не дублируются.",
        reply_markup=main_kb(False),
    )


def handle_cancel(chat_id: int, user_id: int) -> None:
    pending.pop(user_id, None)
    has = bool(user_mirrors(load_db(), user_id))
    send_message(chat_id, "Отменено. Главное меню:", reply_markup=main_kb(has))


async def handle_text_step_async(chat_id: int, user_id: int, text: str) -> None:
    state = pending.get(user_id)
    if not state:
        send_message(
            chat_id,
            "Нажмите /start или кнопку в меню.",
            reply_markup=main_kb(bool(user_mirrors(load_db(), user_id))),
        )
        return

    ref = parse_channel_ref(text)
    if not ref:
        send_message(
            chat_id,
            "❌ Не понял ссылку.\n\n"
            "Пришлите одну из:\n"
            "• https://t.me/name\n"
            "• https://t.me/+XXXX\n"
            "• https://t.me/joinchat/XXXX\n"
            "• @name",
            reply_markup=cancel_kb(),
        )
        return

    if client is None:
        send_message(chat_id, "❌ Userbot не подключен. Запустите login.py + bot.py")
        return

    resolve_channel_info.last_error = ""  # type: ignore[attr-defined]
    info = await resolve_channel_info(ref)
    if not info:
        detail = getattr(resolve_channel_info, "last_error", "") or "неизвестная ошибка"
        send_message(
            chat_id,
            "❌ Не удалось открыть канал.\n\n"
            f"<b>Причина:</b> <code>{detail}</code>\n\n"
            "• Invite-ссылка живая? (не отозвана)\n"
            "• Для заявки на вступление — примите её в канале\n"
            "• Аккаунт из <code>login.py</code> видит этот канал?\n"
            "• Делали <code>python login.py</code>?",
            reply_markup=cancel_kb(),
        )
        return

    cid, title = info

    if state["step"] == "source":
        pending[user_id] = {
            "step": "target",
            "source_id": cid,
            "source_title": title,
            "source_ref": ref,
        }
        send_message(
            chat_id,
            f"✅ Источник: <b>{title}</b>\n"
            f"(читаем через ваш аккаунт — админ там <b>не нужен</b>)\n\n"
            "📤 <b>Шаг 2/2</b>\n\n"
            "Пришлите ссылку <b>вашего</b> канала (куда дублировать).\n\n"
            "Можно invite:\n"
            "• https://t.me/+XXXX\n"
            "• https://t.me/joinchat/XXXX\n"
            "• или @public / https://t.me/name\n\n"
            "Ваш аккаунт (login) должен быть <b>админом</b> с правом публикации.",
            reply_markup=cancel_kb(),
        )
        return

    # target
    source_id = int(state["source_id"])
    if cid == source_id:
        send_message(
            chat_id,
            "❌ Источник и цель не могут быть одним каналом.",
            reply_markup=cancel_kb(),
        )
        return

    if not await can_post_to(cid):
        send_message(
            chat_id,
            f"❌ Аккаунт не может писать в «{title}».\n"
            "Добавьте <b>себя</b> (тот аккаунт, которым делали login) "
            "админом канала с правом публикации, и пришлите ссылку снова.",
            reply_markup=cancel_kb(),
        )
        return

    db = load_db()
    db["mirrors"][str(source_id)] = {
        "target_id": cid,
        "owner_id": user_id,
        "source_title": state["source_title"],
        "target_title": title,
        "source_ref": state["source_ref"],
        "target_ref": ref,
    }
    save_db(db)
    pending.pop(user_id, None)
    await reload_mirrors()

    send_message(
        chat_id,
        "🎉 <b>Готово! Зеркало работает.</b>\n\n"
        f"📡 Откуда (чужой): <b>{state['source_title']}</b>\n"
        f"📤 Куда (ваш): <b>{title}</b>\n\n"
        "Новые посты будут копироваться автоматически.\n"
        "Старые не переносятся.",
        reply_markup=main_kb(True),
    )
    logger.info("Mirror by %s: %s -> %s", user_id, source_id, cid)


def process_update(update: dict, loop: asyncio.AbstractEventLoop) -> None:
    if "callback_query" in update:
        cq = update["callback_query"]
        data = cq.get("data") or ""
        user_id = cq["from"]["id"]
        chat_id = cq["message"]["chat"]["id"]
        answer_callback(cq["id"])
        if data == "setup":
            handle_setup(chat_id, user_id)
        elif data == "help":
            handle_help(chat_id)
        elif data == "status":
            handle_status(chat_id, user_id)
        elif data == "stop":
            handle_stop(chat_id, user_id)
        elif data == "cancel":
            handle_cancel(chat_id, user_id)
        return

    msg = update.get("message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return
    user_id = msg["from"]["id"]
    chat_id = chat["id"]
    text = (msg.get("text") or "").strip()

    if text.startswith("/start") or text.startswith("/menu"):
        handle_start(chat_id, user_id)
    elif text.startswith("/help"):
        handle_help(chat_id)
    elif text:
        fut = asyncio.run_coroutine_threadsafe(
            handle_text_step_async(chat_id, user_id, text),
            loop,
        )
        try:
            fut.result(timeout=60)
        except Exception as e:
            logger.exception("setup step: %s", e)
            try:
                send_message(chat_id, f"❌ Ошибка: <code>{e}</code>")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def bot_polling_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Long-poll Bot API in executor so Telethon keeps the asyncio loop."""
    offset = 0
    try:
        api("deleteWebhook", drop_pending_updates=True)
    except Exception as e:
        logger.warning("deleteWebhook: %s", e)

    me = api("getMe")
    logger.info("UI bot @%s ready", me.get("username"))

    while True:
        try:
            # run blocking urllib in thread
            updates = await asyncio.to_thread(
                lambda off=offset: api(
                    "getUpdates",
                    offset=off,
                    timeout=25,
                    allowed_updates=["message", "callback_query"],
                )
            )
        except Exception as e:
            logger.error("getUpdates: %s", e)
            await asyncio.sleep(3)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                await asyncio.to_thread(process_update, upd, loop)
            except Exception as e:
                logger.exception("process_update: %s", e)


def restore_session_from_env() -> None:
    """Render free disk is ephemeral — session from SESSION_B64 env secret."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    session_file = Path(str(SESSION_PATH) + ".session")
    b64 = os.getenv("SESSION_B64", "").strip()
    if b64:
        try:
            raw = base64.b64decode(b64)
            session_file.write_bytes(raw)
            logger.info("Session restored from SESSION_B64 (%s bytes)", len(raw))
        except Exception as e:
            raise SystemExit(f"SESSION_B64 decode failed: {e}") from e
    cfg_b64 = os.getenv("CONFIG_B64", "").strip()
    if cfg_b64:
        try:
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            DATA_FILE.write_bytes(base64.b64decode(cfg_b64))
            logger.info("Config restored from CONFIG_B64")
        except Exception as e:
            logger.warning("CONFIG_B64 decode failed: %s", e)
    cfg_json = os.getenv("CONFIG_JSON", "").strip()
    if cfg_json and not cfg_b64:
        try:
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            json.loads(cfg_json)
            DATA_FILE.write_text(cfg_json, encoding="utf-8")
            logger.info("Config restored from CONFIG_JSON")
        except Exception as e:
            logger.warning("CONFIG_JSON invalid: %s", e)


def start_health_http_server() -> None:
    """HTTP /health for Render free + UptimeRobot keep-alive pings."""
    port = int(os.getenv("PORT", os.getenv("HEALTH_PORT", "10000")))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"ok"
            if self.path in ("/", "/health", "/healthz", "/ping"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("http " + fmt, *args)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=server.serve_forever, name="health-http", daemon=True)
    t.start()
    logger.info("Health HTTP on 0.0.0.0:%s (/health) — ping this to avoid sleep", port)


async def amain() -> None:
    global client, main_loop
    load_dotenv_file()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    restore_session_from_env()
    start_health_http_server()

    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN (env / .env)")
    if not API_ID or not API_HASH:
        raise SystemExit(
            "Нет API_ID / API_HASH.\n"
            "1) https://my.telegram.org → API development tools\n"
            "2) python login.py"
        )
    session_file = Path(str(SESSION_PATH) + ".session")
    if not session_file.exists():
        raise SystemExit(
            f"Нет сессии {session_file}\n"
            "Сначала login.py локально, потом SESSION_B64 в Render."
        )

    main_loop = asyncio.get_running_loop()
    client = TelegramClient(str(SESSION_PATH), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Сессия не авторизована. Запустите: python login.py")

    me = await client.get_me()
    if getattr(me, "bot", False):
        raise SystemExit(
            "Сессия — это БОТ, а не пользователь.\n"
            "Пригласительные ссылки и чужие приватные каналы так не работают.\n"
            "Удалите data/user.session и выполните:  python login.py\n"
            "Войдите по НОМЕРУ ТЕЛЕФОНА (личный аккаунт)."
        )
    logger.info(
        "Userbot as %s (id=%s)",
        me.username or me.first_name,
        me.id,
    )

    # без incoming=True: иначе свои посты в канале (out) не приходят в handler
    client.add_event_handler(on_new_message, events.NewMessage())
    await reload_mirrors()

    # UI-бот + слушатель каналов; health HTTP — в отдельном потоке
    await asyncio.gather(
        client.run_until_disconnected(),
        bot_polling_loop(main_loop),
    )


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
