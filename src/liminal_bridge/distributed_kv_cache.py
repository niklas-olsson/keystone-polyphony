import json
import base64
import time
from typing import Dict, Any, Optional

try:
    from .mesh import LiminalMesh
except ImportError:
    from mesh import LiminalMesh


class DistributedKVCache:
    """
    Manages the synchronization of the LLM Attention KV Cache across sharded
    WASM instances using a hybrid CRDT and Blob storage approach.

    In Sequence Parallelism, massive context windows are sliced across nodes.
    Since raw KV cache tensors are too large for rapid CRDT gossiping over Websockets/TCP,
    this class:
    1. Instructs the node to dump the raw cache to Pollen (`pln seed`).
    2. Broadcasts the resulting Pollen blob hash over the Keystone CRDT mesh.
    3. Allows downstream Pipeline or Sequence shards to resolve the hash and fetch the cache.
    """

    def __init__(self, mesh: LiminalMesh):
        self.mesh = mesh
        # We use a specific CRDT key prefix for sequence parallel caching
        self.kv_prefix = "llm_cache:"

    async def push_cache_block(
        self, session_id: str, layer_id: int, sequence_idx: int, pollen_blob_hash: str
    ) -> str:
        """
        Publishes the existence of a new KV cache block to the CRDT mesh.

        Args:
            session_id: The unique ID for the current inference generation stream.
            layer_id: The transformer layer this cache belongs to.
            sequence_idx: The sequence chunk/block index (for Context Parallelism).
            pollen_blob_hash: The content-addressed hash of the raw tensor stored in Pollen.
        """
        crdt_key = f"{self.kv_prefix}{session_id}"

        # We store the metadata payload
        cache_metadata = {
            "layer": layer_id,
            "sequence_idx": sequence_idx,
            "hash": pollen_blob_hash,
            "node_id": self.mesh.node_id,
            "timestamp": time.time(),
        }

        # We use an ORSet so multiple shards can append their chunks to the same session simultaneously
        # without overwriting each other (which Register would do).
        await self.mesh.update_set(crdt_key, json.dumps(cache_metadata), urgency="high")
        return crdt_key

    def get_cache_blocks(
        self, session_id: str, layer_id: Optional[int] = None
    ) -> list[Dict[str, Any]]:
        """
        Retrieves all synchronized KV cache blocks for a given session.
        If layer_id is provided, filters the blocks to just that layer.
        """
        crdt_key = f"{self.kv_prefix}{session_id}"
        blocks_raw = self.mesh.get_kv(crdt_key)

        if not blocks_raw:
            return []

        blocks = []
        for item in blocks_raw:
            try:
                block = json.loads(item)
                if layer_id is None or block.get("layer") == layer_id:
                    blocks.append(block)
            except (json.JSONDecodeError, TypeError):
                continue

        # Sort by sequence index to assemble the context sequentially
        blocks.sort(key=lambda x: x.get("sequence_idx", 0))
        return blocks

    def generate_pollen_fetch_script(self, session_id: str, output_dir: str) -> str:
        """
        Helper method that reads the CRDT state and generates a bash script
        with the exact `pln fetch` commands needed to download the physical cache tensors.
        """
        blocks = self.get_cache_blocks(session_id)
        if not blocks:
            return "# No cache blocks found for this session."

        script_lines = [
            "#!/usr/bin/env bash",
            "set -e",
            f"echo '>>> Fetching Distributed KV Cache for session {session_id}...'",
            f"mkdir -p {output_dir}",
        ]

        for b in blocks:
            hash_val = b.get("hash")
            layer = b.get("layer")
            seq = b.get("sequence_idx")
            filename = f"{output_dir}/cache_L{layer}_seq{seq}.bin"
            script_lines.append(f"echo 'Fetching Layer {layer} Block {seq}...'")
            script_lines.append(f"pln fetch {hash_val} --out {filename}")

        return "\n".join(script_lines)


# Example Usage
if __name__ == "__main__":
    import asyncio

    async def run_mock():
        # Mock mesh
        from unittest.mock import MagicMock

        mesh = LiminalMesh(secret_key="mock", storage_provider=SQLiteStorageProvider(":memory:"))
        cache = DistributedKVCache(mesh)

        print("Pushing Mock Cache Blocks to CRDT...")
        await cache.push_cache_block(
            "chat_abc123", layer_id=5, sequence_idx=0, pollen_blob_hash="a1b2c3d4"
        )
        await cache.push_cache_block(
            "chat_abc123", layer_id=5, sequence_idx=1, pollen_blob_hash="e5f6g7h8"
        )

        print("\nRetrieving Blocks from CRDT:")
        blocks = cache.get_cache_blocks("chat_abc123", layer_id=5)
        for b in blocks:
            print(
                f"Layer {b['layer']} Sequence {b['sequence_idx']} -> Blob {b['hash']}"
            )

        print("\nGenerated Pollen Fetch Script:")
        print(cache.generate_pollen_fetch_script("chat_abc123", "/tmp/kv_cache"))

    asyncio.run(run_mock())
