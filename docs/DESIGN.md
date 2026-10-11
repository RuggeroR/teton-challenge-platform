# Teton Wi-Fi Provisioning: Design

Version 1.2, 2026-10-11. This is the implementation design: how the code is organised and how the pieces interact. **What** the system must do is in [SPEC.md](SPEC.md); **why** each choice was made is in [plan_v5.md](plan_v5.md). 1.1 split the transport interface (§5) and moved the BLE spike first (§9); 1.2 applies the post-spike simplifications S1–S4 (§4, §5, §9). The submission writeup for Teton is [ARCHITECTURE.md](ARCHITECTURE.md), to be written at the end.

## 1. System overview

```
┌──────────────── Laptop B: Configurator ───────────────┐        ┌──────────────── Laptop A: Teton Device ────────────────┐
│ cli.py ─► flow.py (guided steps, plain-language text) │        │ cli.py ─► service.py (orchestrator)                    │
│              │                                        │        │              │                                         │
│          client.py (protocol: handshake, commands)    │        │          session.py (protocol: handshake, dispatch)    │
│              │                     │                  │        │              │                  │                      │
│      teton_common: crypto, framing, messages          │        │      teton_common: crypto, framing, messages            │
│              │                                        │        │              │                  │                      │
│   ble_client.py (bleak)  │  sim_client.py (socket)    │ ◄─BLE─►│   gatt_server.py (bless) │ sim_server.py (socket)       │
└───────────────────────────────────────────────────────┘        │              state.py   storage.py   qr.py             │
                                                                 │              wifi/: backend ─► nmcli.py | fake.py      │
                                                                 │                     connectivity.py                    │
                                                                 └──────────────────────────┬─────────────────────────────┘
                                                                                            ▼
                                                                                  NetworkManager ─► Wi-Fi AP
```

Principles:
- **One protocol implementation, many transports.** The handshake, crypto and message handling never know whether bytes travel over BLE, a Unix socket (simulation) or memory (tests).
- **Side effects behind interfaces.** Wi-Fi (`WifiBackend`), transport (`DeviceTransport`, `ClientTransport`) and clock/randomness are injected, so the core logic is testable without hardware.
- **Device is the authority.** It owns the state machine, verifies everything and decides success. The configurator is a thin, friendly client.

## 2. Repository layout

One repository holds both sides plus the shared code. Both sides must speak exactly the same protocol, so they share `teton_common` and are versioned together.

```
CLAUDE.md                     guidance for Claude Code sessions
README.md                     setup, hardware, demo steps, troubleshooting (submission)
pyproject.toml                one package, three importable modules, two console scripts
docs/
  plan_v5.md                  decisions and rationale (source of truth; plan_v4.md kept for history)
  IMPLEMENTATION_PLAN.md      build order, steps and open review questions
  SPEC.md                     requirements and protocol
  DESIGN.md                   this file
  ARCHITECTURE.md             submission writeup (written last)
  research/channels.md        channel comparison used in the writeup
  evidence/                   logs, screenshots, btmon captures
src/teton_common/             shared by both sides, no I/O
  protocol.py                 UUIDs, versions, enums (security types, states, reasons)
  messages.py                 message dataclasses + JSON (de)serialisation + validation
  framing.py                  length-prefix framing, chunking, reassembly
  crypto.py                   X25519/HKDF/HMAC/AES-GCM handshake and envelope
  setup_code.py               generate, format, normalise, validate setup codes
  transport.py                DeviceTransport / ClientTransport interfaces (4 logical channels)
  memory_transport.py         in-memory paired transport for tests
  log.py                      structlog config + secret redaction processor
src/teton_device/
  __main__.py, cli.py         `teton-device run|reset|show-code`
  service.py                  wires transport + session + wifi + state; lifecycle
  session.py                  per-connection protocol handler (handshake, commands, rate limit)
  state.py                    provisioning state machine
  storage.py                  state dir, state.json (device_id, setup_code), permissions
  qr.py                       terminal QR + demo note
  gatt_server.py              BLE transport (bless / BlueZ)
  sim_server.py               Unix-socket transport for --simulate
  wifi/backend.py             WifiBackend interface + result types
  wifi/nmcli.py               NetworkManager implementation (subprocess)
  wifi/fake.py                simulated networks for --simulate and tests
  wifi/connectivity.py        IP, NM connectivity check, gateway ping
src/teton_configurator/
  __main__.py, cli.py         `teton-configurator [--simulate]`
  flow.py                     guided steps and prompts
  client.py                   protocol client (handshake, scan, set_credentials, await result)
  ble_client.py               BLE transport (bleak): discovery + GATT
  sim_client.py               Unix-socket transport for --simulate
  ui_text.py                  plain-language strings, failure reason → message
web/                          COULD: Web Bluetooth phone page
deploy/                       SHOULD: systemd unit, polkit rule
scripts/                      SHOULD: preflight check, hostapd Enterprise test AP
tests/                        pytest suite (see §7)
```

