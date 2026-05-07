"""
addon_bridge.py — HTTP-мост между аддоном (BDS Script API) и ботом
===================================================================

Заменяет ptero_ws.py. Больше не нужен Pterodactyl API.

Аддон (server-net) сам стучится сюда:
  POST /api/player_join   — игрок зашёл, отдаёт {name, xuid}
  GET  /api/pending       — аддон забирает очередь команд (verify, ban, kick, ...)
  POST /api/verify_result — аддон отвечает результатом верификации
  POST /api/command_ack   — аддон подтверждает выполнение команды модерации

Бот добавляет команды через:
  bridge.enqueue_verify(tg_id, code, tg_username)
  bridge.enqueue_command(cmd_dict)   ← для ban/kick/mute

Запускается как asyncio task рядом с aiogram polling.
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from aiohttp import web

logger = logging.getLogger(__name__)

BRIDGE_SECRET  = os.getenv("BRIDGE_SECRET", "changeme")
BRIDGE_PORT    = int(os.getenv("BRIDGE_PORT", "8765"))

# Максимальное время жизни pending-команды (секунд).
# Если аддон не забрал за это время — считаем что сервер упал.
CMD_TTL = 120


class AddonBridge:
    """
    HTTP-сервер для связи с аддоном.
    Хранит:
      _verify_queue  — очередь запросов верификации ожидающих аддон
      _cmd_queue     — очередь команд модерации (ban/kick/mute/unban)
      _known_players — {name: {name, xuid, seen_at}}  (аналог pfids.json)
    """

    def __init__(self, db=None, bot=None):
        self.db  = db
        self.bot = bot

        self._verify_queue: deque  = deque()   # [{tg_id, code, tg_username, enqueued_at}]
        self._cmd_queue: deque     = deque()   # [{id, action, ...payload, enqueued_at}]
        self._known_players: dict  = {}        # {name_lower: {name, xuid, seen_at}}
        self._cmd_id_counter       = 0
        self._runner               = None
        self._site                 = None

    def set_db(self, db):
        self.db = db

    def set_bot(self, bot):
        self.bot = bot

    # ─── Публичный API для бота ───────────────────────────────────────────────

    def enqueue_verify(self, tg_id: str, code: str, tg_username: str):
        """Ставит запрос верификации в очередь для аддона."""
        # Удаляем старый pending от того же tg_id если есть
        self._verify_queue = deque(
            item for item in self._verify_queue if item["tg_id"] != tg_id
        )
        self._verify_queue.append({
            "tg_id":       tg_id,
            "code":        code.upper(),
            "tg_username": tg_username,
            "enqueued_at": time.time(),
        })
        logger.info(f"[bridge] enqueue_verify tg_id={tg_id} code={code}")

    def enqueue_command(self, action: str, **payload) -> int:
        """
        Ставит команду модерации в очередь.
        Возвращает cmd_id для отслеживания.
        """
        self._cmd_id_counter += 1
        cmd_id = self._cmd_id_counter
        self._cmd_queue.append({
            "id":          cmd_id,
            "action":      action,
            "enqueued_at": time.time(),
            **payload,
        })
        logger.info(f"[bridge] enqueue_command id={cmd_id} action={action} payload={payload}")
        return cmd_id

    def get_player(self, name: str) -> dict | None:
        """Возвращает данные игрока по нику (аналог watcher.get_player)."""
        return self._known_players.get(name.lower())

    def search_players(self, query: str) -> list[dict]:
        """Поиск игроков по подстроке ника (аналог watcher.search_players)."""
        q = query.lower()
        return [v for k, v in self._known_players.items() if q in k][:10]

    @property
    def online(self) -> set[str]:
        """Возвращает набор ников которые заходили (аналог watcher.online)."""
        # bridge не отслеживает онлайн в реальном времени —
        # возвращаем всех кто заходил за последние 10 минут
        cutoff = time.time() - 600
        return {v["name"] for v in self._known_players.values()
                if v.get("seen_at", 0) > cutoff}

    # ─── Обработчики HTTP ─────────────────────────────────────────────────────

    def _check_secret(self, request: web.Request) -> bool:
        return request.headers.get("X-Secret") == BRIDGE_SECRET

    async def _handle_player_join(self, request: web.Request) -> web.Response:
        if not self._check_secret(request):
            return web.Response(status=403, text="Forbidden")
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Bad JSON")

        name = (data.get("name") or "").strip()
        xuid = str(data.get("xuid") or "").strip()

        if not name:
            return web.Response(status=400, text="name required")

        entry = {
            "name":    name,
            "xuid":    xuid,
            "seen_at": time.time(),
        }
        self._known_players[name.lower()] = entry
        logger.info(f"[bridge] player_join name={name} xuid={xuid}")

        # Обновляем MC-ник в базе верификации если есть совпадение по xuid
        if self.db and xuid:
            self._update_mc_name_in_db(name, xuid)

        # Проверяем бан при входе
        if self.db:
            ban_cmd = self._check_ban_on_join(name, xuid)
            if ban_cmd:
                self._cmd_queue.append(ban_cmd)

        return web.json_response({"ok": True})

    async def _handle_pending(self, request: web.Request) -> web.Response:
        """
        GET /api/pending — аддон забирает всё что накопилось.
        Возвращает список {type, ...payload}.
        Устаревшие команды отбрасываются.
        """
        if not self._check_secret(request):
            return web.Response(status=403, text="Forbidden")

        now = time.time()
        result = []

        # Verify-запросы
        fresh_verify = deque()
        while self._verify_queue:
            item = self._verify_queue.popleft()
            if now - item["enqueued_at"] <= CMD_TTL:
                result.append({
                    "type":        "verify",
                    "tg_id":       item["tg_id"],
                    "code":        item["code"],
                    "tg_username": item["tg_username"],
                })
            else:
                logger.warning(f"[bridge] verify expired tg_id={item['tg_id']}")
                await self._notify_expired_verify(item)
        # verify_queue уже очищена (popleft), fresh_verify не нужен

        # Команды модерации
        while self._cmd_queue:
            cmd = self._cmd_queue.popleft()
            if now - cmd["enqueued_at"] <= CMD_TTL:
                result.append({"type": "command", **cmd})
            else:
                logger.warning(f"[bridge] command expired id={cmd['id']} action={cmd['action']}")

        return web.json_response(result)

    async def _handle_verify_result(self, request: web.Request) -> web.Response:
        """
        POST /api/verify_result
        Body: {ok, tg_id, mc_name, code?}
        """
        if not self._check_secret(request):
            return web.Response(status=403, text="Forbidden")
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Bad JSON")

        ok      = bool(data.get("ok"))
        tg_id   = str(data.get("tg_id") or "")
        mc_name = data.get("mc_name") or ""
        code    = (data.get("code") or "").upper()

        logger.info(f"[bridge] verify_result ok={ok} tg_id={tg_id} mc_name={mc_name}")

        if not self.db:
            return web.json_response({"ok": True})

        if ok and tg_id and mc_name:
            user = self.db.find_by_tg_id(tg_id)
            if user:
                self.db.confirm_verified(tg_id, mc_name)
                c = user.get("code", "")
                if c:
                    self.db.mark_code_used(c)
                logger.info(f"[bridge] confirmed tg_id={tg_id} mc_name={mc_name}")
                if self.bot:
                    try:
                        await self.bot.send_message(
                            chat_id=int(tg_id),
                            text=(
                                f"✅ <b>Верификация пройдена!</b>\n\n"
                                f"🎮 MC-ник: <code>{mc_name}</code>\n\n"
                                f"Вы получили:\n"
                                f"• Титул <b>«Гражданин»</b>\n"
                                f"• <b>+200 T</b> на баланс\n"
                                f"• <b>+10 Trust Score</b>"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.warning(f"[bridge] notify verify ok failed: {e}")
        else:
            # Аддон не нашёл код — откатываем
            if tg_id:
                self.db.unlink_tg(tg_id)
            if self.bot and tg_id:
                try:
                    await self.bot.send_message(
                        chat_id=int(tg_id),
                        text=(
                            "❌ <b>Верификация не пройдена.</b>\n\n"
                            + (f"Код <code>{code}</code> не найден или истёк на сервере.\n\n" if code else "")
                            + "Получите новый код командой <code>.econ verify</code> в игре и попробуйте снова."
                        ),
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.warning(f"[bridge] notify verify fail failed: {e}")

        return web.json_response({"ok": True})

    async def _handle_command_ack(self, request: web.Request) -> web.Response:
        """
        POST /api/command_ack
        Body: {id, ok, error?}
        Аддон подтверждает выполнение команды модерации.
        """
        if not self._check_secret(request):
            return web.Response(status=403, text="Forbidden")
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Bad JSON")

        cmd_id = data.get("id")
        ok     = data.get("ok", False)
        error  = data.get("error", "")
        logger.info(f"[bridge] command_ack id={cmd_id} ok={ok} error={error}")
        return web.json_response({"ok": True})

    # ─── Вспомогательные методы ───────────────────────────────────────────────

    def _update_mc_name_in_db(self, name: str, xuid: str):
        """Обновляет MC-ник в базе если нашли запись с этим xuid."""
        for uid, user in self.db._data["users"].items():
            stored_xuid = user.get("xuid") or ""
            if stored_xuid and stored_xuid == xuid:
                old_mc = user.get("mc_name")
                if old_mc != name:
                    logger.info(f"[bridge] update mc_name {old_mc} → {name} xuid={xuid}")
                    self.db.set_mc_name(uid, name)
                return
            # Матч по нику если xuid ещё не записан
            if (user.get("mc_name") or "").lower() == name.lower():
                if not stored_xuid:
                    user["xuid"] = xuid
                    self.db._save()
                return

    def _check_ban_on_join(self, name: str, xuid: str) -> dict | None:
        """
        Проверяет при входе игрока — есть ли он в бан-логе по нику или xuid.
        Если да — возвращает команду кика/бана для аддона.
        """
        bans = self.db.get_ban_log()
        for b in bans:
            match = (
                (b.get("target") or "").lower() == name.lower() or
                (xuid and b.get("xuid") == xuid)
            )
            if match:
                logger.info(f"[bridge] banned player joined: {name} xuid={xuid}")
                return {
                    "id":          self._cmd_id_counter + 1,
                    "action":      "ban",
                    "name":        name,
                    "xuid":        xuid,
                    "reason":      b.get("reason", "Вы заблокированы."),
                    "appeal_url":  os.getenv("APPEAL_URL", "@zerkavich"),
                    "enqueued_at": time.time(),
                }
        return None

    async def _notify_expired_verify(self, item: dict):
        """Уведомляет пользователя что его запрос верификации истёк."""
        if not self.bot:
            return
        tg_id = item.get("tg_id")
        if not tg_id:
            return
        if self.db:
            self.db.unlink_tg(tg_id)
        try:
            await self.bot.send_message(
                chat_id=int(tg_id),
                text=(
                    "⏰ <b>Запрос верификации истёк.</b>\n\n"
                    "Сервер не ответил вовремя. Возможно, он временно недоступен.\n"
                    "Попробуйте снова командой <code>.econ verify</code> в игре."
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"[bridge] notify expired failed tg_id={tg_id}: {e}")

    # ─── Запуск / остановка ───────────────────────────────────────────────────

    async def run(self):
        """Запускается как asyncio task."""
        app = web.Application()
        app.router.add_post("/api/player_join",    self._handle_player_join)
        app.router.add_get( "/api/pending",        self._handle_pending)
        app.router.add_post("/api/verify_result",  self._handle_verify_result)
        app.router.add_post("/api/command_ack",    self._handle_command_ack)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", BRIDGE_PORT)
        await self._site.start()
        logger.info(f"[bridge] HTTP сервер запущен на порту {BRIDGE_PORT}")

        # Держим task живым
        while True:
            await asyncio.sleep(3600)

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
