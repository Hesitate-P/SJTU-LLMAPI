from app.config import AppConfig, ProviderConfig
from app.providers import build_chain


def make_cfg():
    return AppConfig(providers=[
        ProviderConfig(name="deepseek", base_url="https://d.test/v1", api_key_env="D",
                       priority=2, models=["deepseek-chat"]),
        ProviderConfig(name="sjtu", base_url="https://s.test/v1", api_key_env="S",
                       priority=1, models=["deepseek-chat", "minimax"]),
        ProviderConfig(name="other", base_url="https://o.test/v1", api_key_env="O",
                       priority=3, model_map={"minimax": "other-mini", "qwen": "other-qwen"}),
        ProviderConfig(name="nokey", base_url="https://n.test/v1", api_key_env="N",
                       priority=4, models=["deepseek-chat"], available=False,
                       unavailable_reason="环境变量 N 未设置"),
    ])


def test_chain_sorted_by_priority():
    chain = build_chain(make_cfg(), "deepseek-chat")
    assert [(p.name, m) for p, m in chain] == [("sjtu", "deepseek-chat"), ("deepseek", "deepseek-chat")]


def test_model_map_translates_name():
    chain = build_chain(make_cfg(), "qwen")
    assert [(p.name, m) for p, m in chain] == [("other", "other-qwen")]


def test_unknown_model_empty_chain():
    assert build_chain(make_cfg(), "no-such-model") == []


def test_unavailable_provider_excluded():
    chain = build_chain(make_cfg(), "deepseek-chat")
    assert all(p.name != "nokey" for p, _ in chain)
