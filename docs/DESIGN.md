# Teton Wi-Fi Provisioning: Design

Version 1.5, 2026-10-18. This is the implementation design: how the code is organised and how the pieces interact. **What** the system must do is in [SPEC.md](SPEC.md); **why** each choice was made is in [plan_v5.md](plan_v5.md). 1.1 split the transport interface (§5) and moved the BLE spike first (§9); 1.2 applied the post-spike simplifications S1–S4 (§4, §5, §9); 1.3 adds the device buttons and LED (`buttons.py`), the pairing window and `set_advertising` (plan_v5 D2: §2, §3, §4, §5, §7), and the Python 3.12 + 3.14 CI matrix (D1: §7); 1.4 applies the Phase 2 review decisions (Q13: five states, §4; Q4, Q5, Q6, Q22: session rules, §5, §6; Q24: `forget_all`, §5; Q10, Q11, Q17: nmcli backend, §2, §6; Q21, Q23: buttons, §4, §4.1, §5); 1.5 applies the Phase 3 review decisions so far (Q25–Q30: configurator texts, §6; Q31–Q37: the protocol client, §5). The submission writeup for Teton is [ARCHITECTURE.md](ARCHITECTURE.md), to be written at the end.

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
└───────────────────────────────────────────────────────┘        │     state.py  storage.py  qr.py  buttons.py (keys+LED) │
                                                                 │              wifi/: backend ─► nmcli.py | fake.py      │
                                                                 │                     connectivity.py                    │
                                                                 └──────────────────────────┬─────────────────────────────┘
                                                                                            ▼
                                                                                  NetworkManager ─► Wi-Fi AP
```

Principles:
- **One protocol implementation, many transports.** The handshake, crypto and message handling never know whether bytes travel over BLE, a Unix socket (simulation) or memory (tests).
- **Side effects behind interfaces.** Wi-Fi (`WifiBackend`), transport (`DeviceTransport`, `ClientTransport`), buttons (`ButtonSource`), the status line and clock/randomness are injected, so the core logic is testable without hardware.
- **No text input on the device.** Its only inputs are simulated physical buttons; everything typed happens on the configurator (plan_v5, D2).
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
  __main__.py, cli.py         `teton-device run [--simulate] [--state-dir] [--verbose]`
  service.py                  wires transport + session + wifi + state + buttons; lifecycle
  buttons.py                  key reader (termios/tty), pairing window, reset double press, status line ("LED")
  session.py                  per-connection protocol handler (handshake, commands, rate limit)
  state.py                    provisioning state machine
  storage.py                  state dir, state.json (device_id, setup_code), permissions
  qr.py                       terminal QR + demo note
  gatt_server.py              BLE transport (bless / BlueZ)
  sim_server.py               Unix-socket transport for --simulate
  wifi/backend.py             WifiBackend interface + result types
  wifi/nmcli.py               NetworkManager implementation: the one subprocess runner, nmcli
                              parsing, NmcliBackend and NmcliConnectivity (the checks)
  wifi/fake.py                simulated networks for --simulate and tests
  wifi/connectivity.py        the success rule: what the checks found -> ConnectResult (pure)
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
 "press the pairing button on the device"         [P] pressed → pairing window (300 s), advertising
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
 on connected: done                                 on connected: PROVISIONED → stop advertising, stay on (LED green)
```

## 4. Device state machine

```
         ┌───── window expires, no session ─────┐
         ▼                                      │
   UNPROVISIONED ──────────[P]─────────────► ADVERTISING ◄──── idle timeout / new hello ─────┐
     ▲       ▲                                  │                                            │
     │       │                             handshake ok                                      │
     │       │                                  ▼                                            │
     │       └─ session ends after ───── SESSION_ESTABLISHED ────────────────────────────────┘
     │          the window expired          │          ▲
     │                               set_credentials   │ failed(reason): profile rolled back
     │                                      ▼          │
     │                                  CONNECTING ────┘
     │                                      │ ok: send connected, save, advertising off
     │                                      ▼
     │                                  PROVISIONED (persisted, stays on, LED green)
     │                                      │
     └──── [R][R] (reset button, double press)
```

