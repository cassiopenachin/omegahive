"""The port's environment slice: a multi-process acceptance run over the port.

This is deployment #0's acceptance (deployment spec §7) and port spec §9's owed
deliverable — the coordinator, worker, and review each run as their own process
(their own container), coordinating only through Postgres via HiveCoordinatorPort.
It reuses the reference greedy Coordinator and the ReviewInstrument as-is; only a
port-native DemoWorker is new. It is NOT the quarantined sim engine.
"""

from .driver import DemoWorker, run_actor, seed_demo

__all__ = ["DemoWorker", "run_actor", "seed_demo"]
