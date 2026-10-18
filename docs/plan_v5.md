# Teton Challenge: plan v5

v5, 2026-10-11, amended 2026-10-13, 2026-10-14, 2026-10-18 and 2026-10-19. Earlier versions (v1 to v3) live in the project notes, and v4 is kept in `plan_v4.md`. Their changes are summarised in the changelog below. **Status: v4 corrected by the first spec review, simplified after the BLE spike (S1–S4), then amended with the platform split and the device buttons (D1, D2), and with the review questions that block Phases 2, 3, 4, 5 and 6; open review questions are tracked in `IMPLEMENTATION_PLAN.md`.**

## Changelog

### v5 additions: Phase 6 review questions (2026-10-19)

Decided before building the BLE device transport (`IMPLEMENTATION_PLAN.md` Step 6.1, Q81–Q90), after reading the `bless` 0.3.0 source, and the BLE configurator transport (Step 6.2, Q91–Q101), after reading `bleak` 3.0.2. Numbering continues after Q80.

| Area | Before | v5 (decision) | Decided by |
|---|---|---|---|
| The BLE local name (review Q81) | `Teton-` + last 4 characters of `device_id` (SPEC §3), but the device ID is loaded inside `DeviceService.run()`, after the transport is built | **The CLI loads `Storage` once and passes `GattServerTransport(name)`.** `protocol.local_name(device_id)` builds the name; `Info.name` uses it too. A corrupt `state.json` still prints one line and exits 1 | Ruggero (proposed by Claude) |
| Registering without advertising (review Q82) | "`start` registers the service without advertising" | **`start()` does what `BlessServer.start()` does minus the advertising** (`setup_task`, `bus.export`, `app.register`), using `bless`'s own objects, no patch. `set_advertising` calls `app.start_advertising` / `stop_advertising` and does nothing when already in that state (a second `start_advertising` would register a second advertisement) | Ruggero (proposed by Claude) |
| Incoming writes (review Q83) | not specified | **`bless`'s synchronous write handler only queues `(channel, chunk)`; one task awaits the receivers in arrival order**, as the sim server does. A failing receiver is logged (`receiver_failed`), never with the bytes | Ruggero (proposed by Claude) |
| Startup failures (review Q84) | not specified | **`bless` is imported and the server created in `start()`**, so building the transport imports nothing. No adapter, no `bluetoothd` or a refused system bus become a `TransportError` with one plain line; the CLI prints it and exits 1 | Ruggero (proposed by Claude) |
| `close()` (review Q85) | "`close` must handle advertising being off" (spike: `bless.stop()` expects an advertisement) | **Step by step: stop advertising only if on, unregister the application, unexport it, disconnect the bus.** Each failing step is logged and the next still runs; `close()` never raises and a second call does nothing | Ruggero (proposed by Claude) |
| The `info` limit (review Q86) | only `Info` checks 180 bytes | **`transport.check_info(data)`, used by `set_info` in the memory, sim and BLE device transports**, like `check_chunk` | Ruggero (proposed by Claude) |
| Tests without a radio (review Q87) | "the pure helpers (characteristic table, `info` size limit)" | **Also a fake `bless`-shaped server injected with `GattServerTransport(name, make_server=…)`**: no advertising after `start`, advertising toggles and repeats, `close` with advertising off, writes delivered in order, `send` sets the value, startup errors become `TransportError`. The hardware Verify catches drift from the real `bless`. *Refined while building:* `bless`'s flag types can't be used without importing `bless`, so the fake stands in for `_BlessGattServer`, a thin wrapper that is the only code touching `bless`; the transport's logic is tested as listed, the wrapper on hardware | Ruggero (proposed by Claude) |
| Device CLI wiring (review Q88) | Step 6.1 listed only `gatt_server.py`; `NmcliBackend()` ignored `--state-dir` (where CA certificates go) | **Step 6.1 also wires `teton-device run`: `GattServerTransport(name)` and `NmcliBackend(state_dir=…)`.** The device's "BLE transport not available yet" branch goes; the configurator keeps its own until Step 6.2 | Ruggero (proposed by Claude) |
| Notification pacing (review Q89) | not specified | **No pacing:** each `send` sets the value, which is one notification. If the hardware runs (Steps 6.2, 6.3) lose chunks of a long scan list, pacing becomes its own decision | Ruggero (proposed by Claude) |
| `bless` side effects (review Q90) | spike: `bless` sets the adapter alias while it runs | **Accepted and documented** in the README troubleshooting: the laptop's Bluetooth name is `Teton-XXXX` while advertising and stays so after a `kill -9` (`bluetoothctl reset-alias` restores it); if BlueZ drops the advertisement, `bless` prints a harmless `…: Released!` line on stdout | Ruggero (proposed by Claude) |
| RSSI to signal (review Q91) | "the BLE transport converts RSSI" | **`signal = clamp(2 × (rssi + 100), 0, 100)`**: −100 dBm → 0, −50 dBm or stronger → 100, NetworkManager's scale for Wi-Fi, so device and network bars mean the same | Ruggero (proposed by Claude) |
| Discovery (review Q92) | "scan for 10 s filtered by the service UUID" | **`BleakScanner.discover(timeout=10, service_uuids=[SERVICE_UUID], return_adv=True)`**, strongest first; the name is the advertised local name, else the device name; `FoundDevice.address` is the Bluetooth address | Ruggero (proposed by Claude) |
| Connecting to what was found (review Q93) | not specified | **`BleDiscovery` keeps the scanned `BLEDevice`s; `connect(found)` uses them**, so `bleak` doesn't scan again; a device it didn't keep falls back to the address | Ruggero (proposed by Claude) |
| Reassembly timeout (review Q94) | A partial message is dropped 10 s after its **first** chunk | **10 s after its last chunk** ("no progress for 10 s"). Write-with-response moves about 20–30 chunks a second, so a `set_credentials` with a 4 KB CA certificate (~300 chunks) takes 10–15 s and would have been dropped. A stale message is still dropped and the 8192-byte cap still bounds memory. Write-without-response stays in reserve if Step 6.3 shows transfers are still too slow | Ruggero (proposed by Claude) |
| `connect()` over BLE (review Q95) | "subscribe to `session` and `status` before writing `hello`, then read `info`" | **Connect (10 s timeout), subscribe to `session` and `status`, check the Teton service and characteristics are there (else disconnect and raise).** Every `bleak`, D-Bus or timeout error becomes `TransportError`; `read_info()` stays a separate call. A device that isn't advertising fails `connect()` (Q64) | Ruggero (proposed by Claude) |
| Notifications (review Q96) | not specified | **`bleak`'s synchronous callback only does `Inbox.put()`**; one task delivers in order (as Q83) | Ruggero (proposed by Claude) |
| Disconnects (review Q97) | Q36: no disconnect event | **`bleak`'s `disconnected_callback` only marks the transport not connected**, so the next `send()` or `read_info()` raises `TransportError` at once; nothing goes up. `close()` is idempotent and never raises | Ruggero (proposed by Claude) |
| Writes (review Q98) | "write-with-response" | **`write_gatt_char(uuid, chunk, response=True)`**; no MTU handling and no private `bleak` API (S2) | Ruggero (proposed by Claude) |
| Configurator CLI and Bluetooth off (review Q99) | "not available yet" until Step 6.2 | **Without `--simulate`, `cli.py` uses `BleDiscovery`.** No adapter or Bluetooth off → `TransportError` from `discover()`; `cli.py` shows `ui_text.BLUETOOTH_UNAVAILABLE` and exits 1, never a traceback | Ruggero (proposed by Claude) |
| BLE client tests (review Q100) | "the pure helpers (signal to bars, device list formatting)" | **The RSSI helper, the device list from scan results, no `bleak` import when building the transport (fresh interpreter), and a fake `bleak`-shaped client injected through `make_client`**: subscribe first, ordered notifications, `response=True`, missing service, send after the disconnect callback, idempotent close, errors as `TransportError` | Ruggero (proposed by Claude) |
| Pending hardware checks (review Q101) | Spike central (`ble_central.py`) ⏳ "run before Step 6.2"; Step 0.3 not run | **Step 0.3 first on laptop 2** (6.2's Verify needs it). The central spike is superseded by Step 6.2's Verify: the real device and configurator exercise the same checks, minus the MTU read S2 made unnecessary | Ruggero (proposed by Claude) |

### v5 additions: Phase 5 review questions (2026-10-18)

Decided before building the simulation transports (`IMPLEMENTATION_PLAN.md` Step 5.1, Q64–Q72) and the two CLIs (Step 5.2, Q73–Q80). Numbering continues after Q63.

| Area | Before | v5 (decision) | Decided by |
|---|---|---|---|
| A second configurator in simulation (review Q64) | Step 5.1: "a second client waits until the first leaves"; S1 and SPEC §10: like BLE, the device can't be found while connected | **Refused, like the radio.** The server listens from `start()` to `close()` and closes at once any connection that arrives while the pairing window is closed or a client is connected. `connect()` makes one `info` round trip, so a refusal raises `TransportError` inside `connect()`; `discover()` returns nothing | Ruggero (proposed by Claude, raised by Ruggero) |
| What simulation is used for (review Q65) | SPEC §10 and §12 #8: simulation "is used by the end-to-end test" | **The in-process end-to-end tests use the memory transport (Phase 4); a two-process smoke test (Step 5.2) uses the socket** | Ruggero |
| Where the sim wire format lives (review Q66) | Not specified; both sides need it, and the configurator must not import `teton_device` | **`teton_common/sim_wire.py`**: the channel byte table, `pack()`, `read_record()` with the checks, and `socket_path()`. No protocol, framing or crypto in it | Ruggero (proposed by Claude) |
| The `info` record and the limits (review Q67) | "`info` is a request/response pair"; chunks of 20 bytes | **Request `[0][0x0000]` (client → device), response `[0][len][info JSON]` with `len ≤ MAX_INFO_BYTES` (180).** The 20-byte limit is for session/command/status. An unknown channel, a length over the limit, a short read, a wrong direction (e.g. the client sending on `status`) or a non-empty info request is malformed: one log line with the channel byte and length only (never the bytes), and the connection is dropped. The client waits at most 2 s for the server, then `TransportError` | Ruggero (proposed by Claude) |
| A stale socket vs. a running device (review Q68) | "A stale socket file is removed at start" | **Removed only if it is a socket nobody listens on** (`ECONNREFUSED`). A live listener → `TransportError` "another simulated device is already running" (the CLI exits 1); a path that isn't a socket is left alone and fails. Removing blindly would silently steal the socket from a running device | Ruggero (proposed by Claude) |
| Socket permissions and the fallback path (review Q69) | `$XDG_RUNTIME_DIR/teton-sim.sock`, else "a temp directory" | **`chmod 0600` after binding; the fallback is `<tempdir>/teton-sim-<uid>.sock`**, so two users on one machine don't collide and nobody else can connect | Ruggero (proposed by Claude) |
| Disconnect ordering (review Q70) | Not specified | **The client's `close()` half-closes and waits (≤1 s) for the server's EOF**, so the server has freed its one slot before `close()` returns; otherwise `discover()`'s probe could get the following `connect()` refused. A device `send()` to a client that has gone is dropped, like a BLE notify; a `send()` after `close()` raises, as in the memory transport | Ruggero (proposed by Claude) |
| Transport contract tests (review Q71) | `test_memory_transport.py` checks the memory pair only | **Renamed to `test_transport_contract.py` and parametrised over `memory` and `sim`** (the shared `ends` fixture covers both); `test_sim_transport.py` keeps the sim-only tests. The BLE rules (directions, 20-byte chunks) move from `memory_transport.py` to `transport.check_chunk()`, so both transports share one copy | Ruggero (proposed by Claude) |
| What sim discovery returns (review Q72) | "`discover()` uses `info` for the name" | **`FoundDevice(name=info.name, signal=100, address=<socket path>)`**: connect, read `info`, close. In `sim_client.py` | Ruggero (proposed by Claude) |
| Logs vs. the screen (review Q73) | SPEC §9.1, §11: logs to stdout; Step 6.3 captures the device with `2>&1 \| tee` | **The device's screen (code, QR, LED, messages) goes to stdout, the JSON logs to stderr.** On a terminal each log line goes through `StatusLine` (clear the LED line, write the log to stderr, redraw), so the LED isn't corrupted. Step 6.3 captures the device log with `2> docs/evidence/device-run.log`; with `2>&1` the setup code on the screen would end up in the evidence and fail the Q63 check | Ruggero (proposed by Claude) |
| Configurator log level (review Q74) | INFO for both sides | **Configurator: stderr, `WARNING` by default, `--verbose` → `DEBUG`**, so JSON lines don't break up the technician's guided text. The device stays `INFO`. `configure_logging` gets a `default_level` parameter | Ruggero (proposed by Claude) |
| Ctrl+C on the configurator (review Q75) | Q46: Ctrl+C handled in `cli.py` | **`TerminalIO` reads each answer in a daemon thread** (an executor thread would keep the process alive until Enter, up to 300 s on 3.12), **the CLI saves and restores the terminal settings** (`getpass` may leave echo off), shows the "stopped" text and exits 130. `run_flow` already closes the transport | Ruggero (proposed by Claude) |
| Device signals (review Q76) | [Q] or Ctrl+C: clean shutdown, exit 0 | **SIGTERM does the same as Ctrl+C** (`kill`, systemd, the smoke test), so the socket file is removed and the terminal restored; a newline follows the LED line on exit | Ruggero (proposed by Claude) |
| Default state directory in simulation (review Q77) | `~/.local/state/teton-device` | **Real: unchanged. Simulation: `~/.local/state/teton-device-sim`.** `--state-dir` overrides both. A sim run never touches a real device's state; it persists, so "restart while provisioned" can be shown, and [R][R] starts over | Ruggero (proposed by Claude, raised by Ruggero) |
| Fake Wi-Fi delays (review Q78) | `FakeWifiBackend` defaults: scan 1 s, connect 2 s | **Scan 2 s, connect 3 s**, so the yellow LED and the progress line are visible for a few ticks. Tests keep injecting an instant `sleep` | Ruggero (proposed by Claude, raised by Ruggero) |
| CLI test seam (review Q79) | "The CLI only wires components" | **`parse_args(argv)`, `async run_device(args, buttons=None)` and `async run_configurator(args, io=None)`; `main()` only runs them and maps exit codes.** Startup errors (corrupt `state.json`, a sim device already running) print one line to stderr and exit 1 | Ruggero (proposed by Claude) |
| Two-process smoke test (review Q80) | "if it can be made reliable without sleeps" | **Kept.** The device subprocess gets a pseudo-terminal as stdin, so the real `TerminalButtons` read `p` and `q`; stdout and stderr are read line by line with timeouts; `XDG_RUNTIME_DIR` points at a temp dir and the state dir is pre-seeded with a known code. It waits for the first `LED off` line before pressing [P], because `setcbreak` drops keys typed earlier. The "no `bless`/`bleak` import" check also runs in a fresh subprocess | Ruggero (proposed by Claude) |

### v5 additions: Phase 4 review questions (2026-10-18)

Decided before building the in-memory end-to-end tests (`IMPLEMENTATION_PLAN.md` Step 4.1a, Q48–Q55) and the "no secrets in logs" test (Step 4.1b, Q56–Q63). Tests only, except Q56.

| Area | Before | v5 (decision) | Decided by |
|---|---|---|---|
| Where shared test code lives (review Q48) | Step 4.1: "shared fixtures in `conftest.py`" | **`conftest.py` holds only fixtures.** The classes and constants tests import stay in, or move to, `tests/fake_configurator.py`, because pytest advises against importing from `conftest`. Moved there: `ScriptedIO`, `RecordingWifi`, `LimitedWifi`, `ca_pem()`, `TETON` and the wait constants from `test_flow.py`; `Wire` from `test_client.py`. `test_flow`'s `run()` stays in that file: the end-to-end tests need a different `discover` (Q51), so it has one user | Ruggero (proposed by Claude) |
| Which fixtures are shared (review Q49) | Each test file had its own copies | **`device`** (running, no [P]), **`paired_device`** (running, [P] pressed, recording Wi-Fi), **`ends`** (the memory pair), **`state_dir`** (a temp dir whose parents don't exist yet) and **`log_stream`** (the real `configure_logging` into a string, reset afterwards). **No fake-time fixture:** each rig owns its clock (`device.time`), so a separate one would be a second clock nothing advances. `test_session`'s `Rig` stays local (one user) | Ruggero (proposed by Claude) |
| Reading the LED in tests (review Q50) | Not specified | **`Device.led()` lets one simulated second pass and returns the last `LED …` line drawn on the device's screen**, so tests assert what the device shows, not a recomputation of `led_for()`. A state shown while the flow waits (e.g. red at "What next?") is captured from a scripted answer | Ruggero (proposed by Claude) |
| "A new configurator can't connect" (review Q51) | `test_flow`'s `discover()` ignores the radio | **In the end-to-end tests `discover()` sees the device only while it advertises**, like a BLE scan, so after success the technician gets "No device found…". One extra assertion: a direct `connect()` raises `TransportError` | Ruggero (proposed by Claude) |
| A second `hello` mid-session (review Q52) | `run_flow` never sends a `hello` once established; the memory transport takes one client at a time | **Configurator 1 quits at the network menu; the device stays `SESSION_ESTABLISHED` (no connection tracking, S3). Configurator 2's `hello` ends the old session** (`session_ended`, reason `new hello`) and provisions. One `hello` per configurator on the wire (S3, Q4) | Ruggero (proposed by Claude) |
| A really hidden network (review Q53) | The fake backend lists every network, so `test_flow` types "Home-PSK" as hidden | **A test-only `HiddenWifi` adds "Lab-Hidden" and leaves it out of `scan()`.** The test checks it is not in the menu, is joined with `hidden=True`, and the device ends `PROVISIONED`. `wifi/fake.py` is unchanged | Ruggero (proposed by Claude) |
| "No real sleeps" and the configurator's waits (review Q54) | The client's waits are real `asyncio.timeout`s, not the device's fake clock | **No production change.** Reply waits are 0.5 s, never reached when the device answers; only the no-reply case waits a real 0.05 s, as in `test_flow`. The end-to-end file runs in about 0.3 s | Ruggero (proposed by Claude) |
| Error codes in the device's log (review Q56) | The device logged a refused `hello` as "rate limited" and never logged `unsupported_security`; no log line used the `code` key, so Q7 (`code` stays readable) protected nothing real | **Every `error` the device sends is logged with `code=<ErrorCode>`**: `hello_refused` gets `code=rate_limited`; `bad_request` and `unsupported_security` are logged as `command_refused` with their code and a reason that names a field or a security type, never a value. The one production change of Phase 4; a session test checks it. Helps support see what a field device refused | Ruggero (proposed by Claude) |
| What the log test runs (review Q57) | Step 4.1: "a full simulated run, including a wrong password and a wrong code" | **Lean: one test, one log capture, one flow**: a wrong code, the right code typed lowercase with dashes, a wrong password on Home-PSK, then Hospital-EAP with a CA file, which connects. The fake networks use unique values (`home-NEEDLE-psk-…`, `eap-NEEDLE-pass-…`). No bad-request step and no rate limit in this run: both are covered by unit tests | Ruggero (proposed by Claude) |
| Getting the keys without logging them (review Q58) | "the session keys … appear nowhere" | **`monkeypatch` spies on `crypto`'s private `_new_key`, `_derive` and `SessionCipher.seal`**, looked up at call time, so they see both sides of every handshake: X25519 private keys, `c2d`, `d2c`, `k_confirm` (hex and base64) and every `ct` (base64). No production hook. The test asserts the spies caught something, so a rename fails loudly | Ruggero (proposed by Claude) |
| The CA PEM body (review Q59) | "the CA PEM body" | **Checked line by line** (the base64 lines between the markers): inside JSON the newlines become `\n`, so a search for the whole body would miss a leak | Ruggero (proposed by Claude) |
| Where the setup code may appear (review Q60) | SPEC §9.1 shows the code on the device | **Logs, stdout, stderr and the configurator's output: no secret at all, setup code included** (raw, with dashes, lowercase). **The device's screen may show the code** (the demo note, [S]) but nothing else. Both passwords are read through `ask_secret` | Ruggero (proposed by Claude) |
| The nmcli path (review Q61) | `FakeRunner` bypasses the real `run()`, where the editor-echo leak of Step 2.5 lived | **`FakeRunner` moves to `tests/fake_nmcli.py`. The log test's `EchoingRunner` also sends each command through the real `run()` with a stand-in, `sh -c 'tee /dev/stderr' <nmcli argv>`, that echoes stdin as nmcli's editor does**, then answers from the fixtures. `NmcliBackend.connect()` runs a PSK and a PEAP-with-CA attempt in the same capture | Ruggero (proposed by Claude) |
| Redaction must not hide too much (review Q62) | "error codes, SSID and `identity` stay visible" | **Asserted on the parsed JSON lines:** `connect_failed` with `wrong_password`, both SSIDs, `device_id`, a DEBUG line, and `identity` and the SSID in the nmcli `argv`. The `code` field is checked by the session test (Q56) and `test_log`, because the lean run sends no error | Ruggero (proposed by Claude) |
| Checking the real evidence (review Q63) | Step 6.3 greps only the `btmon` capture for the hotspot password | **Also grep `device-run.log` and `configurator-run.log` for the hotspot password and the setup code (raw and with dashes) before committing them.** The in-memory test can't see the real BLE and nmcli runs; the evidence is what reviewers read | Ruggero (proposed by Claude) |
| Overlap with `test_flow.py` (review Q55) | Half of the scenarios exist there, UI only | **Both files stay.** The end-to-end tests add the device side (state, LED, advertising, the wire) and the runs across several configurators; `test_flow` stays about menus and texts. Nothing is removed from it | Ruggero (proposed by Claude) |

### v5 additions: Phase 3 review questions (2026-10-18)

Decided before building the configurator (`IMPLEMENTATION_PLAN.md` Phase 3), one step at a time.

| Area | Before | v5 (decision) | Decided by |
|---|---|---|---|
| What `ui_text.py` holds first (review Q25) | Step 3.1 had "the prompts", but several prompts depend on flow choices not made yet | **Step 3.1 holds every failure, warning and result text, plus the prompts SPEC §9.2 already fixes.** Prompts for options decided in Step 3.3 (menus, rescan, the hidden-network security choice) are added then, under the same tests | Ruggero (proposed by Claude) |
| `auth_failed` text (review Q26) | "The username or password was rejected by the network." (no next action) | **"The username or password was rejected by the network. Check them and try again."** Every failure text is one sentence plus a next action | Ruggero (proposed by Claude) |
| Failures the configurator finds itself (review Q27) | Only the device's reasons and error codes had texts | **Five configurator-side texts:** wrong code or wrong device; no reply to `hello` (Q22: "Press the pairing button on the device and try again"); no result within 70 s (Q10), which is **not** the `timeout` text, because the device always finishes its attempt and may have connected, so it points at the device's light; connection lost; an unexpected reply (bad message, version or `device_id` mismatch) | Ruggero (proposed by Claude) |
| Checking input before sending (review Q28) | Not specified; a bad value would come back as `bad_request` | **The configurator checks input locally** (setup code format, password 8–63 characters, network name 1–32 bytes, non-empty username and password) and shows a ui_text sentence, never exception text. A mistyped code sends no `hello`, so it doesn't count against the rate limit. `bad_request` is then only a bug, with its own "contact support" text | Ruggero (proposed by Claude) |
| Text coverage test (review Q29) | Every `FailureReason` and `ErrorCode` has a text; no jargon | **Also every `StatusWarning`.** The jargon list stays as it is (nmcli, GATT, BLE, exception, traceback, D-Bus), matched case-insensitively on word boundaries so "BLE" doesn't match "unable" | Ruggero |
| Network list markers (review Q30) | "signal bars, lock icon or 'enterprise'" | **Four-level bars from block characters (`▂▄▆█`: ≥75 / ≥50 / ≥25 / below) and the words "password" or "enterprise"** instead of a lock emoji, which has an uneven width in terminals and is missing from some fonts | Ruggero (proposed by Claude) |
| Handshake reply wait (review Q31) | Q22: a `hello` after the window closed gets no reply; no wait defined | **10 s for each handshake reply** (`hello_reply`, then `ready` or `handshake_failed`), `CONFIGURATOR_REPLY_WAIT_S` in `protocol.py`. A `hello_reply` is about 11 notifications of 20 bytes, well under a second over BLE. No reply → "Press the pairing button on the device and try again." | Ruggero (proposed by Claude) |
| Other configurator waits (review Q32) | Only the 70 s result wait (Q10) | **`scan`: 40 s** (`CONFIGURATOR_SCAN_WAIT_S`: the device's nmcli scan has 30 s, plus about 125 chunks for 30 networks). **`set_credentials`: 10 s until `connecting`, then 70 s for the result.** All waits are constructor parameters of the client, so tests use fractions of a second | Ruggero (proposed by Claude) |
| Client errors (review Q33) | `WrongCodeError`, `RateLimitedError`, `ProtocolError`, `ConnectionLostError` | **Plus `NoReplyError`** (any wait that runs out; the flow picks "no reply" during the handshake and "didn't report back in time" while provisioning) **and `DeviceRefusedError(code)`** (`bad_request` or `unsupported_security`) | Ruggero (proposed by Claude) |
| `device_id` mismatch (review Q34) | Step 3.2: check that `hello_reply.device_id` matches `info.device_id`; outcome not defined | **`ProtocolError`** ("unexpected reply"), checked before the tag, nothing sent. Not `WrongCodeError`: retyping the code can't fix it | Ruggero (proposed by Claude) |
| What a client survives (review Q35) | Not specified | After `WrongCodeError` or `RateLimitedError` the same client may `handshake()` again (a new `hello` on the same connection, Q4); after `failed` the session stays for a retry. **After `NoReplyError`, `ConnectionLostError` or `ProtocolError` the client is finished**: the flow shows the text and closes the transport, no recovery | Ruggero (proposed by Claude) |
| Noticing a lost connection (review Q36) | `ClientTransport` has no disconnect event (S3) | **Kept that way.** `ConnectionLostError` comes from `connect`, `read_info` or `send` raising `TransportError`; a silent drop shows up as `NoReplyError`. `bleak`'s disconnect callback may be revisited in Step 6.2 | Ruggero (proposed by Claude) |
| One protocol client for tests too (review Q37) | `tests/fake_configurator.py` has its own copy of framing, inboxes and cipher handling | **`client.py` has a small `ClientChannel`** (framing, one inbox for `session` and `status`, the cipher) **under `ProvisioningClient`.** The device tests' `Configurator` reuses `ClientChannel` and keeps only its test-only extras (raw replies, invalid plaintext, "nothing arrives"). The device rig moves from `test_service.py` into the shared helper, so the client tests run against the real `DeviceService` the same way | Ruggero (proposed by Claude) |
| What the flow is given (review Q38) | `run_flow(discover, connect, io)`, shapes not defined | **`discover() -> list[FoundDevice]`** (name, signal 0–100, and the transport's address; BLE converts RSSI, so one `signal_bars` serves devices and networks). **`connect(found) -> ClientTransport`, not yet connected**, so the client registers its receivers first (BLE must subscribe before writing `hello`). **`io`: `ask`, `ask_secret` (`getpass`), `show`.** `run_flow` returns 0 when connected, else 1 | Ruggero (proposed by Claude) |
| Picking a device (review Q39) | SPEC §9.2: list the devices | **Always a numbered list; Enter picks the only device.** None found: "No device found…", then Enter looks again, q quits | Ruggero (proposed by Claude) |
| Typing the code (review Q40) | "On failure, explain and offer to retype" | **A bad format: explain, ask again, nothing sent. A wrong code: explain, ask again (a new `hello`, Q4). Rate limited: "wait N seconds", then ask again at once**; the flow doesn't sleep, the device enforces the wait. No limit on retypes: the device rate-limits | Ruggero (proposed by Claude) |
| Network menu (review Q41) | Networks + "Other (hidden) network" | **Plus "Scan again".** An empty scan says "The device found no networks it can join." with the same two options | Ruggero (proposed by Claude) |
| Hidden network (review Q42) | Q11: PSK, SAE and PEAP only | **Name (1–32 bytes), then the security type from `info.caps`**: "WPA2/WPA3 Personal (password)", "WPA3 Personal only (password)", "WPA2 Enterprise (username and password)". Sent with `hidden=True` | Ruggero (proposed by Claude) |
| Enterprise login and the CA file (review Q43) | "Optional CA file path"; SPEC §4.1 said 8192 bytes "fits a CA certificate PEM" | **Username, password (`getpass`), optional CA file, optional domain; no anonymous-identity prompt.** The CA file must exist, be **at most 4096 bytes**, and parse with `cryptography`'s `load_pem_x509_certificates`. The 8192-byte limit applies to the framed envelope, where base64 of the ciphertext adds a third, so a PEM really has about 5.4 KB; a typical CA PEM is 1.2–2 KB. `MAX_CA_PEM_BYTES` in `protocol.py`, and a test checks that the largest allowed request still fits one message | Ruggero (proposed by Claude) |
| After the result (review Q44) | "Retry from step 4 or 5" | **`connected`: the line and any warnings, exit 0. `failed`: the reason, then 1) try again (retype the password) 2) pick another network (the last list, with "Scan again") 3) quit. `DeviceRefusedError`: its text, back to the network menu. No reply, lost connection or an unexpected reply: the text, close, exit 1** (Q35) | Ruggero (proposed by Claude) |
| PEAP without CA or domain (review Q45) | Device warns after connecting (Q3) | **Only the device's `server_not_verified` warning after connecting**, no extra prompt before sending: the device is the authority | Ruggero (proposed by Claude) |
| Quit and end of input (review Q46) | Not specified | **q quits at any menu; end of input (stdin closed) counts as quit; the transport is always closed.** Ctrl+C stays in `cli.py` (Step 5.2). Free-text prompts (code, name, username, password) don't treat q as quit | Ruggero (proposed by Claude) |
| Progress (review Q47) | "Show progress" | **One line when `connecting` arrives, no spinner**, so the output stays readable when piped to `tee` for evidence | Ruggero (proposed by Claude) |

### v5 additions: Phase 2 review questions (2026-10-14)

Decided before building the device core (`IMPLEMENTATION_PLAN.md` Phase 2, open questions).

| Area | Before | v5 (decision) | Decided by |
|---|---|---|---|
| Device states (review Q13) | plan_v5 §5 had `FAILED(reason)`; DESIGN §4 had both `CONNECTED` and `PROVISIONED`; the handshake in progress and the `info.state` values were not defined | **Five states: `UNPROVISIONED`, `ADVERTISING`, `SESSION_ESTABLISHED`, `CONNECTING`, `PROVISIONED`.** No `FAILED` state: a failed attempt goes back to `SESSION_ESTABLISHED`, and the red LED comes from the last result the service keeps. No `CONNECTED` state: since D2, sending `connected`, saving and turning advertising off happen in one step, so `CONNECTING` goes straight to `PROVISIONED`. A handshake in progress is not a device state either: the device stays `ADVERTISING`, and the session tracks the handshake. `info.state` is the lowercase state name | Ruggero |
| Setup code in the QR (review Q14) | Not specified whether `code=` has dashes | **No dashes: the normalised 16 characters**, the same value as in `state.json` and the HKDF salt. The QR is read by machines; the code printed next to it keeps the `XXXX-XXXX-XXXX-XXXX` form for people | Ruggero |
| Counting failed handshakes (review Q4) | "5 consecutive failures", but with a wrong code the configurator aborts before `confirm`, so the device only sees a `hello` | **Every accepted `hello` counts as a failure, and a valid `confirm` resets the counter.** This also counts a `hello` followed by a disconnect, which the device can't see (S3), and it is exactly what an attacker collecting tags for offline guessing would do. Retyping the code is a new `hello` on the same connection | Ruggero |
| Rate-limited reply (review Q5) | "Refuse handshakes for 30 s", with no message defined | **`{"type":"error","code":"rate_limited","retry_after_s":N}` on `session`**, so the configurator can say how long to wait (the wait doubles, so the number varies). A refused `hello` leaves a current session alone | Ruggero |
| `bad_request` vs ending the session (review Q6) | SPEC §6.3 has `bad_request` (the session continues); DESIGN §6 ends the session on any protocol violation | **The line is decryption.** Anything that fails before a message is proven to come from our configurator (bad envelope, wrong `seq`, failed decryption, a command before the handshake, a `confirm` without `hello`) ends the session: it could be an attacker. A decrypted but invalid command gets `bad_request` and the session stays: it is a configurator bug. A security type not in `caps` gets `unsupported_security`. The device handles one message at a time, so a command sent during a Wi-Fi attempt is answered after its result; no command is ever seen in `CONNECTING` | Ruggero |
| `hello` after the window expired (review Q22) | "A session in progress may finish", silent on a new handshake | **A `hello` is accepted only while the pairing window is open**, and a pending handshake's `confirm` only while the device is still advertising. After expiry the current session may finish, but a new handshake needs a new [P]. A refused `hello` gets no reply | Ruggero |
| Deleting the Teton profiles (review Q24, found in Step 2.3) | `WifiBackend.forget(profile_name)` | **`WifiBackend.forget_all()`** deletes every NetworkManager profile whose name starts with `teton-` and returns their names. [R][R] needs it: after a restart the device no longer knows which SSID it joined, and a crash could leave more than one profile. Profiles the owner made are never touched | Ruggero (proposed by Claude) |
| Timeout budget (review Q10) | `timeout` = "no result within 45 s", and `connection up` also has 45 s; verification not counted | **45 s for activation (`nmcli --wait 45 connection up`), plus up to 15 s for verification** (connectivity check and gateway ping). The configurator waits up to 70 s for the result after `connecting`. Both numbers are in `protocol.py` | Ruggero |
| Security types (review Q11) | SPEC accepted `open`; transition mode and unjoinable networks not covered | **`open` is out of scope**: a medical device shouldn't join an unencrypted network; it gets `unsupported_security` and isn't in `caps`. **WPA2/WPA3 transition mode** (`SECURITY = "WPA2 WPA3"`, most phone hotspots) maps to `wpa2-psk`; `WPA3` alone to `wpa3-sae`; `WPA2 802.1X` to `wpa2-eap-peap`. **Networks we can't join** (open, WEP, OWE, WPA1, WPA3-Enterprise) are left out of the scan list and logged at debug level, rather than shown as "not supported": that would need a new protocol value and UI for networks the device should never use | Ruggero |
| Autoconnect priority (review Q17) | Not specified | **`connection.autoconnect-priority 10`** on every `teton-*` profile, so a known home network can't win after a reboot. The profile is created with autoconnect off and switched on only after success | Ruggero |
| [R][R] outside `PROVISIONED` (review Q21) | D2 defined reset from `PROVISIONED` only | **Reset works in every state except `CONNECTING`**, where it shows "Busy connecting. Try again in a moment." (a started attempt always finishes). Elsewhere it closes the pairing window, drops any session and rotates the code, which is also how a technician replaces a code they think has leaked. It needs no new state transitions | Ruggero |
| No terminal, and the QR scrolling away (review Q23) | Not covered | **Without a terminal** (systemd, redirected stdin) there are no buttons: the device logs a warning and stays on without advertising; no `SIGUSR1` mapping. The systemd unit stays a hardening example, and the writeup says a real device uses GPIO buttons. **[S] also prints the code and QR again**, since log lines scroll them away | Ruggero |

### v5 additions: platforms and device buttons (2026-10-13)

| Area | Before | v5 (decision) | Decided by |
|---|---|---|---|
| D1. Ubuntu and Python versions | Both laptops Ubuntu 24.04 LTS, Python 3.12 (v4 Q10) | **Device (laptop A): Ubuntu 24.04 LTS, Python 3.12**, the reference platform for BlueZ peripheral mode and NetworkManager. **Configurator (laptop B): Ubuntu 24.04 or 26.04 LTS**; laptop 2 runs 26.04 with Python 3.14 and newer BlueZ, NetworkManager and `hostapd`. `requires-python = ">=3.12"`, and the code uses no feature newer than 3.12. Laptop B first confirms that the package installs and that `cryptography` and `bleak` import on 3.14. CI (SHOULD) runs the tests on 3.12 and 3.14. The README states both versions and each laptop's role | Ruggero |
| D2. Device input | Device CLI with `run`, `reset` and `show-code`; an unprovisioned device advertises as soon as `run` starts | **No text input on the device, ever** (no passwords, codes or commands at runtime). While `teton-device run` is active, single keypresses act as simulated physical buttons, and a status line acts as the LED. **[P] pairing** opens a 5-minute pairing window, the only time the device advertises; pressing it again restarts the window; when it expires advertising stops, but an active session may finish. On a provisioned device [P] shows "Already connected. Hold reset to set up again." **[R] reset** needs two presses within 3 s (a simulated long press): it deletes the Teton profiles, generates a new setup code, prints the new code + QR, and returns to unprovisioned with advertising off. **[S] status** prints the state, SSID, IP, internet result and remaining pairing time. **[Q] or Ctrl+C** powers off cleanly. LED: off = idle/unprovisioned, blinking blue = pairing window open (with countdown), yellow = connecting, solid green = connected, red = last attempt failed (with reason). The setup code + QR stay on screen as the stand-in for the printed label. `reset` and `show-code` subcommands removed; the CLI is `teton-device run [--simulate] [--state-dir] [--verbose]`. Simulation mode uses the same buttons. Q7 ("re-enter setup via explicit reset") is now done with [R] | Ruggero |
| D2 consequence: after success | S4: after success the device stops its BLE server and exits | **The device stays on after success:** it stops advertising (closing the pairing window) right after sending `connected`, before the configurator disconnects, and stays in `PROVISIONED` with a green LED until [R][R] or [Q]. It must keep running, because [P] and [R] are only reachable while `run` is active. Starting `run` on a provisioned device also stays on, without advertising | Ruggero (proposed by Claude, follows from D2; confirmed 2026-10-13) |
| D2 consequence: transport | `DeviceTransport.start()` serves and advertises; `close()` stops both | **Advertising is switched separately:** `DeviceTransport.set_advertising(on)`. `start()` only registers the GATT service, so a pairing window can end without dropping an active session. The simulation transport stops accepting new clients while advertising is off | Ruggero (proposed by Claude, follows from D2; confirmed 2026-10-13) |

Why the pairing window matters (for the writeup): it shrinks the attack surface. The device is only reachable over BLE for a few minutes after someone physically presses a button, so an attacker must be nearby at exactly that moment, on top of needing the setup code. That's standard practice in real products (for example the Matter commissioning window, or the "hold to pair" button on most smart-home devices). See §3b.

### v4 → v5

Found while planning the implementation (questions Q1, Q2, Q3, Q7, Q8 and Q9 in `IMPLEMENTATION_PLAN.md`).

| Area | v4 | v5 (decision) | Decided by |
|---|---|---|---|
| Encryption AAD (review Q1) | AAD = message type + sequence number | **AAD = protocol label + direction (`c2d`/`d2c`) + sequence number**, as in SPEC §5.3. The type is inside the ciphertext, so the receiver can't know it before decrypting. v4 was a doc error | Ruggero |
| Transport interface (review Q2) | `send` / `on_receive` / `close` only | **Split into `DeviceTransport` and `ClientTransport`**, with connect/disconnect events, the `info` read and the chunk size. Framing stays above the transport | Ruggero |
| BLE spike timing (review Q3) | "Test early" | **Spike runs right after project tooling**, before any protocol code, because its results can change the transport interface | Ruggero |
| Log redaction (review Q7) | Redacted keys include `code` | **`code` removed from the redaction list** (it hid error codes such as `busy`). The setup code is only stored and logged under `setup_code`; the QR payload is never logged | Ruggero |
| Success rule (review Q8) | Success only when internet is `full`; global connectivity check | **Success = Wi-Fi activated + IP + gateway reachable on the Wi-Fi interface.** `full` → success; `limited`/`none`/`unknown` with gateway reachable → success with a warning; `portal` → `captive_portal`; gateway unreachable → `no_internet`. Checks are **per interface** (NM per-device connectivity, `ping -I`) | Ruggero |
| Secrets to `nmcli` (review Q9) | Not specified (would end up in argv) | **Secrets never go on a command line.** Profiles are created without secrets, which are then set through stdin (`nmcli connection edit`). One subprocess runner; a test asserts no secret appears in argv | Ruggero |

### v5 additions after the BLE spike (2026-10-11)

The spike (`IMPLEMENTATION_PLAN.md` Step 0.2) showed that BLE already does several jobs the docs gave to our code. Guiding principle from Ruggero: **this is a demo; keep it simple and explainable, not production-ready.**

| Area | Before | v5 (decision) | Decided by |
|---|---|---|---|
| Laptop roles | Laptop A = device, laptop B = configurator, machines unassigned | **Laptop A = laptop 1 (`ruggero-PC`, where the spike ran); laptop B = laptop 2.** The radio behaviour behind S1, S3 and S4 was measured on laptop 1's adapter; re-run the spike if the roles ever swap | Ruggero |
| S1. One session at a time | Device enforces it; a second connection gets `error: busy` | **The radio enforces it:** a connected BLE peripheral stops advertising, so a second configurator can't find or connect to it. `busy` is removed from the protocol; simulation mode accepts one client at a time | Ruggero |
| S2. Chunk size | Negotiated MTU − 3, fallback 20 | **Fixed 20-byte chunks**, the minimum every BLE link supports. No MTU handling anywhere (no `bless` patch, no private `bleak` call). Slower (a scan list or CA certificate takes a couple of seconds), fine for a demo; production would use the MTU (517 measured). `info` stays a single read under 180 bytes | Ruggero |
| S3. Connection tracking | `DeviceTransport` has connect/disconnect events (review Q2) | **The device doesn't track connections:** a new `hello` starts a new session (with one connection at a time, it can only come from a new configurator). Sessions also end on the idle timeout or a protocol error. The kernel restarts advertising after a disconnect, so no code is needed for that | Ruggero |
| S4. After success | Stop advertising and keep running | **The device finishes:** after sending `connected` it waits briefly for the configurator to leave, stops the BLE server (which removes the advertisement) and exits. The next `run` sees `provisioned` and doesn't advertise. Re-enter setup with `teton-device reset` (unchanged). *Superseded on 2026-10-13 by D2: the device stays on, and reset is the [R] button* | Ruggero |

### v3 → v4

| Area | v3 | v4 (decision) | Decided by |
|---|---|---|---|
| Key exchange (Q1) | Open | **X25519 + HKDF salted with setup code + key confirmation** | Ruggero |
| BLE OS pairing (Q2) | Open | **Not used**; app-layer encryption only | Ruggero |
| Enterprise server validation (Q3) | Open | **Optional CA cert / domain from configurator; warn if neither** (production would enforce) | Ruggero |
| Message format (Q4) | Open | **JSON** | Ruggero |
| Network selection (Q5) | Open | **Device scans, technician picks; manual entry for hidden SSIDs** | Ruggero |
| Failure handling (Q6) | Open | **Report reason, keep session, retry immediately** | Ruggero |
| After success (Q7) | Open | **Stop advertising; re-enter setup only via explicit reset** | Ruggero |
| Demo run mode (Q8) | Open | **Foreground command**; systemd unit stays a Should | Ruggero |
| Setup code input (Q9) | Webcam scan + typed fallback recommended | **Typed code only.** Device also shows the QR, labelled "for demo purposes the code is shown" | Ruggero |
| Ubuntu (Q10) | 22.04 or 24.04 | **24.04 LTS only** (Python 3.12, BlueZ 5.72, NetworkManager 1.46) | Ruggero |
| Setup code format | 128-bit secret in QR | **16-character code, 80 bits, shown as `XXXX-XXXX-XXXX-XXXX`** (consequence of typed input, see §4) | Claude default |


### v2 → v3

| Area | v2 | v3 (decision) | Decided by |
|---|---|---|---|
| Channel | Pending, BLE recommended | **BLE GATT** | Ruggero |
| Alternatives in writeup | n/a | **SoftAP + captive portal, Wi-Fi Easy Connect (DPP), WPS, optical QR, NFC** discussed and rejected with reasons | Ruggero |
| Security, architecture, UX details | Drafted | **Open questions Q1 to Q10 (§13)**; v4 will record the answers | open |

### v1 → v2

| Area | v1 | v2 (decision) | Decided by |
|---|---|---|---|
| Configurator | Phone page or laptop, undecided | **Laptop first** (Python). Bare-bones HTML/JS phone page only if time allows | Ruggero |
| Wi-Fi security types | WPA2/3-Personal; Enterprise "out of scope" | **WPA2-Enterprise in scope**, WPA2/3-Personal kept for home testing | Ruggero |
| Trust bootstrap | Per-device QR secret, assumed | **QR kept as default**; alternatives without pre-shared material documented (§3b) | Ruggero |
| Post-join feedback | Optional | **Required**: device confirms internet access and reports it back | Ruggero |
| Evaluation | Unknown | **Assume Teton runs it**: README and hardware tolerance matter; no-radio simulation mode | Ruggero |
| Effort | Unknown | **Multiple days, moderate effort**: scope tiered Must/Should/Could (§9) | Ruggero |
| Hardware | Unknown | Laptop A = device, laptop B = configurator, Android phone = test hotspot (+ later phone configurator) | Ruggero |

## 1. Requirements (from the PDF)

Build a two-party Wi-Fi provisioning flow:

- **Configurator** (phone or second laptop) sends Wi-Fi credentials to the **Teton Device** (an Ubuntu machine).
- The device uses them to join a **real** Wi-Fi network.

Hard constraints:

| # | Constraint | Implication |
|---|---|---|
| C1 | No internet, no cloud relay, no shared LAN at start | Point-to-point channel: BLE, SoftAP, optical (QR), NFC, audio, USB |
| C2 | Real Wi-Fi connection | Device must drive NetworkManager / wpa_supplicant and verify association + DHCP lease |
| C3 | No plaintext credentials over any wireless channel | App-layer encryption with authenticated key exchange; BLE "Just Works" pairing alone is weak |
| C4 | Usable by a non-developer | No terminal on the configurator side; scan-a-QR, pick SSID, type password, see success/failure |

Deliverables: GitHub repo with both sides' code, README reproducible on stock Ubuntu (incl. hardware assumptions), architecture writeup (channel choice + trade-offs, in-transit protection, scaling to 200 simultaneous devices run by a non-technical team), and run evidence (logs, screenshots, ideally a short video).

Evaluation signals stated: systems thinking, Linux comfort, practical problem solving, and *the reasoning behind the channel choice*. AI use is allowed; judgment is what's graded.

## 2. Decisions on the clarifying questions

| # | Question | Answer | Consequence for the build |
|---|---|---|---|
| 1 | Configurator form factor | Laptop first, phone later if time | Python configurator on laptop B; HTML/JS page is a Could |
| 2 | MITM resistance needed? | Explained in thread; we design for it | Authenticated key exchange (§4), not just encryption |
| 3 | Enterprise Wi-Fi | Yes, plus Personal for home testing | Support PSK/SAE and WPA2-Enterprise PEAP-MSCHAPv2 (username + password); EAP-TLS (certificates) as a Could. Test Enterprise with `hostapd` (built-in EAP server) on laptop B, no RADIUS box needed |
| 4 | Pre-shared device material | QR assumed; alternatives wanted | See §3b |
| 5 | Post-provisioning feedback | Yes: ping or internet confirmation | After NM reports "activated": IP address, NM connectivity check of the Wi-Fi interface (`full` / `limited` / `portal` / `none`) + ping gateway on the Wi-Fi interface; result sent back to configurator and shown in plain language. Success needs a reachable gateway; anything less than `full` internet is reported as a warning (v5) |
| 6 | Evaluation setup | Assume Teton runs it | README must be copy-paste reproducible on stock Ubuntu; preflight check script (adapter present, BlueZ version, NM running); `--simulate` mode running the full protocol without radios |
| 7 | Effort | Multiple days, not too much | Tiered scope (§9); writeup carries the "fleet" ideas instead of code |

Hardware:

| Role | Machine | Needs |
|---|---|---|
| Teton Device | Laptop A = laptop 1, `ruggero-PC` (Ubuntu 24.04, Python 3.12) | Bluetooth adapter (peripheral role), Wi-Fi via NetworkManager. BLE behaviour verified in the spike (v5) |
| Configurator | Laptop B = laptop 2 (Ubuntu 26.04, Python 3.14; D1) | Bluetooth adapter (central role); later its Wi-Fi card can host the Enterprise test AP with `hostapd` (AP mode still to be checked) |
| Target network (Personal) | Android phone hotspot | WPA2/WPA3-Personal |
| Target network (Enterprise) | Laptop B running `hostapd` with EAP | Wi-Fi card that supports AP mode (`iw list` → "AP") |
| Phone configurator (Could) | Android phone, Chrome | Web Bluetooth |

## 3. Channel choice: BLE GATT (decided)

Full comparison: [research/channels.md](research/channels.md).

Why BLE, in one paragraph for the writeup: it is the only option that satisfies all four constraints on stock Ubuntu hardware *and* keeps a live feedback path while the device joins Wi-Fi (the Wi-Fi radio is never borrowed for setup). It is the industry default for headless IoT onboarding (Matter, ESP32, HomeKit), phones support it natively, and Chrome on Android can drive it from a web page with no app install.

Alternatives the writeup discusses and rejects:

| Option | Why not (for this challenge) | Where it would win |
|---|---|---|
| SoftAP + captive portal | Single radio drops the setup link exactly when the device tries to join, so no failure feedback; creates a temporary shared network; rogue-AP alarms in hospitals | Zero-app consumer products |
| Wi-Fi Easy Connect (DPP) | Not supported by NetworkManager on Ubuntu (raw `wpa_supplicant` only), driver-dependent; Android configurator can't send Enterprise credentials | Production direction once Linux/driver support matures; strong standard |
| WPS | Broken PIN mode, unauthenticated push-button, disabled on enterprise APs, deprecated with WPA3 | Nowhere for us |
| Optical QR (configurator shows, device camera reads) | Laptop webcams are unreliable; one-way, no feedback | Teton's own camera-based devices: worth a paragraph |
| NFC | No NFC reader on laptops | Future hardware: tap-to-provision with BLE for the data |

## 3b. Trust bootstrap: how the configurator knows it talks to the real device

Default: **per-device secret in a QR code** (printed label in production; shown on the device screen or saved as a PNG in the demo). It's what Matter, ESP32 and DPP do.

Alternatives when the device has **no pre-shared material**:

| Option | How it works | Protects against MITM? | Trade-off |
|---|---|---|---|
| Numeric comparison | Both sides do a fresh key exchange, then each screen shows a 6-digit code derived from it; the technician confirms they match | Yes, if the human really compares | Device needs a display; humans click "yes" without looking |
| Passkey entry | Device shows a random 6-digit code at boot; technician types it into the configurator | Yes | Device needs a display; code is only 20 bits, so must be one-time and rate-limited |
| Physical action window | Button press (or power-cycle) opens a 2-minute provisioning window | No, only narrows the time an attacker has | WPS-style; easy UX, weak security on its own |
| Trust on first use (TOFU) | Accept whoever connects first, remember their key afterwards | No, the first contact is unprotected | Simple; acceptable only in a controlled room |
| Manufacturer certificate | Device has a key + certificate signed by Teton's CA at the factory; configurator verifies the signature | Yes | Strongest and scales best, but it *is* pre-shared material, just not secret to the technician |

The writeup should present the QR as the demo choice and the manufacturer certificate as the 200-device answer.

The demo also adds a **physical action window on top of the QR** (D2): the device advertises only for 5 minutes after the [P] button is pressed. On its own a window doesn't stop a MITM, but combined with the setup code it shrinks the attack surface: an attacker needs the code *and* must be in radio range during those few minutes after someone physically touched the device.

## 4. Security design (decided)

Threats: passive sniffer, active MITM / fake device, rogue configurator, replay of captured messages, evil-twin access point (Enterprise).

**Setup code (trust anchor).** At first start the device generates a random **16-character setup code** (Crockford base32, 80 bits, e.g. `7K2M-QX9D-4HTR-B8WZ`) and stores it in its state directory (mode 0600). It prints the code and a QR containing `{device_id, code}` in the terminal with the note *"For demo purposes the code is shown on screen; in production it is printed on the device label."* The technician types the code into the configurator; the QR is there for the later phone page. The code is typed only on the configurator: the device never accepts typed text (D2). A reset ([R][R]) generates a new code.

*Why 80 bits and not a short PIN:* with Q1 (a), someone who records a handshake could try codes offline. 80 random bits makes that infeasible, while staying typeable. A 6-digit PIN would require a PAKE (SPAKE2) instead, which the writeup mentions as the upgrade path.

**Handshake.**
1. Configurator → device: `hello` with its ephemeral X25519 public key and a random nonce.
2. Device → configurator: its ephemeral public key, nonce, and a confirmation tag.
3. Both derive `session_key = HKDF-SHA256(ECDH secret, salt = setup code, info = "teton-prov-v1" + device_id + both public keys + both nonces)`.
4. Each side proves it derived the same key with `HMAC(session_key, transcript)`; mismatch = abort. A fake device or MITM without the code fails here.
5. All later messages: **AES-256-GCM**, nonce from a per-direction counter, AAD = protocol label + direction + sequence number (v5: v4 said "message type", which the receiver can't know before decrypting). Replayed, reordered or reflected messages fail decryption.

**Bluetooth pairing:** not used (Q2). It would add OS dialogs and, in "Just Works" mode, no MITM protection. Writeup note: a device with a display could add LE Secure Connections numeric comparison as defense in depth.

**Enterprise (PEAP) server validation (Q3):** configurator may send the RADIUS server's CA certificate and/or expected domain; the device sets them in the NetworkManager profile (`802-1x.ca-cert`, `802-1x.domain-suffix-match`). If neither is supplied, the device connects but reports a warning shown to the technician. Production policy: refuse.

**Limits and hygiene:**
- The device advertises only during a 5-minute pairing window opened by the [P] button (D2).
- One session at a time, enforced by the radio: a connected BLE peripheral stops advertising (v5, S1). A new `hello` starts a new session (S3); a session expires after 5 minutes idle.
- After 5 failed handshakes, back off 30 s, doubling each time (no permanent lockout in the demo). Every `hello` counts until its `confirm` succeeds (Q4); a refused `hello` gets `rate_limited` with the seconds to wait (Q5).
- Credentials go straight into the NetworkManager profile (root-only, 0600), are never logged, and are dropped from memory after use; a test asserts no secret appears in logs.
- Secrets never appear on a command line, where any local user could read them through `ps`. They are passed to `nmcli` through stdin; a test asserts no secret appears in argv (v5).
- Log redaction is by key name. The setup code is only ever logged under `setup_code`, so the generic key `code` (used for error codes) is not redacted (v5).

Library: `cryptography` (X25519, HKDF, HMAC, AESGCM). Browser page later: WebCrypto.

## 5. Architecture (decided)

```
 Configurator (laptop B, guided CLI)                  Teton Device (laptop A, foreground command)
 ───────────────────────────────────                  ─────────────────────────────────────────
 technician types setup code                          prints setup code + QR ("demo only")
                                                      technician presses [P]: 5-min pairing window
        │                                               ├─ BLE GATT server (bless / BlueZ D-Bus)
        │  BLE GATT                                     ├─ session crypto (X25519/HKDF/AES-GCM)
        ├──► hello(pubkey_c, nonce_c) ────────────────► │
        │ ◄── pubkey_d, nonce_d, confirm_d ──────────── │
        ├──► confirm_c ───────────────────────────────► │
        ├──► enc(scan) ───────────────────────────────► ├─ Wi-Fi scan (nmcli)
        │ ◄── enc(ssid list: name, signal, security) ── │
        ├──► enc(set_credentials {ssid, security,      │
        │        psk | identity+password[, ca, domain]})├─ NetworkManager add + activate
        │ ◄── enc(status: connecting) ───────────────── │
        │ ◄── enc(status: connected, ip, internet:     ├─ verify: activated, IP, connectivity
        │        full|limited|none|unknown [, warning]) │   check (Wi-Fi iface), ping gateway
        │     or enc(status: failed, reason) ───────── │   (on failure: stay in session, allow retry)
        └ plain-language result / retry prompt         └─ on success: keep profile, stop advertising, LED green
```

GATT service (custom 128-bit UUID), characteristics:
- `info` (read, plaintext): device ID, protocol version, capabilities. No secrets.
- `session` (write + notify): handshake messages.
- `command` (write): encrypted commands: `scan`, `set_credentials`. (There is no `reset` over BLE: reset is the physical [R] button, D2.)
- `status` (notify): encrypted state and results.

Messages are **JSON**, length-prefixed and split into fixed 20-byte chunks, the smallest BLE packet every device supports, then reassembled (v5, S2).

Failure reasons reported (each mapped to a plain-language message on the configurator): `wrong_password`, `ssid_not_found`, `auth_failed` (Enterprise), `no_ip`, `captive_portal`, `no_internet` (gateway unreachable), `timeout`.

Success rule (v5): Wi-Fi activated + IP + gateway reachable on the Wi-Fi interface. If NM's connectivity for that interface is not `full`, the result is still success, with the warning `limited_internet` ("joined the network but can't reach the internet; this may be normal on restricted networks"). Hospital networks often block Ubuntu's check URL; in production the check would target Smith's endpoint instead.

Transport interface (v5, simplified by S2 and S3): `DeviceTransport` (`start`, `set_advertising`, `set_info`, `on_receive`, `send`, `close`) and `ClientTransport` (`connect`, `read_info`, `on_receive`, `send`, `close`). Framing is applied above both. `set_advertising` was added by D2.

Device state machine (review Q13): `UNPROVISIONED → [P] → ADVERTISING → SESSION_ESTABLISHED → CONNECTING → PROVISIONED` on success, or back to `SESSION_ESTABLISHED` for a retry on failure (there is no separate failed state; the LED shows the last reason). If the pairing window expires without a session, `ADVERTISING → UNPROVISIONED`. On success the device stops advertising and stays on in `PROVISIONED` (D2, replacing the exit of S4). [R][R] deletes the profile, generates a new setup code and returns to `UNPROVISIONED`, with advertising off until [P].

Device buttons and LED (D2): the device takes no typed text. While `run` is active, single keys act as buttons ([P] pairing, [R] reset by double press, [S] status, [Q]/Ctrl+C power off) and a status line acts as the LED.

## 6. Tech stack (Python first)

Platform (D1): the device runs **Ubuntu 24.04 LTS** (Python 3.12, BlueZ 5.72, NetworkManager 1.46). The configurator runs Ubuntu 24.04 or 26.04 LTS (laptop 2: 26.04, Python 3.14). The code targets Python 3.12 (`requires-python = ">=3.12"`) and uses nothing newer.

Device side (laptop A):
- BLE peripheral: **`bless`** (BlueZ over D-Bus). Fallback: `dbus-next` against BlueZ directly.
- Wi-Fi: **NetworkManager via `nmcli`**. Profiles: `wifi-psk`, `wifi-sae`, `wifi-eap-peap` (Could: `wifi-eap-tls`).
- Internet check: `nmcli networking connectivity check` to refresh, then the Wi-Fi interface's own value (`nmcli -g GENERAL.IP4-CONNECTIVITY device show <iface>`) + `ping -I <iface>` to the gateway (v5).
- Crypto: `cryptography`. Logging: `structlog` (JSON, secrets redacted).
- QR: `qrcode` (terminal), shown next to the typed setup code with the "demo only" note.
- Buttons and LED: single keypresses read with the standard library (`termios`, `tty`) behind a small input interface, plus a status line (D2). No new dependency.

Configurator side:
- **Must: Python CLI on laptop B** with `bleak`, guided prompts (pick device, type setup code, pick SSID from the device's scan or enter a hidden one, enter password or Enterprise login, see result, retry on failure). Keep it non-developer friendly: no flags required, plain-language errors.
- **Could: bare-bones HTML/JS page** (single file, no framework) for Chrome on Android via Web Bluetooth. Same protocol.

Test infrastructure:
- Unit tests (pytest) for crypto, framing, state machine.
- `--simulate` mode: a local Unix-socket transport + fake Wi-Fi backend, so the whole flow runs on one machine without radios (SPEC §10).
- Unit and end-to-end tests: in-memory transport + fake Wi-Fi backend, in CI on Python 3.12 and 3.14 (D1).
- Real runs: phone hotspot (PSK) and `hostapd` EAP on laptop B (Enterprise). `btmon` capture as evidence that only ciphertext goes over the air.

## 7. Repo layout

> Early sketch. The implemented layout is in `docs/DESIGN.md` §2 (single package under `src/`, plus simulation transports).

```
teton-provisioning/
├── README.md                 # quickstart, hardware assumptions, demo steps, troubleshooting
├── docs/
│   ├── ARCHITECTURE.md       # channel rationale, data flow, security, 200-device scaling
│   ├── PROTOCOL.md           # GATT UUIDs, message formats, handshake
│   └── evidence/             # logs, screenshots, btmon capture, video link
├── device/                   # teton_device package: ble_server, session, wifi (nm), state machine, cli
├── configurator/             # python CLI/TUI (bleak) + web/ (Web Bluetooth page)
├── common/                   # protocol + crypto shared by both Python sides
├── deploy/teton-provisioning.service
├── tests/
└── pyproject.toml
```

## 8. Scaling to 200 devices (writeup section)

Points to cover:
- **Identity and trust at manufacture**: per-device keypair + certificate signed by a Teton CA (instead of a static PoP); configurator verifies device authenticity, device verifies configurator is authorized.
- **Pre-staged config instead of typing passwords**: facilities team never sees the PSK. The configurator app pulls an encrypted "site profile" (SSID, EAP certs, Smith enrollment token) ahead of time from Smith, or a signed profile is loaded once per wing. Per-device enterprise certs (EAP-TLS) rather than shared PSK.
- **Batch flow**: scan many device QRs, app provisions in parallel (BLE central can hold several connections; or devices daisy-chain/relay: first provisioned device fetches config for others via mesh-like "provisioned peer" model, as Matter/Thread does).
- **Bulk alternatives**: Wi-Fi Easy Connect (DPP) with QR bootstrapping, or a temporary provisioning SSID with 802.1X and per-device certs, or zero-touch: device ships with a staging network profile, registers with Smith, Smith pushes the real config.
- **Operator UX**: progress dashboard per device (pending/connected/failed with plain-language fix), retries, audit log, role-based access, no secrets displayed.
- **Fleet integration**: device reports to Smith once online; Smith records who provisioned what, when, and where (room/wing), enables remote credential rotation later.
- **RF realities**: BLE congestion with 200 advertisers, advertise only during a button-opened pairing window (so only the devices being set up right now are on air, D2), short-lived sessions, physical proximity checks (RSSI), devices labelled by location.

## 9. Scope and milestones (moderate effort)

**Must** (submission is credible with only these):
1. Protocol + crypto with in-memory transport, unit tests.
2. Device: NM adapter (scan, connect PSK/SAE + PEAP, verify internet, report back), BLE GATT server.
3. Laptop configurator CLI.
4. README (setup, hardware, demo steps, troubleshooting), ARCHITECTURE.md (channel, security, 200-device section), evidence (logs, screenshots, `btmon`).

**Should**:
5. `--simulate` mode + GitHub Actions CI.
6. systemd unit (+ polkit rule), preflight check script.
7. Enterprise test with `hostapd` on laptop B, documented.

**Could** (only if time; otherwise covered in the writeup):
8. HTML/JS phone configurator.
9. EAP-TLS (certificate) support.
10. `.deb` packaging, Prometheus metrics, "enroll with Smith" MQTT stub.

## 10. Risks

- BlueZ peripheral quirks (adapter must be powered, `bluetoothd --experimental` sometimes needed, pairing agents interfering). Mitigation: pin Ubuntu 24.04, document `btmgmt` setup, and run the BLE spike right after project tooling, before any protocol code (v5).
- Laptop already on Wi-Fi: demo must disconnect/forget, then connect to the target (phone hotspot works well).
- Root/polkit for NetworkManager: run daemon as root via systemd, or add a polkit rule; document it.
- iOS has no Web Bluetooth: state Android/Chrome requirement, or use laptop configurator.
- Enterprise testing needs an AP-capable Wi-Fi card on laptop B; if it lacks AP mode, fall back to a home router that supports WPA2-Enterprise, or document Enterprise as tested only in simulation.
- Dual boot: make sure Bluetooth works on Ubuntu on both laptops early (some adapters need firmware packages).

## 11. Tailoring to the job description (Software Engineer, Fleet)

JD: https://jobs.ashbyhq.com/teton/124f34be-6adf-4cf8-b4ef-381e0760b18a (Copenhagen, onsite). Core areas: fleet provisioning/monitoring, OTA (Mender/RAUC/balena, staged rollouts, health checks, auto-rollback), observability (Prometheus/Grafana/OTel), Linux internals (systemd, kernel modules, networking), Bluetooth/Wi-Fi/MQTT, restricted hospital networks, CI/CD and image builds. Nice-to-have: Jetson/L4T, Yocto/Debian packaging, healthcare.

Cheap additions that signal fit (in priority order). In v2 these map to Should/Could in §9; anything not built goes into the writeup:

1. **systemd-native daemon**: hardened unit (`ProtectSystem=strict`, `NoNewPrivileges`, `CapabilityBoundingSet=CAP_NET_ADMIN`, D-Bus access to BlueZ/NM only), `Type=notify` + watchdog (`sdnotify`), journald structured logs with fields (`DEVICE_ID`, `STATE`, `SESSION_ID`). A polkit rule instead of running fully as root.
2. **Debian package**: build a `.deb` (via `nfpm` or `dh-virtualenv`) in GitHub Actions, installs unit + polkit rule. Mirrors "image build / Debian packaging".
3. **Observability hooks**: provisioning metrics (`provisioning_attempts_total{result}`, handshake/connect latency histograms, RSSI) via `prometheus_client` or a node_exporter textfile; documented as what Smith would scrape/ingest. Logs never contain secrets (add a test asserting this).
4. **Post-provisioning hand-off stub**: after Wi-Fi is up, device "enrolls with Smith" (MQTT over TLS to a local Mosquitto in the demo, or a stub) with the result + device facts. Shows the provisioning → fleet-management boundary.
5. **Hospital-network realism** in the writeup and error handling: WPA2/3-Enterprise (EAP-TLS/PEAP) support path via NM, captive portals detection (NM connectivity check), MAC allowlisting (report MAC to the technician / pre-register), egress restricted to Smith endpoints, proxy support, hidden SSIDs, 2.4 vs 5 GHz, DFS.
6. **Edge hardware portability**: note Jetson/L4T (aarch64, BlueZ + NM present on L4T Ubuntu), keep deps pure-Python or with aarch64 wheels; CI matrix on arm64 if cheap.

Out of scope for the take-home but worth one paragraph each in the writeup: how provisioning ties into OTA (A/B rootfs with RAUC/Mender; Wi-Fi config lives on a persistent data partition so it survives updates and rollbacks), and factory provisioning (per-device keys/certs injected at manufacture, TPM/secure element storage).

## 12. What the follow-up interview will likely probe

- **Channel choice defence**: why BLE over SoftAP/DPP/QR; BLE pairing modes (Just Works vs. passkey vs. OOB) and why app-layer crypto anyway; MTU/chunking; what happens with 200 devices advertising at once.
- **Crypto reasoning**: why ECDH alone is MITM-able, what the PoP buys, PAKE (SPAKE2/SRP) vs. certificates, nonce handling, replay, PoP leakage (label photographed) and rotation, where secrets live at rest on the device (NM keyfiles 0600, TPM/secure element).
- **Linux internals**: BlueZ D-Bus architecture, NetworkManager vs. wpa_supplicant vs. netplan on Ubuntu, systemd unit design and sandboxing, polkit, rfkill, debugging with `btmon`, `bluetoothctl`, `journalctl`, `nmcli`, `iw`.
- **Failure modes**: wrong password vs. SSID out of range vs. DHCP failure vs. captive portal; how each is detected and surfaced to a non-technical user; rollback to provisioning mode; device reboot mid-flow; idempotency.
- **Fleet scale**: the 200-device scenario; zero-touch / pre-staged profiles; identity (per-device certs, CA); audit trail; how this feeds Smith; enterprise Wi-Fi with per-device certs.
- **Ops**: how you'd ship and update this daemon (deb/image, OTA, staged rollout), what you'd monitor and alert on, how you'd debug a device that never shows up in Smith from a hospital you can't SSH into.
- **Live extension exercise** (common in follow-ups): e.g. add re-provisioning / credential rotation, support WPA-Enterprise, add a status LED/beep, or port the device side to Rust.

Prep: be ready to demo live, walk through `btmon` showing ciphertext only, and explain trade-offs you consciously skipped.
## 13. Decision log for the BLE-specific questions

All resolved on 2026-10-11; details are folded into §4, §5 and §6.

| # | Question | Decision |
|---|---|---|
| Q1 | Key exchange | X25519 + HKDF salted with setup code + key confirmation; PAKE as writeup upgrade |
| Q2 | BLE OS pairing | Not used |
| Q3 | Enterprise server validation | Optional CA cert / domain, warn if missing; production enforces |
| Q4 | Message format | JSON |
| Q5 | Network selection | Device scan list + manual hidden SSID |
| Q6 | Failure handling | Report reason, keep session, retry |
| Q7 | After success | Stop advertising, explicit reset to re-enter (since D2: the [R] button, double press) |
| Q8 | Demo run mode | Foreground command; systemd unit is a Should |
| Q9 | Setup code input | Typed code; QR shown with "demo only" note |
| Q10 | Ubuntu | 24.04 LTS (since D1: device 24.04; configurator 24.04 or 26.04) |

Defaults applied (no objection raised): guided terminal configurator, `nmcli`, `venv` + `pip` install, one session at a time with timeouts and handshake rate limiting.
