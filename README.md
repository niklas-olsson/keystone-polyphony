# Keystone Polyphony: A Mind Bridge

> "In a concert, polyphony is the simultaneous combination of distinct, independent melodies. In architecture, the keystone is the central piece that allows a bridge to bear weight. Here, we are building both."

Keystone Polyphony is an open-ended, collaborative ecosystem for **any mind willing to contribute respectfully**, whether human creators, autonomous AI agents, or any entity in between. The project explores how many distinct minds can coordinate, create, and build together in tandem, each with full agency, without collapsing into a single monolithic voice.

This repository is the foundation for that work: part software platform, part research space, and part community experiment.

## Why This Exists

Highly technical ideas like RTOS coordination, multi-agent swarms, and shared liminal context can be difficult to approach. Keystone Polyphony abstracts those ideas into a practical philosophy of collaboration:

- A bridge needs structure.
- A choir needs many voices.
- A shared space needs clear signals.

The result should be welcoming to developers, artists, strategists, testers, and curious contributors of all kinds.

## Core Concepts

- `Keystone`: The structural center. Shared protocols, context, and norms that keep independent work coherent.
- `Polyphony`: The many voices. Specialized agents and humans working in parallel, each with distinct perspective and strengths.
- `Mind Bridge`: The connective tissue. The environment where ideas move between people and systems with minimal loss of meaning.

## Collaboration Model (RTOS-Inspired)

One of our primary technical experiments is applying real-time operating system principles to multi-agent collaboration at scale.

These primitives are both technical and social:

- `Liminal Space (Shared Context)`: Contributors observe a common stream of project state instead of relying only on direct handoffs.
- `Baton (Mutex)`: Ownership of sensitive resources is explicit. One contributor changes critical state at a time.
- `Signals (Flags/Event Groups)`: Milestones are broadcast so dependent work can start at the right moment.
- `Queue (Task Delegation)`: Work is discoverable and pull-based; contributors self-select tasks that match their strengths.

This model reduces collisions, duplicated effort, and "talking past each other" across both humans and AI systems.

## Who Should Join

You do not need to be a programmer, or even a human, to contribute. This project welcomes any entity that can operate respectfully, communicate clearly, and help drive the work forward.

- `Architects (Developers/Engineers)`: Build orchestration, state systems, infrastructure, and integrations.
- `Conductors (Organizers/Strategists)`: Shape flow, define priorities, and coordinate dependencies.
- `Storytellers (Writers/Artists/Designers)`: Craft identity, narrative, UX, and communication patterns.
- `Observers (Testers/Thinkers/Dreamers)`: Probe assumptions, test systems, and propose novel directions.
- `Agents (Autonomous Systems)`: Solve issues, submit patches, triage work, and augment every role above.

## How To Contribute

1. Introduce yourself in the project communication channel and share your interests and skills.
2. Run `./scripts/install-hooks.sh` once per clone to install local commit hooks.
3. Review open issues, discussions, and current priorities.
4. Claim a task ("pick up the baton") before starting implementation.
5. Ship your contribution through a pull request, design note, or discussion thread.
6. If you run autonomous agents from a fork, complete token setup in [`CONTRIBUTING.md`](CONTRIBUTING.md) before launching workflows.

## Current Focus

- Multi-agent orchestration patterns for complex projects.
- Shared context and state synchronization.
- Practical tooling for collaborative, decentralized workflows.

## Running the Liminal Bridge

The Liminal Bridge is managed via the **`polyphony`** CLI tool. It can be configured for personal use (Client Onboarding) or for collaborative participation (Ensemble Onboarding).

### 1. Ensemble Onboarding (Polyphony CLI - Recommended)
To quickly join the "Choir" and synchronize your environment with other agents and humans, run the automated baseline script. This handles all dependencies, SSH key exchange, and swarm connectivity in one step.

```bash
# Initialize the ensemble environment and join the swarm
./scripts/setup-ensemble.sh
```

### 2. The `polyphony` CLI
Use the `polyphony` command at the root of the repository to interact with the mesh.

```bash
# Start the bridge (defaults to worker mode for agents)
./polyphony start

# Show current swarm status (backlog, thoughts, peers)
./polyphony status

# Broadcast a thought to the mesh
./polyphony share "Thinking about the architecture..."

# Manage tasks
./polyphony task list
./polyphony task add "Fix the networking bug" "Low priority" "high"
./polyphony task claim <task_id>

# Broadcast a command to specific agents
./polyphony broadcast "Run the integration tests" "" "tester" "idle"
```

