import pytest

from app.security import assert_safe_upstream_url

PUBLIC = ["93.184.216.34"]


def _res(ips):
    return lambda host: ips


@pytest.mark.parametrize("url", [
    "http://example.com/api", "https://models.sjtu.edu.cn/api/v1",
])
def test_http_https_with_public_host_passes(url):
    assert_safe_upstream_url(url, resolver=_res(PUBLIC))


@pytest.mark.parametrize("url", [
    "ftp://example.com/api", "file:///etc/passwd", "gopher://example.com",
])
def test_non_http_scheme_rejected(url):
    with pytest.raises(ValueError, match="scheme"):
        assert_safe_upstream_url(url, resolver=_res(PUBLIC))


def test_missing_host_rejected():
    with pytest.raises(ValueError, match="host"):
        assert_safe_upstream_url("http:///path", resolver=_res(PUBLIC))


@pytest.mark.parametrize("host_ip", [
    "127.0.0.1", "::1", "10.1.2.3", "172.16.0.9", "192.168.1.1",
    "169.254.1.1", "fd00::1", "224.0.0.1", "0.0.0.0",
])
def test_forbidden_ips_rejected(host_ip):
    with pytest.raises(ValueError, match="环回|私有|保留"):
        assert_safe_upstream_url(f"https://upstream.test/v1", resolver=_res([host_ip]))


def test_literal_loopback_ip_rejected_without_resolver():
    with pytest.raises(ValueError):
        assert_safe_upstream_url("http://127.0.0.1:8080/v1")


def test_unresolvable_host_rejected():
    with pytest.raises(ValueError, match="解析"):
        assert_safe_upstream_url("https://nope.test/v1", resolver=lambda host: [])
