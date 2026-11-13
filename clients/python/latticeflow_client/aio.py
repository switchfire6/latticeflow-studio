"""``AsyncStudio``: the :class:`~latticeflow_client.Studio` API as coroutines (for asyncio / Jupyter).

Each method runs the blocking call in a worker thread (``asyncio.to_thread``), so
an event loop stays responsive while a phonon run or an MD stream is in flight;
``await studio.stop()`` from another task cancels it.

>>> studio = await AsyncStudio.open("http://localhost:8000")
>>> await studio.relax()
>>> ph = await studio.phonons(cell_mode="primitive")
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

from latticeflow_client.client import Studio

_STATE = {"crystal", "history", "model", "phonon", "last_reply", "base_url", "ws_url", "connected", "timeout"}


class AsyncStudio:
    def __init__(self, studio: Studio) -> None:
        self.sync = studio

    @classmethod
    async def open(cls, url: str = "http://localhost:8000", **kw: Any) -> "AsyncStudio":
        return cls(await asyncio.to_thread(Studio, url, **kw))

    async def __aenter__(self) -> "AsyncStudio":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await asyncio.to_thread(self.sync.close)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in _STATE:
            return getattr(self.sync, name)
        attr = getattr(self.sync, name)
        if not callable(attr) or name in ("to_ase", "to_pymatgen", "soft_mode"):
            return attr

        @functools.wraps(attr)
        async def call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(attr, *args, **kwargs)

        return call
