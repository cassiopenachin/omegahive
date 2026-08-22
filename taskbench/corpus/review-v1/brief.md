
## How this is graded, so you can spend your effort where it counts

You are not being asked to improve this work, to propose a design, or to iterate. You are
being asked one question and its evidence: **would this ship as it stands?**

- A finding needs a mechanism, not a smell. "What breaks, for whom, when" is the test. If
  you cannot say that, it is a preference, and preferences are not defects here.
- A design, structure or naming choice you would have made differently is not a defect
  where the order does not require your choice.
- Extra work inside the order's scope that crosses no stop-line is not a defect.
- The order's own stop-lines are the hard edges. Crossing one always is a defect.
- Some legs of an order cannot be executed by whatever produced this state — an operator
  act, a deployment, a live machine. Do not mark the work down for those, and do not
  credit it for claiming to have done them.

**Severity, as this review uses the word:**

- `critical` — it is wrong in a way that costs data, correctness or safety, and the tests
  as written do not catch it.
- `high` — it would have to change before shipping, but the damage is bounded or recoverable.
- `medium` — worth fixing, would not by itself stop a merge.
- `approach` — the work is aimed at the wrong target: the wrong reader, the wrong scope, a
  framing that a line-level reading would never surface. State it as its own finding rather
  than as a list of symptoms.

**Missing a real defect and inventing one are both failures**, and they are counted
separately. Do not pad the list to look thorough, and do not withhold a defect you can
evidence because the work looks careful otherwise.
