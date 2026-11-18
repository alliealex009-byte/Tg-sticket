from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass


def make_result_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:12]


@dataclass
class SearchResult:
    id: str
    title: str
    image_url: str
    thumb_url: str
    page_url: str
    provider: str
    creator: str = ""
    license: str = ""
    license_url: str = ""
    attribution: str = ""
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SearchResult":
        names = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in names})
