# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

"""Tests for OrbitExch discovery scraper."""

import asyncio
from unittest.mock import MagicMock

from nautilus_trader.adapters.orbitexch.discovery_scraper import OrbitExchScraper
from src.arbitrage.common.venue_configs import OrbitExchVenueConfig


class _FakeLocator:
    def __init__(self, calls: list[str]):
        self.calls = calls
        self.first = self

    async def count(self):
        self.calls.append("count")
        return 1

    async def click(self):
        self.calls.append("click")


def test_find_and_click_sport_waits_for_menu_before_click(monkeypatch):
    """查找 sport 前必须先等待菜单渲染完成。"""
    calls: list[str] = []
    scraper = OrbitExchScraper(OrbitExchVenueConfig())
    scraper._page = MagicMock()
    scraper._page.locator.side_effect = lambda _: calls.append("locator") or _FakeLocator(calls)

    async def wait_for_sport_menu_ready(_sport_name):
        calls.append("wait_sport")
        return True

    async def wait_for_quiet_network():
        calls.append("wait_network")

    async def click_all_sport(_sport_name):
        calls.append("click_all")
        return True

    monkeypatch.setattr(scraper, "_wait_for_sport_menu_ready", wait_for_sport_menu_ready)
    monkeypatch.setattr(scraper, "_wait_for_quiet_network", wait_for_quiet_network)
    monkeypatch.setattr(scraper, "_click_all_sport", click_all_sport)

    assert asyncio.run(scraper.find_and_click_sport("Soccer")) is True
    assert calls == ["wait_sport", "locator", "count", "click", "wait_network", "click_all"]


def test_find_and_click_competition_waits_for_page_ready(monkeypatch):
    """点击 competition 后必须等待详情页和比赛行数据就绪。"""
    calls: list[str] = []
    scraper = OrbitExchScraper(OrbitExchVenueConfig())
    scraper._page = MagicMock()
    scraper._page.locator.side_effect = lambda _: calls.append("locator") or _FakeLocator(calls)

    async def wait_for_competition_visible(_competition_name):
        calls.append("wait_competition")
        return True

    async def wait_for_competition_page_ready():
        calls.append("wait_page")
        return True

    monkeypatch.setattr(scraper, "_wait_for_competition_visible", wait_for_competition_visible)
    monkeypatch.setattr(scraper, "_wait_for_competition_page_ready", wait_for_competition_page_ready)

    assert asyncio.run(scraper.find_and_click_competition("English Premier League")) is True
    assert calls == ["wait_competition", "locator", "count", "click", "wait_page"]


def test_wait_for_sport_menu_ready_passes_playwright_arg_keyword():
    """Playwright wait_for_function 的参数必须通过 arg 关键字传入。"""
    scraper = OrbitExchScraper(OrbitExchVenueConfig())
    scraper._page = MagicMock()

    async def wait_for_function(*args, **kwargs):
        assert len(args) == 1
        assert kwargs["arg"] == "Tennis"
        assert kwargs["timeout"] == scraper._page_load_timeout_ms()

    scraper._page.wait_for_function = wait_for_function

    assert asyncio.run(scraper._wait_for_sport_menu_ready("Tennis")) is True


def test_wait_for_competition_visible_passes_playwright_arg_keyword():
    """competition 等待也必须使用 Playwright arg 关键字。"""
    scraper = OrbitExchScraper(OrbitExchVenueConfig())
    scraper._page = MagicMock()

    async def wait_for_function(*args, **kwargs):
        assert len(args) == 1
        assert kwargs["arg"] == "ATP Bucharest 2026"
        assert kwargs["timeout"] == scraper._page_load_timeout_ms()

    scraper._page.wait_for_function = wait_for_function

    assert asyncio.run(scraper._wait_for_competition_visible("ATP Bucharest 2026")) is True
