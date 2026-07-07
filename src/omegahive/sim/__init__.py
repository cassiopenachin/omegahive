"""The simulation harness — quarantined from the substrate (§8).

The discrete-event engine, stub reactors (workers, and the greedy coordinator used as
the port reference client), and the scenario loader/schema. The substrate packages
(events, gateway, board, port) import NONE of this; test_sim_quarantine.py enforces it.
"""
