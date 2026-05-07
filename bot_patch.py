"""
Вставить в bot.py:
  1. В импорты сверху добавить:
       from verify_gen import generate_token

  2. Добавить этот хендлер (например после cmd_status):
"""

# ─── /token — выдаёт токен верификации ───────────────────────────────────────

@dp.message(Command("token"))
async def cmd_token(msg: Message):
    uid  = str(msg.from_user.id)
    data = db.get_user(uid)

    if data and data.get("verified"):
        await msg.answer(
            "✅ <b>Вы уже верифицированы!</b>\n\n"
            "Повторная верификация не нужна.",
            parse_mode="HTML"
        )
        return

    if not await check_subscription(msg.from_user.id):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📢 Подписаться",
                                 url=f"https://t.me/{TG_CHANNEL.lstrip('@')}")
        ]])
        await msg.answer(
            f"⚠️ Для верификации сначала подпишитесь на {TG_CHANNEL}",
            reply_markup=kb
        )
        return

    token = generate_token(msg.from_user.id)
    await msg.answer(
        f"🔑 <b>Ваш токен верификации:</b>\n\n"
        f"<code>{token}</code>\n\n"
        f"В игре введите:\n"
        f"<code>.verify {token}</code>\n\n"
        f"⚠️ Токен действителен <b>24 часа</b>.\n"
        f"Токен привязан к вашему Telegram ID — никому не передавайте.",
        parse_mode="HTML"
    )


# ─── Обновить main_menu_kb — добавить кнопку «Получить токен» ────────────────
#
# В существующей функции main_menu_kb заменить строку с "✅ Верифицироваться":
#
#   [InlineKeyboardButton(text="✅ Верифицироваться", callback_data="menu:verify")],
#
# на две кнопки:
#
#   [InlineKeyboardButton(text="🔑 Получить токен",    callback_data="menu:token")],
#   [InlineKeyboardButton(text="❓ Как верифицироваться", callback_data="menu:help")],
#
# И добавить callback:

@dp.callback_query(F.data == "menu:token")
async def cb_menu_token(call: CallbackQuery):
    uid  = str(call.from_user.id)
    data = db.get_user(uid)
    if data and data.get("verified"):
        await call.answer("✅ Вы уже верифицированы!", show_alert=True)
        return

    if not await check_subscription(call.from_user.id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться",
                                  url=f"https://t.me/{TG_CHANNEL.lstrip('@')}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")],
        ])
        await call.message.edit_text(
            f"⚠️ Для верификации сначала подпишитесь на {TG_CHANNEL}",
            reply_markup=kb,
        )
        await call.answer()
        return

    token = generate_token(call.from_user.id)
    await call.message.edit_text(
        f"🔑 <b>Ваш токен верификации:</b>\n\n"
        f"<code>{token}</code>\n\n"
        f"В игре введите:\n"
        f"<code>.verify {token}</code>\n\n"
        f"⚠️ Токен действителен <b>24 часа</b>.\n"
        f"Токен привязан к вашему Telegram ID — никому не передавайте.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back"),
        ]]),
        parse_mode="HTML"
    )
    await call.answer()
