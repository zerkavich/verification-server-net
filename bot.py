import os
import time
import asyncio
import logging
import aiohttp
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from db import Database
from moderation import is_admin, mod_action, parse_mod_args
from ptero_ws import PteroConsoleWatcher
from addon_bridge import AddonBridge
from admin_panel import (
    AdminState, admin_main_kb, admin_main_text, back_kb,
    BAN_HELP, KICK_HELP, MUTE_HELP, SEARCH_HELP, SEARCH_MC_HELP, UNLINK_HELP,
    format_ban_log, format_tg_search_result, format_player_card, parse_panel_ban,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


BOT_TOKEN       = os.getenv("BOT_TOKEN")
PTERODACTYL_URL = os.getenv("PTERODACTYL_URL", "https://my.aurorix.net")
PTERODACTYL_KEY = os.getenv("PTERODACTYL_KEY")
SERVER_ID       = os.getenv("SERVER_ID", "6daf8160-16ab-4a5b-ac25-3e35cb75a3d4")
TG_CHANNEL      = os.getenv("TG_CHANNEL", "@zerkavich")
CHECK_SUB       = os.getenv("CHECK_SUBSCRIPTION", "false").lower() == "true"
APPEAL_URL      = os.getenv("APPEAL_URL", "@zerkavich")

# !!! ОДИНАКОВЫЙ СЕКРЕТ В БОТЕ И В АДДОНЕ (tg_verify.js) !!!
SECRET          = os.getenv("VERIFY_SECRET", "ЗАМЕНИ_МЕНЯ")

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())
db  = Database("data.json")

bridge = AddonBridge(db=db, bot=bot)

watcher = PteroConsoleWatcher(
    panel_url   = PTERODACTYL_URL,
    api_key     = PTERODACTYL_KEY or '',
    server_id   = SERVER_ID,
    output_file = 'pfids.json',
    appeal_url  = APPEAL_URL,
    db          = db,
)

# ─── Генерация токена (зеркало алгоритма из tg_verify.js) ────────────────────

_B36 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'

def _to_base36(n: int) -> str:
    if n == 0:
        return '0'
    s = ''
    while n > 0:
        s = _B36[n % 36] + s
        n //= 36
    return s

def _djb2hash(raw: str) -> int:
    h = 0
    for c in raw:
        h = ((h << 5) + h + ord(c)) & 0xFFFFFFFF
    return h % (36 ** 6)

def _hash_to_b36_6(h: int) -> str:
    s = ''
    for _ in range(6):
        s = _B36[h % 36] + s
        h //= 36
    return s

def make_token(tg_id: int) -> str:
    day = int(time.time()) // 86400
    raw = f"{SECRET}{tg_id}{day}"
    h6  = _djb2hash(raw)
    return f"{_to_base36(tg_id)}_{_hash_to_b36_6(h6)}"

async def check_subscription(user_id: int) -> bool:
    if not CHECK_SUB:
        return True
    try:
        member = await bot.get_chat_member(TG_CHANNEL, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return True


# ─── Главное меню верификации (кнопки) ───────────────────────────────────────

def main_menu_kb(is_verified: bool = False) -> InlineKeyboardMarkup:
    if is_verified:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мой статус", callback_data="menu:status")],
            [InlineKeyboardButton(text="❓ Помощь",     callback_data="menu:help")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Верифицироваться", callback_data="menu:verify")],
        [InlineKeyboardButton(text="📋 Мой статус",       callback_data="menu:status")],
        [InlineKeyboardButton(text="❓ Как это работает", callback_data="menu:help")],
    ])


# ─── /start ──────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    uid = str(msg.from_user.id)
    data = db.get_user(uid)
    is_verified = bool(data and data.get("verified"))

    admin_hint = ""
    if is_admin(msg.from_user.id):
        admin_hint = "\n\n🔧 <b>Режим администратора активен.</b> /admin"

    await msg.answer(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Бот верификации сервера Minecraft.\n"
        "Привяжи Telegram-аккаунт к своему MC-профилю.\n"
        f"📢 Канал: {TG_CHANNEL}"
        f"{admin_hint}",
        reply_markup=main_menu_kb(is_verified),
        parse_mode="HTML"
    )


# ─── Callback: главное меню ───────────────────────────────────────────────────

