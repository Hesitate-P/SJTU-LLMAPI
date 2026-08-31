import pytest

from app.failover import Breaker, ErrorKind, classify_status


@pytest.mark.parametrize("status,expected", [
    (200, ErrorKind.OK), (400, ErrorKind.CLIENT), (404, ErrorKind.CLIENT),
    (429, ErrorKind.RATE_LIMIT), (402, ErrorKind.QUOTA),
    (500, ErrorKind.SERVER), (503, ErrorKind.SERVER),
])
def test_classify_by_status(status, expected):
    assert classify_status(status) is expected


@pytest.mark.parametrize("body", ["quota exceeded", "insufficient balance", "额度已用尽", "配额不足"])
def test_429_with_quota_body_is_quota(body):
    assert classify_status(429, body) is ErrorKind.QUOTA


def test_breaker_opens_and_expires():
    breaker = Breaker(cooldown_429=60.0)
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=100.0)
    assert breaker.is_open("sjtu", now=100.0) is True
    assert breaker.is_open("sjtu", now=159.9) is True
    assert breaker.is_open("sjtu", now=160.1) is False  # 到期放行（半开）


def test_cooldown_durations_by_kind():
    breaker = Breaker(cooldown_429=60.0, cooldown_quota=1800.0, cooldown_network=15.0)
    assert breaker.record_failure("a", ErrorKind.RATE_LIMIT, now=0) == 60
    assert breaker.record_failure("b", ErrorKind.QUOTA, now=0) == 1800
    assert breaker.record_failure("c", ErrorKind.NETWORK, now=0) == 15
    assert breaker.record_failure("d", ErrorKind.SERVER, now=0) == 15


def test_client_error_does_not_open():
    breaker = Breaker()
    breaker.record_failure("sjtu", ErrorKind.CLIENT, now=0)
    assert breaker.is_open("sjtu", now=0) is False


def test_half_open_failure_reopens():
    breaker = Breaker(cooldown_429=10.0)
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=0)
    assert breaker.is_open("sjtu", now=11) is False
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=11)
    assert breaker.is_open("sjtu", now=20) is True


def test_remaining():
    breaker = Breaker(cooldown_429=60.0)
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=100.0)
    assert breaker.cooldown_remaining("sjtu", now=130.0) == pytest.approx(30.0)