- `state.py` validates transitions against one table and raises `InvalidTransition` on any other. Every transition is logged.
- Five states, no more (review Q13). A failed attempt is not a state: it goes back to `SESSION_ESTABLISHED`, and the service keeps the last result for the red LED. There is no `CONNECTED` state: sending `connected`, saving `provisioned` and turning advertising off happen in one step, so `CONNECTING` goes straight to `PROVISIONED`. A handshake in progress is not a state either: the device stays `ADVERTISING` and `DeviceSession` tracks the handshake. `info.state` is the lowercase state name.
- **The pairing window is not a state.** It is a timer owned by the service (`PairingWindow` in `buttons.py`): [P] opens it or restarts it at 300 s, and its expiry stops advertising. While it is open the device is in `ADVERTISING` or in a session. When it expires with no session, the device goes back to `UNPROVISIONED`. When it expires during a session, the session may finish (retries included); once that session ends (idle timeout or protocol error) the device goes back to `UNPROVISIONED`, not to `ADVERTISING`.
- [P] in `PROVISIONED` doesn't change the state; it only shows "Already connected. Hold reset to set up again."
- [R][R] in `PROVISIONED` deletes the Teton profiles, rotates the setup code, prints the new code + QR and goes to `UNPROVISIONED`, with advertising off until [P]. [R][R] works the same way in every other state except `CONNECTING`, where it only shows "Busy connecting. Try again in a moment." (review Q21). It first closes the pairing window, so a session in progress ends in `UNPROVISIONED`; no new transitions are needed.
- `PROVISIONED` is persisted (in `state.json`). After success the device stops advertising, before the configurator disconnects, so BlueZ can't re-enable it (spike, check g), and stays on with a green LED (plan_v5, D2, replacing the exit of S4). On restart a provisioned device does not advertise; [P] doesn't change that.
- The device does not track BLE connections (plan_v5, S3). One connection at a time is guaranteed by the radio: a connected peripheral stops advertising, and BlueZ restarts advertising after the disconnect while the advertisement is still registered, i.e. while the window is open (S1). A new `hello` ends any previous session; otherwise a session ends on the 300 s idle timeout or a protocol error.
- A connection attempt that has started always finishes, even if the configurator is gone. The result is logged, and if it failed the profile is rolled back.

### 4.1 Buttons and status line (`buttons.py`)

- **Key reader.** `TerminalButtons` puts the terminal in single-key mode (`tty.setcbreak`, standard library) rather than raw mode, so Ctrl+C still works. Each keypress arrives at once without Enter, and stdin is read through `loop.add_reader`. Keys typed before the reader starts are discarded (`setcbreak` flushes typed-ahead input), so a key pressed before the device listens counts for nothing. The original `termios` settings are restored on exit, also after a crash. The reader yields `Button` values (`PAIR`, `RESET`, `STATUS`, `POWER`) and ignores every other key; it never collects text. Escape sequences (arrows, F1 = `ESC O P`, Alt+key) are skipped as a whole by the pure `buttons_in()`, so they can't press a button. Without a terminal it logs a warning and yields nothing; the controller keeps ticking, so the device stays on (review Q23).
- **Pure helpers, injected clock.** `PairingWindow(clock, duration_s=300)` (`open()`, `remaining()`, `is_open()`) and `ResetDetector(clock, within_s=3)` (`press() -> bool`, true on the second press within 3 s) hold no I/O, so tests drive them with a fake clock.
- **Status line.** `StatusLine` derives the LED from the state, the window and the last result, by priority: yellow (connecting) > green (provisioned) > red (last attempt failed, with the reason; cleared by the next attempt or a reset) > blinking blue (window open, with countdown) > off. On a terminal it redraws one line at the bottom, and log lines are printed above it (clear the line, write the log, redraw). When stdout isn't a terminal (for example piped to `tee` for evidence), it prints LED changes as ordinary lines instead, without escape codes or countdown ticks.
- **Controller.** `ButtonController` turns buttons into actions: [P] opens or restarts the window (or shows "Already connected. Hold reset to set up again." when provisioned), [R] arms the reset and a second [R] within 3 s runs it, [S] prints the device's status text (which ends with the code + QR, review Q23) and the window's remaining time, [Q] powers off. A 1 s tick closes an expired window, calls `DeviceActions.on_tick()` (the session idle check) and redraws the LED. The controller reads the key source through `contextlib.aclosing`, so the terminal is restored as soon as it stops, also after an exception. It calls the service through a small `DeviceActions` protocol (§5), so its tests inject buttons through `QueueButtons` (an in-memory `ButtonSource`) and a fake `DeviceActions`, with no service, transport or terminal.