@dp.callback_query(F.data == "menu:help")
async def cb_menu_help(call: CallbackQuery):
    await call.message.edit_text(
        "❓ <b>Как верифицироваться:</b>\n\n"
        "1️⃣ Нажмите «Верифицироваться» — бот выдаст код\n"
        "2️⃣ Зайдите на сервер Minecraft\n"
        "3️⃣ Введите <code>.econ verify ВАШ_КОД</code>\n\n"
        "✅ После верификации вы получите:\n"
        "• Титул <b>«Гражданин»</b>\n"
        "• <b>+200 T</b> на баланс\n"
        "• <b>+10 Trust Score</b>\n\n"
        "⚠️ Код действителен до конца суток (UTC).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back"),
        ]]),
        parse_mode="HTML"
    )
    await call.answer()


@dp.callback_query(F.data == "menu:status")
async def cb_menu_status(call: CallbackQuery):
    uid  = str(call.from_user.id)
    data = db.get_user(uid)
    if data and data.get("verified"):
        mc = data.get("mc_name") or "—"
        text = (
            f"✅ <b>Аккаунт верифицирован!</b>\n\n"
            f"🎮 MC-ник: <code>{mc}</code>\n"
            f"📅 Дата: {data.get('verified_at', '—')}"
        )
    else:
        text = (
            "❌ <b>Не верифицирован.</b>\n\n"
            "Нажмите «Верифицироваться» чтобы привязать аккаунт."
        )
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back"),
        ]]),
        parse_mode="HTML"
    )
    await call.answer()


@dp.callback_query(F.data == "menu:back")
async def cb_menu_back(call: CallbackQuery):
    uid = str(call.from_user.id)
    data = db.get_user(uid)
    is_verified = bool(data and data.get("verified"))
    await call.message.edit_text(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Бот верификации сервера Minecraft.\n"
        "Привяжи Telegram-аккаунт к своему MC-профилю.\n"
        f"📢 Канал: {TG_CHANNEL}",
        reply_markup=main_menu_kb(is_verified),
        parse_mode="HTML"
    )
    await call.answer()


@dp.callback_query(F.data == "menu:verify")
async def cb_menu_verify(call: CallbackQuery):
    uid = str(call.from_user.id)
    data = db.get_user(uid)
    if data and data.get("verified"):
        await call.answer("✅ Вы уже верифицированы!", show_alert=True)
        return

    if not await check_subscription(call.from_user.id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{TG_CHANNEL.lstrip('@')}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")],
        ])
        await call.message.edit_text(
            f"⚠️ Для верификации сначала подпишитесь на {TG_CHANNEL}",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await call.answer()
        return

    token = make_token(call.from_user.id)
    await call.message.edit_text(
        "🔑 <b>Ваш код верификации:</b>\n\n"
        f"<code>{token}</code>\n\n"
        f"Введите в игре:\n"
        f"<code>.econ verify {token}</code>\n\n"
        "⚠️ Код действителен до конца суток (UTC).\n"
        "Зайдите на сервер и введите команду выше.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back"),
        ]]),
        parse_mode="HTML"
    )
    await call.answer()


# ─── /help ───────────────────────────────────────────────────────────────────

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "❓ <b>Как верифицироваться:</b>\n\n"
        "1️⃣ Нажмите «Верифицироваться» — бот выдаст код\n"
        "2️⃣ Зайдите на сервер Minecraft\n"
        "3️⃣ Введите <code>.econ verify ВАШ_КОД</code>\n\n"
        "✅ После верификации вы получите:\n"
        "• Титул <b>«Гражданин»</b>\n"
        "• <b>+200 T</b> на баланс\n"
        "• <b>+10 Trust Score</b>\n\n"
        "⚠️ Код действителен до конца суток (UTC).",
        parse_mode="HTML"
    )


# ─── /status ─────────────────────────────────────────────────────────────────

@dp.message(Command("status"))
async def cmd_status(msg: Message):
    uid  = str(msg.from_user.id)
    data = db.get_user(uid)
    if data and data.get("verified"):
        mc = data.get("mc_name") or "—"
        await msg.answer(
            f"✅ <b>Аккаунт верифицирован!</b>\n\n"
            f"🎮 MC-ник: <code>{mc}</code>\n"
            f"📅 Дата: {data.get('verified_at', '—')}",
            parse_mode="HTML"
        )
    else:
        await msg.answer(
            "❌ <b>Не верифицирован.</b>\n\n"
            "Нажмите «Верифицироваться» в меню, чтобы получить код.",
            parse_mode="HTML"
        )



