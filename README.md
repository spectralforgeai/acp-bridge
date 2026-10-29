# ACP Bridge

A self-hosted, single-client relay between an authenticated WebSocket and a real [Agent Client Protocol (ACP)](https://agentclientprotocol.com/) agent speaking newline-delimited JSON-RPC over stdio. The Python package and command are `acp-bridge`; the module is `acp_bridge`. Version: **0.1.0**, Python **3.12**.

The backend is independent of glasses, iOS, Home Assistant, speech recognition, audio processing, models, and model vendors. It does not include an agent, model, MCP server, provider account, or credentials. You install and authorize those separately. There is **no built-in demonstration agent or fallback response**.

## Prerequisites and limits

- A Linux/POSIX host for the relay, with Python 3.12 and [uv](https://docs.astral.sh/uv/) (the container and CI use uv 0.13.6), or Docker Engine with the Compose plugin.
- A **real ACP stdio agent**, installed either on that host or on an independently managed host reached through a stdio transport such as SSH. Follow that agent's installation, authentication, provider, and tool-permission instructions. The relay cannot turn a generic chat CLI or an MCP server into an ACP agent.
- An absolute workspace path that exists **on the agent host**, and an agent that supports the ACP operations your client needs. The relay validates configuration structure, not agent compatibility, workspace existence, or provider authorization.
- A compatible native/custom client that can send a bearer header during a WebSocket handshake and speak the subset below. A browser's standard WebSocket constructor cannot set this header directly.
- For remote access, a trusted HTTPS/WSS endpoint, DNS/certificate provisioning as appropriate, and an ordinary WebSocket-capable reverse proxy. No Cloudflare account or other tunnel service is required.

Run **one process, one worker, and one replica**. Exactly one authenticated WebSocket may be active; another receives HTTP 429. This is a single-tenant deployment, not a multi-user hosted service. Each accepted connection starts a fresh agent process/transport; disconnect, timeout, or relay shutdown tears down its local process group before admitting another connection. There is no session persistence, automatic reconnection, or replay. SSH transport shutdown does not guarantee that arbitrary remote daemonized descendants stop: configure the remote agent account/service to contain and expire its own processes.

## Native quickstart

Clone the source and install the locked dependencies:

```sh
git clone https://github.com/spectralforgeai/acp-bridge.git
cd acp-bridge
uv sync --locked
umask 077
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Store the generated token in a password manager and put it in `BRIDGE_DEVICE_TOKEN` in `.env`. Use a fresh, randomly generated 256-bit token for each deployment, not a password or the example text. The accepted format is at least 43 ASCII characters with no whitespace. Treat possession of it as access to the configured agent and its tools.

**Edit `.env` before starting.** The required values are deliberately empty in the template. For a locally installed agent, replace the following paths and entire command argv with your agent's documented stdio launch command:

```dotenv
ACP_AGENT_COMMAND='["/absolute/path/to/your/acp-agent"]'
ACP_WORKING_DIRECTORY=/absolute/path/to/agent/workspace
```

Those paths are templates, not installed programs. `ACP_AGENT_COMMAND` is a nonempty JSON array of argv strings, executed without a local shell. Shell operators, pipes, variable expansion, and shell quoting inside an argument are not interpreted. Do not put secrets in command arguments. Install the agent outside the relay environment if it has incompatible dependencies.

Load only your own trusted `.env` file; the shell command below executes shell syntax in it. Keep the template's shell-compatible `KEY=value` / single-quoted JSON format:

```sh
set -a
. ./.env
set +a
uv run --locked acp-bridge --host 127.0.0.1 --port 8080
```

`python -m acp_bridge` is also supported in the installed environment. Do not launch Uvicorn separately with extra workers, access/debug frame logging, or different transport limits; the CLI configures the supported single-worker transport. Native agent/provider environment variables must be provisioned separately by the operator.

In another terminal:

```sh
curl --fail http://127.0.0.1:8080/healthz
```

The response is `{"status":"ok"}`. This proves only that the relay is serving HTTP after configuration validation. **Health is not agent readiness**: no agent is started until an authenticated client connects. Confirm real readiness by completing ACP `initialize`, agent-specific authentication if required, and `session/new` through that client, then a real prompt.

## Docker Compose with an external agent

The image contains the relay and OpenSSH client, **not your ACP agent or its credentials**. The portable example is SSH to an agent you administer. Do not use the native local-agent example inside the stock image: that binary is not installed there. Other transports require an operator-maintained image/installation with the real executable and its dependencies.

1. Clone the repository and prepare `.env` and a token as above. Install and authenticate a real ACP agent on the remote host. Choose a dedicated, least-privilege SSH account with access only to its intended workspace and tools; provision provider/MCP credentials on the agent host, not in this image. An unattended remote launch must produce **only ACP JSON-RPC on stdout**, without interactive login, shell banners, or a TTY.
2. Create a dedicated SSH key, authorize its public key on the agent account, and obtain the host public key/fingerprint through an authenticated channel (for example, the server console or your administrator). Construct `known_hosts` from the verified key. Do not blindly trust `ssh-keyscan` output, disable host-key checks, or use `accept-new`. Restrict the authorized key's command/forwarding capabilities where your agent deployment permits it.
3. Put the private key at `secrets/id_ed25519` and verified host keys at `secrets/known_hosts`. Uncomment **both** `volumes` mounts in `compose.yaml`. They are existing-file, read-only mounts; Docker will not create a missing source as a directory.
4. Replace the example account/host and remote executable below with your real values. Set the agent-side workspace. This template uses strict host verification, no password prompts, no forwarded agent, no port forwarding, and no TTY:

```dotenv
ACP_AGENT_COMMAND='["ssh","-F","/dev/null","-T","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes","-o","UserKnownHostsFile=/run/agent/known_hosts","-o","IdentitiesOnly=yes","-o","ForwardAgent=no","-o","ClearAllForwardings=yes","-o","ConnectTimeout=10","-i","/run/agent/id_ed25519","agent-user@agent.example","/absolute/path/to/your/acp-agent"]'
ACP_WORKING_DIRECTORY=/absolute/path/to/agent/workspace
```

The hostnames and executable above are placeholders requiring replacement, not service defaults. The final SSH argument is an administrator-chosen remote command; SSH executes it through the remote login shell. Use a fixed command and never interpolate client text. If the agent requires arguments, configure its remote command according to SSH and agent documentation. Arrange noninteractive key unlocking separately if using an encrypted key; the supplied command intentionally cannot prompt.

### Mount ownership and credentials

The container runs as the named user `acp-bridge`, **UID/GID `10001:10001`**. On a conventional rootful Linux Docker host, prepare the mounts after putting the real files in place:

```sh
chmod 700 secrets
sudo chown 10001:10001 secrets/id_ed25519
sudo chmod 400 secrets/id_ed25519
sudo chown root:root secrets/known_hosts
sudo chmod 444 secrets/known_hosts
chmod 600 .env
```

Keep the host `secrets/` directory owned by the deploying administrator. A read-only bind mount does not fix incorrect source permissions: UID 10001 must be able to read the key, while no other untrusted host user may read it. For rootless Docker, user-namespace remapping, Docker Desktop, or SELinux, arrange the corresponding mapped ownership/file-sharing/label policy rather than making the private key world-readable. The image creates `/run/agent`; no runtime root step or key copy is needed.

Additional credential/configuration files can be mounted with the same long-form read-only bind syntax under `/run/agent`, using existing sources and explicit ownership. The relay accepts its device token via **`BRIDGE_DEVICE_TOKEN` only**, not an undocumented `_FILE` variable. `.env` stays on the host and out of the image, but the injected token is visible to users who can inspect the container environment; restrict Docker/host administration access. The provided Compose service forwards only the listed relay variables, not arbitrary provider values from `.env`. Provision external agent credentials remotely, or deliberately configure additional mounts/environment for a transport that requires them. Never mount the Docker socket.

### Start and stop

After the required configuration and mounts are ready:

```sh
docker compose up --build -d
curl --fail http://127.0.0.1:8080/healthz
docker compose logs --tail 50 acp-bridge
docker compose down
```

Required empty/unset values fail Compose interpolation, and malformed values fail relay startup. A structurally valid command that cannot start/connect is detected only on client admission. The container health check is HTTP liveness, not SSH/provider readiness.

Compose binds **127.0.0.1:8080** only. It uses one service instance, a read-only root filesystem, no capabilities, `no-new-privileges`, bounded `/tmp` tmpfs, and process/memory/CPU/log limits. Do not scale it or add workers. Only `/tmp` is writable in the supplied setup; mounts remain read-only. The Docker context is allowlisted to package/build inputs, excluding Git history, `.env`, keys, tests, and runtime files. Python dependencies are installed from `uv.lock`; Python and uv base images are digest-pinned. Rebuild and deliberately update pins/lockfiles as security updates are adopted; a pinned base is not an automatic security-update mechanism.

## HTTPS/WSS and client configuration

Keep port 8080 private. Terminate TLS at your existing reverse proxy using a certificate trusted by the client; forward WebSocket upgrades and the `Authorization` header to `127.0.0.1:8080`. For example, a host-installed [Caddy](https://caddyserver.com/docs/quick-starts/reverse-proxy) can use:

```caddyfile
bridge.example {
    reverse_proxy 127.0.0.1:8080
}
```

Replace the domain and arrange DNS/certificate issuance (or your own trusted private CA). Caddy handles WebSocket upgrades. Nginx or another proxy is equally valid when configured for upgrades. A containerized proxy's loopback is not the host's loopback; explicitly design its private upstream networking rather than exposing the relay publicly. Set proxy idle timeouts to accommodate the relay's configured lifetimes. Preserve the header without logging it, do not log WebSocket bodies, and do not add bypass routes. No TLS is provided by the relay itself.

Configure your client with:

- Endpoint: `wss://your-domain/acp` (not `/healthz`). `ws://127.0.0.1:8080/acp` is for trusted local development only.
- Handshake header: `Authorization: Bearer <your-device-token>`. Never put the token in the URL/query string. There is no cookie or query-token authentication.
- ACP initialization and any authentication flow required by your selected agent, followed by a new session and prompts. Do not assume one agent's login, model identifier, or authentication method works for another.
- Explicit filesystem/terminal/permission handling appropriate to that client and your policy. The separately managed iOS client advertises no filesystem/terminal capabilities and declines permission requests; **the generic relay does not enforce that blanket capability policy for other clients**.

Rotate a token by changing the server environment and client secret, then restarting/recreating the relay. There is no live rotation endpoint or second-token overlap. Keep client tokens in the platform's secure credential store.

The companion iOS application is managed separately as a free TestFlight distribution for now. Open its **Settings > Agent connection** (or **Complete setup**), enter your HTTPS origin (for example, `https://bridge.example`, without `/acp`) and the same device token; the app constructs the authenticated WSS endpoint. This repository distributes the backend, not TestFlight invitations. Any compatible client can implement this transport without that application.

## Transport, ACP subset, and MCP

`/acp` is this project's **custom WebSocket JSON-RPC transport**, not a claim that remote WSS is a standardized ACP transport. Each WebSocket **text** message carries one JSON-RPC 2.0 object; the subprocess side carries one UTF-8 JSON object per newline. Binary messages, batches, malformed envelopes, duplicate JSON keys, and non-finite numbers are rejected. There is no WebSocket subprotocol negotiation or SSE endpoint.

Client-to-agent method allowlist:

- `initialize`
- `authenticate`
- `session/new`
- `session/prompt`
- `session/cancel`
- `session/close`

Other client methods return JSON-RPC `-32601`. Support for an allowed method still depends on the actual agent; the relay does not implement it for the agent. JSON-RPC responses and valid agent-to-client messages are relayed, including streamed session updates and permission requests. Client capabilities and prompt payloads remain opaque. Session listing/loading/resumption, model/configuration selection methods, and arbitrary extension methods are not supported by this client method subset.

For `session/new`, the relay overwrites `cwd` with `ACP_WORKING_DIRECTORY`, rejects nonempty client-supplied `mcpServers`, and supplies only administrator-owned `ACP_MCP_SERVERS`. The workspace is **not a sandbox**. Configure agent OS permissions, tool authorization, and MCP permissions separately. A capable client can advertise filesystem/terminal support and answer agent requests; decide whether that is acceptable before giving it the device token. Do not treat client capability declarations as a substitute for agent-side authorization.

ACP is the client-to-agent protocol here. **MCP is the agent-to-tool/server protocol**, not this WebSocket API: the relay neither runs MCP tools nor connects to integration endpoints. Optional `ACP_MCP_SERVERS` is a JSON array of ACP MCP descriptors interpreted by your agent. Its HTTP/SSE URLs, stdio commands, paths, headers, and environment are agent-side resources. Use descriptors your selected agent supports and keep their credentials private. The default `[]` means no additional MCP descriptors; it does not prohibit tools already configured in the agent.

## Configuration and deadlines

| Environment variable | Requirement / default |
| --- | --- |
| `BRIDGE_DEVICE_TOKEN` | Required; at least 43 ASCII non-whitespace characters; generate 256 random bits. |
| `ACP_AGENT_COMMAND` | Required; nonempty JSON argv array, no local shell. |
| `ACP_WORKING_DIRECTORY` | Required; absolute agent-side path. |
| `ACP_MCP_SERVERS` | Optional JSON array, default `[]`; administrator-owned descriptors. |
| `ACP_MAX_FRAME_BYTES` | `1048576`; allowed range 256–16777216 bytes. Applies to RPC payload/frame boundaries, not an audio-upload facility. |
| `ACP_STARTUP_SECONDS` | `20`; startup/first-agent-message deadline. |
| `ACP_IDLE_SECONDS` | `300`; maximum inactivity. |
| `ACP_SESSION_SECONDS` | `3600`; maximum connection lifetime, even if active. |
| `ACP_IO_SECONDS` | `10`; individual I/O deadline. |
| `ACP_TERMINATE_SECONDS` | `0.5`; local process-group TERM grace before KILL. |

Time values must be finite and positive. The CLI also sets bounded WebSocket queues and disables compression and access logs. Agent stderr is discarded, and transport failures are reported generically without commands, credentials, or exception details. No reconnect/replay or client-originated configuration of commands, workspaces, or MCP servers is offered.

## Security, data, and operations

The relay sees prompts, replies, ACP authentication payloads, tool metadata, and other content sent through the connection in memory and forwards them to the configured agent/transport. It does not intentionally persist conversation bodies or log the bearer token/frames. This is not an end-to-end encrypted channel through the relay: TLS terminates at your proxy, and SSH protects only its own hop. Proxies, host administrators, client software, agents, MCP servers, and external model providers have their own access, logging, retention, and processing policies. Self-hosting this relay does **not** imply that inference or tools run locally or that data never leaves your network.

The device token is removed from the launched subprocess environment; other relay environment values are inherited by that subprocess. ACP authentication and any secrets embedded in configured MCP descriptors are necessarily available to the agent. Use least privilege, avoid secrets in argv, and restrict host access. The relay's command/MCP ownership and admission controls are not a prompt-injection defense, a tool-approval system, or an agent sandbox. Audit and authorize the agent and each tool separately, including side-effecting actions and remote data access. Do not enable transport frame tracing or expose credentials in support reports.

### Troubleshooting

| Symptom | What to check |
| --- | --- |
| Compose refuses to start | Fill all three required `.env` values; JSON argv must be single-quoted as one dotenv value. |
| `Relay configuration is invalid` | Token length/ASCII/whitespace, JSON structures, absolute workspace, finite positive timeouts, and frame range. Errors intentionally omit secret values. |
| HTTP 401 on WebSocket handshake | Exactly one correctly formatted bearer header, matching server/client tokens, and proxy header forwarding. Some unsupported ASGI servers may expose denial as 403 instead. |
| HTTP 429 | The one connection slot is in use or its local subprocess cleanup has not completed. Close the existing client; do not scale the service. |
| `/healthz` works, ACP fails with 1011 | Real agent command availability, read permissions, verified SSH host keys, agent account/provider login, ACP-only stdout, and compatible protocol. Diagnose the agent separately as its service account without logging real request bodies or secrets. |
| SSH key cannot be read / permissions rejected | Read-only source file exists; container UID/mapped UID can read it; private key is not group/world-readable; host policy permits the mount. |
| Connection closes with 1008 | Startup, idle, or session deadline, or invalid protocol input. Check proxy lifetime settings as well. There is no automatic replay. |
| Permission/tool requests fail | Check your client's response policy and the agent/tool account permissions. The relay does not approve tools on your behalf. |

## Development checks and artifacts

From the repository root, with Python 3.12 and uv 0.13.6:

```sh
uv sync --locked --extra test
uv run --locked pytest -q
uv build
uv venv --python 3.12 /tmp/acp-bridge-install
uv pip install --python /tmp/acp-bridge-install/bin/python dist/*.whl
/tmp/acp-bridge-install/bin/acp-bridge --help
docker build --tag acp-bridge:local .
```

Use a fresh temporary install path. CI runs the locked behavior/boundary tests, builds and installs the wheel, preserves distribution artifacts, and builds the container. Test-only agent fixtures are not installed as a product agent or included in the container. Deployment readiness still requires your configured real agent/client path; a green test run or image build cannot validate your external credentials or tool policy.

## License

The backend source in this repository is [MIT licensed](LICENSE). That does not license separately distributed clients, agents, models, MCP servers, or their services; consult each component's own terms.
