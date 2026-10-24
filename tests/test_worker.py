import datetime

from taskiq import TaskiqMessage

from bot.worker.broker import RetryMiddleware, schedule_source


def test_parse_time_key_with_colon_prefix() -> None:
    key = "ticket-bot:schedule:time:2026-10-24T05:38"
    assert schedule_source._parse_time_key(key) == datetime.datetime(
        2026, 10, 24, 5, 38, tzinfo=datetime.UTC
    )


def test_retry_delay_is_exponential() -> None:
    middleware = RetryMiddleware(default_delay=60, max_delay_exponent=600)
    message = TaskiqMessage(task_id="1", task_name="t", labels={}, args=[], kwargs={})
    delays = [middleware.make_delay(message, retries) for retries in range(1, 6)]
    assert delays == [60, 120, 240, 480, 600]
