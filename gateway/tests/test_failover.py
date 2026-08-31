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


def test_short_cooldown_inside_long_window_returns_effective_deadline():
    breaker = Breaker()
    assert breaker.record_failure("p", ErrorKind.QUOTA, now=100.0) == 1900.0
    assert breaker.record_failure("p", ErrorKind.RATE_LIMIT, now=200.0) == 1900.0  # 不是 260.0
    assert breaker.cooldown_remaining("p", now=200.0) == 1700.0


def test_acquire_probe_rejects_during_cooldown():
    breaker = Breaker(cooldown_429=10.0)
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=0)
    assert breaker.acquire_probe("sjtu", now=5) is False


def test_acquire_probe_never_opened_allows_all():
    breaker = Breaker()
    assert breaker.acquire_probe("sjtu", now=1) is True
    assert breaker.acquire_probe("sjtu", now=1) is True  # 从未打开：不设探测标志，全放行
    assert breaker.is_open("sjtu", now=1) is False


def test_half_open_single_probe_admission():
    breaker = Breaker(cooldown_429=10.0)
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=0)
    assert breaker.acquire_probe("sjtu", now=11) is True   # 到期首人成为唯一探测
    assert breaker.acquire_probe("sjtu", now=11) is False  # 并发他人被拒（真半开）
    assert breaker.is_open("sjtu", now=11) is True         # 探测中对外仍视为开

    breaker.record_success("sjtu")  # 探测成功
    assert breaker.is_open("sjtu", now=11) is False
    assert breaker.acquire_probe("sjtu", now=11) is True
    assert breaker.acquire_probe("sjtu", now=11) is True   # 恢复全放行


def test_half_open_probe_failure_recools_and_clears_probing():
    breaker = Breaker(cooldown_429=10.0, cooldown_network=15.0)
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=0)
    assert breaker.acquire_probe("sjtu", now=11) is True
    breaker.record_failure("sjtu", ErrorKind.NETWORK, now=11)  # 探测失败
    assert breaker.is_open("sjtu", now=20) is True          # 重新冷却
    assert breaker.acquire_probe("sjtu", now=20) is False
    assert breaker.acquire_probe("sjtu", now=26.1) is True  # probing 已清，到期可再探


def test_release_probe_allows_next_probe():
    breaker = Breaker(cooldown_429=10.0)
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=0)
    assert breaker.acquire_probe("sjtu", now=11) is True
    breaker.release_probe("sjtu")  # 探测者放弃（如软饱和）
    assert breaker.is_open("sjtu", now=11) is False
    assert breaker.acquire_probe("sjtu", now=11) is True  # 释放后下一请求可再探
