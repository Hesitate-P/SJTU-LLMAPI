import pytest

from app.ratelimit import TokenBucket


async def test_burst_capacity_immediate():
    bucket = TokenBucket(rate_per_minute=9, burst=3)
    assert [await bucket.acquire(0) for _ in range(3)] == [True, True, True]


async def test_exhausted_bucket_fails_fast_with_zero_wait():
    bucket = TokenBucket(rate_per_minute=9, burst=1)
    assert await bucket.acquire(0) is True
    assert await bucket.acquire(0) is False


async def test_waits_for_refill_within_max_wait():
    # 600/分钟 = 10/秒，空桶后 0.1 秒可再取一个
    bucket = TokenBucket(rate_per_minute=600, burst=1)
    assert await bucket.acquire(0) is True
    assert await bucket.acquire(2.0) is True


async def test_times_out_when_refill_too_slow():
    bucket = TokenBucket(rate_per_minute=6, burst=1)  # 0.1/秒
    assert await bucket.acquire(0) is True
    assert await bucket.acquire(0.05) is False


def test_zero_rate_rejected():
    with pytest.raises(ValueError):
        TokenBucket(rate_per_minute=0)


def test_negative_burst_rejected():
    with pytest.raises(ValueError):
        TokenBucket(rate_per_minute=9, burst=-1)
