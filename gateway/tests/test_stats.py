from app.failover import ErrorKind
from app.stats import Stats


def test_record_and_snapshot():
    stats = Stats()
    stats.record("sjtu", ErrorKind.OK)
    stats.record("sjtu", ErrorKind.RATE_LIMIT)
    stats.record("sjtu", ErrorKind.RATE_LIMIT)
    stats.record("deepseek", ErrorKind.OK)
    stats.note_switched_away("sjtu")
    stats.note_soft_saturation("sjtu")
    snap = stats.snapshot()
    assert snap["sjtu"]["requests"] == 3
    assert snap["sjtu"]["success"] == 1
    assert snap["sjtu"]["rate_limited"] == 2
    assert snap["sjtu"]["switched_away"] == 1
    assert snap["sjtu"]["soft_saturated"] == 1
    assert snap["deepseek"]["success"] == 1