## 3. Main flow

```
Configurator                                        Device
 discover (BLE scan for service UUID) ───────────►  advertising
 read info ──────────────────────────────────────►  {device_id, caps, state}
 hello(pub_c, nonce_c) ──────────────────────────►  derive keys
                     ◄────────────────────────────  hello_reply(pub_d, nonce_d, confirm_d)
 verify confirm_d, send confirm(confirm_c) ───────►  verify → SESSION_ESTABLISHED
                     ◄────────────────────────────  enc(ready)
 enc(scan) ──────────────────────────────────────►  nmcli scan
                     ◄────────────────────────────  enc(scan_result)
 enc(set_credentials) ───────────────────────────►  CONNECTING: nmcli add + up
                     ◄────────────────────────────  enc(status connecting)
                                                    verify IP, connectivity, ping
                     ◄────────────────────────────  enc(status connected | failed+reason)
 on failed: retry from scan or credentials          on failed: delete profile, back to SESSION_ESTABLISHED
 on connected: done                                 on connected: PROVISIONED → stop BLE server, exit
```

## 4. Device state machine

```
UNPROVISIONED ──run──► ADVERTISING ──handshake ok──► SESSION_ESTABLISHED ──set_credentials──► CONNECTING
      ▲                    ▲   ▲                            ▲                                   │
      │                    │   └── idle timeout / new hello ┤                                   ├─► CONNECTED ─► PROVISIONED (process exits)
      │                    │                                └───────── failed(reason) ─────────┘
      └──────── reset (local CLI only) ◄──────────────────────────────────────────────── PROVISIONED
```

- `state.py` validates transitions and raises on invalid ones. Every transition is logged.
- `PROVISIONED` is persisted (in `state.json`). After it, `run` stops the BLE server and exits; on restart, a provisioned device does not advertise (plan_v5, S4).
- The device does not track BLE connections (plan_v5, S3). One connection at a time is guaranteed by the radio: a connected peripheral stops advertising, and BlueZ restarts advertising after the disconnect (S1). A new `hello` ends any previous session; otherwise a session ends on the 300 s idle timeout or a protocol error.
- A connection attempt that has started always finishes, even if the configurator is gone. The result is logged, and if it failed the profile is rolled back.

## 5. Key interfaces (contracts for implementation)

```python
class DeviceTransport(Protocol):                 # teton_common/transport.py (device side)
    async def start(self) -> None: ...                           # serve + advertise
    def set_info(self, data: bytes) -> None: ...                  # value served on INFO reads
    def on_receive(self, channel: Channel, cb: Callable[[bytes], Awaitable[None]]) -> None: ...
    async def send(self, channel: Channel, data: bytes) -> None: ...
    async def close(self) -> None: ...                           # stop serving; ends advertising

class ClientTransport(Protocol):                 # teton_common/transport.py (configurator side)
    async def connect(self) -> None: ...
    async def read_info(self) -> bytes: ...
    def on_receive(self, channel: Channel, cb: Callable[[bytes], Awaitable[None]]) -> None: ...
    async def send(self, channel: Channel, data: bytes) -> None: ...
    async def close(self) -> None: ...
# Channel = INFO | SESSION | COMMAND | STATUS. INFO is a plain read (not framed);
# framing (fixed 20-byte chunks) is applied above the transport for SESSION, COMMAND and STATUS.
# No connect/disconnect events and no MTU: see plan_v5 S2, S3.

class WifiBackend(Protocol):                     # teton_device/wifi/backend.py
    async def scan(self) -> list[Network]: ...
    async def connect(self, creds: Credentials, timeout_s: float) -> ConnectResult: ...
    async def forget(self, profile_name: str) -> None: ...

class Connectivity(Protocol):                    # teton_device/wifi/connectivity.py
    async def verify(self, interface: str) -> VerifyResult: ...   # ip, internet: full|limited|portal|none

class DeviceSession:                             # teton_device/session.py
    async def handle_session_msg(self, data: bytes) -> None: ...  # hello / confirm
    async def handle_command(self, data: bytes) -> None: ...      # encrypted scan / set_credentials

class ProvisioningClient:                        # teton_configurator/client.py
    async def handshake(self, setup_code: str) -> DeviceInfo: ...
    async def scan(self) -> list[Network]: ...
    async def provision(self, creds: Credentials) -> AsyncIterator[Status]: ...
```

