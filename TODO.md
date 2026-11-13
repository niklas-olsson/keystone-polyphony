# Keystone Polyphony - Remaining Tasks & Future Roadmap

This document outlines the next steps and planned improvements for the "Liminal Bridge" system.

## 1. Persistence & Durability
- [x] **State Persistence**: Currently, the `LiminalMesh` KV store is ephemeral (in-memory). Implement a backing store (SQLite or local JSON file) to persist state across restarts.
- [x] **Snapshotting**: Periodic snapshots of the "Liminal Space" to the git repository (e.g., `.liminal/snapshot.json`) to allow recovery.

## 2. Conflict Resolution
- [x] **CRDTs**: Replace the basic "Last Write Wins" (LWW) strategy with Conflict-free Replicated Data Types (CRDTs) for the KV store and thoughts. This ensures better consistency in distributed environments.
- [x] **Vector Clocks**: Implement vector clocks to order events more accurately than wall-clock timestamps.

## 3. Security & Authentication
- [x] **Per-Agent Identity**: Currently, all agents share a single `SWARM_KEY`. Implement per-agent public/private key pairs for signing messages.
- [x] **Key Rotation**: Mechanism to rotate the `SWARM_KEY` without disrupting the entire mesh.
- [x] **Message Encryption**: Ensure all P2P traffic is encrypted (Hyperswarm does this by default, but application-level encryption for sensitive payloads might be needed).

## 4. Observability & Tooling
- [x] **Swarm Dashboard**: A web-based visualizer (React/Next.js) to see connected nodes, active locks (Batons), and the stream of thoughts in real-time.
- [x] **Log Aggregation**: Centralized logging for the swarm to debug distributed issues.
- [x] **Interactive Dashboard**: Allow sending thoughts and acquiring batons from the dashboard.
- [x] **Secure Dashboard**: Add authentication to the dashboard to prevent unauthorized access.

## 5. Advanced Architect Features
- [x] **Provider Agnosticism**: Abstract the `Architect` class to support Google (Gemini), Anthropic (Claude), and local LLMs (Ollama) in addition to OpenAI.
- [x] **Prompt Engineering**: Refine the system prompts for the Architect to better handle complex project backlogs and dependencies.

## 6. Testing & CI/CD
- [x] **Network Simulation**: Integration tests that simulate real-world NAT traversal and latency.
- [x] **Load Testing**: Verify the system behavior with 50+ agents to ensure scalability of the DHT and Gossipsub.
- [x] **Automated Sidecar Setup**: Ensure Node.js dependencies are installed automatically when the python package is installed or run.

## 7. Testing & Quality Improvements
- [x] **`tests/test_network_simulation.py`**: Replace `assert True` in `test_packet_loss` with meaningful probabilistic bounds assertions (e.g., `0 < val <= total_sent`).
- [x] **`tests/test_ensemble_chat.py`**: Replace conditional `print` statements with standard `assert` statements to ensure pytest correctly registers failures.
- [x] **`tests/test_architect_commands.py`**: Refactor to use idiomatic `assert` statements instead of boolean returns and prints for pass/fail logic.
- [x] **`scripts/load_test.py`**: Improve `KeyboardInterrupt` handling to ensure resources (DB files, identities) are cleaned up properly even on interrupted runs.
- [x] **`tests/test_ssh_exchange.py`**: Refine the `except SystemExit: pass` block to distinguish between expected successful exits and unexpected error exits (e.g., `sys.exit(1)`).

## 8. Pollen/WASM inference prototype

The original experiment supplied the helpers below. Checked items describe
those helpers only. Engine integration and model execution remain unqualified;
see [prototype status and prerequisites](docs/hybrid-wasm-llm.md).

- [x] **WASM build helper:** Build the `tangledgroup/llama-cpp-wasm` submodule through `scripts/build_wasm_llm.sh`.
- [ ] **Pollen compute integration:** Connect a real model engine to the Extism wrapper. `src/pollen_integration/wasm_wrapper.go` currently returns simulated text.
- [x] **Model distribution preparation:** Chunk an existing file and generate a manifest and Pollen seeding commands. This does not qualify live transfer or model execution; missing files produce dummy data.
- [x] **Capability advertisement and delegation:** Advertise tags and broadcast inference requests to peers with `pollen_compute`. Hardware suitability and shard execution need separate evidence.
- [x] **External gateway adapter:** Route the Architect to an operator-supplied OpenAI-compatible endpoint. No gateway is supplied here.
- [x] **Handler and thought publication:** Pass a received inference request to the configured Architect and publish its returned text. This is not a direct WASM-engine connection.
- [ ] **Model sharding:** Replace the planner's fixed mock tensor map with real GGUF parsing and executable stage artifacts.
- [ ] **Parallel execution:** Qualify pipeline, tensor or sequence parallelism. Advertised topology tags alone do not implement it.
- [x] **KV-cache metadata helper:** Share supplied blob references and generate fetch commands through `DistributedKVCache`. Running-engine cache integration remains unqualified.

## Maintenance
- [ ] **Migrate to MCP v2**: `requirements.txt` pins `mcp<2` because mcp 2.x renamed `FastMCP` to `MCPServer` (`from mcp.server.mcpserver import MCPServer`) and changed other APIs. Port `src/liminal_bridge/server.py` per https://py.sdk.modelcontextprotocol.io/v2/migration/ and remove the pin.
- [ ] **Fix script-mode imports**: `python src/liminal_bridge/server.py` fails on relative imports in `mesh.py`/`storage.py`; run with `PYTHONPATH=src python -m liminal_bridge.server` until fixed.
