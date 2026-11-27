from datetime import datetime, timezone

import pytest

from tgdl.storage.repo import DuplicateDownload, NewJob, Repository

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def job(chat_id=-100, msg_id=1, media="doc:1", source="live") -> NewJob:
    return NewJob(chat_id=chat_id, chat_title="Chat", message_id=msg_id, grouped_id=None,
                  media_unique_id=media, media_kind="document", file_name="a.bin",
                  file_size=10, message_date=NOW, source=source)


async def complete(repo: Repository, job_id: int) -> None:
    j = await repo.get_job(job_id)
    await repo.record_download(j, category="Others", file_name="a.bin", file_size=10,
                               saved_path="/x/a.bin")


async def test_enqueue_then_same_message_is_queued_duplicate(repo: Repository) -> None:
    jid, reason = await repo.enqueue(job())
    assert jid and reason is None
    assert await repo.enqueue(job()) == (None, "queued_message")


async def test_same_media_in_other_chat_is_queued_duplicate(repo: Repository) -> None:
    await repo.enqueue(job())
    assert await repo.enqueue(job(chat_id=-200, msg_id=9)) == (None, "queued_media")


async def test_downloaded_message_skipped(repo: Repository) -> None:
    jid, _ = await repo.enqueue(job())
    await complete(repo, jid)
    assert await repo.enqueue(job()) == (None, "downloaded_message")
    # same message id but a different media id (edited message) is still skipped
    assert await repo.enqueue(job(media="doc:999")) == (None, "downloaded_message")


async def test_downloaded_media_skipped_across_chats(repo: Repository) -> None:
    jid, _ = await repo.enqueue(job())
    await complete(repo, jid)
    assert await repo.enqueue(job(chat_id=-555, msg_id=77)) == (None, "downloaded_media")


async def test_failed_job_can_be_requeued(repo: Repository) -> None:
    jid, _ = await repo.enqueue(job())
    await repo.set_job_status(jid, "failed", error="boom")
    jid2, reason = await repo.enqueue(job(source="retry"))
    assert jid2 and jid2 != jid and reason is None


async def test_different_messages_are_independent(repo: Repository) -> None:
    a, _ = await repo.enqueue(job(msg_id=1, media="doc:1"))
    b, _ = await repo.enqueue(job(msg_id=2, media="doc:2"))
    assert a and b and a != b


async def test_unique_index_blocks_race(repo: Repository) -> None:
    """Bypass the pre-check: the partial unique index must still reject."""
    await repo.enqueue(job())
    orig = repo.find_duplicate

    async def no_precheck(*a, **kw):
        if kw.get("include_queue", True) and not getattr(no_precheck, "called", False):
            no_precheck.called = True
            return None
        return await orig(*a, **kw)

    repo.find_duplicate = no_precheck  # type: ignore[method-assign]
    jid, reason = await repo.enqueue(job())
    assert jid is None and reason == "queued_message"


async def test_record_download_is_unique(repo: Repository) -> None:
    jid, _ = await repo.enqueue(job())
    await complete(repo, jid)
    j = await repo.get_job(jid)
    assert j["status"] == "done"
    with pytest.raises(DuplicateDownload):
        await repo.record_download(j, category="Others", file_name="a.bin", file_size=10,
                                   saved_path="/x/a.bin")


async def test_reset_running_jobs(repo: Repository) -> None:
    jid, _ = await repo.enqueue(job())
    claimed = await repo.claim_job()
    assert claimed["id"] == jid and claimed["attempts"] == 1
    await repo.set_part_path(jid, "/x/a.bin.part")
    assert await repo.reset_running_jobs() == ["/x/a.bin.part"]
    j = await repo.get_job(jid)
    # the interrupted attempt does not count
    assert j["status"] == "pending" and j["attempts"] == 0 and j["part_path"] is None


async def test_reads_consistent_under_concurrent_writes(repo: Repository) -> None:
    """Regression: on the shared connection, a split execute/fetch could see
    rows mutated mid-read (NULL status in GROUP BY results)."""
    import asyncio

    for i in range(200):
        await repo.enqueue(job(msg_id=i, media=f"doc:{i}"))

    async def writer() -> None:
        while (j := await repo.claim_job()) is not None:
            await repo.set_job_status(j["id"], "done")

    async def reader() -> list[dict]:
        out = []
        for _ in range(100):
            out.append(await repo.job_counts())
            await asyncio.sleep(0)
        return out

    results = await asyncio.gather(writer(), writer(), reader(), reader())
    for counts in results[2] + results[3]:
        assert None not in counts and sum(counts.values()) == 200
