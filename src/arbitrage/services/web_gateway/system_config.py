"""
系统级配置
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class SystemConfig:
    """系统级配置"""

    orbitexch_page_load_timeout_sec: float = 60.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SystemConfig":
        return cls(
            orbitexch_page_load_timeout_sec=float(
                data.get(
                    "orbitexch_page_load_timeout_sec",
                    data.get("orbitexch_page_load_wait_sec", 60.0),
                )
            ),
        )

    def update_from_dict(self, data: dict[str, Any]) -> None:
        if "orbitexch_page_load_timeout_sec" in data:
            self.orbitexch_page_load_timeout_sec = float(
                data["orbitexch_page_load_timeout_sec"]
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "orbitexch_page_load_timeout_sec": self.orbitexch_page_load_timeout_sec,
        }
