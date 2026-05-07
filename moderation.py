"""
moderation.py — команды модерации через addon_bridge
=====================================================

send_server_command(cmd_str) больше не существует.
Используется bridge.enqueue_command(action, **payload).

Функция mod_action принимает bridge вместо watcher.
"""

import os
import logging

logger = logging.getLogger(__name__)

ADMIN_IDS_RAW = os.getenv("ADMIN_TG_IDS", "")
APPEAL_URL    = os.getenv("APPEAL_URL", "@zerkavich")


def get_admin_ids() -> set[int]:
    ids = set()
    for part in ADMIN_IDS_RAW.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def is_admin(tg_id: int) -> bool:
    admins = get_admin_ids()
    if not admins:
        return False
    return tg_id in admins


async def mod_action(action: str, db=None, bridge=None, **kwargs) -> tuple[bool, str]:
    """
    Ставит команду модерации в очередь для аддона через bridge.

    Автоподстановки:
    - xuid по нику через bridge.get_player(name)
    - tgId/tgNick по MC-нику из базы верификации (db)
    """
    name  = kwargs.get("name")
    tg_id = kwargs.get("tg_id")

    # 1. Автоподстановка xuid через bridge
    if name and bridge and not kwargs.get("xuid"):
        player_data = bridge.get_player(name)
        if player_data:
            if player_data.get("xuid"):
                kwargs["xuid"] = player_data["xuid"]
                logger.info(f"[mod] xuid={kwargs['xuid']} для {name}")

    # 1b. Если name не задан, но есть xuid — ищем ник через known_players
    xuid_arg = kwargs.get("xuid")
    if not name and bridge and xuid_arg:
        for entry in bridge._known_players.values():
            if entry.get("xuid") == xuid_arg:
                kwargs["name"] = entry["name"]
                name = entry["name"]
                logger.info(f"[mod] name={name} resolved from xuid={xuid_arg}")
                break

    # 2. Если бан — подтягиваем TG-данные по MC-нику из базы
    if action == "ban" and db is not None:
        if name:
            tg_user = db.find_by_mc_name_any(name)
            if tg_user:
                if not kwargs.get("tgId") and tg_user.get("tg_id"):
                    kwargs["tgId"]  = tg_user["tg_id"]
                    logger.info(f"[mod] tgId={kwargs['tgId']} для {name}")
                if not kwargs.get("tgNick") and tg_user.get("tg_name"):
                    kwargs["tgNick"] = tg_user["tg_name"].lstrip("@")
                    logger.info(f"[mod] tgNick={kwargs['tgNick']} для {name}")
        # Если бан по tg_id — подтягиваем ник и xuid
        if tg_id and not name and db and bridge:
            all_names = db.get_all_mc_names_for_tg(str(tg_id))
            if all_names:
                last = all_names[-1]
                if not kwargs.get("name"):
                    kwargs["name"] = last
                pdata = bridge.get_player(last)
                if pdata:
                    if pdata.get("xuid") and not kwargs.get("xuid"):
                        kwargs["xuid"] = pdata["xuid"]

    if bridge is None:
        return False, "bridge не инициализирован"

    kwargs["appeal_url"] = APPEAL_URL

    try:
        bridge.enqueue_command(action, **kwargs)
        return True, "OK"
    except Exception as e:
        logger.error(f"[mod] enqueue_command error: {e}")
        return False, str(e)


def parse_mod_args(text: str) -> dict:
    """
    Парсит строку вида:
      Steve Читы
      xuid:253544 Читы
      Steve 60 Спам   (для мута)
    """
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return {}

    target_raw = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    result = {}

    if target_raw.startswith("xuid:"):
        result["xuid"] = target_raw[5:]
    else:
        result["name"] = target_raw

    rest_parts = rest.split(maxsplit=1)
    if rest_parts and rest_parts[0].isdigit():
        result["duration_min"] = int(rest_parts[0])
        result["reason"] = rest_parts[1] if len(rest_parts) > 1 else "Нарушение правил"
    else:
        result["reason"] = rest if rest else "Нарушение правил"

    return result