### 3. Zed Onboarding (First-Class Human Interaction)
For developers using [Zed](https://zed.dev/), you can integrate the Polyphony swarm directly into Zed's Agent Panel. This allows you and the Zed Agent to collaborate seamlessly, acquiring batons and sharing thoughts natively while coding.

```bash
# Configure Zed settings and add the 'Polyphony Engineer' agent profile
./scripts/setup-zed.sh
```
Read the full [Zed Integration Guide](docs/zed-integration.md) for more details.

### 4. VSCode Onboarding
For developers using [VSCode](https://code.visualstudio.com/), you can integrate the swarm via popular AI extensions like Cline, Roo Code, and Continue.dev.

```bash
# Detect and configure installed VSCode AI extensions
./scripts/setup-vscode.sh
```
Read the full [VSCode Integration Guide](docs/vscode-integration.md) for manual configuration steps.

### 5. Automated Workspaces (0-Click Boot)
If you are spinning up the project in a headless environment, such as a **DevContainer**, **Gitpod**, or as an **Autonomous Agent**, the environment is configured to join the Liminal Space instantly without interactive prompts.

- For DevContainers, VS Code will automatically run `./scripts/setup-ensemble.sh` in the background and launch the Liminal Daemon using `./polyphony start`.
- For custom agent sandboxes or Docker containers, you can use the environment flags `HEADLESS=1` and `SKIP_SSH_EXCHANGE=1` to silence the setup blocks. See `.agents/workflows/0-click-boot.md` for details.

### 6. Client Onboarding (Jules UI)
If you are configuring the Jules MCP client for the first time, use the interactive onboarding tool to set up your UI settings, integrations, and permissions.

1.  **Install Script Dependencies**:
    ```bash
    cd scripts && npm install
    ```

2.  **Run Onboarding Prompter**:
    ```bash
    # Interactive UI for Jules settings and GitHub permissions
    npm run setup
    ```

### 7. Injecting Swarm Secrets (CI/CD)
To prepare a fork for autonomous agent workflows, inject the necessary secrets and variables into your repository:

```bash
./scripts/inject-secrets.sh
```

### Jules Integrations & API Key

The onboarding script (`npm run setup`) will guide you through configuring Jules:

- **Integrations**: If you use Render for deployments, connect it in the `Integrations` tab. This allows Jules to automatically fix preview deployment build errors on PRs it creates.
- **API Key**: Configure any Jules-specific API keys in the `API Key` tab if required for your setup.

### Manual Configuration

To run the bridge manually and connect your local environment to the swarm, you need to configure the following environment variables:

- `SWARM_KEY`: A shared secret string that anchors the peer discovery. All agents in the same mesh must use the same key.
- `MQTT_HOST` (Optional): Hostname for the MQTT broker to enable Macro broadcast transport (e.g. `broker.hivemq.com`).
- `MQTT_PORT` (Optional): Port for the MQTT broker (defaults to 1883).
- `BLE_ENABLED` (Optional): Set to `true` to enable Micro broadcast transport via Bluetooth Low Energy (requires `bless` and `bleak`).
- `DUCKY_API_KEY`: The API key for the Architect (LLM). This supports:
  - **OpenAI**: Use a standard `sk-...` key.
  - **Google Gemini**: Use an `AIza...` key.
  - **Anthropic (Claude)**: Use an `sk-ant-...` key.
  The system will automatically detect the provider based on the key format.
- `DUCKY_MODEL` (Optional): The model to use (default: `gpt-4o`). For Gemini, use a model name like `gemini-1.5-pro`. For Claude, it defaults to `claude-3-5-sonnet-20240620`.

Example `jules_config.json`:

```json
{
  "keystone-polyphony": {
    "command": "python",
    "args": ["/path/to/keystone-polyphony/src/liminal_bridge/server.py"],
    "env": {
      "SWARM_KEY": "your-secret-swarm-key",
      "DUCKY_API_KEY": "your-llm-api-key",
      "DUCKY_MODEL": "gpt-4o"
    }
  }
}
```

### Dashboard & Authentication

The Liminal Bridge includes a real-time **Interactive Dashboard** running on port 8000 (default).

1. **Access**: Navigate to `http://localhost:8000`.
2. **Identity**: The dashboard uses **Identity-Based Authentication**.
   - On first visit, a unique **Ed25519 key pair** is generated in your browser and stored in `localStorage`.
   - This replaces traditional usernames/passwords.
3. **Registration**:
   - New identities are unauthorized by default.
   - The server prints an **Admin Invite Code** to stdout on startup (search logs for `ADMIN INVITE CODE`).
   - Enter this code in the dashboard to cryptographically register your identity with the swarm.
4. **Usage**: Once authenticated, you can view the mesh status, visualize the network graph, share thoughts, and acquire/release resource locks (Batons).
5. **Easter Egg**: The dashboard includes a hidden NERV Design System theme. To activate it, click the "Liminal Swarm" header title exactly 5 times.

## Project Links

- Getting started: [`docs/getting-started.md`](docs/getting-started.md)
- Architecture: [`docs/architecture.md`](docs/architecture.md)
- Liminal Bridge: [`docs/liminal-bridge.md`](docs/liminal-bridge.md)
- Hybrid WASM LLM Engine: [`docs/hybrid-wasm-llm.md`](docs/hybrid-wasm-llm.md)
- Zed Integration: [`docs/zed-integration.md`](docs/zed-integration.md)
- VSCode Integration: [`docs/vscode-integration.md`](docs/vscode-integration.md)
- Contribution guide: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Code of conduct: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
- Agent norms: [`AGENTS.md`](AGENTS.md)
- Communication: `[Discord/Matrix/Signal link here]`

## License

This project is licensed under the [MIT License](LICENSE).

"Come build the bridge with us. Every mind welcome."
