"""保活：对 Supabase 做一次简单查询。连不上就发 Telegram 警报并以非零状态结束。

用法：python -m scripts.keepalive
"""
from __future__ import annotations

import logging
import sys

from bot.config import ROOT_DIR, MissingEnvError, check_env
from bot.db import DatabaseError, SupabaseDB
from bot.notifier import TelegramNotifier, format_alert
from bot.safety import install_log_redaction, redact

log = logging.getLogger("keepalive")


def run(db, notifier) -> int:
    try:
        db.ping()
    except DatabaseError as e:
        notifier.send(format_alert(
            "保活：数据库连不上", str(e),
            "到 Supabase Dashboard 打开项目，如果显示 Paused 就点 Resume",
        ))
        return 1
    print("Supabase OK")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    install_log_redaction()
    from dotenv import load_dotenv

    load_dotenv(ROOT_DIR / ".env", override=False)
    try:
        env = check_env()
    except MissingEnvError as e:
        print(redact(str(e)))
        return 1
    db = SupabaseDB(env["SUPABASE_URL"], env["SUPABASE_SERVICE_KEY"])
    notifier = TelegramNotifier(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])
    return run(db, notifier)


if __name__ == "__main__":
    sys.exit(main())
