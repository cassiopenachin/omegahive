"""The coordinator-ladder experiment harness (stage 2 §5–§8).

V2a (this slice) is the deterministic half: the pre-registered seed generator, the k=1
fork board (on V1's ready_when/prune), seed-driven event-driven workers, the §7
coordination metrics, and the ladder runner sweeping the R0 greedy rung across seeds.
No LLM, no keys. R1+ (the vanilla/OmegaClaw rungs) attach in later slices.
"""
