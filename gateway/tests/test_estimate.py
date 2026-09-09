from app.estimate import estimate_request_tokens


def msg(content):
    return {"messages": [{"role": "user", "content": content}]}


def test_pure_ascii():
    body = msg("a" * 100)
    # (100*0.25)*1.1 + 4 = 27.5 + 4 → 31.5 → int
    assert estimate_request_tokens(body) == int(100 * 0.25 * 1.1) + 4


def test_pure_chinese():
    body = msg("中" * 100)   # 码点 > 0x2E80
    assert estimate_request_tokens(body) == int(100 * 0.6 * 1.1) + 4


def test_mixed_and_parts():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "你好"}, {"type": "text", "text": "world"},
        {"type": "image_url", "image_url": {"url": "data:..."}}]}]}
    zh, en = 2 * 0.6, 5 * 0.25
    assert estimate_request_tokens(body) == int((zh + en) * 1.1) + 4


def test_max_tokens_counted():
    body = {**msg("hi"), "max_tokens": 500}
    assert estimate_request_tokens(body) == int(2 * 0.25 * 1.1) + 4 + 500


def test_max_completion_tokens_equivalent():
    body = {**msg("hi"), "max_completion_tokens": 500}
    assert estimate_request_tokens(body) == int(2 * 0.25 * 1.1) + 4 + 500


def test_empty_messages():
    assert estimate_request_tokens({"messages": []}) == 0
