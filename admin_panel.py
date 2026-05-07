"""
admin_panel.py — Скрытая админ-панель TG-бота
================================================
Доступна только через /admin (только для ADMIN_TG_IDS).
Никогда не упоминается в /start и /help для обычных пользователей.

Возможности:
  • Бан по нику / pfid / xuid / TG-нику / TG-ID
  • Разбан (список из базы)
  • Кик онлайн-игрока
  • Мут с длительностью
  • Статистика верификаций
  • Последние баны (лог)
  • Поиск: кто верифицирован от TG-аккаунта?
  • Отвязка TG от MC-аккаунта
"""

import json
import logging
from datetime import datetime
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, Message,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

logger = logging.getLogger(__name__)

# ─── FSM состояния ────────────────────────────────────────────────────────────

class AdminState(StatesGroup):
    waiting_ban_input    = State()
    waiting_unban_pick   = State()
    waiting_kick_input   = State()
    waiting_mute_input   = State()
    waiting_search_tg    = State()
    waiting_search_mc    = State()
    waiting_unlink_input = State()   # ввод TG ID или MC-ника для отвязки

# ─── Главное меню панели ──────────────────────────────────────────────────────

def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔨 Бан",          callback_data="adm:ban"),
            InlineKeyboardButton(text="✅ Разбан",        callback_data="adm:unban"),
        ],
        [
            InlineKeyboardButton(text="👢 Кик",          callback_data="adm:kick"),
            InlineKeyboardButton(text="🔇 Мут",          callback_data="adm:mute"),
        ],
        [
            InlineKeyboardButton(text="🎮 Найти игрока", callback_data="adm:search_mc"),
            InlineKeyboardButton(text="🔍 Поиск по TG",  callback_data="adm:search_tg"),
        ],
        [
            InlineKeyboardButton(text="📋 Лог банов",    callback_data="adm:banlog"),
            InlineKeyboardButton(text="📊 Статистика",   callback_data="adm:stats"),
        ],
        [
            InlineKeyboardButton(text="🔗 Отвязать TG",  callback_data="adm:unlink"),
        ],
        [
            InlineKeyboardButton(text="❌ Закрыть",       callback_data="adm:close"),
        ],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main"),
    ]])


# ─── Текст главного меню ──────────────────────────────────────────────────────

