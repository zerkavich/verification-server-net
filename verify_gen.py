"""
verify_gen.py — генератор и валидатор токенов верификации.
Подключается к bot.py: from verify_gen import generate_token

Алгоритм идентичен tg_verify.js (JS-сторона):
  token = BASE36(tg_id) + "_" + BASE36(djb2(SECRET + str(tg_id) + str(utc_day))).zfill(6)
Токен живёт сутки (принимаются сегодня и вчера).
"""

import time

# ─── ХАРДКОД СЕКРЕТА — совпадает с tg_verify.js ──────────────────────────────
SECRET = "zerk_hmac_2025_secret"

# ─── DJB2 ────────────────────────────────────────────────────────────────────

def djb2(s: str) -> int:
    h = 5381
    for c in s.encode("utf-8"):
        h = ((h << 5) + h) ^ c
        h &= 0xFFFFFFFF          # 32-bit unsigned, как в JS (>>> 0)
    return h

# ─── BASE36 ──────────────────────────────────────────────────────────────────

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"

def to_base36(n: int) -> str:
    if n == 0:
        return "0"
    s = ""
    while n:
        s = _B36[n % 36] + s
        n //= 36
    return s

def from_base36(s: str) -> int:
    return int(s, 36)

# ─── UTC ДЕНЬ ─────────────────────────────────────────────────────────────────

def utc_day() -> int:
    return int(time.time() // 86400)

# ─── ПУБЛИЧНЫЙ API ────────────────────────────────────────────────────────────

def generate_token(tg_id: int | str) -> str:
    """Генерирует токен для данного tg_id. Вызывать по команде /token."""
    tg_id = str(tg_id)
    day   = utc_day()
    h     = djb2(SECRET + tg_id + str(day))
    return to_base36(int(tg_id)) + "_" + to_base36(h).zfill(6)

def verify_token(token: str) -> dict:
    """
    Проверяет токен. Принимает сегодня и вчера.
    Возвращает {"ok": True, "tg_id": str} или {"ok": False}.
    """
    token = token.strip().lower()
    parts = token.split("_")
    if len(parts) != 2:
        return {"ok": False}
    id_part, hash_part = parts
    try:
        tg_id_num = from_base36(id_part)
        if tg_id_num <= 0:
            return {"ok": False}
    except Exception:
        return {"ok": False}

    tg_id = str(tg_id_num)
    day   = utc_day()
    for d in (day, day - 1):
        expected = to_base36(djb2(SECRET + tg_id + str(d))).zfill(6)
        if expected == hash_part:
            return {"ok": True, "tg_id": tg_id}
    return {"ok": False}


# ─── БЫСТРЫЙ ТЕСТ ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_id = 123456789
    tok = generate_token(test_id)
    print(f"Token for {test_id}: {tok}")
    result = verify_token(tok)
    print(f"Verify result: {result}")
    assert result["ok"] and result["tg_id"] == str(test_id), "FAIL"
    print("Self-test OK")
