"""Python client for LatticeFlow Studio (``/ws/studio`` session + ``/api`` REST).

>>> import latticeflow_client as lf
>>> studio = lf.connect("http://localhost:8000")
>>> studio.load(preset="srtio3"); studio.relax(); ph = studio.phonons()
"""

from latticeflow_client.aio import AsyncStudio
from latticeflow_client.client import Reply, Studio, connect
from latticeflow_client.errors import StreamStopped, StudioConnectionError, StudioError, StudioTimeout
from latticeflow_client.protocol import COMMANDS, CommandSpec, Step
from latticeflow_client.structures import crystal_to_ase, crystal_to_pymatgen, parse_formula

__version__ = "0.1.0"

__all__ = [
    "AsyncStudio", "COMMANDS", "CommandSpec", "Reply", "Step", "StreamStopped", "Studio",
    "StudioConnectionError", "StudioError", "StudioTimeout", "connect", "crystal_to_ase",
    "crystal_to_pymatgen", "parse_formula", "__version__",
]