def admin_main_text(db) -> str:
    stats  = db.get_stats()
    bans   = db.get_ban_log()
    total  = stats.get("total", 0)
    verif  = stats.get("verified", 0)
    nbans  = len(bans)

    return (
        "🔧 <b>Админ-панель</b>\n\n"
        f"👤 Пользователей: <b>{total}</b>\n"
        f"✅ Верифицировано: <b>{verif}</b>\n"
        f"🔨 Банов в логе: <b>{nbans}</b>\n\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )


# ─── Форматирование лога банов ───────────────────────────────────────────────

def format_ban_log(bans: list, limit: int = 10) -> str:
    if not bans:
        return "📋 <b>Лог банов пуст.</b>"
    lines = ["📋 <b>Последние баны:</b>\n"]
    for b in bans[:limit]:
        lines.append(
            f"🔨 <code>{b.get('target','?')}</code>\n"
            f"   📝 {b.get('reason','—')}\n"
            f"   👮 {b.get('by','?')} · 🕐 {b.get('at','?')}\n"
        )
    return "\n".join(lines)


# ─── Поиск верифицированного пользователя по TG ──────────────────────────────

def format_tg_search_result(result: dict | None, query: str) -> str:
    if not result:
        return f"🔍 По запросу <code>{query}</code> ничего не найдено.\n\n💡 Попробуйте @ник или числовой TG ID."
    return (
        f"🔍 <b>Найден пользователь:</b>\n\n"
        f"🆔 TG ID: <code>{result.get('tg_id','—')}</code>\n"
        f"📱 TG ник: <b>{result.get('tg_name','—')}</b>\n"
        f"🎮 MC ник: <code>{result.get('mc_name') or '—'}</code>\n"
        f"✅ Верифицирован: <b>{'да' if result.get('verified') else 'нет'}</b>\n"
        f"📅 Дата: {result.get('verified_at','—')}\n\n"
        f"💡 Для бана: <code>/ban pfid:XXXX Причина</code>\n"
        f"или передай TG ID в причину бана."
    )


# ─── Подсказки ────────────────────────────────────────────────────────────────

BAN_HELP = (
    "🔨 <b>Бан игрока</b>\n\n"
    "Отправь в одном сообщении:\n"
    "<code>цель | причина</code>\n\n"
    "<b>Варианты цели:</b>\n"
    "• <code>Steve</code> — по нику\n"
    "• <code>pfid:abc123ef</code> — по PFID\n"
    "• <code>xuid:2535447942693535</code> — по XUID\n"
    "• <code>tg:@username</code> — по TG-нику\n"
    "• <code>tgid:123456789</code> — по TG ID\n\n"
    "<b>Примеры:</b>\n"
    "<code>Steve | Читы</code>\n"
    "<code>pfid:581017c2 | X-Ray</code>\n"
    "<code>tg:@someuser | Дюп</code>\n\n"
    "Отправь /admin чтобы отменить."
)

KICK_HELP = (
    "👢 <b>Кик игрока</b>\n\n"
    "Формат: <code>Ник | Причина</code>\n\n"
    "Пример: <code>Steve | AFK</code>\n\n"
    "Отправь /admin чтобы отменить."
)

MUTE_HELP = (
    "🔇 <b>Мут игрока</b>\n\n"
    "Формат: <code>Ник | минуты | причина</code>\n\n"
    "Пример: <code>Steve | 60 | Спам</code>\n\n"
    "Отправь /admin чтобы отменить."
)

SEARCH_HELP = (
    "🔍 <b>Поиск по Telegram</b>\n\n"
    "Отправь TG-ник или ID:\n"
    "• <code>@username</code>\n"
    "• <code>123456789</code>\n\n"
    "Найдёт верифицированного игрока и покажет его MC-ник.\n\n"
    "Отправь /admin чтобы отменить."
)

SEARCH_MC_HELP = (
    "🎮 <b>Поиск игрока по MC-нику</b>\n\n"
    "Отправь ник (или часть ника) игрока:\n"
    "<code>Steve</code>\n"
    "<code>imskysc</code> — поиск по подстроке\n\n"
    "Бот покажет pfid, xuid и когда последний раз заходил.\n\n"
    "Отправь /admin чтобы отменить."
)

UNLINK_HELP = (
    "🔗 <b>Отвязать TG от аккаунта</b>\n\n"
    "Отправь одно из:\n"
    "• TG ID пользователя: <code>123456789</code>\n"
    "• TG-ник: <code>@username</code>\n"
    "• MC-ник: <code>Steve</code>\n\n"
    "После отвязки пользователь сможет пройти верификацию заново "
    "с правильным кодом.\n\n"
    "⚠️ Код будет освобождён — его можно будет использовать снова.\n\n"
    "Отправь /admin чтобы отменить."
)


def format_player_card(p: dict) -> str:
    name    = p.get("name", "?")
    pfid    = p.get("pfid") or "—"
    xuid    = p.get("xuid") or "—"
    updated = p.get("updated") or "неизвестно"
    return (
        f"🎮 <b>{name}</b>\n"
        f"├ pfid: <code>{pfid}</code>\n"
        f"├ xuid: <code>{xuid}</code>\n"
        f"└ 🕐 {updated}"
    )


# ─── Парсер ввода бана из панели ─────────────────────────────────────────────

def parse_panel_ban(text: str, db) -> dict:
    parts = text.split("|", 1)
    target_raw = parts[0].strip()
    reason = parts[1].strip() if len(parts) > 1 else "Нарушение правил"
    if not reason:
        reason = "Нарушение правил"

    result = {"reason": reason, "tg_note": None}

    if target_raw.lower().startswith("pfid:"):
        result["pfid"] = target_raw[5:].strip().lower()

    elif target_raw.lower().startswith("xuid:"):
        result["xuid"] = target_raw[5:].strip()

    elif target_raw.lower().startswith("tgid:"):
        tg_id = target_raw[5:].strip()
        user = db.find_by_tg_id(tg_id)
        if user:
            result["tg_note"] = f"tgid:{tg_id} → {user.get('tg_name','?')}"
            mc_name = user.get("mc_name")
            result["name"] = mc_name if mc_name else user.get("tg_name", f"tguser_{tg_id}")
        else:
            result["tg_note"] = f"tgid:{tg_id} (не найден в базе)"
            result["name"] = f"tgid_{tg_id}"

    elif target_raw.lower().startswith("tg:"):
        tg_name = target_raw[3:].strip().lstrip("@")
        user = db.find_by_tg_name(tg_name)
        if user:
            result["tg_note"] = f"TG @{tg_name} → {user.get('mc_name','?')}"
            mc_name = user.get("mc_name")
            result["name"] = mc_name if mc_name else user.get("tg_name", tg_name)
        else:
            result["tg_note"] = f"TG @{tg_name} (не найден в базе)"
            result["name"] = f"tguser_{tg_name}"

    else:
        result["name"] = target_raw

    return result