## 5. Key interfaces (contracts for implementation)

```python
class DeviceTransport(Protocol):                 # teton_common/transport.py (device side)
    async def start(self) -> None: ...                           # register the service; no advertising yet
    async def set_advertising(self, on: bool) -> None: ...       # pairing window open/closed (D2)
    def set_info(self, data: bytes) -> None: ...                  # value served on INFO reads
    def on_receive(self, channel: Channel, cb: Callable[[bytes], Awaitable[None]]) -> None: ...
    async def send(self, channel: Channel, data: bytes) -> None: ...
    async def close(self) -> None: ...                           # stop serving and advertising (power off)

class ClientTransport(Protocol):                 # teton_common/transport.py (configurator side)
    async def connect(self) -> None: ...
    async def read_info(self) -> bytes: ...
    def on_receive(self, channel: Channel, cb: Callable[[bytes], Awaitable[None]]) -> None: ...
    async def send(self, channel: Channel, data: bytes) -> None: ...
    async def close(self) -> None: ...
# Channel = INFO | SESSION | COMMAND | STATUS. INFO is a plain read (not framed);
# framing (fixed 20-byte chunks) is applied above the transport for SESSION, COMMAND and STATUS.
# No connect/disconnect events and no MTU: see plan_v5 S2, S3.
# set_advertising(False) does not drop an active connection (spike: the connection survives
# unregistering the advertisement). The socket transport stops accepting new clients instead.

class ButtonSource(Protocol):                    # teton_device/buttons.py
    def __aiter__(self) -> AsyncGenerator[Button, None]: ...     # PAIR | RESET | STATUS | POWER
# TerminalButtons (termios/tty) on the device; QueueButtons in tests.

class DeviceActions(Protocol):                   # teton_device/buttons.py, implemented by DeviceService
    def state(self) -> DeviceState: ...                         # [P] ignored if PROVISIONED; [R] refused if CONNECTING
    def last_failure(self) -> FailureReason | None: ...         # red LED until the next attempt or a reset
    async def set_advertising(self, on: bool) -> None: ...      # window opened / closed or expired
    async def reset(self) -> None: ...                          # forget profiles, rotate code, print code + QR
    def status_text(self) -> str: ...                           # state, SSID, IP, internet, code + QR (window: controller)
    async def power_off(self) -> None: ...
    def on_tick(self) -> None: ...                              # every second: the session idle check
# state() replaces is_provisioned(): the controller also needs CONNECTING (Q21) and the LED
# needs the state and the last failure. The pairing window belongs to the controller, which
# adds its remaining time to the [S] text.

class WifiBackend(Protocol):                     # teton_device/wifi/backend.py
    async def scan(self) -> list[Network]: ...
    async def connect(self, creds: Credentials, timeout_s: float) -> ConnectResult: ...
    async def forget_all(self) -> list[str]: ...                 # delete every teton-* profile (Q24)

class Connectivity(Protocol):                    # teton_device/wifi/backend.py; NmcliConnectivity in nmcli.py
    async def verify(self, interface: str) -> VerifyResult: ...   # ip, internet: full|limited|portal|none

class DeviceSession:                             # teton_device/session.py
    async def handle_session_msg(self, data: bytes) -> None: ...  # hello / confirm
    async def handle_command(self, data: bytes) -> None: ...      # encrypted scan / set_credentials
    def check_idle(self) -> None: ...                             # called by the service's 1 s tick
    def end(self, reason: str) -> None: ...                       # also used by [R][R] (Q21)
    last_result: tuple[str, ConnectResult] | None                 # (ssid, result): red LED, [S] text
# One DeviceSession lives as long as the service and holds at most one session; a lock makes it
# handle one message at a time, so a Wi-Fi attempt is a single step that always finishes.
# HandshakeLimiter (same module): every hello counts until its confirm succeeds (Q4).
# A reply whose session was ended meanwhile (e.g. [R][R] during a scan) is dropped and logged.

class DeviceService:                             # teton_device/service.py, implements DeviceActions
    def __init__(self, transport, wifi, storage, buttons, clock, sleep, status_line): ...
    async def run(self) -> None: ...                            # until [Q] or Ctrl+C; always closes the transport
# StateMachine(initial, on_change) calls on_change(new_state) after every transition; the
# service uses it to keep `info.state` current, whoever caused the transition.

class ClientChannel:                             # teton_configurator/client.py (Q37)
    async def send_session(self, msg: SessionMessage) -> None: ...  # plaintext handshake
    async def send_command(self, msg: CommandMessage) -> None: ...  # sealed with `cipher`
    async def receive(self, timeout_s: float) -> tuple[Channel, SessionMessage | StatusMessage]: ...
    def new_session(self) -> None: ...                           # drop pending replies, cipher, framing
    cipher: SessionCipher | None                                 # set once the handshake succeeds
# Whole messages on the configurator's channels: framing, one inbox for session + status, and
# the cipher. Shared with the device tests' Configurator, so the protocol exists once.

class ProvisioningClient:                        # teton_configurator/client.py
    def __init__(self, transport, reply_wait_s=10, scan_wait_s=40, result_wait_s=70): ...  # Q31, Q32
    async def connect(self) -> None: ...
    async def read_info(self) -> Info: ...
    async def handshake(self, setup_code: str) -> Info: ...      # again after a wrong code (Q4)
    async def scan(self) -> list[Network]: ...
    async def provision(self, creds: Credentials) -> AsyncIterator[Status]: ...  # connecting, then result
    async def close(self) -> None: ...
# Typed errors (Q33), none carries a secret: WrongCodeError, RateLimitedError(retry_after_s),
# NoReplyError, DeviceRefusedError(code), ProtocolError (incl. a device_id that differs from
# info, Q34), ConnectionLostError (a TransportError; no disconnect event, Q36). After
# NoReplyError, ConnectionLostError or ProtocolError the client is finished (Q35).
```