`crypto.py` exposes a pure, synchronous API: `Handshake` objects for each role that produce and consume handshake messages, and an `Envelope` (`seal(plaintext) -> bytes`, `open(bytes) -> plaintext`) that tracks sequence numbers.

## 6. Error handling

- Protocol violations (bad JSON, unexpected `seq`, decryption failure, wrong state) end the session, are logged with a reason, and never crash the service.
- Wi-Fi failures are not protocol errors. They become `status failed` with a reason (SPEC §7), and the session continues.
- External commands (`nmcli`, `ping`) run through one runner function, with timeouts. Their raw output goes to the debug log only, never to the configurator, and is redacted. Secrets are passed through stdin, never in argv (SPEC §5.5).
- The configurator turns every failure into one plain-language sentence plus a next action (retry, retype the code, contact IT).

## 7. Testing strategy

| Layer | Tests | Hardware |
|---|---|---|
| `setup_code` | generate/format/normalise/validate, ambiguous characters | none |
| `crypto` | round trip; wrong code fails confirmation; tampered ciphertext, replayed or skipped `seq` rejected; distinct keys per direction | none |
| `framing` | chunk size 20 (the fixed size in use) and a larger one, so framing stays generic, max size, interleaved channels, timeout reset | none |
| `messages` | schema validation, bad input rejected | none |
| `state` | allowed/forbidden transitions, persistence of PROVISIONED | none |
| `wifi/nmcli` | parse recorded `nmcli` outputs (fixtures) into networks and failure reasons; with a fake runner, no secret ever appears in argv; per-interface success rule (full / limited + gateway / portal / gateway down) | none |
| `log` | no secret appears in captured logs during a full simulated run; error codes such as `bad_request` stay visible | none |
| end to end | device service + configurator client over `memory_transport` with `wifi/fake` (PSK ok, wrong password then retry, wrong code, portal) | none |
| manual | real BLE + real Wi-Fi on the two laptops; `btmon` capture; Enterprise via hostapd | yes |

CI (SHOULD): GitHub Actions on `ubuntu-24.04` runs `ruff check`, `ruff format --check` and `pytest`. The radio libraries are imported lazily, so CI needs no Bluetooth.

## 8. Dependencies

| Package | Used by | Why |
|---|---|---|
| `cryptography` | common | Audited X25519, HKDF, HMAC, AES-GCM |
| `structlog` | common | Structured JSON logs + redaction processor |
| `bless` | device | BLE GATT server on BlueZ via D-Bus |
| `bleak` | configurator | BLE client, cross-platform |
| `qrcode` | device | Terminal QR |
| `pytest`, `pytest-asyncio`, `ruff` | dev | Tests and lint/format |

Everything else comes from the standard library (`asyncio`, `json`, `secrets`, `subprocess`, `argparse`).

## 9. Known risks (from plan_v5 §10)

- BlueZ peripheral quirks: the hardware spike (IMPLEMENTATION_PLAN Step 0.2) confirmed `bless` 0.3.0 works on laptop 1 without root or pairing. It also showed that `bless` hides the MTU and read offsets; we avoid needing them with fixed 20-byte chunks and a short `info` (plan_v5, S2). The behaviour S1, S3 and S4 rely on was measured on laptop 1's adapter; re-run the spike if laptop 2 ever becomes the device.
- Laptop A (laptop 1) is already on Wi-Fi: provisioning switches networks. On failure, the rollback relies on NetworkManager reconnecting the previous profile.
- Running without root: polkit allows the active desktop user to manage connections. BlueZ may need `sudo` or a group, so the README documents both.
- Enterprise testing needs AP mode on laptop B's Wi-Fi card (`iw list`). If it isn't supported, Enterprise is tested in simulation only, and the writeup says so.
