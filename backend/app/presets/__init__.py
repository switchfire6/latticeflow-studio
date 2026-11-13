"""Curated seed crystals for LatticeFlow Studio (spec §5.3, §7 Phase 1 step 3).

Public API (fixed contract used by the REST / WebSocket layers)::

    from app.presets import DEFAULT_PRESET_ID, PresetInfo, list_presets, load_preset

Structures are stored as explicit lattice + asymmetric-unit data in
:mod:`app.presets.library` and expanded once at import time. Nothing here
touches the network.
"""

from app.presets.library import (
    DEFAULT_PRESET_ID,
    PresetInfo,
    StabilityExpectation,
    list_presets,
    load_preset,
)

__all__ = [
    "DEFAULT_PRESET_ID",
    "PresetInfo",
    "StabilityExpectation",
    "list_presets",
    "load_preset",
]