# ═══════════════════════════════════════════════════════════════════════════════
# СКРЫТАЯ АДМИН-ПАНЕЛЬ
# ═══════════════════════════════════════════════════════════════════════════════

@dp.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.clear()
    await msg.answer(
        admin_main_text(db),
        reply_markup=admin_main_kb(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm:main")
async def cb_admin_main(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return
    await state.clear()
    await call.message.edit_text(
        admin_main_text(db),
        reply_markup=admin_main_kb(),
        parse_mode="HTML"
    )
    await call.answer()


@dp.callback_query(F.data == "adm:close")
async def cb_admin_close(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.answer("Панель закрыта.")


# ─── Бан ─────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "adm:ban")
async def cb_admin_ban(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return
    await state.set_state(AdminState.waiting_ban_input)
    await call.message.edit_text(BAN_HELP, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()


@dp.message(AdminState.waiting_ban_input)
async def process_ban_input(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    if msg.text and msg.text.startswith("/admin"):
        await state.clear()
        await msg.answer(admin_main_text(db), reply_markup=admin_main_kb(), parse_mode="HTML")
        return

    data = await state.get_data()
    prefill_name = data.get("prefill_name")

    if prefill_name:
        reason = (msg.text or "Нарушение правил").strip() or "Нарушение правил"
        args = {"name": prefill_name, "reason": reason}
    else:
        args = parse_panel_ban(msg.text or "", db)

    if not args.get("name") and not args.get("pfid") and not args.get("xuid"):
        await msg.answer("❌ Не удалось определить цель. Попробуй ещё раз.", parse_mode="HTML")
        return

    wait = await msg.answer("⏳ Баню...")
    ok, err = await mod_action("ban", db=db, watcher=watcher,
        name=args.get("name"),
        pfid=args.get("pfid"),
        xuid=args.get("xuid"),
        reason=args.get("reason", "Нарушение правил"),
        by=msg.from_user.username or msg.from_user.first_name,
    )
    await wait.delete()

    target_str = args.get("name") or args.get("pfid") or args.get("xuid") or "?"
    tg_note    = args.get("tg_note")

    if ok:
        # Подтягиваем pfid/xuid из watcher для полного досье
        _pfid = args.get("pfid")
        _xuid = args.get("xuid")
        _name = args.get("name")
        if _name and watcher and not _pfid:
            _pd = watcher.get_player(_name)
            if _pd:
                _pfid = _pfid or _pd.get("pfid")
                _xuid = _xuid or _pd.get("xuid")
        # TG-данные
        _tg_id = None; _tg_name = None
        if _name:
            _tgu = db.find_by_mc_name_any(_name)
            if _tgu:
                _tg_id   = str(_tgu.get("tg_id") or "")
                _tg_name = _tgu.get("tg_name", "")
        db.add_ban_log(
            target_str, args["reason"], str(msg.from_user.id),
            pfid=_pfid, xuid=_xuid, tg_id=_tg_id, tg_name=_tg_name,
        )
        note_line = f"\n📱 TG: {tg_note}" if tg_note else ""
        await msg.answer(
            f"🔨 <b>Игрок заблокирован!</b>\n\n"
            f"👤 Цель: <code>{target_str}</code>{note_line}\n"
            f"📝 Причина: {args['reason']}\n"
            f"👮 {msg.from_user.username or msg.from_user.first_name}",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
    else:
        await msg.answer(
            f"❌ Ошибка: <code>{err}</code>",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
    await state.clear()


# ─── Разбан ──────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "adm:unban")
async def cb_admin_unban(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return

    bans = db.get_ban_log()
    if not bans:
        await call.message.edit_text(
            "📋 <b>Лог банов пуст.</b>\n\nНечего разбанивать.",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
        await call.answer()
        return

    rows = []
    for i, b in enumerate(bans[:10]):
        t    = b.get("target", "?")[:16]
        pfid = b.get("pfid", "")
        pfid_short = pfid[:8] if pfid else "—"
        label = f"🔓 {t} | {pfid_short}"
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"adm:unban_card:{i}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")])

    await call.message.edit_text(
        "🔓 <b>Выбери игрока для разбана:</b>\n\nПоследние 10 забаненных.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:unban_card:"))
async def cb_unban_card(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return

    idx = int(call.data.split(":")[2])
    bans = db.get_ban_log()
    if idx >= len(bans):
        await call.answer("❌ Запись не найдена", show_alert=True)
        return

    b = bans[idx]
    target  = b.get("target", "?")
    pfid    = b.get("pfid")
    xuid    = b.get("xuid")
    tg_id   = b.get("tg_id")
    tg_name = b.get("tg_name")
    reason  = b.get("reason", "—")
    at      = b.get("at", "—")
    by      = b.get("by", "—")

    card = (
        f"📋 <b>Досье:</b>\n\n"
        f"👤 Ник: <code>{target}</code>\n"
        f"🔑 pfid: <code>{pfid or '—'}</code>\n"
        f"🆔 xuid: <code>{xuid or '—'}</code>\n"
        f"📱 TG: {('@' + tg_name) if tg_name else ('ID: ' + str(tg_id) if tg_id else '—')}\n\n"
        f"📝 Причина: {reason}\n"
        f"🕐 Дата: {at}  👮 {by}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разбанить", callback_data=f"adm:unban_do:{idx}")],
        [InlineKeyboardButton(text="◀️ Назад",    callback_data="adm:unban")],
    ])
    await call.message.edit_text(card, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data.startswith("adm:unban_do:"))
async def cb_unban_do(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return

    raw = call.data.split(":", 2)[2]
    # Поддержка нового формата (индекс) и старого (имя, на случай старых сообщений)
    bans = db.get_ban_log()
    b = None
    if raw.isdigit() and int(raw) < len(bans):
        b = bans[int(raw)]
    else:
        # fallback: ищем по target
        b = next((x for x in bans if x.get("target") == raw), None)

    if not b:
        await call.message.edit_text("❌ Запись не найдена.", reply_markup=back_kb(), parse_mode="HTML")
        await call.answer()
        return

    target = b.get("target")
    pfid   = b.get("pfid")
    xuid   = b.get("xuid")

    ok, err = await mod_action(
        "unban",
        watcher=watcher,
        name=target,
        pfid=pfid,
        xuid=xuid,
        by=call.from_user.username or call.from_user.first_name,
    )
    if ok:
        await call.message.edit_text(
            f"✅ <b>{target}</b> разблокирован!\n"
            f"🔑 pfid: <code>{pfid or '—'}</code>",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
    else:
        await call.message.edit_text(
            f"❌ Ошибка: <code>{err}</code>",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
    await call.answer()


# ─── Кик ─────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "adm:kick")
async def cb_admin_kick(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return
    await state.set_state(AdminState.waiting_kick_input)
    await call.message.edit_text(KICK_HELP, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()


@dp.message(AdminState.waiting_kick_input)
async def process_kick_input(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    if msg.text and msg.text.startswith("/admin"):
        await state.clear()
        await msg.answer(admin_main_text(db), reply_markup=admin_main_kb(), parse_mode="HTML")
        return

    parts  = (msg.text or "").split("|", 1)
    name   = parts[0].strip()
    reason = parts[1].strip() if len(parts) > 1 else "Кик администратором"

    ok, err = await mod_action(
        "kick",
        name=name,
        reason=reason,
        by=msg.from_user.username or msg.from_user.first_name,
    )
    if ok:
        await msg.answer(
            f"👢 <b>{name}</b> кикнут.\n📝 {reason}",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
    else:
        await msg.answer(
            f"❌ Ошибка: <code>{err}</code>",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
    await state.clear()


# ─── Мут ─────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "adm:mute")
async def cb_admin_mute(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return
    await state.set_state(AdminState.waiting_mute_input)
    await call.message.edit_text(MUTE_HELP, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()


@dp.message(AdminState.waiting_mute_input)
async def process_mute_input(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    if msg.text and msg.text.startswith("/admin"):
        await state.clear()
        await msg.answer(admin_main_text(db), reply_markup=admin_main_kb(), parse_mode="HTML")
        return

    parts  = (msg.text or "").split("|")
    name   = parts[0].strip() if len(parts) > 0 else ""
    dur    = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 60
    reason = parts[2].strip() if len(parts) > 2 else "Нарушение правил"

    ok, err = await mod_action(
        "mute",
        name=name,
        reason=reason,
        duration_min=dur,
        by=msg.from_user.username or msg.from_user.first_name,
    )
    if ok:
        await msg.answer(
            f"🔇 <b>{name}</b> заглушен на {dur} мин.\n📝 {reason}",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
    else:
        await msg.answer(
            f"❌ Ошибка: <code>{err}</code>",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
    await state.clear()


# ─── Поиск MC-игрока ─────────────────────────────────────────────────────────

@dp.callback_query(F.data == "adm:search_mc")
async def cb_admin_search_mc(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return
    await state.set_state(AdminState.waiting_search_mc)
    await call.message.edit_text(SEARCH_MC_HELP, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()


@dp.message(AdminState.waiting_search_mc)
async def process_search_mc(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    if msg.text and msg.text.startswith("/admin"):
        await state.clear()
        await msg.answer(admin_main_text(db), reply_markup=admin_main_kb(), parse_mode="HTML")
        return

    query = (msg.text or "").strip()
    wait = await msg.answer("⏳ Ищу игрока...")
    results = watcher.search_players(query)
    await wait.delete()

    if not results:
        await msg.answer(
            f"❌ Игрок <code>{query}</code> не найден.\n\n"
            "💡 Игрок должен зайти на сервер хотя бы раз после старта бота.",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if len(results) == 1:
        p = results[0]
        name = p.get("name", "?")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔨 Забанить", callback_data=f"adm:quickban:{name}")],
            [InlineKeyboardButton(text="◀️ Назад",    callback_data="adm:main")],
        ])
        await msg.answer(
            f"✅ <b>Найден игрок:</b>\n\n{format_player_card(p)}",
            reply_markup=kb,
            parse_mode="HTML"
        )
    else:
        lines = [f"🔎 Найдено <b>{len(results)}</b> игроков по запросу <code>{query}</code>:\n"]
        for p in results:
            lines.append(format_player_card(p))
        lines.append("\n💡 Уточни запрос для точного совпадения.")
        await msg.answer("\n".join(lines), reply_markup=back_kb(), parse_mode="HTML")
    await state.clear()


@dp.callback_query(F.data.startswith("adm:quickban:"))
async def cb_quickban(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return
    name = call.data.split(":", 2)[2]
    await state.set_state(AdminState.waiting_ban_input)
    await state.update_data(prefill_name=name)
    await call.message.edit_text(
        f"🔨 <b>Бан игрока</b> <code>{name}</code>\n\n"
        "Отправь причину бана:\n"
        "<code>Читы</code>\n"
        "<code>X-Ray</code>\n"
        "<code>Дюп</code>\n\n"
        "pfid и xuid подставятся автоматически.\n\n"
        "Отправь /admin чтобы отменить.",
        reply_markup=back_kb(),
        parse_mode="HTML"
    )
    await call.answer()


# ─── Поиск по TG ─────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "adm:search_tg")
async def cb_admin_search_tg(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return
    await state.set_state(AdminState.waiting_search_tg)
    await call.message.edit_text(SEARCH_HELP, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()


@dp.message(AdminState.waiting_search_tg)
async def process_search_tg(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    if msg.text and msg.text.startswith("/admin"):
        await state.clear()
        await msg.answer(admin_main_text(db), reply_markup=admin_main_kb(), parse_mode="HTML")
        return

    query = (msg.text or "").strip()
    result = None

    if query.isdigit():
        result = db.find_by_tg_id(query)
    else:
        result = db.find_by_tg_name(query.lstrip("@"))
        if not result:
            result = db.find_by_mc_name(query)

    await msg.answer(
        format_tg_search_result(result, query),
        reply_markup=back_kb(),
        parse_mode="HTML"
    )
    await state.clear()


# ─── Отвязка TG ──────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "adm:unlink")
async def cb_admin_unlink(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return
    await state.set_state(AdminState.waiting_unlink_input)
    await call.message.edit_text(UNLINK_HELP, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()


@dp.message(AdminState.waiting_unlink_input)
async def process_unlink_input(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    if msg.text and msg.text.startswith("/admin"):
        await state.clear()
        await msg.answer(admin_main_text(db), reply_markup=admin_main_kb(), parse_mode="HTML")
        return

    query = (msg.text or "").strip().lstrip("@")

    # Пробуем по TG ID
    if query.isdigit():
        ok, user = db.unlink_tg(query)
        if ok:
            mc = user.get("mc_name") or "—"
            tg = user.get("tg_name") or "—"
            await msg.answer(
                f"✅ <b>TG отвязан успешно.</b>\n\n"
                f"🆔 TG ID: <code>{query}</code>\n"
                f"📱 TG ник: {tg}\n"
                f"🎮 MC ник: <code>{mc}</code>\n\n"
                "Пользователь может пройти верификацию заново.",
                reply_markup=back_kb(),
                parse_mode="HTML"
            )
        else:
            # Возможно это MC-ник из цифр — маловероятно, но проверим
            await msg.answer(
                f"❌ Пользователь с TG ID <code>{query}</code> не найден в базе.",
                reply_markup=back_kb(),
                parse_mode="HTML"
            )
        await state.clear()
        return

    # Пробуем по TG-нику
    user_by_tg = db.find_by_tg_name(query)
    if user_by_tg:
        tg_id = user_by_tg.get("tg_id", "")
        ok, user = db.unlink_tg(tg_id)
        if ok:
            mc = user.get("mc_name") or "—"
            await msg.answer(
                f"✅ <b>TG отвязан успешно.</b>\n\n"
                f"📱 TG ник: @{query}\n"
                f"🎮 MC ник: <code>{mc}</code>\n\n"
                "Пользователь может пройти верификацию заново.",
                reply_markup=back_kb(),
                parse_mode="HTML"
            )
        else:
            await msg.answer("❌ Ошибка при удалении.", reply_markup=back_kb(), parse_mode="HTML")
        await state.clear()
        return

    # Пробуем по MC-нику
    ok, tg_id, user = db.unlink_tg_by_mc(query)
    if ok:
        tg = user.get("tg_name") or "—"
        await msg.answer(
            f"✅ <b>TG отвязан успешно.</b>\n\n"
            f"🎮 MC ник: <code>{query}</code>\n"
            f"📱 TG ник: {tg}\n"
            f"🆔 TG ID: <code>{tg_id}</code>\n\n"
            "Пользователь может пройти верификацию заново.",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
    else:
        await msg.answer(
            f"❌ Пользователь <code>{query}</code> не найден.\n\n"
            "Попробуй TG ID, @ник или MC-ник.",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
    await state.clear()


# ─── Лог банов ───────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "adm:banlog")
async def cb_admin_banlog(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return
    bans = db.get_ban_log()
    await call.message.edit_text(
        format_ban_log(bans, limit=10),
        reply_markup=back_kb(),
        parse_mode="HTML"
    )
    await call.answer()


# ─── Статистика ──────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "adm:stats")
async def cb_admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return
    stats   = db.get_stats()
    verified = db.get_all_verified()
    total   = stats.get("total", 0)
    ver_cnt = stats.get("verified", 0)

    recent = sorted(verified, key=lambda u: u.get("verified_at", ""), reverse=True)[:5]
    recent_lines = "\n".join(
        f"  • {u.get('tg_name','?')} → <code>{u.get('mc_name') or '—'}</code> ({u.get('verified_at','?')})"
        for u in recent
    ) or "  (нет)"

    text = (
        f"📊 <b>Статистика верификаций</b>\n\n"
        f"👤 Всего пользователей: <b>{total}</b>\n"
        f"✅ Верифицировано: <b>{ver_cnt}</b>\n"
        f"❌ Не верифицировано: <b>{total - ver_cnt}</b>\n\n"
        f"🕐 <b>Последние верификации:</b>\n{recent_lines}"
    )
    await call.message.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()


# ═══════════════════════════════════════════════════════════════════════════════
# ТЕКСТОВЫЕ КОМАНДЫ МОДЕРАЦИИ
# ═══════════════════════════════════════════════════════════════════════════════

@dp.message(Command("ban"))
async def cmd_ban(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer(
            "📋 <b>Использование:</b>\n"
            "/ban <code>Ник</code> <code>Причина</code>\n"
            "/ban <code>pfid:abc123</code> <code>Причина</code>\n"
            "/ban <code>xuid:253544</code> <code>Причина</code>",
            parse_mode="HTML"
        )
        return
    args = parse_mod_args(parts[1])
    wait = await msg.answer("⏳ Бан...")
    ok, err = await mod_action("ban", db=db, watcher=watcher,
        name=args.get("name"),
        pfid=args.get("pfid"),
        xuid=args.get("xuid"),
        reason=args.get("reason", "Нарушение правил"),
        by=msg.from_user.username or msg.from_user.first_name,
    )
    await wait.delete()
    target_str = args.get("name") or args.get("pfid") or args.get("xuid")
    if ok:
        _pfid2 = args.get("pfid")
        _xuid2 = args.get("xuid")
        _name2 = args.get("name")
        if _name2 and watcher and not _pfid2:
            _pd2 = watcher.get_player(_name2)
            if _pd2:
                _pfid2 = _pfid2 or _pd2.get("pfid")
                _xuid2 = _xuid2 or _pd2.get("xuid")
        _tg_id2 = None; _tg_name2 = None
        if _name2:
            _tgu2 = db.find_by_mc_name_any(_name2)
            if _tgu2:
                _tg_id2   = str(_tgu2.get("tg_id") or "")
                _tg_name2 = _tgu2.get("tg_name", "")
        db.add_ban_log(
            target_str, args.get("reason"), str(msg.from_user.id),
            pfid=_pfid2, xuid=_xuid2, tg_id=_tg_id2, tg_name=_tg_name2,
        )
        await msg.answer(
            f"🔨 <b>Заблокирован!</b>\n\n"
            f"👤 Цель: <code>{target_str}</code>\n"
            f"📝 Причина: {args.get('reason')}",
            parse_mode="HTML"
        )
    else:
        await msg.answer(f"❌ Ошибка: {err}")


@dp.message(Command("unban"))
async def cmd_unban(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("📋 /unban <code>Ник/pfid:/xuid:</code>", parse_mode="HTML")
        return
    args = parse_mod_args(parts[1])
    wait = await msg.answer("⏳ Разбан...")
    ok, err = await mod_action(
        "unban",
        name=args.get("name"),
        pfid=args.get("pfid"),
        xuid=args.get("xuid"),
        by=msg.from_user.username or msg.from_user.first_name,
    )
    await wait.delete()
    target_str = args.get("name") or args.get("pfid") or args.get("xuid")
    if ok:
        await msg.answer(f"✅ <b>{target_str}</b> разблокирован!", parse_mode="HTML")
    else:
        await msg.answer(f"❌ Ошибка: {err}")


@dp.message(Command("kick"))
async def cmd_kick(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("📋 /kick <code>Ник</code> <code>Причина</code>", parse_mode="HTML")
        return
    args = parse_mod_args(parts[1])
    ok, err = await mod_action(
        "kick",
        name=args.get("name"),
        reason=args.get("reason", "Кик администратором"),
        by=msg.from_user.username or msg.from_user.first_name,
    )
    if ok:
        await msg.answer(f"👢 <b>{args.get('name','?')}</b> кикнут.", parse_mode="HTML")
    else:
        await msg.answer(f"❌ Ошибка: {err}")


@dp.message(Command("mute"))
async def cmd_mute(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer(
            "📋 /mute <code>Ник</code> <code>минуты</code> <code>Причина</code>",
            parse_mode="HTML"
        )
        return
    args = parse_mod_args(parts[1])
    ok, err = await mod_action(
        "mute",
        name=args.get("name"),
        reason=args.get("reason", "Нарушение правил"),
        duration_min=args.get("duration_min", 60),
        by=msg.from_user.username or msg.from_user.first_name,
    )
    if ok:
        await msg.answer(
            f"🔇 <b>{args.get('name','?')}</b> заглушен на {args.get('duration_min', 60)} мин.",
            parse_mode="HTML"
        )
    else:
        await msg.answer(f"❌ Ошибка: {err}")


# ─── Неизвестные сообщения ───────────────────────────────────────────────────

@dp.message()
async def unknown(msg: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        return
    uid = str(msg.from_user.id)
    data = db.get_user(uid)
    is_verified = bool(data and data.get("verified"))
    hint = " Или /admin для панели управления." if is_admin(msg.from_user.id) else ""
    await msg.answer(
        f"❓ Используйте меню ниже для верификации.{hint}",
        reply_markup=main_menu_kb(is_verified),
        parse_mode="HTML"
    )


# ─── Запуск ──────────────────────────────────────────────────────────────────

async def main():
    logger.info("Bot starting...")
    # AddonBridge — HTTP-сервер для связи с аддоном (заменяет ptero_ws + Pterodactyl)
    bridge.set_db(db)
    asyncio.create_task(bridge.run())
    logger.info("[bot] AddonBridge запущен")
    # ptero_ws watcher оставляем для обратной совместимости (модерация, логи консоли)
    watcher.set_bot(bot)
    asyncio.create_task(watcher.run())
    logger.info("[bot] ptero_ws watcher запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
