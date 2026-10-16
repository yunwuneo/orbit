"""Wake timing only; a due time never asserts that a story event occurred."""

from datetime import timedelta
from .threads import Threads
from .timeutil import parse, stamp


def wake_seconds(engine):
    config, store, current = engine.config, engine.store, engine.clock()
    candidates = [current + timedelta(seconds=config.interval_seconds)]
    if config.continuation.enabled and not store.get("paused", False):
        poll = store.get("next_input_poll")
        life = store.get("continuation_life_at")
        candidates.append(parse(poll) if poll else current)
        candidates.append(parse(life) + timedelta(seconds=config.interval_seconds) if life else current)
        repo = Threads(store)
        for row in store.db.execute("SELECT * FROM thread_tasks WHERE status='pending'"):
            due = max(parse(row["due_at"]), parse(row["next_at"]) if row["next_at"] else current)
            # Avoid a hot wake for a task whose causal writes or projection are still unavailable.
            if not engine.continuation.shared_pending() and repo.ready(dict(row), stamp(max(current, due))):
                candidates.append(due)
    for row in store.pending():
        if row["status"] == "pending" and store.write_ready(row["id"]):
            candidates.append(parse(row["next_at"]) if row["next_at"] else current)
    return max(1.0, (min(candidates) - current).total_seconds())
