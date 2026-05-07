import json
import os
from datetime import datetime


class Database:
    def __init__(self, path: str = "data.json"):
        self.path = path
        self._data = {"users": {}, "used_codes": []}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                pass

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def get_user(self, tg_id: str) -> dict | None:
        return self._data["users"].get(str(tg_id))

    def save_pending(self, tg_id: str, code: str, tg_name: str):
        """Сохраняет pending-запись верификации. verified=False до подтверждения от аддона."""
        self._data["users"][str(tg_id)] = {
            "verified": False,   # ← False! ptero_ws выставит True после ok=True от аддона
            "code": code,
            "tg_name": tg_name,
            "tg_id": str(tg_id),
            "mc_name": None,
            "verified_at": None,
        }
        self._save()

    def mark_verified(self, tg_id: str, code: str, tg_name: str):
        """Устарело — используйте save_pending(). Оставлено для совместимости."""
        self.save_pending(tg_id, code, tg_name)

    def confirm_verified(self, tg_id: str, mc_name: str):
        """Вызывается ptero_ws после ok=True от аддона. Выставляет verified=True и mc_name."""
        uid = str(tg_id)
        user = self._data["users"].get(uid)
        if user:
            user["verified"] = True
            user["mc_name"] = mc_name
            user["verified_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            hist = user.setdefault("mc_names_history", [])
            if mc_name and mc_name not in hist:
                hist.append(mc_name)
            self._save()

    def set_mc_name(self, tg_id: str, mc_name: str):
        uid = str(tg_id)
        if uid in self._data["users"]:
            user = self._data["users"][uid]
            user["mc_name"] = mc_name
            hist = user.setdefault("mc_names_history", [])
            if mc_name and mc_name not in hist:
                hist.append(mc_name)
            self._save()

    def unlink_tg(self, tg_id: str) -> tuple[bool, dict | None]:
        """
        Отвязывает TG-аккаунт. Удаляет запись, освобождает код.
        Возвращает (True, user_dict) если найден, (False, None) иначе.
        """
        uid = str(tg_id)
        user = self._data["users"].get(uid)
        if not user:
            return False, None
        code = user.get("code", "").upper()
        if code and code in self._data.get("used_codes", []):
            self._data["used_codes"].remove(code)
        del self._data["users"][uid]
        self._save()
        return True, user

    def unlink_tg_by_mc(self, mc_name: str) -> tuple[bool, str | None, dict | None]:
        """
        Отвязывает TG по MC-нику. Возвращает (успех, tg_id, user_dict).
        """
        needle = mc_name.lower()
        for uid, user in list(self._data["users"].items()):
            if (user.get("mc_name") or "").lower() == needle:
                code = user.get("code", "").upper()
                if code and code in self._data.get("used_codes", []):
                    self._data["used_codes"].remove(code)
                del self._data["users"][uid]
                self._save()
                return True, uid, user
        return False, None, None

    def is_code_used(self, code: str) -> bool:
        return code.upper() in self._data.get("used_codes", [])

    def mark_code_used(self, code: str):
        codes = self._data.setdefault("used_codes", [])
        if code.upper() not in codes:
            codes.append(code.upper())
        self._save()

    def get_stats(self) -> dict:
        total    = len(self._data["users"])
        verified = sum(1 for u in self._data["users"].values() if u.get("verified"))
        return {"total": total, "verified": verified}

    def add_ban_log(self, target: str, reason: str, by_tg_id: str,
                    pfid: str | None = None, xuid: str | None = None,
                    tg_id: str | None = None, tg_name: str | None = None):
        bans = self._data.setdefault("ban_log", [])
        entry = {
            "target": target,
            "reason": reason,
            "by":     by_tg_id,
            "at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        if pfid:    entry["pfid"]    = pfid
        if xuid:    entry["xuid"]    = xuid
        if tg_id:   entry["tg_id"]   = tg_id
        if tg_name: entry["tg_name"] = tg_name
        bans.insert(0, entry)
        if len(bans) > 500:
            bans[:] = bans[:500]
        self._save()

    def get_ban_log(self) -> list:
        return self._data.get("ban_log", [])

    def find_by_tg_id(self, tg_id: str) -> dict | None:
        return self._data["users"].get(str(tg_id))

    def find_by_tg_name(self, tg_name: str) -> dict | None:
        needle = tg_name.lstrip("@").lower()
        for user in self._data["users"].values():
            stored = user.get("tg_name", "").lstrip("@").lower()
            if stored == needle:
                return user
        return None

    def find_by_mc_name(self, mc_name: str) -> dict | None:
        needle = mc_name.lower()
        for user in self._data["users"].values():
            if (user.get("mc_name") or "").lower() == needle:
                return user
        return None

    def find_by_mc_name_any(self, mc_name: str) -> dict | None:
        needle = mc_name.lower()
        for user in self._data["users"].values():
            if (user.get("mc_name") or "").lower() == needle:
                return user
            for hist_name in user.get("mc_names_history", []):
                if hist_name.lower() == needle:
                    return user
        return None

    def find_pending_by_code(self, code: str) -> tuple[str | None, dict | None]:
        """Ищет pending-запись (verified=False) по коду. Возвращает (tg_id, user) или (None, None)."""
        code = code.upper()
        for uid, user in self._data["users"].items():
            if user.get("code", "").upper() == code and not user.get("verified"):
                return uid, user
        return None, None

    def get_all_mc_names_for_tg(self, tg_id: str) -> list[str]:
        user = self.find_by_tg_id(tg_id)
        if not user:
            return []
        names = list(user.get("mc_names_history", []))
        cur = user.get("mc_name")
        if cur and cur not in names:
            names.append(cur)
        return names

    def get_all_verified(self) -> list[dict]:
        return [u for u in self._data["users"].values() if u.get("verified")]