`crypto.py` exposes a pure, synchronous API: `Handshake` objects for each role that produce and consume handshake messages, and an `Envelope` (`seal(plaintext) -> bytes`, `open(bytes) -> plaintext`) that tracks sequence numbers.

## 6. Error handling

- Protocol violations end the session, are logged with a reason, and never crash the service. The line is decryption (review Q6): anything that fails before a message is proven to come from the configurator (bad envelope JSON, unexpected `seq`, decryption failure, a command before the handshake, a `confirm` without `hello`) ends the session; a decrypted but invalid command gets `error: bad_request` and the session stays. Garbage on the plaintext `session` channel is logged and dropped.
- Handshake refusals don't end a current session: a `hello` outside the pairing window gets no reply (Q22), and a rate-limited one gets `error: rate_limited` with `retry_after_s` (Q5).
- Wi-Fi failures are not protocol errors. They become `status failed` with a reason (SPEC §7), and the session continues.
- External commands (`nmcli`, `ping`) run through one runner function (`wifi/nmcli.run`), with a timeout on every call, `LC_ALL=C`, and a kill if the command overruns. Only `wifi/nmcli.py` runs commands; `wifi/connectivity.py` holds the pure success rule. Their raw output goes to the debug log only, never to the configurator, and is redacted. Secrets are passed through stdin, never in argv (SPEC §5.5).
- The configurator turns every failure into one plain-language sentence plus a next action (retry, retype the code, contact IT). All of them live in `ui_text.py`: the device's reasons and error codes, the warnings, and the failures the configurator finds itself (wrong code, no reply, no result within 70 s, connection lost, unexpected reply; review Q27). Input is checked locally before anything is sent, and exception text is never shown (review Q28). A test checks that every `FailureReason`, `ErrorCode` and `StatusWarning` has a text, and that no text contains jargon (review Q29).

## 7. Testing strategy

