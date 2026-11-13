# Pollen/WASM integration: prototype status

Polyphony includes helpers for a Pollen/WASM inference experiment. The supplied
[Extism wrapper](../src/pollen_integration/wasm_wrapper.go) returns **simulated
text**: it uppercases the prompt and adds a fixed suffix. It loads no model,
calls no llama.cpp binding and emits no generated token stream.

These components do not establish a working local or distributed model runtime.
This status describes the integration supplied here, not Pollen's capabilities
in other deployments. The original prototype contributions remain available in
[the wrapper history](https://github.com/niklas-olsson/keystone-polyphony/commit/69ff29f09fb176a7b072e5347a4ac27da634bb54)
and [the guide history](https://github.com/niklas-olsson/keystone-polyphony/commit/252f6466c37f6e336524607f32dfb50899e57998).

## Available helpers and their limits

| Component | Supplied behavior and boundary |
| --- | --- |
| [WASM build helper](../scripts/build_wasm_llm.sh) | Builds the `tangledgroup/llama-cpp-wasm` submodule and copies its output to `dist/wasm_llm/`. It does not connect that engine to the Extism wrapper. |
| [Model distributor](../src/liminal_bridge/gguf_distributor.py) | Chunks an existing file, records SHA-256 hashes and writes a manifest and `pln seed` script. A missing input instead creates 500 MiB of dummy chunks with synthetic metadata. |
| [Pipeline planner](../src/liminal_bridge/gguf_sharder.py) | Uses a fixed mock 32-layer tensor map. Its stage plan does not read the supplied model's tensor metadata or create the referenced `stage_*.gguf` files. |
| [Inference delegation](../src/liminal_bridge/mesh.py) | Broadcasts `run_inference` to peers advertising `pollen_compute`. The [server handler](../src/liminal_bridge/server.py) calls its configured Architect and publishes the returned text as a thought. This does not invoke the wrapper directly or establish distributed execution. |
| [KV-cache metadata](../src/liminal_bridge/distributed_kv_cache.py) | Shares caller-supplied blob references through a CRDT and generates fetch commands. It does not connect model tensors to a running engine. |

Capability and topology tags describe advertised properties. They do not prove
hardware suitability, tensor parallelism or a qualified inference result.

## Development prerequisites and outputs

The WASM build helper needs Git, Bash, network access and the upstream build
prerequisites. It initializes the submodules and installs the latest Emscripten
SDK if `emcc` is absent. On Windows it also enables Git's global `core.longpaths`
setting. Its output is an upstream build artifact, not an integrated Pollen
deployment. No engine/model/environment combination is qualified by this guide.

The [model preparation helper](../scripts/prepare_mesh.sh) requires the repository
Python environment in `.venv`, a real input file and storage for the output
chunks. Check that the input exists before use; missing-file dummy output is
not model data. From the repository root, for an existing model:

```bash
./scripts/prepare_mesh.sh /absolute/path/model.gguf --nodes 4 --chunk-size 50 --out-dir ./mesh_deployment
```

This writes byte chunks, `manifest.json`, `seed_model_to_pollen.sh` and a
**mock-based** `pipeline_topology_plan.txt`. It does not create runnable model
shards. Review generated commands before use. Seeding additionally requires a
separately installed and configured Pollen CLI; blob transfer alone does not
demonstrate inference.

## Optional external gateway

The [Architect adapter](../src/liminal_bridge/architect.py) requires an externally
supplied gateway implementing `/v1/chat/completions` and its expected response
shape. This repository does not supply that gateway. With no `DUCKY_PROVIDER`
override, the `pollen:` model prefix selects the adapter:

```bash
export DUCKY_MODEL="pollen:my_distributed_model"
export POLLEN_GATEWAY_URL="http://127.0.0.1:3000"
```

These settings only select a model name and endpoint for a separately started
Architect. They neither launch a gateway nor load a local model. If omitted,
the gateway URL defaults to `http://localhost:3000`. A loopback URL does not
establish browser-local inference or where the gateway performs its work.

## Evidence needed for an inference claim

A working integration needs its exact engine and model revisions, build/runtime
environment, gateway dependency, resource requirements and retained model result.
Keep simulated output, local model execution and connected execution distinct.
The wrapper and helpers above supply no such end-to-end qualification.
Connectivity or model output also grants no [protected-task authority](managed-runtime.md).
