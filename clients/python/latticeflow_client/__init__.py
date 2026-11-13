"""Python client for LatticeFlow Studio (``/ws/studio`` session + ``/api`` REST).

>>> import latticeflow_client as lf
>>> studio = lf.connect("http://localhost:8000")
>>> studio.load(preset="srtio3"); studio.relax(); ph = studio.phonons()
>>> pid = studio.create_project("Perovskites")["id"]
>>> studio.wait_jobs(studio.submit_jobs(pid, [studio.save_structure(pid)["id"]], ["relax", "phonons"]))
"""

from latticeflow_client.aio import AsyncStudio
from latticeflow_client.client import Reply, Studio, connect
from latticeflow_client.errors import StreamStopped, StudioConnectionError, StudioError, StudioTimeout
from latticeflow_client.protocol import COMMANDS, JOB_FINISHED, CommandSpec, JobStep, Step, job_step
from latticeflow_client.structures import crystal_to_ase, crystal_to_pymatgen, parse_formula

__version__ = "0.5.0"

__all__ = [
    "AsyncStudio", "COMMANDS", "CommandSpec", "JOB_FINISHED", "JobStep", "Reply", "Step", "StreamStopped", "Studio",
    "StudioConnectionError", "StudioError", "StudioTimeout", "connect", "crystal_to_ase",
    "crystal_to_pymatgen", "job_step", "parse_formula", "__version__",
]