| Layer | Tests | Hardware |
|---|---|---|
| `setup_code` | generate/format/normalise/validate, ambiguous characters | none |
| `crypto` | round trip; wrong code fails confirmation; tampered ciphertext, replayed or skipped `seq` rejected; distinct keys per direction | none |
| `framing` | chunk size 20 (the fixed size in use) and a larger one, so framing stays generic, max size, interleaved channels, timeout reset | none |
| `messages` | schema validation, bad input rejected | none |
| `state` | allowed/forbidden transitions (incl. [P] and window expiry, [R][R] from PROVISIONED), persistence of PROVISIONED | none |
| `buttons` | driven by injected button events and a fake clock: pairing window open / expire / restart; reset needs two presses within 3 s (one press, or two presses 4 s apart, do nothing); [P] ignored when provisioned; [S] and [Q]; key → button mapping, other keys ignored; LED priority; status line on a terminal and on a pipe | none |
| `service` (with buttons) | nothing advertised before [P]; a session survives window expiry, and the device returns to UNPROVISIONED when it ends; [R][R] rotates the code, calls `forget_all` and turns advertising off; after success advertising is off and the device stays on | none |
| `wifi/nmcli` | parse recorded `nmcli` outputs (fixtures) into networks and failure reasons; with a fake runner, no secret ever appears in argv; per-interface success rule (full / limited + gateway / portal / gateway down) | none |
| `log` | no secret appears in captured logs during a full simulated run; error codes such as `bad_request` stay visible | none |
| end to end | device service + configurator client over `memory_transport` with `wifi/fake` and an injected [P] (PSK ok, wrong password then retry, wrong code, portal) | none |
| manual | real BLE + real Wi-Fi on the two laptops; `btmon` capture; Enterprise via hostapd | yes |

CI (SHOULD): GitHub Actions on `ubuntu-24.04` runs `ruff check`, `ruff format --check` and `pytest` on **Python 3.12 and 3.14** (plan_v5, D1). 3.12 is the device's version and catches any use of newer features; 3.14 is laptop 2's configurator. The radio libraries are imported lazily, so CI needs no Bluetooth.

## 8. Dependencies

| Package | Used by | Why |
|---|---|---|
| `cryptography` | common | Audited X25519, HKDF, HMAC, AES-GCM |
| `structlog` | common | Structured JSON logs + redaction processor |
| `bless` | device | BLE GATT server on BlueZ via D-Bus |
| `bleak` | configurator | BLE client, cross-platform |
| `qrcode` | device | Terminal QR |
| `pytest`, `pytest-asyncio`, `ruff` | dev | Tests and lint/format |

Everything else comes from the standard library (`asyncio`, `json`, `secrets`, `subprocess`, `argparse`, `termios`, `tty`).

## 9. Known risks (from plan_v5 §10)

- BlueZ peripheral quirks: the hardware spike (IMPLEMENTATION_PLAN Step 0.2) confirmed `bless` 0.3.0 works on laptop 1 without root or pairing. It also showed that `bless` hides the MTU and read offsets; we avoid needing them with fixed 20-byte chunks and a short `info` (plan_v5, S2). The behaviour S1, S3 and S4 rely on was measured on laptop 1's adapter; re-run the spike if laptop 2 ever becomes the device.
- Laptop A (laptop 1) is already on Wi-Fi: provisioning switches networks. On failure, the rollback relies on NetworkManager reconnecting the previous profile.
- Running without root: polkit allows the active desktop user to manage connections. BlueZ may need `sudo` or a group, so the README documents both.
- Two Python versions: the device runs 3.12, laptop 2 runs 3.14 (plan_v5, D1). The whole package (including `bless`, a dependency even on the configurator) must install on 3.14; IMPLEMENTATION_PLAN Step 0.3 checks it.
- Stopping advertising mid-session (pairing window expiry) relies on the spike's observation that the connection survives unregistering the advertisement, and on BlueZ not re-enabling it after the later disconnect. Step 6.1 verifies both on laptop 1.
- Enterprise testing needs AP mode on laptop B's Wi-Fi card (`iw list`). If it isn't supported, Enterprise is tested in simulation only, and the writeup says so.
