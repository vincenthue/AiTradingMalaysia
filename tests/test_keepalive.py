from bot.db import DatabaseUnavailableError, InMemoryDB
from scripts.keepalive import run
from tests.test_main import Quiet


def test_keepalive_ok():
    assert run(InMemoryDB(10000), Quiet()) == 0


def test_keepalive_alerts_when_db_down():
    class Down(InMemoryDB):
        def ping(self):
            raise DatabaseUnavailableError("down")

    n = Quiet()
    assert run(Down(10000), n) == 1
    assert "Resume" in n.sent[0]
