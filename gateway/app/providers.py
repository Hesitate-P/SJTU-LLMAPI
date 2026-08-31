"""请求模型名 -> 候选供应商链（priority 升序）。"""
from __future__ import annotations

from .config import AppConfig, ProviderConfig


def build_chain(cfg: AppConfig, model: str) -> list[tuple[ProviderConfig, str]]:
    chain: list[tuple[ProviderConfig, str]] = []
    for provider in cfg.providers:
        if not provider.available:
            continue
        if model in provider.model_map:
            chain.append((provider, provider.model_map[model]))
        elif model in provider.models:
            chain.append((provider, model))
    chain.sort(key=lambda entry: entry[0].priority)
    return chain
