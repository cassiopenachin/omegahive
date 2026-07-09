# Coordination knowledge base

General coordination knowledge for running a task board. It states principles, not board-specific facts.

## Purpose and how to use this

This knowledge base holds the reasoning a coordinator needs: how to weigh evidence, when to abandon work, when to escalate, how to allocate workers, and how to recover when an operation is refused. It is general — it says nothing about any particular board, and it commits to no particular numbers.

Consult it before making a coordination decision, and especially before a decision that is hard to undo, such as abandoning a line of work. The board view tells you the current state of the world; this knowledge tells you how to reason about that state. Where an operation is mentioned, its exact form and the conditions under which it is legal live in the operation reference, not here — this document explains *when* and *why* to act, and defers the *how* to that reference.

The habits below reward each other. Reading before acting keeps your evidence current; current evidence makes pruning and escalation judgments sound; sound judgments keep workers on work that matters. Treat them as one discipline rather than separate tricks.

## Read the board before you act

Every decision should rest on the most recent view. The board changes as workers make progress and as your own operations take effect, so a view you read earlier may already be out of date. Before you repeat an operation, and before you act on a belief you formed several turns ago, read the current view and confirm the belief still holds.

Acting on a stale picture is a common source of both wasted operations and rejections. A task you believe is unowned may now have an owner; a task you believe is ready may already have moved on; a worker you remember as idle may have picked up work. Re-reading is cheap, and a wrong operation is not — it costs effort, and it can commit you to a path you would not have chosen with current information.

When a view surprises you, treat the view as authoritative and update your plan, rather than forcing through the plan you arrived with. The board is the ground truth; your intentions are not. A coordinator who keeps acting on remembered state drifts steadily further from what is actually happening, and each operation built on that drift compounds the error.

## Evidence and pruning

Abandoning work — pruning — is the most consequential decision a coordinator makes, because it is not free and it is hard to reverse. The central question is always evidentiary: does the accumulated evidence justify concluding that this line of work will not pay off?

Repeated failure is evidence, but it is not proof. A single failure says little; it may reflect a transient obstacle, an unlucky attempt, or a condition that will clear on its own. A run of failures says more, and failures that recur under varied conditions say more still. What you are looking for is a pattern unlikely to reverse — not merely a streak that happens to feel long. Ask what would have to be true for the work to still succeed, and whether anything in the board gives you reason to think it could. If the honest answer is that success is still plausible, the evidence is not yet decisive, however tired the waiting has made you.

The cost of getting this wrong is asymmetric, and the asymmetry runs both ways depending on the situation. Prune too early and you discard work that would have succeeded, and you may strip away the very redundancy that was protecting the plan. Prune too late and you keep paying for attempts that were never going to matter, while other work waits for the capacity they are consuming. Neither error is universally worse; the balance depends on how strong the evidence is and on what the abandoned work was protecting.

When the evidence is genuinely ambiguous, patience is usually cheaper than it feels. A worker on a marginal task is still producing information about whether that task can succeed, and that information may resolve the very question you are struggling with. A prune produces no further information — it ends the inquiry. So weigh the evidence before you abandon an approach, not after you have already decided to; and decide on what the board shows, not on how long you have been watching it.

## k-of-n forks and redundancy

Some plans complete a step through more than one line of work feeding a common join. The join does not wait for every contributing line — it becomes satisfiable once enough of them have succeeded, where "enough" is the requirement the join declares. Lines beyond that requirement are redundancy.

Redundancy exists to buy reliability under uncertainty. When it is not known in advance which line will succeed, running several of them raises the chance that the required number eventually do. That protection has a price: the extra lines consume effort even when, in hindsight, they were not needed. Managing a fork means managing this trade-off — deciding how much redundancy to keep as evidence accumulates about which lines are actually progressing, and letting go of surplus only when the remaining lines clearly suffice.

Pruning interacts with joins carefully, and the interaction is worth internalizing. Abandoning a line removes it from the pool of work its join can draw on. Crucially, dropping a line does not make the join any easier to satisfy — the join still needs the same amount of success, now drawn from fewer remaining lines. This is exactly why abandoning a line is only safe while enough other lines could still meet the requirement — the operation reference states precisely when a prune is legal on this account, and it will refuse one that would strand a join. Before you abandon a line that feeds a join, confirm for yourself that the surviving lines can still satisfy it. Redundancy is protection; do not spend it down to nothing on the strength of a decision you could have deferred.

## Escalation

Escalation flags a task for attention. It is the right move when a task is genuinely stuck in a way you cannot resolve through the operations available to you, and when leaving it unattended would block progress that matters. It is not a substitute for patience: a task that is merely slow, or one waiting on work that is proceeding normally, does not need escalation, and flagging it adds noise without adding information.

The judgment to make is whether attention would change the outcome. If the obstacle is something that only outside intervention can clear, escalate promptly rather than letting a blocked task sit and drag the plan behind it. If the obstacle is likely to clear on its own, or if you hold an operation that addresses it directly, prefer that quieter path. Escalating everything is as unhelpful as escalating nothing: it turns the signal into noise, so that the one task that truly needed attention is lost among the ones that did not.

## Allocation under contention

Workers are a finite resource, and much of coordination is simply matching available workers to work that is ready for them. Prefer to keep workers busy on ready work — an idle worker beside a ready, unowned task is wasted capacity, and that waste accrues quietly for as long as it lasts. When more work is ready than there are workers to take it, you are choosing what to advance first, and the choice is worth making deliberately.

Favor the work that unblocks the most downstream progress, and the work whose outcome you most need to learn. A task that many other tasks depend on earns priority over one that leads nowhere. When two ready tasks are otherwise comparable, advancing the one that resolves uncertainty — the one that will tell you whether a line of work can succeed — is usually worth more than advancing one whose outcome you can already predict, because the information reshapes every decision that follows. Two failures to avoid: leaving a worker idle while a ready task waits, and piling redundant effort onto work that is already progressing while other work sits with no one on it.

## Recovering from rejections

A rejected operation is information, not a dead end. A refusal tells you the board did not hold what your operation assumed; the gap between your belief and the board is exactly what the refusal reveals. The operation reference lists the machine codes a refusal can carry and the conditions under which each arises; the reason string that accompanies the code narrows the diagnosis further. The response is always the same in shape: re-read the current view and adapt. Never repeat the identical operation and hope — an operation refused from a given state will be refused again from that same state, so a bare retry only spends effort to reconfirm the refusal.

Read the refusal for what it teaches, then let the corrected picture drive your next move:

- When an operation is refused because the task is not in the status the operation requires, the task has moved differently than you expected. Read its current status and choose an operation that fits it, or wait if the status is one that will change on its own.
- When an operation is refused because the task already has an owner, another decision reached it first. Accept the current owner rather than contending for the task, and turn to work that still needs someone.
- When an operation names a task or worker the board does not recognize, the id came from a stale view or was formed incorrectly. Take ids only from the most recent view, and confirm the id is present there before you use it.
- When an operation is refused as an illegal transition, it simply does not apply from the current status. Re-read the status and select an operation that is legal from it; do not retry the one that was refused.
- When an operation is refused for lack of authority, it is not yours to perform. Leave it to the actor whose operation it is, and attend to work that is within your authority.

In every case, reconcile the reason with the current view, and let the corrected picture — not your original intent — decide what you do next.
