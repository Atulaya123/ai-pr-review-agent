"""Redis-backed LangGraph checkpointer.

Hand-rolled against BaseCheckpointSaver directly, not the official
langgraph-checkpoint-redis package — that package pulls in redisvl, which
requires redis>=6.3, while arq (this project's job queue) hard-pins
redis<6. No single `redis` version satisfies both in one virtualenv; this
was verified by actually installing both and hitting the resulting
ModuleNotFoundError, not assumed from changelogs.

Deliberately simpler than the reference InMemorySaver: this stores each
checkpoint's full channel_values inline per save rather than InMemorySaver's
content-addressed blob deduplication + delta-channel-history optimization.
Correct per the BaseCheckpointSaver contract (every CheckpointTuple field is
populated properly), just not storage-optimized — the right tradeoff for
this project's checkpoint volume (per-review-run, not high-frequency).

Key layout (all scoped under the `checkpoint_prefix`, default "aipr"):
    {prefix}:cp:{thread_id}:{ns}:{checkpoint_id}   -> hash: checkpoint/metadata
                                                       blobs + parent id
    {prefix}:idx:{thread_id}:{ns}                  -> sorted set of
                                                       checkpoint_ids (score
                                                       = insertion order),
                                                       for `list()` ordering
    {prefix}:writes:{thread_id}:{ns}:{checkpoint_id} -> hash of pending
                                                         writes keyed by
                                                         "{task_id}:{idx}"
"""

from __future__ import annotations

import pickle
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    ChannelVersions,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from redis.asyncio import Redis


class RedisCheckpointSaver(BaseCheckpointSaver[str]):
    """Async-only: this project's WorkflowEngine only ever calls the graph
    through ainvoke/aget_state, so only the a* methods are implemented. The
    sync methods raise NotImplementedError (the BaseCheckpointSaver default)
    rather than silently doing the wrong thing if ever called from sync code.
    """

    def __init__(self, redis_client: Redis, *, prefix: str = "aipr") -> None:
        super().__init__()
        self._redis = redis_client
        self._prefix = prefix

    @classmethod
    def from_url(cls, redis_url: str, *, prefix: str = "aipr") -> RedisCheckpointSaver:
        return cls(Redis.from_url(redis_url, decode_responses=False), prefix=prefix)

    def _cp_key(self, thread_id: str, ns: str, checkpoint_id: str) -> str:
        return f"{self._prefix}:cp:{thread_id}:{ns}:{checkpoint_id}"

    def _idx_key(self, thread_id: str, ns: str) -> str:
        return f"{self._prefix}:idx:{thread_id}:{ns}"

    def _writes_key(self, thread_id: str, ns: str, checkpoint_id: str) -> str:
        return f"{self._prefix}:writes:{thread_id}:{ns}:{checkpoint_id}"

    async def _build_tuple(
        self, thread_id: str, ns: str, checkpoint_id: str, config: RunnableConfig
    ) -> CheckpointTuple | None:
        # redis-py's stubs type these as Awaitable[T] | T (shared sync/async
        # generics) — always Awaitable[T] in practice since self._redis is
        # redis.asyncio.Redis; mypy can't resolve that on its own.
        raw = await self._redis.hgetall(self._cp_key(thread_id, ns, checkpoint_id))  # type: ignore[misc]
        if not raw:
            return None

        checkpoint: Checkpoint = self.serde.loads_typed(
            (raw[b"checkpoint_type"].decode(), raw[b"checkpoint_bytes"])
        )
        metadata: CheckpointMetadata = self.serde.loads_typed(
            (raw[b"metadata_type"].decode(), raw[b"metadata_bytes"])
        )
        parent_id = raw.get(b"parent_checkpoint_id")
        parent_id_str = parent_id.decode() if parent_id else None

        writes_raw = await self._redis.hgetall(self._writes_key(thread_id, ns, checkpoint_id))  # type: ignore[misc]
        pending_writes = []
        for field, envelope in writes_raw.items():
            # This envelope (task_id/channel/value_type/value_bytes/task_path)
            # is this class's own internal Redis encoding, not part of
            # LangGraph's checkpoint format — plain pickle is fine and avoids
            # ambiguity about which serde type tag it needs.
            task_id, channel, value_type, value_bytes, _task_path = pickle.loads(envelope)
            pending_writes.append((task_id, channel, self.serde.loads_typed((value_type, value_bytes))))

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": parent_id_str}}
                if parent_id_str
                else None
            ),
            pending_writes=pending_writes,
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id: str = config["configurable"]["thread_id"]
        ns: str = config["configurable"].get("checkpoint_ns", "")

        checkpoint_id = get_checkpoint_id(config)
        if checkpoint_id is None:
            latest = await self._redis.zrevrange(self._idx_key(thread_id, ns), 0, 0)
            if not latest:
                return None
            checkpoint_id = latest[0].decode()

        return await self._build_tuple(thread_id, ns, checkpoint_id, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            # No thread scoping given — this store doesn't maintain a global
            # thread index (not needed by this project's WorkflowEngine,
            # which always scopes by thread_id), so there's nothing to list.
            return
        thread_id: str = config["configurable"]["thread_id"]
        ns: str = config["configurable"].get("checkpoint_ns", "")

        checkpoint_ids = [
            cid.decode() for cid in await self._redis.zrevrange(self._idx_key(thread_id, ns), 0, -1)
        ]

        before_id = get_checkpoint_id(before) if before else None
        yielded = 0
        for checkpoint_id in checkpoint_ids:
            if before_id and checkpoint_id >= before_id:
                continue
            tup = await self._build_tuple(thread_id, ns, checkpoint_id, config)
            if tup is None:
                continue
            if filter and not all(tup.metadata.get(k) == v for k, v in filter.items()):
                continue
            if limit is not None and yielded >= limit:
                break
            yielded += 1
            yield tup

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"]["checkpoint_ns"]
        checkpoint_id = checkpoint["id"]
        parent_id = config["configurable"].get("checkpoint_id")

        checkpoint_type, checkpoint_bytes = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_bytes = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))

        mapping: dict[str, Any] = {
            "checkpoint_type": checkpoint_type,
            "checkpoint_bytes": checkpoint_bytes,
            "metadata_type": metadata_type,
            "metadata_bytes": metadata_bytes,
        }
        if parent_id:
            mapping["parent_checkpoint_id"] = parent_id

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(self._cp_key(thread_id, ns, checkpoint_id), mapping=mapping)
            pipe.zadd(self._idx_key(thread_id, ns), {checkpoint_id: time.time()})
            await pipe.execute()

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        key = self._writes_key(thread_id, ns, checkpoint_id)

        existing_fields = {f.decode() for f in await self._redis.hkeys(key)}  # type: ignore[misc]
        mapping: dict[str, Any] = {}
        for idx, (channel, value) in enumerate(writes):
            field = f"{task_id}:{WRITES_IDX_MAP.get(channel, idx)}"
            if WRITES_IDX_MAP.get(channel, idx) >= 0 and field in existing_fields:
                continue
            value_type, value_bytes = self.serde.dumps_typed(value)
            mapping[field] = pickle.dumps((task_id, channel, value_type, value_bytes, task_path))

        if mapping:
            await self._redis.hset(key, mapping=mapping)  # type: ignore[misc]

    async def adelete_thread(self, thread_id: str) -> None:
        pattern = f"{self._prefix}:*:{thread_id}:*"
        async for key in self._redis.scan_iter(match=pattern):
            await self._redis.delete(key)
