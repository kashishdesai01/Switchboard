import asyncio
from pathlib import Path

from switchboard.operations import Operations


def test_unresolved_write_survives_process_restart_and_reconciles(tmp_path: Path, monkeypatch):
    async def run():
        now = 1000
        monkeypatch.setattr("switchboard.operations.time", lambda: now)
        database = tmp_path / "operations.sqlite"
        operations = Operations(database, "bayview@1")
        trace = operations.start("twilio")
        await trace.begin_mutation("lost-operation", "book")

        restarted = Operations(database, "bayview@1")
        now += 16
        data = await restarted.snapshot()
        assert data["unresolved_mutations"][0]["status"] == "pending"

        async def receipt(operation):
            return operation == "lost-operation"

        await restarted.reconcile(receipt)
        snapshot = await restarted.snapshot()
        assert snapshot["unresolved_mutations"] == []
        (recovered,) = snapshot["settled_mutations"]
        assert (recovered["operation"], recovered["status"], recovered["checked"]) == (
            "lost-operation",
            "committed",
            now,
        )

    asyncio.run(run())


def test_handoff_review_is_idempotent(tmp_path: Path):
    async def run():
        ops = Operations(tmp_path / "operations.sqlite", "bayview@1")
        trace = ops.start("browser_text")
        trace.outcome = "handoff"
        await ops.finish(trace)
        assert await ops.mark_reviewed(trace.id)
        when = (await ops.snapshot())["recent"][0]["reviewed_at"]
        assert await ops.mark_reviewed(trace.id)
        assert (await ops.snapshot())["recent"][0]["reviewed_at"] == when

    asyncio.run(run())


def test_reconciliation_rotates_a_full_backlog_and_survives_restart(tmp_path, monkeypatch):
    from contextlib import closing

    async def run():
        now = 1000
        monkeypatch.setattr("switchboard.operations.time", lambda: now)
        database = tmp_path / "operations.sqlite"
        ops = Operations(database, "bayview@1")
        with closing(ops.connect()) as db, db:
            db.executemany(
                "INSERT INTO mutations (operation, trace_id, action, status, created) VALUES (?, 'trace', 'book', 'unknown', ?)",
                [(f"operation-{index:02}", now + index) for index in range(55)],
            )

        visited = []

        async def receipt(operation):
            visited.append(operation)
            return operation == "operation-54"

        now = 1100
        await ops.reconcile(receipt)
        assert visited == [f"operation-{index:02}" for index in range(50)]
        assert (await ops.snapshot())["unresolved_mutations"][-1]["checked"] == now
        restarted = Operations(database, "bayview@1")
        now += 30
        visited.clear()
        await restarted.reconcile(receipt)
        assert visited[:5] == [f"operation-{index:02}" for index in range(50, 55)]
        assert not any(
            item["operation"] == "operation-54"
            for item in (await restarted.snapshot())["unresolved_mutations"]
        )

    asyncio.run(run())


def test_legacy_mutation_table_gains_reconciliation_column(tmp_path):
    import sqlite3
    from contextlib import closing

    database = tmp_path / "operations.sqlite"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("""
            CREATE TABLE mutations (
                operation TEXT PRIMARY KEY, trace_id TEXT NOT NULL, action TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL
            )
        """)
        db.execute("INSERT INTO mutations VALUES ('legacy', 'trace', 'book', 'unknown', 1)")
    ops = Operations(database, "bayview@1")
    (item,) = ops._unresolved()
    assert item["operation"] == "legacy"
    assert item["checked"] is None
