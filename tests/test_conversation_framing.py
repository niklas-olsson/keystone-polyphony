"""Required Node boundary tests; missing locked prerequisites are failures."""

import asyncio
import json
from pathlib import Path
import shutil
import subprocess

from src.liminal_bridge.conversation import MAX_PAGE_BYTES, canonical
from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.storage import SQLiteStorageProvider

ROOT = Path(__file__).resolve().parents[1]


def test_restricted_node_stream_boundaries():
    node = shutil.which("node")
    assert node is not None, "Node is required for conversation stream tests"
    package = ROOT / "src/liminal_bridge/sidecar/node_modules/streamx/package.json"
    assert package.is_file(), "Run npm ci in src/liminal_bridge/sidecar first"
    assert json.loads(package.read_text())["version"] == "2.23.0"
    result = subprocess.run(
        [node, "--test", "tests/test_conversation_framing.js"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_full_production_signed_encrypted_page_fits_actual_node_reader(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("src.liminal_bridge.conversation.time.time", lambda: 1000.0)
    mesh = LiminalMesh(
        "bounded-page-fixture",
        SQLiteStorageProvider(str(tmp_path / "page.db")),
        identity_path=str(tmp_path / "identity.pem"),
    )
    store = mesh.require_conversation().store
    sent = []

    async def submit(operation, envelope):
        assert operation == "broadcast"
        sent.append(envelope)

    mesh._send_to_sidecar = submit
    try:
        for key in ("first", "other"):
            store.post("page", "x" * 8192, request_key=key)
        empty = store.post("page", "", request_key="third")
        used = sum(len(canonical(item).encode()) for item in store.page(0)["records"])
        size = len(canonical({"cursor": 4, "record": empty}).encode())
        remaining = MAX_PAGE_BYTES - used - size
        assert 0 < remaining <= 8192
        store.post("page", "x" * remaining, request_key="forth")
        store.post("page", "held for next page", request_key="fifth")
        page = store.page(0)
        assert (
            sum(len(canonical(item).encode()) for item in page["records"])
            == MAX_PAGE_BYTES
        )
        assert page["has_more"] is True
        asyncio.run(
            mesh.broadcast(
                {
                    "type": "chat_page",
                    "target": "ab" * 8,
                    "request_id": "cd" * 16,
                    **page,
                },
                urgency="high",
            )
        )
        raw = (
            canonical({"type": "message", "peer_id": "ef" * 32, "payload": sent[0]})
            + "\n"
        ).encode()
        assert len(raw) <= 65536
        code = (
            "const f=require('./src/liminal_bridge/sidecar/conversation-framing');"
            "let seen=0;f.readBoundedJson(process.stdin,v=>{"
            "if(!f.isEnvelope(v.payload))throw Error();seen++;},"
            "()=>process.exit(2),()=>process.exit(seen===1?0:3));"
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", code],
            input=raw,
            cwd=ROOT,
            capture_output=True,
            timeout=3,
        )
        assert result.returncode == 0, result.stderr
    finally:
        mesh.storage.conn.close()
