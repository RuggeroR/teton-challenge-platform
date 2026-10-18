# Implementation plan

Version 9, 2026-10-19. This plan turns [SPEC.md](SPEC.md) and [DESIGN.md](DESIGN.md) into small steps, in the order we build them. Why each decision was made is in [plan_v5.md](plan_v5.md). Version 2 applied the answers to review questions Q1, Q2, Q3, Q7, Q8 and Q9; version 3 applied the post-spike simplifications S1–S4 and fixed the laptop roles; version 4 applies D1 (laptop B on Ubuntu 26.04 / Python 3.14: new Step 0.3, CI matrix) and D2 (device buttons and LED: new Step 2.7, `set_advertising`, the device stays on after success, no `reset`/`show-code` subcommands) (plan_v5); version 5 records the answers to the open questions that block Phase 2 (plan_v5, "Phase 2 review questions"); version 6 records the Phase 3 answers, step by step (plan_v5, "Phase 3 review questions"); version 7 records the Phase 4 answers (plan_v5, "Phase 4 review questions", Q48–Q63); version 8 records the Phase 5 answers (plan_v5, "Phase 5 review questions", Q64–Q80); version 9 records the Step 6.1 and 6.2 answers (plan_v5, "Phase 6 review questions", Q81–Q101). If this plan disagrees with those documents, they win.

## How to read this plan

- We build **bottom-up**: each step depends only on steps already finished.
- Scope follows SPEC §12: **MUST** first, then **SHOULD**, then **COULD**. Two exceptions: the BLE hardware spike runs right after project tooling (Step 0.2), because it can change the transport interface; and simulation mode (SHOULD) comes before the real BLE transports, because it is cheap and lets us debug the full protocol without radios.
- Every step lists its goal, the files it touches, the tests to write, how you can verify it yourself, and the SPEC/DESIGN sections it implements.
- 🔧 **HARDWARE** marks steps that need the laptops or the phone. Every other step runs on any machine.
- **Roles (fixed in plan_v5):** laptop A, the Teton Device, is **laptop 1** (`ruggero-PC`, where the spike ran; Ubuntu 24.04, Python 3.12). Laptop B, the configurator and Enterprise test AP, is **laptop 2** (Ubuntu 26.04, Python 3.14; D1). The code uses nothing newer than Python 3.12.
- **Guiding principle (plan_v5):** this is a demo. Prefer the simplest version that works and can be explained, over production completeness.
- Every step ends with the same checks:
  ```bash
  . .venv/bin/activate
  pytest                                   # all tests pass
  ruff check . && ruff format --check .    # no lint errors, formatting clean
  ```
- Phases map to milestones. CLAUDE.md asks for one branch and one PR per milestone. You create the branches and commit; I only tell you when a step is ready to commit.
- Open questions are in the section at the end. A step that depends on an unanswered question says so.

## Overview

| Phase | Steps | Scope | Hardware |
|---|---|---|---|
| 0. Tooling + BLE spike + laptop B check | 0.1–0.3 | MUST | 🔧 0.2, 0.3 |
| 1. Shared pure code (`teton_common`) | 1.1–1.7 | MUST | no |
| 2. Device core (`teton_device`) | 2.1–2.8 | MUST | 2.5 (scan only, one laptop) |
| 3. Configurator (`teton_configurator`) | 3.1–3.3 | MUST | no |
| 4. End-to-end test (in memory) | 4.1 | MUST | no |
| 5. Simulation mode + CLIs | 5.1–5.2 | SHOULD (CLIs are MUST) | no |
| 6. Real BLE + real Wi-Fi | 6.1–6.3 | MUST | 🔧 yes |
| 7. SHOULD items | 7.1–7.3 | SHOULD | 🔧 7.2, 7.3 optional |
| 8. Docs and writeup | 8.1–8.3 | MUST | no |
| 9. COULD items | 9.x | COULD, only if you ask | varies |

---

## Phase 0: Tooling

### Step 0.1: Project tooling and package skeleton

- **Goal:** `pip install -e ".[dev]"` works, both console scripts exist, and `pytest` and `ruff` run on an almost empty codebase.
- **Files:**
  - `pyproject.toml`: package `teton-provisioning`, `requires-python = ">=3.12"`, dependencies from DESIGN §8 only (`cryptography`, `structlog`, `bless`, `bleak`, `qrcode`), `dev` extra (`pytest`, `pytest-asyncio`, `ruff`), console scripts `teton-device` and `teton-configurator`, a ruff config (line length, rule set incl. import sorting), and a pytest config (`asyncio_mode = "auto"`, `testpaths = ["tests"]`). The build backend is setuptools, the default, so no extra tooling is needed.
  - `.gitignore`: it is currently a Visual Studio template. Add the Python entries (`.venv/`, `*.egg-info/`, `.pytest_cache/`, `.ruff_cache/`, `build/`, `dist/`). It also ignores `*.log` and `[Ll]ogs/`, which would silently drop our evidence logs, so add `!docs/evidence/**`.
  - `src/teton_device/cli.py` and `src/teton_configurator/cli.py`: a minimal `main()` that prints "not implemented yet", so the console scripts can be installed. `__main__.py` delegates to it.
  - `tests/test_package.py`: a smoke test (the packages import, and the console scripts are registered).
- **Tests:** imports of the three packages, plus `teton-device --help` and `teton-configurator --help` exit 0.
- **Verify:**
  ```bash
  python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
  teton-device --help; teton-configurator --help   # both print usage
  pytest                                           # 1-2 passed
  ruff check . && ruff format --check .            # All checks passed
  git status --short                               # .venv/ and caches don't show up
  ```
- **Implements:** SPEC §2 (install), DESIGN §2, §8.

### Step 0.2: 🔧 HARDWARE: BLE spike (`bless` on Ubuntu 24.04)

- **Goal:** confirm what `bless` can do on BlueZ 5.72 before writing any protocol code. It is the riskiest unknown, and the results can change the transport interface (review Q3).
- **Files:** `scripts/spike/ble_peripheral.py` and `scripts/spike/ble_central.py`. They are throwaway code, not product code.
- **Core checks (from your answer to Q3):**

  | # | Question | How |
  |---|---|---|
  | a | Laptop A advertises one test service | look for `Teton-SPIK` + the service UUID in `bluetoothctl scan le` on laptop B, or in nRF Connect on the phone |
  | b | Read, write and notify all work | the central (laptop B with `bleak`, or nRF Connect) reads one characteristic, writes to one, and receives a notification |
  | c | The negotiated MTU | the central prints `client.mtu_size`; the peripheral logs the `mtu` from the BlueZ write options, if `bless` passes them through |
  | d | Whether it works **without `sudo`** | run the peripheral as your user first; if registration fails, retry with `sudo` and note exactly what was needed |

- **Extra checks (cheap, and needed by `DeviceTransport.on_disconnect` and by `busy`):**

  | # | Question | How |
  |---|---|---|
  | e | The peripheral notices a disconnect | disconnect the central and see whether `bless` gives any event (an unsubscribe, or D-Bus `Device1.Connected`) |
  | f | The peripheral can tell two clients apart | connect the laptop and the phone together and check whether writes carry the device path |
  | g | Advertising can be stopped while connected | stop it and check that the connection still delivers a notification |
  | h | No OS pairing dialog appears | watch both desktops |

- **You do:**
  1. Laptop A: `python scripts/spike/ble_peripheral.py`.
  2. Laptop B: `python scripts/spike/ble_central.py`. Alternatively, use **nRF Connect** on the Android phone as the central. Then the spike needs only one laptop.
  3. Optionally, in parallel: `sudo btmon` on laptop A, to see the ATT traffic and the MTU exchange.
- **Output:** a results table that I add to this plan. If (e) or (f) fail, see Q15; if anything forces a change to `DeviceTransport`, we record it before Step 1.7.
- **Implements:** DESIGN §9. plan_v5 changelog (review Q3).

#### Results (2026-10-11, laptop 1 + Pixel 9 with nRF Connect)

Setup: Ubuntu 24.04.5, kernel 7.0.0-38, BlueZ 5.72, Intel Meteor Lake CNVi adapter, `bless` 0.3.0. Evidence: `docs/evidence/spike-peripheral*.log` and `docs/evidence/spike-connect*.btsnoop` (read with `btmon -r <file>`).

| # | Check | Result | Evidence |
|---|---|---|---|
| a | Advertises, phone connects | ✅ | `LE Enhanced Connection Complete, Role: Peripheral`; phone discovers `18388b82…` and the 4 characteristics |
| b | Read / write / notify | ✅ | `info` sent whole in one 204-byte Read Response; `hello` written to `session` → `echo:hello` notification; 14 `status` notifications |
| c | MTU | ✅ 517 | Android requests 517 and BlueZ accepts. Our code sees it **only through a patch**: BlueZ passes `options={'device', 'link', 'mtu'}` on every read and write, and `bless` drops them |
| d | No `sudo` | ✅ | Ran as uid 1000; GATT app and advertisement registered |
| e | Disconnect noticed | ✅ | `Device1` `Connected: False` signal on the system bus |
| f | Tell clients apart | ✅ partly | Every read and write carries the client's `device` path (through the patch). Two simultaneous clients not tested, and mostly moot (see g) |
| g | Advertising while connected | ⚠️ different from expected | The controller **ends advertising when a client connects** (`LE Advertising Set Terminated`), and the kernel **re-enables it about 30 ms after the disconnect** (`LE Set Extended Advertising Enable`). The connection survives unregistering the advertisement |
| h | No pairing | ✅ | 0 SMP (pairing) packets in either capture |
| i | Long reads | ✅ at MTU 517, untested at small MTU | 204 bytes fit in one packet. `bless` ignores the read `offset`, so a long read at a small MTU would return garbage; the spike's patch returns the right slice |
| — | `bleak` central (`ble_central.py`) | superseded | Superseded by Step 6.2's Verify, with the real device and configurator; the MTU read is unnecessary since S2 (review Q101) |

Observations:
- Android uses a new random (resolvable) address on every connection, so a phone can be told apart within a connection but not recognised later.
- `bless` sets the adapter's Bluetooth **alias** (the laptop's visible name) to the advertised name while it runs, and resets it on stop (the reset takes about a second to show).
- Troubleshooting: the first connection attempt hung with no event on the laptop. Restarting the nRF Connect app fixed it. At the same time the Ubuntu Bluetooth settings panel had made the adapter discoverable, and the kernel logged `Opcode 0x2036 failed: -16` (advertising parameters busy). Keep the settings panel closed during runs; both go in the README troubleshooting table.

Design consequences:
1. **One connection at a time is enforced by the radio:** while a configurator is connected, the device doesn't advertise, so a second configurator can neither find nor connect to it. → Decided as **S1**: `busy` removed.
2. **Stopping advertising must be explicit and must happen before the configurator disconnects,** otherwise the kernel re-enables it on disconnect. → Decided as **S4**: after success the device stops its BLE server and exits. (Since D2 it stops advertising and stays on; the ordering rule still holds.)
3. **Advertising comes back by itself after a failed session** (for example a wrong setup code), so the device doesn't need a restart. → Together with 1, decided as **S3**: the device doesn't track connections; a new `hello` starts a new session.
4. **`bless` alone can't give us the MTU** (so the chunk size would be stuck at 20) **and breaks long reads.** → Decided as **S2**: fixed 20-byte chunks and an `info` under 180 bytes, so `bless` is used as is.

### Step 0.3: 🔧 HARDWARE (laptop B): install check on Ubuntu 26.04 / Python 3.14

- **Goal:** confirm that the package installs on laptop 2's Python 3.14 and that the configurator's libraries work there, before we build on them (plan_v5, D1).
- **Files:** none. The results go into this plan, and laptop 2's column into the README "Tested on" table.
- **You do (laptop 2):**
  ```bash
  lsb_release -rs; python3 --version                  # 26.04, Python 3.14.x
  python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
  python -c "import cryptography, bleak; print(cryptography.__version__, bleak.__version__)"
  python -c "from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey as K; K.generate(); print('x25519 ok')"
  pytest                                              # same result as on laptop 1
  ```
- **Watch for:** there is one distribution, so `pip` also installs `bless` (and its D-Bus libraries) on laptop B. It only has to install there, not run. If `bless` or `bleak` doesn't install on 3.14 (no wheel and a failing source build), we stop and decide; one option is an optional `device` extra, which changes DESIGN §8.
- **Implements:** plan_v5 D1. SPEC §2.

---

## Phase 1: Shared pure code (`teton_common`)

No I/O in this phase: no sockets, no subprocesses, no clock or randomness except through injected parameters.

### Step 1.1: Protocol constants

- **Goal:** one place for every protocol constant, so no magic strings elsewhere.
- **Files:** `src/teton_common/protocol.py`, `tests/test_protocol.py`.
- **Content:**
  - `PROTOCOL_VERSION = 1` and `PROTOCOL_LABEL = "teton-prov-v1"`.
  - The service and characteristic UUIDs.
  - Enums: `Channel` (info, session, command, status), `Security` (open?, wpa2-psk, wpa3-sae, wpa2-eap-peap; see Q11), `DeviceState`, `FailureReason` (the 7 reasons of SPEC §7), `ErrorCode`.
  - Limits: max message 8192, chunk size 20 (fixed, S2), reassembly timeout 10 s, session idle 300 s, connect timeout 45 s, rate limit 5 failures / 30 s doubling to 600 s.
- **Tests:** the UUIDs are valid and distinct, the enum string values match the SPEC exactly, and the capability list matches SPEC §6.1.
- **Verify:** `pytest tests/test_protocol.py -v`
- **Implements:** SPEC §4, §5.4, §6, §7.

### Step 1.2: Setup code

- **Goal:** generate, format, normalise and validate the 80-bit setup code.
- **Files:** `src/teton_common/setup_code.py`, `tests/test_setup_code.py`.
- **Content:**
  - `generate(randbits=secrets.randbits) -> str`: 16 Crockford base32 characters.
  - `format_code(code) -> "XXXX-XXXX-XXXX-XXXX"`.
  - `normalize(user_input) -> str`: uppercase, strip spaces and `-`, map `O→0`, `I→1`, `L→1`, and raise `InvalidSetupCode` unless exactly 16 valid characters remain.
  - The exception message never contains the input.
- **Tests:** length and alphabet of generated codes; no `I L O U` in the output; generated codes are distinct; format round trip; normalisation of lowercase, spaces, dashes and ambiguous letters; rejects `U`, wrong lengths and symbols; the error text contains no input.
- **Verify:** `pytest tests/test_setup_code.py -v`, then try it by hand: `python -c "from teton_common import setup_code as s; c=s.generate(); print(s.format_code(c))"`
- **Implements:** SPEC §3. DESIGN §7 (`setup_code` row).

### Step 1.3: Messages

- **Goal:** typed messages with strict JSON encode/decode and validation, so the rest of the code never handles raw dicts.
- **Files:** `src/teton_common/messages.py`, `tests/test_messages.py`.
- **Content:**
  - Frozen dataclasses for `Hello`, `HelloReply`, `Confirm`, `HandshakeFailed`, `Envelope` (`seq`, `ct`), `Ready`, `Scan`, `SetCredentials` (with a nested `EapCredentials`), `ScanResult` / `NetworkInfo`, `Status`, `Error` and `Info`.
  - One `encode(msg) -> bytes` and one `decode(data, allowed=...) -> Message` that dispatches on `type`.
  - Binary fields are base64 in JSON and `bytes` in Python.
  - Validation: SSID 1–32 UTF-8 bytes, PSK 8–63, known security values, exact key and nonce lengths, and that unknown or missing fields are rejected.
  - Secret fields (`psk`, `password`, `ca_cert_pem`) use `field(repr=False)`, so a stray `print(msg)` or an exception never shows them.
- **Tests:** round trip for every message type; invalid JSON, a wrong type, missing fields, bad base64, wrong lengths, an SSID over 32 bytes and a PSK of 7 or 64 characters are all rejected with `MessageError`; `repr()` of `SetCredentials` contains no secret.
- **Verify:** `pytest tests/test_messages.py -v`
- **Implements:** SPEC §5.1, §5.3 (envelope shape), §6. DESIGN §7 (`messages`).

### Step 1.4: Framing

- **Goal:** split a message into 20-byte chunks and put it back together, separately per channel.
- **Files:** `src/teton_common/framing.py`, `tests/test_framing.py`.
- **Content:**
  - `frame(payload, chunk_size) -> list[bytes]` adds the 2-byte big-endian length prefix and splits the result.
  - `Reassembler(clock=time.monotonic)` has `feed(chunk) -> bytes | None` and `reset()`. It rejects a declared length over 8192, and drops a partial message older than 10 s.
  - `CHUNK_SIZE = 20` (S2). `frame()` still takes the size as a parameter, so tests can show the framing is generic and production could pass `MTU - 3`.
- **Tests:** chunk size 20 round trip, plus one larger size; exactly 8192 works and 8193 is rejected; interleaved channels using separate reassemblers; the timeout reset, with a fake clock; `reset()`; an empty payload; no chunk is ever longer than 20 bytes.
- **Verify:** `pytest tests/test_framing.py -v`
- **Implements:** SPEC §4.1. DESIGN §7 (`framing`).

### Step 1.5: Crypto (handshake + envelope)

- **Goal:** the whole security protocol as a pure, synchronous API, exactly as in SPEC §5.
- **Files:** `src/teton_common/crypto.py`, `tests/test_crypto.py`.
- **Content:**
  - `ConfiguratorHandshake(setup_code)` has `.hello() -> Hello` and `.on_hello_reply(reply) -> (Confirm, SessionKeys)`. It raises `HandshakeError` if `confirm_d` doesn't match.
  - `DeviceHandshake(device_id, setup_code)` has `.on_hello(hello) -> HelloReply` and `.on_confirm(confirm) -> SessionKeys`.
  - The transcript and HKDF follow §5.2 exactly. Tags are compared with `hmac.compare_digest`.
  - `Envelope(key, direction)` has `seal(plaintext) -> Envelope message` and `open(envelope) -> plaintext`, with the nonce and AAD from §5.3. It accepts only the expected `seq`.
  - All primitives come from `cryptography`. Randomness (keys and nonces) can be injected, for a fixed test vector.
- **Tests:**
  - A full round trip gives the same keys on both sides.
  - A wrong setup code fails at the configurator's `confirm_d` check, and a forged `confirm_c` fails at the device.
  - Tampered ciphertext, tampered `seq`, a replayed `seq`, a skipped `seq` and a sealed message opened in the wrong direction are all rejected.
  - `k_c2d`, `k_d2c` and `k_confirm` are all different.
  - An all-zero X25519 public key is rejected.
  - A **known-answer vector** with fixed keys and nonces pins the derivation, so a later change, or the COULD web page, can be checked against it.
  - Error messages contain no key material.
- **Verify:** `pytest tests/test_crypto.py -v`
- **Decided:** the AAD is `"teton-prov-v1|" + direction + "|" + str(seq)` as in SPEC §5.3 (review Q1).
- **Implements:** SPEC §5.1–5.3. DESIGN §5 (crypto API), §7 (`crypto`).

### Step 1.6: Logging and redaction

- **Goal:** structured JSON logs where secret fields can't leak, even by accident.
- **Files:** `src/teton_common/log.py`, `tests/test_log.py`.
- **Content:**
  - `configure_logging(verbose: bool, stream=sys.stdout)`: structlog emits JSON lines with `ts`, `level` and `event`, and binds context (`device_id`, `session_id`, `state`).
  - `redact_secrets` processor: walks nested dicts and lists, and replaces the values of the SPEC §5.5 keys (`psk`, `password`, `setup_code`, `confirm`, `pub`, `ct`, `ca_cert_pem`) with `"[REDACTED]"`. `code` is not in the list, so error codes stay readable (review Q7).
- **Tests:** each listed key is redacted at the top level and when nested; non-secret keys are kept; an error event with `code="bad_request"` still shows `bad_request` while `setup_code` is redacted (review Q7); the output is valid JSON; `--verbose` switches the level to DEBUG. The full-run "no secret in logs" test comes in Step 4.1, once a full run exists.
- **Verify:** `pytest tests/test_log.py -v`
- **Implements:** SPEC §5.5, §11.

### Step 1.7: Transport interfaces + in-memory transport

- **Goal:** the abstraction that lets one protocol implementation run over BLE, a Unix socket or memory.
- **Files:** `src/teton_common/transport.py`, `src/teton_common/memory_transport.py`, `tests/test_memory_transport.py`.
- **Content:**
  - `DeviceTransport` (`start`, `set_advertising`, `set_info`, `on_receive`, `send`, `close`) and `ClientTransport` (`connect`, `read_info`, `on_receive`, `send`, `close`), as in DESIGN §5 (review Q2, simplified by S2 and S3: no connection events, no chunk size; `set_advertising` added by D2).
  - `info` is a plain read of raw JSON and is not framed (SPEC §4.1).
  - A small `FramedTransport` helper that combines a transport, `framing.frame` and one `Reassembler` per channel, so framing sits above the transport.
  - `memory_transport.pair()` returns a device end and a client end. The client can only connect while the device end is advertising, and turning advertising off keeps an existing connection.
- **Tests:** `read_info()` returns what `set_info()` set; messages arrive on the right channel in order; large messages go through framing in 20-byte chunks; after `close()`, sending raises an error on both ends; `connect()` fails while advertising is off; `set_advertising(False)` doesn't drop a connected client.
- **Verify:** `pytest tests/test_memory_transport.py -v` (renamed `tests/test_transport_contract.py` in Step 5.1, Q71)
- **Implements:** DESIGN §1 (principles), §5 (`DeviceTransport`, `ClientTransport`).

---

## Phase 2: Device core (`teton_device`)

### Step 2.1: State machine and storage

- **Goal:** the device always knows its state, invalid transitions are impossible, and identity and provisioning survive restarts.
- **Files:** `src/teton_device/state.py`, `src/teton_device/storage.py`, `tests/test_state.py`, `tests/test_storage.py`.
- **Content:**
  - `StateMachine` with the DESIGN §4 transitions, including `UNPROVISIONED → ADVERTISING` on [P], `ADVERTISING → UNPROVISIONED` when the window expires, and `PROVISIONED → UNPROVISIONED` on reset. `transition(to)` raises `InvalidTransition` on a forbidden transition, and every transition is logged.
  - `Storage(state_dir)` has `load_or_create()`, which creates `device_id` (`TETON-` + 6 hex) and a setup code, creates the directory `0700` and the file `0600`, and writes atomically (temp file + `os.replace`). It also has `mark_provisioned()` and `reset()` (new setup code, `provisioned` cleared, `device_id` kept), used by the [R] button.
  - `state.json` also holds a `provisioned` flag. SPEC §3 lists only `device_id` and `setup_code`, but DESIGN §4 requires it to be persisted.
- **Tests:** every allowed transition; a set of forbidden ones; the state after reload when provisioned; the permissions are 0700 and 0600; the file is created once and reused afterwards; `reset` changes the code, clears `provisioned` and keeps `device_id`; a corrupt `state.json` gives a clear error.
- **Verify:** `pytest tests/test_state.py tests/test_storage.py -v`
- **Decided:** Q13: five states, no `CONNECTED` and no `FAILED`; a handshake in progress stays `ADVERTISING`.
- **Implements:** SPEC §3 (identity, storage, rotation). DESIGN §4, §7 (`state`).
- **Code to update:** the `DeviceState.PROVISIONED` comment in `protocol.py` still says the process exits (S4); since D2 the device stays on.

### Step 2.2: Setup code display (QR)

- **Goal:** print the code, the terminal QR and the "demo only" note.
- **Files:** `src/teton_device/qr.py`, `tests/test_qr.py`.
- **Content:** `qr_payload(device_id, code)` returns `TETON:v1;id=…;code=…`. `render(device_id, code) -> str` returns the formatted code, an ASCII QR (from `qrcode`) and the exact note text from SPEC §3. It is printed on start and after every reset.
- **Tests:** the payload format; the output contains the formatted code and the note.
- **Verify:** `pytest tests/test_qr.py -v`, then `python -c "from teton_device.qr import render; print(render('TETON-3F9A2C','7K2MQX9D4HTRB8WZ'))"` and scan the QR with your phone camera; it should show the payload text.
- **Decided:** Q14: `code=` is the normalised 16 characters, without dashes.
- **Implements:** SPEC §3 (display).

### Step 2.3: Wi-Fi backend interface + fake backend

- **Goal:** the only door to Wi-Fi, and a fake that behaves like the SPEC §10 networks.
- **Files:** `src/teton_device/wifi/backend.py`, `src/teton_device/wifi/fake.py`, `tests/test_wifi_fake.py`.
- **Content:**
  - The `WifiBackend` protocol (`scan`, `connect`, `forget_all`; Q24) and `Connectivity` (`verify`).
  - Result types: `Network`, `Credentials` (secrets use `repr=False`), `ConnectResult` (`ok`, `reason`, `ip`, `internet`, `warnings`; `internet` is needed by the `connected` status, SPEC §6.3) and `VerifyResult`.
  - `FakeWifiBackend` with `Home-PSK`, `Hospital-EAP`, `Cafe-Portal` and `Weak-Signal`. Its delays are injectable, so tests don't wait 45 s.
  - The `server_not_verified` warning rule (PEAP without a CA and without a domain) lives in one shared function. Both the fake and the real backend use it, so they can't disagree.
- **Tests:** the right password connects; a wrong password gives `wrong_password`; an unknown SSID gives `ssid_not_found`; Cafe-Portal gives `captive_portal`; Weak-Signal gives `timeout`; Hospital-EAP with a bad user gives `auth_failed`; PEAP without a CA or domain gives the warning; scan results are deduplicated, sorted and capped at 30.
- **Verify:** `pytest tests/test_wifi_fake.py -v`
- **Implements:** SPEC §6.3 (scan result rules), §7, §10 (fake networks). DESIGN §5 (`WifiBackend`, `Connectivity`).

### Step 2.4: NetworkManager backend (`nmcli`), parsing and commands

- **Goal:** the real backend, with all decision logic in pure functions that are tested against recorded `nmcli` output.
- **Files:** `src/teton_device/wifi/nmcli.py`, `src/teton_device/wifi/connectivity.py`, `tests/test_nmcli_parsing.py`, `tests/fixtures/nmcli/*`.
- **Content:**
  - **Pure functions:**
    - `parse_scan(stdout)` handles `-t` escaping (`\:` inside SSIDs), empty and hidden SSIDs, and maps nmcli `SECURITY` strings to our enum.
    - `build_add_args(creds, profile_name)` builds the arguments for PSK, SAE and PEAP: `802-1x.eap peap`, `phase2-auth mschapv2`, and `ca-cert` / `domain-suffix-match` when given.
    - `map_failure(returncode, stderr)` maps nmcli errors to a `FailureReason`. Unknown errors become `timeout`, and the raw text goes to the debug log only.
    - `parse_device_show(stdout)` reads the IP address, the gateway and the interface's own connectivity (`GENERAL.IP4-CONNECTIVITY`, e.g. `4 (full)`).
    - `decide_result(...)`, in `connectivity.py`, applies the success rule (review Q8): `portal` → `captive_portal`; `full` → success; anything else → success with the warning `limited_internet` if the gateway answers, otherwise `no_internet`.
  - **I/O wrapper:** `NmcliBackend(runner=...)` calls `nmcli` through one injectable async `runner` that applies a timeout to every call and can feed stdin. It implements the SPEC §8 sequence: scan, add `teton-<ssid>` **without secrets**, set the secret by piping `set …`/`save`/`quit` into `nmcli connection edit id teton-<ssid>` (review Q9), `up --wait 45`, verify, and on failure delete the profile.
  - **Connectivity:** `NmcliConnectivity.verify(interface)`, in `nmcli.py` so nmcli is called from one module only, checks for an IPv4 address, runs `nmcli networking connectivity check` to refresh, reads the **interface's** connectivity, then pings the gateway **on that interface** (`ping -I <iface> -c 2 -W 2`) when it isn't `full` (review Q8).
  - **CA certificate:** a supplied CA is written to `<state_dir>/certs/<profile>.pem` (mode `0600`), because NetworkManager needs a file path.
  - **Wi-Fi interface:** auto-detected as the first Wi-Fi device that NetworkManager manages. There is no new CLI flag.
- **Tests:**
  - Parsing the fixtures.
  - Mapping each SPEC §7 reason, including unknown text → `timeout`.
  - The argument lists for each security type. A fake runner records the commands that would run, and the tests check that a failed connect deletes the profile.
  - **No secret in argv:** across every recorded command (PSK, SAE, PEAP), neither the PSK nor the EAP password appears in any argument; they appear only in stdin (review Q9).
  - The success rule: `full`; `portal`; `limited` + gateway ok (success + `limited_internet`); `limited` + gateway down (`no_internet`); `none` and `unknown` + gateway ok.
- **Fixtures:** at first, hand-written from the NetworkManager 1.46 messages and labelled as such. Step 6.3 replaces them with outputs recorded on the laptops, anonymised so no real neighbour SSIDs end up in the repo.
- **Verify:** `pytest tests/test_nmcli_parsing.py -v`. No hardware in this step.
- **Decided:** Q10 (45 s activation + 15 s verification; the configurator waits 70 s), Q11 (no `open`; transition mode → `wpa2-psk`; unjoinable networks left out of the scan), Q17 (`autoconnect-priority 10`).
- **Implements:** SPEC §5.5 (credentials to NM, no secrets in argv), §6.3 (`internet`, warnings), §7, §8. DESIGN §6 (one runner, timeouts, raw output), §7 (`wifi/nmcli`).

### Step 2.5: 🔧 HARDWARE (one laptop, read-only): real scan smoke test

- **Goal:** prove that the real backend can scan on this laptop before we build on it.
- **Files:** none. Fixtures may be updated if real output differs from the hand-written ones.
- **You do (either laptop):**
  ```bash
  python -c "import asyncio; from teton_device.wifi.nmcli import NmcliBackend; [print(n) for n in asyncio.run(NmcliBackend().scan())]"
  ```
  Expected: a list of nearby networks with signal and security type, the strongest first, and no duplicates. This command doesn't change any connection.
- **Also compare the real output with the hand-written fixtures (read-only):**
  ```bash
  nmcli -t -f SSID,SIGNAL,SECURITY device wifi list | head -5          # vs tests/fixtures/nmcli/scan.txt
  nmcli -t -f DEVICE,TYPE,STATE device                                  # vs devices.txt
  nmcli -t -f GENERAL.IP4-CONNECTIVITY,IP4.ADDRESS,IP4.GATEWAY device show <wifi-iface>   # vs device-show-full.txt
  nmcli -t -f NAME,UUID connection show | head -3                       # vs connections.txt
  ```
  Don't paste neighbours' SSIDs into the repo: only the format matters.
- **Also confirm the stdin approach of review Q9 (this one creates and deletes a throwaway profile, so you run it):**
  ```bash
  nmcli connection add type wifi con-name teton-stdin-test ssid teton-stdin-test wifi-sec.key-mgmt wpa-psk autoconnect no
  printf 'set wifi-sec.psk dummy-password-123\nsave\nquit\n' | nmcli connection edit id teton-stdin-test
  nmcli -s -g 802-11-wireless-security.psk connection show teton-stdin-test   # dummy-password-123
  nmcli connection delete teton-stdin-test
  ```
  If the secret isn't saved, we fall back to `passwd-file`, as your answer to Q9 suggests.
- **Implements:** SPEC §5.5, §8.1, §8.2.

#### Results (2026-10-18, laptop 1, NetworkManager 1.46)

| # | Check | Result |
|---|---|---|
| a | `device show` with `-t -f GENERAL.IP4-CONNECTIVITY,IP4.ADDRESS,IP4.GATEWAY` | ✅ exactly the fixture format (`4 (full)`, `ADDRESS[1]` with `/24`, gateway); fixtures unchanged |
| b | Secret through stdin (review Q9) | ✅ `set wifi-sec.psk …` / `save` / `quit` piped into `nmcli connection edit` is saved; read back with `nmcli -s -g` |
| c | A failed `set`/`save` in the editor | ⚠️ exit code **0**, with `Error: Failed to save … property is invalid` in the output. The backend already treats "Error" in the editor output as a failure; the real output is now the fixture `edit-save-failed.txt` |
| d | What the editor prints | ⚠️ it **echoes every command it reads**, so its output contains the secret. Fixed: `run()` never logs the output of a command fed through stdin; a test checks it |
| e | Real scan through `NmcliBackend().scan()` | ✅ nearby networks, strongest first, no duplicates |

### Step 2.6: Device session (protocol handler)

- **Goal:** one connection's worth of protocol on the device: handshake, rate limit, encrypted commands, status notifications, idle timeout.
- **Files:** `src/teton_device/session.py`, `tests/test_session.py`.
- **Content:**
  - `DeviceSession(channels, wifi, storage, state, limiter, clock)`:
    - `handle_session_msg`: `hello` → `hello_reply`, then `confirm` → keys + encrypted `ready`.
    - `handle_command`: opens the envelope, then dispatches `scan` or `set_credentials` and sends `status connecting`, then `connected` or `failed`.
  - `HandshakeLimiter` gives 5 failures, then 30 s doubling to 600 s, with an injected clock, and resets on success.
  - A new `hello` ends any previous session and starts a new one (S3). Protocol violations end the session and are logged with a reason. Wi-Fi failures do not end it.
  - Credentials are deleted (`del`) right after `connect()`.
- **Tests (a minimal test client built from `crypto` over the memory transport, with the fake Wi-Fi):**
  - A good handshake, then `ready`.
  - A wrong code gives `handshake_failed` and is counted.
  - The 6th failure is refused, and is accepted again after the backoff, with a fake clock.
  - `scan` returns the fake list; `set_credentials` gives `connecting` then `connected`.
  - A wrong password gives `failed` and then a retry works in the same session.
  - A bad envelope or wrong `seq` ends the session.
  - A command before the handshake ends the session.
  - 300 s idle ends the session, with a fake clock.
- **Verify:** `pytest tests/test_session.py -v`
- **Decided:** Q4 (every `hello` counts until its `confirm` succeeds; retyping is a new `hello`), Q5 (`rate_limited` + `retry_after_s`), Q6 (fails before decryption → end the session; after → `bad_request`), Q22 (`hello` only while the window is open).
- **Implements:** SPEC §5.1, §5.4, §6.2, §6.3. DESIGN §3, §5 (`DeviceSession`), §6.

### Step 2.7: Buttons and status line (`buttons.py`)

- **Goal:** the device's only input, single keys acting as physical buttons, plus its status "LED", all testable without a terminal (plan_v5, D2).
- **Files:** `src/teton_device/buttons.py`, `tests/test_buttons.py`.
- **Content (DESIGN §4.1):**
  - `Button` (`PAIR`, `RESET`, `STATUS`, `POWER`) and a key mapping: `p`, `r`, `s`, `q`, case-insensitive. Every other key is ignored, so no text is ever collected.
  - `TerminalButtons`: `tty.setcbreak` + `loop.add_reader` on stdin, original `termios` settings restored in a `finally`. Cbreak, not raw, so Ctrl+C stays a normal SIGINT. `QueueButtons`: the in-memory `ButtonSource` for tests.
  - `PairingWindow(clock, duration_s=300)` and `ResetDetector(clock, within_s=3)`: pure, with an injected clock.
  - `ButtonController(buttons, actions, clock)`: maps buttons to `DeviceActions` (DESIGN §5), with a 1 s tick for the countdown and window expiry.
  - `Led` and `StatusLine(stream)`: the LED priority from DESIGN §4.1; one redrawn line on a terminal, plain lines on a pipe.
- **Tests (injected button events, fake clock, fake `DeviceActions`):**
  - [P] opens the window and turns advertising on; after 300 s the tick turns it off; [P] at 200 s restarts the window, so it is still open at 400 s.
  - [R] once does nothing but show "Press reset again to confirm"; [R] twice within 3 s calls `reset()`; two presses 4 s apart don't.
  - [P] on a provisioned device shows "Already connected. Hold reset to set up again." and doesn't turn advertising on.
  - [S] prints the status text with the remaining time; [Q] calls `power_off()`.
  - Other keys are ignored; the key mapping is case-insensitive.
  - LED priority for each combination; `StatusLine` writes escape codes only to a terminal.
  - `TerminalButtons` restores the terminal settings, tested on a pseudo-terminal (`os.openpty`), so no real keyboard is needed.
- **Verify:** `pytest tests/test_buttons.py -v`. You try the real keys in Step 5.2.
- **Decided:** Q21 (reset in every state except `CONNECTING`), Q23 (no terminal: warning, no buttons, no `SIGUSR1`; [S] prints the code + QR again).
- **Implements:** SPEC §9.1 (buttons, LED). DESIGN §4.1, §5 (`ButtonSource`, `DeviceActions`).

### Step 2.8: Device service (orchestrator)

- **Goal:** tie the pieces together: the state machine, persistence, the buttons and the pairing window.
- **Files:** `src/teton_device/service.py`, `tests/test_service.py`.
- **Content:**
  - `DeviceService(transport, wifi, storage, buttons, clock, sleep, status_line)` implements `DeviceActions` (`sleep` drives the 1 s tick in tests; `status_line` is where the code, QR and LED are drawn):
    - `run()` loads state, shows the code + QR, sets `info`, starts the transport **without advertising**, and runs the `ButtonController` and the session together until [Q] or Ctrl+C.
    - `set_advertising(on)` follows the pairing window. When the window closes during a session, the session may finish; when it ends, the state goes to `UNPROVISIONED` (DESIGN §4).
    - `reset()` deletes the `teton-*` profiles through `WifiBackend` (never `nmcli` directly), calls `Storage.reset()`, prints the new code + QR and goes to `UNPROVISIONED` with advertising off.
    - A connection attempt that has started always finishes, even if the configurator has gone (DESIGN §4).
    - On success it persists `provisioned`, turns advertising off before the configurator disconnects, and stays on in `PROVISIONED` (D2, replacing the exit of S4).
    - Started on an already provisioned device it stays on, green LED, without advertising.
  - No connection tracking and no `busy`: the radio allows one configurator at a time (S1, S3).
  - Transport errors never crash the service. Power off closes the transport and restores the terminal.
- **Tests (memory transport, `QueueButtons`, fake clock):** nothing is advertised before [P]; the happy path after [P] ends `PROVISIONED` with advertising off and the service still running; a provisioned state doesn't advertise, even after [P]; a session survives window expiry and the device returns to `UNPROVISIONED` when it ends; a second `hello` mid-session starts a new session; a failed attempt rolls back and the session stays usable; [R][R] calls `forget_all`, rotates the code and leaves advertising off; [Q] closes the transport.
- **Verify:** `pytest tests/test_service.py -v`
- **Decided:** Q21, Q22.
- **Implements:** SPEC §4, §5.4, §8.5, §8.6, §9.1. DESIGN §4, §6.

---

## Phase 3: Configurator (`teton_configurator`)

### Step 3.1: Plain-language text

- **Goal:** every message the technician can see, in one file: one sentence plus a next action.
- **Files:** `src/teton_configurator/ui_text.py`, `tests/test_ui_text.py`.
- **Content:** the prompts SPEC §9.2 fixes, the progress lines, `failure_text(reason)` with the SPEC §7 texts, `error_text(code)` (`unsupported_security`, `bad_request`, rate-limited with the seconds to wait), `warning_text(warning)`, the connected line, the five configurator-side failures (wrong code / wrong device, no reply, no result within 70 s, connection lost, unexpected reply; Q27), the input-check texts (Q28), "press the pairing button on the device", "No device found. Press the pairing button on the device and try again.", and the network list markers (Q30). Prompts for options decided in Step 3.3 are added there (Q25).
- **Tests:** every `FailureReason`, `ErrorCode` and `StatusWarning` has a text (so a newly added enum value fails the test until it gets one); no text contains jargon (nmcli, GATT, BLE, exception, traceback, D-Bus), matched on word boundaries; every failure text has a next action after its first sentence; the signal bars and markers.
- **Verify:** `pytest tests/test_ui_text.py -v`
- **Decided:** Q25 (scope of 3.1), Q26 (`auth_failed` next action), Q27 (configurator-side failures), Q28 (local input checks), Q29 (`StatusWarning` covered too), Q30 (bars and words, no lock emoji).
- **Implements:** SPEC §7, §9.2. CLAUDE.md "plain language".

### Step 3.2: Protocol client

- **Goal:** the configurator's half of the protocol, independent of UI and radio.
- **Files:** `src/teton_configurator/client.py`, `tests/test_client.py`.
- **Content:**
  - `ClientChannel` (Q37): framing, one inbox for `session` and `status`, the cipher. `tests/fake_configurator.py` is rebuilt on it, and the device rig moves there from `test_service.py`.
  - `ProvisioningClient(transport, reply_wait_s, scan_wait_s, result_wait_s)` has:
    - `connect()`, `read_info()` and `close()`;
    - `handshake(code) -> Info`, which also checks that `hello_reply.device_id` matches `info.device_id` (else `ProtocolError`, Q34), and may be called again after a wrong code (Q4);
    - `scan() -> list[Network]`;
    - `provision(creds) -> AsyncIterator[Status]`: `connecting`, then `connected` or `failed`.
  - Waits: 10 s per handshake reply (Q31), 40 s for a scan, 10 s for `connecting`, 70 s for the result (Q32, Q10).
  - Typed errors: `WrongCodeError`, `RateLimitedError`, `NoReplyError`, `DeviceRefusedError`, `ProtocolError` and `ConnectionLostError` (Q33, Q35, Q36).
- **Tests (against the real `DeviceService` over the memory transport, with the fake Wi-Fi, `QueueButtons` and fake time):** a full success; a wrong code raises `WrongCodeError` and no `set_credentials` is ever sent (checked on the wire), then the right code works on the same connection; a retry after `failed`; the rate limit (`RateLimitedError` with the seconds); a `hello` after the window closed (`NoReplyError`, Q22); a `device_id` mismatch; no result in time; `unsupported_security`; a lost connection; no error text contains a secret.
- **Decided:** Q31–Q37.
- **Verify:** `pytest tests/test_client.py -v`
- **Implements:** SPEC §5.1 (configurator side), §6. DESIGN §5 (`ProvisioningClient`).

### Step 3.3: Guided flow

- **Goal:** the six-step technician experience of SPEC §9.2, testable without a terminal.
- **Files:** `src/teton_configurator/flow.py`, `tests/test_flow.py`.
- **Content:**
  - `run_flow(discover, connect, io, make_client)`, where `discover() -> list[FoundDevice]`, `connect(found) -> ClientTransport` (not yet connected) and `io` has `ask`, `ask_secret` and `show`, so tests can script the answers (Q38). Returns 0 when connected, else 1.
  - The steps:
    1. Ask the technician to press the pairing button on the device, then pick a device from a numbered list; Enter picks the only one; none found → look again (Q39).
    2. Type the code. A bad format, a wrong code or a rate limit is explained, then the code is asked again (Q40).
    3. Pick a network, "Other (hidden) network" or "Scan again" (Q41, Q42).
    4. Enter the credentials for that security type (for Enterprise: an optional CA file, checked against `MAX_CA_PEM_BYTES` and parsed as PEM, and an optional domain; Q43).
    5. Watch the progress: one line (Q47).
    6. See the result, and on failure retry with the same network or pick another (Q44).
  - Signal bars and "password" / "enterprise" next to each network (Q30).
  - The password is read with `getpass`. q quits at menus; end of input quits; the transport is always closed (Q46).
- **Tests (scripted io + the real `DeviceService` over the memory transport + fake Wi-Fi):** the happy path output contains "Connected" and the IP; a wrong code, then retype, then success; a wrong password, then retry, then success; a hidden network; the server-not-verified warning for PEAP without a CA, and none with a CA file; the limited-internet warning; no device found, then found; rescan; bad CA files; no reply (Q22); no result in time; quit and end of input; the largest allowed request fits one message.
- **Decided:** Q38–Q47.
- **Verify:** `pytest tests/test_flow.py -v`
- **Implements:** SPEC §9.2, §7.

---

## Phase 4: End-to-end test (in memory)

### Step 4.1: Full-flow tests + "no secrets in logs"

- **Goal:** the whole system in one process, plus the test that enforces the secret-hygiene rule.
- **Files:** `tests/test_end_to_end.py`, `tests/test_no_secrets_in_logs.py`, `tests/conftest.py` (shared fixtures: `device`, `paired_device`, `ends`, `state_dir`, `log_stream`; the classes stay in `tests/fake_configurator.py`, Q48, Q49).
- **Tests:**
  - **End to end (4.1a):** the real `DeviceService` and the real `run_flow` with scripted answers. Each test checks the `ui_text` the technician saw and the device's state and LED. PSK success (advertising off, the service still running); wrong password, then a retry in the same session; wrong code, then retype, with no command on the wire before the good code; captive portal (back to `SESSION_ESTABLISHED`, red LED); limited internet; PEAP with a CA (no warning) and without CA or domain (`server_not_verified`); a really hidden network (Q53); a second `hello` from a new configurator restarting the session (Q52); the rate limit, then success after the fake clock moves on; the window expiring before `hello` (no reply), then [P] again; after success no device is found (Q51), then [R][R] + [P] and the new code works. Each run starts with an injected [P].
  - **No secrets in logs (4.1b, lean, Q56–Q62):** one run through the real `configure_logging(verbose=True)` (not `capture_logs`, which skips the redaction): a wrong code, the code typed lowercase with dashes, a wrong password, then PEAP with a CA file, which connects; plus `NmcliBackend` attempts through the real `run()` with an echoing stand-in (Q61). None of the PSKs, the EAP password, the setup code (raw, dashed, lowercase), the X25519 private keys and session keys (hex and base64, caught by spies in `crypto`, Q58), the base64 ciphertexts or the CA PEM lines (Q59) appears in the logs, stdout, stderr or the configurator's output; the device's screen may show only the code (Q60). The SSIDs, `identity`, `device_id` and the failure reason stay visible (Q62). The device logs every error it sends with its `code` (Q56, a session test).
- **Verify:** `pytest tests/test_end_to_end.py tests/test_no_secrets_in_logs.py -v`
- **Implements:** SPEC §12 MUST #6 (unit tests list complete). DESIGN §7 (end to end, `log`).

**At this point:** the protocol, crypto and device logic are complete and tested without any hardware. This is a good milestone for a PR.

---

## Phase 5: Simulation mode + CLIs

### Step 5.1: Unix-socket transports

- **Goal:** the same protocol between two processes, with only the radio simulated.
- **Files:** `src/teton_common/sim_wire.py` (Q66), `src/teton_device/sim_server.py`, `src/teton_configurator/sim_client.py`, `src/teton_common/transport.py` (`check_chunk`, Q71), `tests/test_sim_transport.py`, `tests/test_transport_contract.py` (renamed from `test_memory_transport.py`, Q71).
- **Content:**
  - The socket is a byte stream, so each chunk is wrapped as `[u8 channel][u16 length][chunk]`. This is a small multiplexing layer under our normal framing.
  - `info` is a request (an empty record) and a response (at most `MAX_INFO_BYTES`) (Q67).
  - Malformed records (unknown channel, too long, short read, wrong direction, non-empty info request) drop the connection with one log line that names the channel byte and length, never the bytes (Q67).
  - The socket lives at `$XDG_RUNTIME_DIR/teton-sim.sock`, falling back to `<tempdir>/teton-sim-<uid>.sock` when the variable is unset (CI), mode `0600` (Q69).
  - A leftover socket nobody listens on is removed at start; a live one fails the start; a path that isn't a socket is left alone (Q68). The file is removed on `close()`.
  - Chunks are 20 bytes, exactly as on BLE (S2).
  - One configurator at a time, like BLE: a connection that arrives while the window is closed or a client is connected is closed at once; `connect()` makes one `info` round trip, so it raises `TransportError` there (Q64). `set_advertising(False)` stops accepting new clients but keeps the connected one, like BLE (D2).
  - The client's `close()` waits for the server's EOF, so the next connection is never refused by mistake; a device `send()` to a vanished client is dropped (Q70).
  - `discover()` returns `FoundDevice(name=info.name, signal=100, address=<socket path>)` while the device accepts connections, else nothing (Q72).
- **Tests:** the server and client in one event loop on a socket in `tmp_path`; a full handshake + scan through the real `ProvisioningClient` and `DeviceService`; one configurator at a time (the second is refused and can't discover the device until the first leaves); no connect while advertising is off; malformed records on both sides; a stale, a live and a non-socket file at the path; the socket mode; a device that powers off mid-session gives `ConnectionLostError`. The contract checks of the memory transport run against the sim pair too (Q71).
- **Verify:** `pytest tests/test_sim_transport.py tests/test_transport_contract.py -v`
- **Implements:** SPEC §10 (transport).

### Step 5.2: CLIs

- **Goal:** the real commands of SPEC §9.
- **Files:** `src/teton_device/cli.py`, `src/teton_configurator/cli.py`, `tests/test_cli.py`.
- **Content:**
  - `teton-device run [--simulate] [--state-dir PATH] [--verbose]`. There are no other subcommands: reset is the [R] button of Step 2.7, and the code + QR are printed on start and after every reset (D2).
  - `teton-configurator [--simulate] [--verbose]`.
  - `run` without `--simulate` imports BLE lazily. Until Phase 6 it prints a clear "BLE transport not available yet" and exits 1.
  - `run` uses `TerminalButtons` in both modes. [Q], Ctrl+C and SIGTERM exit cleanly (0) and restore the terminal (Q76).
  - `--simulate`: the sim transport, `FakeWifiBackend` (scan 2 s, connect 3 s, Q78) and the state dir `~/.local/state/teton-device-sim` unless `--state-dir` is given (Q77).
  - The screen is stdout; logs go to stderr, through the status line on a terminal (Q73). The configurator logs at `WARNING` unless `--verbose` (Q74).
  - The configurator supplies `TerminalIO` (daemon threads for `input`/`getpass`); Ctrl+C restores the terminal, closes the transport and exits 130 (Q75).
  - Only wiring: `parse_args`, `run_device(args, buttons=None)`, `run_configurator(args, io=None)`, `main()` (Q79).
- **Tests:** argument parsing; `reset` and `show-code` are rejected as unknown commands; `--state-dir` is respected; `run --simulate` with `QueueButtons` ([P], then [Q]) starts and stops cleanly; no `bless`/`bleak` import in sim mode (a fresh subprocess); a two-process smoke test: the device subprocess on a pseudo-terminal, the configurator's `run_flow` with scripted answers over the socket (Q80); SIGTERM stops the device process cleanly (Q76); Ctrl+C at the configurator's password prompt exits 130 with echo restored and the device freed (Q75). `test_buttons.py` and `test_log.py` cover the log stream of `StatusLine` and `default_level`.
- **Verify (two terminals, any machine):**
  ```bash
  # terminal 1
  teton-device run --simulate --state-dir /tmp/teton-sim
  # press P: the status line turns blinking blue with a 5:00 countdown
  # terminal 2
  teton-configurator --simulate
  # use the code printed in terminal 1; pick Home-PSK; password correct-horse-battery → "Connected"
  # try a wrong password first → plain-language failure, then retry works
  # terminal 1: status line solid green; P → "Already connected…"; S → state, SSID, IP; R R → new code + QR; Q → exits
  ```
- **Implements:** SPEC §9.1, §9.2, §10. SPEC §12 SHOULD #8 (first half).

---

## Phase 6: Real BLE + real Wi-Fi (MUST)

### Step 6.1: BLE device transport (`gatt_server.py`)

- **Goal:** the device transport over BLE, implementing the same interface as the memory and socket transports.
- **Files:** `src/teton_device/gatt_server.py`, `tests/test_gatt_server.py`; `src/teton_common/protocol.py` and `messages.py` (`local_name`, Q81); `src/teton_common/transport.py`, `memory_transport.py`, `src/teton_device/sim_server.py` (`check_info`, Q86); `src/teton_device/cli.py`, `tests/test_cli.py` (wiring, Q88); README troubleshooting (Q90).
- **Content:**
  - A `bless` server with the 4 characteristics from SPEC §4, no encryption flags (no pairing), the local name `Teton-XXXX` and the service UUID in the advertisement. The name comes from the CLI: `GattServerTransport(name)`, built with `protocol.local_name(device_id)` after loading `Storage` (Q81).
  - Implements `DeviceTransport` with `bless` used as is (S2), no patch:
    - `start` does what `BlessServer.start()` does minus the advertising (`setup_task`, `bus.export`, `app.register`), and is where `bless` is imported and the server created; no adapter, no `bluetoothd` or a refused bus become a one-line `TransportError` (Q82, Q84).
    - `set_advertising` registers or unregisters the advertisement (`app.start_advertising` / `stop_advertising`, as the spike did) and does nothing when already in that state (Q82).
    - `set_info` keeps the `info` value, at most 180 bytes (`transport.check_info`, shared with the memory and sim transports, Q86); reads return it.
    - Writes: `bless`'s synchronous handler queues `(channel, chunk)`; one task awaits the receivers in arrival order (Q83).
    - `send` checks the chunk and sets the characteristic value, which BlueZ notifies to a subscribed client; no pacing (Q89).
    - `close` goes step by step: stop advertising only if on, unregister, unexport, disconnect the bus; each failure is logged, it never raises, and a second call does nothing (Q85).
  - `bless` is imported lazily.
  - `teton-device run` without `--simulate` uses `GattServerTransport(name)` and `NmcliBackend(state_dir=…)`; the device's "not available yet" branch goes (Q88).
- **Tests (no radio):** importing `teton_device` and running the sim and memory paths doesn't import `bless`; the pure helpers (characteristic table, `info` size limit, `local_name`); with a fake server injected through `make_server`, standing in for `_BlessGattServer`, the thin wrapper that is the only code touching `bless` (Q87): `start` doesn't advertise, advertising toggles and repeats, `close` with advertising off and twice, writes reach the receivers in order, `send` sets the value, a refused direction or a 21-byte chunk is rejected, startup errors become `TransportError`; the CLI builds the BLE transport with the device's name and the Wi-Fi backend with the state directory, without importing `bless`.
- **Verify (🔧 laptop A + phone):** `teton-device run`, then in nRF Connect:
  - nothing is advertised until you press [P];
  - after [P] the service is visible, `info` reads as JSON, and the other characteristics are present;
  - connect, then wait for the 5-minute window to expire: the connection keeps working; disconnect: advertising does **not** come back (check with `btmon` for `LE Set Extended Advertising Enable`).
- **Decided:** Q81–Q90.
- **Implements:** SPEC §4. DESIGN §2 (`gatt_server.py`).

### Step 6.2: BLE configurator transport (`ble_client.py`)

- **Goal:** discovery and the GATT client with `bleak`.
- **Before:** Step 0.3 on laptop 2 (review Q101).
- **Files:** `src/teton_configurator/ble_client.py`, `tests/test_ble_client.py`; `src/teton_common/framing.py`, `tests/test_framing.py` (Q94); `src/teton_configurator/cli.py`, `ui_text.py`, `tests/test_cli.py` (Q99).
- **Content:**
  - `BleDiscovery.discover()`: scan for 10 s filtered by the service UUID (`BleakScanner.discover(..., return_adv=True)`), strongest first, named by the advertised local name; signal = `clamp(2 × (rssi + 100), 0, 100)` (Q91, Q92). It keeps the scanned `BLEDevice`s, so `connect(found)` doesn't scan again (Q93). No adapter or Bluetooth off → `TransportError`, shown by the CLI as `ui_text.BLUETOOTH_UNAVAILABLE`, exit 1 (Q99).
  - `BleClientTransport.connect()`: connect (10 s), subscribe to `session` and `status` **before** writing `hello`, check the Teton service is there; `read_info()` reads `info`. No MTU handling (S2), so only public `bleak` API. Every `bleak`, D-Bus or timeout error becomes `TransportError` (Q95).
  - Notifications only do `Inbox.put()`, delivered in order (Q96); `disconnected_callback` only marks the transport not connected (Q97); `close()` is idempotent and never raises.
  - Writes use write-with-response, so chunks arrive in order (Q98).
  - `bleak` is imported lazily.
  - The reassembly timeout counts from the last chunk, not the first (Q94).
  - `teton-configurator` without `--simulate` uses `BleDiscovery` (Q99).
- **Tests (no radio):** the pure helpers (RSSI to signal, device list from scan results); building the transport doesn't import `bleak` (fresh interpreter); with a fake `bleak`-shaped client injected through `make_client` (Q100): subscribing comes before anything else, notifications in order, writes with `response=True`, a missing service, send after the disconnect callback, idempotent close, errors as `TransportError`; a slow message that keeps progressing is not dropped (Q94); the CLI wiring and the Bluetooth-off sentence.
- **Verify (🔧 both laptops):** laptop A `teton-device run`, then press [P]; laptop B `teton-configurator`. The device appears; the code is accepted; the scan list shows the real networks around laptop A. Stop before entering credentials. That comes in 6.3.
- **Decided:** Q91–Q101.
- **Implements:** SPEC §4, §9.2 (steps 1–4). DESIGN §2 (`ble_client.py`).

### Step 6.3: 🔧 HARDWARE: real Wi-Fi run and evidence

- **Goal:** meet SPEC §12 MUST #1–#5 on real hardware and record the evidence.
- **Files:** `docs/evidence/` (logs, screenshots, `.btsnoop` capture, `README.md` index), and `tests/fixtures/nmcli/` (real recorded outputs, anonymised).
- **You do:**
  1. Phone: turn on the hotspot (WPA2-Personal; also try WPA3 if the phone offers it). Pick a password you don't use anywhere else, because it will appear in evidence notes.
  2. Laptop A: `sudo btmon -w docs/evidence/run-psk.btsnoop` in one terminal, and `teton-device run --verbose 2> docs/evidence/device-run.log` in another (logs only: the screen, with the setup code, stays out of the file, review Q73; follow it with `tail -f` in a third terminal), then press [P].
  3. Laptop B: `teton-configurator 2>&1 | tee docs/evidence/configurator-run.log`.
  4. Run the scenarios:
     - **wrong setup code:** a clear message, and nothing else is sent;
     - **wrong hotspot password:** `wrong_password`, then a retry in the same session;
     - **correct password:** "Connected, IP …, internet OK".
  5. Check the capture: `btmon -r docs/evidence/run-psk.btsnoop | grep -c "<hotspot password>"` gives `0`. Also grep for the SSID; the SSID travels encrypted too, so it should also be 0. Then, before committing anything, grep both `.log` files for the hotspot password and the setup code, raw and with dashes (`grep -ci`): all must give `0` (review Q63).
  6. Take screenshots of both terminals.
  7. Record `nmcli` outputs for the fixtures: the wrong-password and unknown-SSID errors, and the scan output.
  8. On laptop A afterwards: press [R] twice to delete the Teton profile and rejoin your normal Wi-Fi, then [Q].
- **Notes about laptop A:**
  - Laptop A has many saved Wi-Fi profiles with autoconnect on. After a failed attempt, NetworkManager reconnects one of them, which is the rollback SPEC §8.6 expects.
  - After success, a home network in range with a higher autoconnect priority could take over later (Q17). For a clean demo, run it away from your home network, or temporarily `nmcli connection modify <home> connection.autoconnect no`. That choice is yours to make.
- **Implements:** SPEC §12 MUST #1–#5, §8. DESIGN §7 (manual row).

**At this point all MUST items except the docs are done.**

---

## Phase 7: SHOULD items

### Step 7.1: CI (GitHub Actions)

- **Goal:** ruff + pytest on every push and PR.
- **Files:** `.github/workflows/ci.yml` (a new directory, required by SPEC §12 #8).
- **Content:** `ubuntu-24.04`, a Python matrix of **3.12 and 3.14** through `actions/setup-python` (D1), `pip install -e ".[dev]"`, `ruff check .`, `ruff format --check .`, `pytest`. 3.12 is the device's version and catches any use of newer features; 3.14 is laptop 2's. No Bluetooth needed, because of the lazy imports.
- **Verify:** after you push a branch, the Actions tab shows a green run.
- **Depends on:** Q16 (whether to move it earlier).
- **Implements:** SPEC §12 SHOULD #8 (second half). DESIGN §7 (CI).

### Step 7.2: 🔧 HARDWARE: WPA2-Enterprise (PEAP) test with `hostapd`

- **Goal:** prove the PEAP path against a real 802.1X authenticator.
- **Files:**
  - `scripts/hostapd-enterprise/hostapd.conf`: 2.4 GHz channel 6, because Intel cards usually can't start an AP on 5 GHz; `wpa=2`, `wpa_key_mgmt=WPA-EAP`, `ieee8021x=1`, `eap_server=1`.
  - `scripts/hostapd-enterprise/eap_user`: PEAP outer, MSCHAPv2 inner, user `nurse01`.
  - `scripts/hostapd-enterprise/make-certs.sh`: a self-signed CA + server cert with `openssl`.
  - `scripts/hostapd-enterprise/dnsmasq.conf`: DHCP. `hostapd` doesn't hand out addresses; `dnsmasq` comes from the `dnsmasq-base` package, which NetworkManager already depends on.
  - `scripts/hostapd-enterprise/README.md` and `docs/evidence/enterprise-*`.
- **You do (on the laptop acting as the AP):**
  1. Run `make-certs.sh`.
  2. `nmcli device set <wifi-if> managed no`.
  3. `sudo ip addr add 10.42.0.1/24 dev <wifi-if>`.
  4. `sudo hostapd hostapd.conf` and `sudo dnsmasq -C dnsmasq.conf -d`.
  5. Afterwards: `nmcli device set <wifi-if> managed yes`.
- **Scenarios:**
  - PEAP with the CA + domain gives connected with no warning.
  - PEAP without either gives connected + `server_not_verified`.
  - A wrong password gives `auth_failed`.
- **Problem:** the AP has no uplink, so internet checks give `limited`/`none`. What counts as success here is Q8.
- **Role conflict:** laptop B can't be the AP and use its Wi-Fi for anything else at the same time. Its Bluetooth still works, so it can stay the configurator.
- **Implements:** SPEC §12 SHOULD #9, §8.2 (PEAP).

### Step 7.3: systemd unit, polkit rule, preflight script

- **Goal:** show how the device would run in production, plus a one-command environment check.
- **Files:** `deploy/teton-device.service`, `deploy/50-teton-provisioning.rules`, `scripts/preflight.sh`, a README section.
- **Content:**
  - **Unit:** a hardened unit (`ProtectSystem=strict`, `NoNewPrivileges=yes`, `StateDirectory=`, a minimal capability set) of `Type=notify`. The `sd_notify` message is sent with the standard library (a datagram on `$NOTIFY_SOCKET`), so no new dependency is needed.
  - **Polkit rule:** lets a `teton` service user manage NetworkManager connections and scan.
  - **Preflight script:** the read-only checks from the README "Check your machine" section, each with a clear OK or FIX line. It accepts Ubuntu 26.04 for the configurator role.
  - **Buttons under systemd:** a service has no terminal, so no keys (Q23).
- **Tests:** `bash -n scripts/preflight.sh`; `systemd-analyze verify deploy/teton-device.service`; a pytest for the `sd_notify` helper, against a temporary socket.
- **Verify:** `scripts/preflight.sh` on both laptops prints all OK. Optionally (🔧, needs `sudo`, done by you): install the unit and run `systemctl start teton-device`.
- **Implements:** SPEC §12 SHOULD #10. plan_v5 §11.1.

---

## Phase 8: Docs and writeup (MUST)

### Step 8.1: README

- **Goal:** someone at Teton can reproduce everything from a stock Ubuntu install (24.04 for the device, 24.04 or 26.04 for the configurator).
- **Files:** `README.md`. The Requirements section already exists.
- **Content:** both Ubuntu versions and the role of each laptop (the Requirements section already has them, D1); the device buttons and LED; the quickstart in simulation (2 terminals); the real demo per laptop; the Enterprise test; a troubleshooting table (adapter off or `rfkill`, the BlueZ GATT cache on the client, `bluetoothctl remove <addr>`, NetworkManager permissions, the `sudo` fallback for BlueZ, `btmon` needs root, a hotspot with no internet); the second laptop's column in "Tested on" (Ubuntu 26.04, Python 3.14, from Step 0.3).
- **Verify:** you follow the README on the second laptop from scratch.
- **Implements:** SPEC §12 MUST #7.

### Step 8.2: ARCHITECTURE.md (the writeup)

- **Goal:** the submission writeup the challenge asks for.
- **Files:** `docs/ARCHITECTURE.md`, `docs/evidence/README.md` (an index of the evidence).
- **Content:**
  - The channel choice and rejected alternatives (from `research/channels.md` and plan_v5 §3).
  - The trust bootstrap and its alternatives (plan_v5 §3b), and the pairing window: the device is only reachable over BLE for a few minutes after someone physically presses a button, which shrinks the attack surface and is standard practice in real products (plan_v5, D2).
  - How credentials are protected in transit: the handshake, why 80 bits, a threat table, and what's out of scope.
  - The data flow and state machine.
  - Failure handling.
  - Known limitations, stated honestly.
  - 200 devices in a hospital wing (plan_v5 §8).
  - Fleet and OTA notes (plan_v5 §11).
- **Verify:** you read it and can defend each paragraph.
- **Implements:** SPEC §12 MUST #7. plan_v5 §8, §11, §12.

### Step 8.3: Acceptance checklist

- **Goal:** map every SPEC §12 criterion to a test or an evidence file.
- **Files:** a short table at the end of `docs/evidence/README.md`.
- **Verify:** every MUST row points at something that exists.

---

## Phase 9: COULD (only if you ask)

| # | Item | Notes |
|---|---|---|
| 9.1 | Phone page `web/index.html` (Web Bluetooth + WebCrypto) | Reuses the known-answer vector from Step 1.5. 🔧 needs Chrome on Android. WebCrypto supports X25519 in recent Chrome; this needs checking first |
| 9.2 | EAP-TLS | A new security type, per-device client certificates, and the `hostapd` config extended |
| 9.3 | `.deb`, Prometheus metrics, Smith enrollment stub | Each would need a new dependency or tool; ask first |

---

## Hardware summary

| Step | Laptop A = laptop 1 (device) | Laptop B = laptop 2 (configurator) | Phone | Blocked by an environment problem? |
|---|---|---|---|---|
| 0.3 Install check on Python 3.14 | no | yes | no | possibly: wheels for 3.14 |
| 2.5 Scan smoke test + stdin check | yes (any laptop) | no | no | no |
| 0.2 BLE spike | yes | yes, **or** the phone with nRF Connect | optional | no; adapter role "peripheral" confirmed on laptop 1 |
| 6.1 / 6.2 BLE transports | yes | yes | optional (nRF Connect) | no |
| 6.3 Real Wi-Fi + evidence | yes, `sudo btmon` | yes | hotspot | no; `btmon` needs `sudo` (expected) |
| 7.2 Enterprise | yes | yes, as AP + configurator | no | no; AP mode, `iw` and `hostapd` confirmed on laptop 1 |
| 7.3 systemd (optional) | yes, `sudo` to install | no | no | no |
| 8.1 README walk-through | either | either | no | no |

**Before Step 0.2:** run the README "Check your machine" commands on the **second laptop**, and send me the output, so I can add its column to "Tested on". Nothing has been checked there yet.

---

## Decided review questions

Answered on 2026-10-11 and recorded in [plan_v5.md](plan_v5.md), SPEC 1.2 and DESIGN 1.2.

| # | Question | Decision |
|---|---|---|
| Q1 | AAD contents | SPEC §5.3 is right: label + direction + `seq`. plan_v4 §4 corrected in plan_v5 |
| Q2 | Transport interface incomplete | Split into `DeviceTransport` and `ClientTransport`; framing stays above; `info` unframed (DESIGN §5, SPEC §4.1) |
| Q3 | BLE spike timing | Right after project tooling: now Step 0.2 |
| Q7 | Redaction key `code` hides error codes | `code` removed from the list; setup code only under `setup_code`; test that error codes stay visible (SPEC §5.5) |
| Q8 | Connectivity rules | Option A: success = activated + IP + gateway reachable on the Wi-Fi interface; not `full` → warning `limited_internet`; `portal` → `captive_portal`; gateway down → `no_internet`; per-interface checks (SPEC §6.3, §7, §8) |
| Q9 | Secrets in argv | Never. Profile without secrets, then `nmcli connection edit` through stdin; one runner; argv test (SPEC §5.5, §8). Confirmed on hardware in Step 2.5 |
| Q15 | `busy` over BLE | Not needed: the radio allows one configurator at a time; `busy` removed (S1) |
| Q19 | Stopping advertising | `DeviceTransport.close()` after success; the device exits (S4) |
| Q20 | MTU and long reads with `bless` | Option C: fixed 20-byte chunks, `info` under 180 bytes, `bless` as is (S2) |
| Q18 | Logging the Enterprise username | Keep `identity` and `anonymous_identity` visible, for debugging Enterprise logins (2026-10-13). Not in the redaction list |
| Q12 | `reset` while `run` is active | Gone (2026-10-13): there is no `reset` command, reset is the [R] button inside `run` (D2) |
| Q13 | State names | Five states: `UNPROVISIONED`, `ADVERTISING`, `SESSION_ESTABLISHED`, `CONNECTING`, `PROVISIONED` (2026-10-14). No `FAILED` (a failure returns to `SESSION_ESTABLISHED`, the LED shows the last reason); no `CONNECTED` (`CONNECTING` goes straight to `PROVISIONED`); a handshake in progress stays `ADVERTISING`; `info.state` is the lowercase name (SPEC §6.1, DESIGN §4) |
| Q14 | Dashes in the QR | None (2026-10-14): `code=` is the normalised 16 characters, the same value as stored and used as the HKDF salt; the printed code keeps the dashes (SPEC §3) |
| Q4 | Counting failed handshakes; retyping the code | Every accepted `hello` counts as a failure; a valid `confirm` resets the counter. Retyping is a new `hello` on the same connection (2026-10-14, SPEC §5.4) |
| Q5 | Rate-limited reply | `error` `rate_limited` with `retry_after_s`, plaintext on `session`; a current session continues (2026-10-14, SPEC §5.4, §6.3) |
| Q6 | `bad_request` vs ending the session | Fails before decryption → end the session; a decrypted but invalid command → `bad_request`, the session stays; not in `caps` → `unsupported_security`. One message at a time, so nothing is seen in `CONNECTING` (2026-10-14, SPEC §6.3, DESIGN §6) |
| Q22 | `hello` after the window expired | Accepted only while the window is open; a pending handshake is dropped if the window closes before `confirm`. No reply when refused (2026-10-14, SPEC §5.4) |
| Q10 | Timeout budget | 45 s activation (`--wait 45`) + 15 s verification; the configurator waits 70 s (2026-10-14, SPEC §7, §8.3; `protocol.py`) |
| Q11 | Security types | `open` out of scope (`unsupported_security`); `WPA2 WPA3` → `wpa2-psk`, `WPA3` → `wpa3-sae`, `WPA2 802.1X` → `wpa2-eap-peap`; unjoinable networks left out of the scan list (2026-10-14, SPEC §6.2, §6.3, §8.1) |
| Q17 | Autoconnect priority | `connection.autoconnect-priority 10`; autoconnect off until success (2026-10-14, SPEC §8.2) |
| Q21 | [R][R] outside `PROVISIONED` | Every state except `CONNECTING` ("Busy connecting. Try again in a moment."); closes the window, drops any session, rotates the code; no new transitions (2026-10-18, SPEC §9.1, DESIGN §4) |
| Q23 | No terminal; the QR scrolling away | No terminal → a warning, no buttons, the device stays on; no `SIGUSR1`. [S] prints the code + QR again (2026-10-18, SPEC §9.1, DESIGN §4.1) |
| Q24 | Deleting the Teton profiles on reset | `WifiBackend.forget_all()` deletes every `teton-*` profile, so it works after a restart (2026-10-14, raised in Step 2.3; DESIGN §5, SPEC §9.1) |
| Q25 | What `ui_text.py` holds in Step 3.1 | Every failure, warning and result text plus the prompts SPEC §9.2 fixes; prompts for Step 3.3 options are added in 3.3 (2026-10-18) |
| Q26 | `auth_failed` had no next action | "…rejected by the network. Check them and try again." (2026-10-18, SPEC §7) |
| Q27 | Failures the configurator finds itself | Texts for wrong code / wrong device, no reply to `hello`, no result within 70 s (not the `timeout` text: points at the device's light), connection lost, unexpected reply (2026-10-18, SPEC §9.2) |
| Q28 | Checking input before sending | Local checks of code format, password length, network name length, non-empty username/password; no exception text shown; a mistyped code sends no `hello` (2026-10-18, SPEC §9.2) |
| Q29 | Scope of the text test | Also every `StatusWarning`; jargon list unchanged, matched on word boundaries (2026-10-18) |
| Q30 | Network list markers | Bars `▂▄▆█` (≥75 / ≥50 / ≥25 / below) and "password" / "enterprise", no lock emoji (2026-10-18, SPEC §9.2) |
| Q31 | Handshake reply wait (Q22) | 10 s per handshake reply, `CONFIGURATOR_REPLY_WAIT_S` (2026-10-18, SPEC §9.2) |
| Q32 | Other configurator waits | Scan 40 s; `connecting` 10 s, then the result 70 s; all injectable (2026-10-18, SPEC §9.2) |
| Q33 | Client errors | Plus `NoReplyError` and `DeviceRefusedError(code)` (2026-10-18, DESIGN §5) |
| Q34 | `device_id` mismatch | `ProtocolError`, before the tag, nothing sent (2026-10-18) |
| Q35 | What a client survives | Wrong code / rate limit: handshake again; `failed`: retry; no reply / lost / protocol error: finished (2026-10-18) |
| Q36 | Lost connection | No disconnect event: `TransportError` → `ConnectionLostError`, a silent drop → `NoReplyError` (2026-10-18) |
| Q37 | Test client vs real client | `ClientChannel` shared by `ProvisioningClient` and the tests' `Configurator`; the device rig moves to the helper (2026-10-18) |
| Q38 | What the flow is given | `discover() -> list[FoundDevice]`, `connect(found) -> ClientTransport` (unconnected), `io` with `ask` / `ask_secret` / `show`; exit 0 or 1 (2026-10-18, DESIGN §5) |
| Q39 | Picking a device | Numbered list, Enter picks the only one; none found → look again (2026-10-18, SPEC §9.2) |
| Q40 | Typing the code | Bad format / wrong code / rate limited: explain and ask again; no sleep, no limit (2026-10-18, SPEC §9.2) |
| Q41 | Network menu | Plus "Scan again"; an empty scan has its own text (2026-10-18, SPEC §9.2) |
| Q42 | Hidden network | Name, then security from `info.caps` with router-label wording; `hidden=True` (2026-10-18, SPEC §9.2) |
| Q43 | Enterprise and CA file | No anonymous identity; CA file ≤ 4096 bytes (`MAX_CA_PEM_BYTES`), parsed as PEM; SPEC §4.1 corrected (2026-10-18) |
| Q44 | After the result | Connected → exit 0; failed → retry / other network / quit; refused → network menu; no reply, lost, unexpected → exit 1 (2026-10-18, SPEC §9.2) |
| Q45 | PEAP without CA or domain | Only the device's warning after connecting (2026-10-18) |
| Q46 | Quit and end of input | q at menus, EOF = quit, transport always closed; Ctrl+C in `cli.py` (2026-10-18) |
| Q47 | Progress | One line at `connecting`, no spinner (2026-10-18) |
| Q48 | Where shared test code lives | `conftest.py` only fixtures; classes and constants in `tests/fake_configurator.py` (2026-10-18) |
| Q49 | Shared fixtures | `device`, `paired_device`, `ends`, `state_dir`, `log_stream`; no fake-time fixture, each rig owns its clock (2026-10-18) |
| Q50 | LED in tests | `Device.led()`: one simulated second, then the last `LED …` line on the device's screen (2026-10-18) |
| Q51 | A new configurator can't connect | End-to-end `discover()` sees the device only while it advertises; plus a refused direct `connect()` (2026-10-18) |
| Q52 | Second `hello` mid-session | Configurator 1 quits at the network menu, configurator 2's `hello` ends that session and provisions (S3, Q4) (2026-10-18) |
| Q53 | Hidden network | Test-only `HiddenWifi`: a network left out of the scan, joined by name (2026-10-18) |
| Q54 | No real sleeps | Client waits stay real: 0.5 s, and 0.05 s only for no reply; no production change (2026-10-18) |
| Q55 | Overlap with `test_flow` | Both stay: end to end adds the device side and multi-run scenarios (2026-10-18) |
| Q56 | Error codes in the device's log | Every `error` sent is logged with `code=`: `hello_refused` (rate limited), `command_refused` (bad_request, unsupported_security). The one production change of Phase 4 (2026-10-18, SPEC §5.5, DESIGN §6) |
| Q57 | What the log test runs | Lean: one flow with a wrong code, a dashed lowercase code, a wrong password, PEAP + CA success; unique secret values; no bad-request or rate-limit step (2026-10-18) |
| Q58 | Keys without logging them | `monkeypatch` spies on `crypto._new_key`, `_derive`, `SessionCipher.seal`; they must catch something (2026-10-18) |
| Q59 | CA PEM body | Checked line by line (2026-10-18) |
| Q60 | Where the setup code may appear | Only on the device's screen; never in logs, stdout, stderr or the configurator's output (2026-10-18) |
| Q61 | The nmcli path | `FakeRunner` in `tests/fake_nmcli.py`; the log test also runs the real `run()` with an echoing `sh` stand-in (2026-10-18) |
| Q62 | Not hiding too much | SSIDs, `identity`, `device_id`, the failure reason and DEBUG lines asserted visible; `code` via the session test and `test_log` (2026-10-18) |
| Q63 | Real evidence | Step 6.3 also greps the run logs for the hotspot password and the setup code before committing (2026-10-18) |
| Q64 | A second configurator in simulation | Refused like the radio: closed at once while the window is closed or a client is connected; `connect()` makes an `info` round trip, so it fails there; `discover()` finds nothing (2026-10-18, SPEC §5.4, §10) |
| Q65 | What simulation is used for | Not the in-process end-to-end tests (memory transport); a two-process smoke test (2026-10-18, SPEC §10, §12) |
| Q66 | Where the sim wire format lives | `teton_common/sim_wire.py`, no protocol, framing or crypto (2026-10-18, DESIGN §2) |
| Q67 | `info` record and limits | Empty request, response ≤ 180 bytes; 20 bytes for the other channels; malformed → one log line without the bytes, connection dropped; client waits 2 s (2026-10-18, SPEC §10) |
| Q68 | Stale socket | Removed only if nobody listens; a live one fails the start; a non-socket is left alone (2026-10-18, SPEC §10) |
| Q69 | Socket permissions, fallback path | `0600`; `<tempdir>/teton-sim-<uid>.sock` (2026-10-18, SPEC §10) |
| Q70 | Disconnect ordering | Client `close()` waits for the server's EOF; device `send()` to a vanished client is dropped (2026-10-18, SPEC §10, DESIGN §5) |
| Q71 | Contract tests | `test_transport_contract.py` over memory and sim; `transport.check_chunk()` shared (2026-10-18, DESIGN §7) |
| Q72 | Sim discovery | `FoundDevice(info.name, 100, socket path)` (2026-10-18) |
| Q73 | Logs vs. the screen | Screen on stdout, logs on stderr through the status line; Step 6.3 uses `2>` for the device log (2026-10-18, SPEC §9.1, §11, DESIGN §4.1) |
| Q74 | Configurator log level | `WARNING` by default, `--verbose` → `DEBUG`; `configure_logging(default_level=…)` (2026-10-18, SPEC §11) |
| Q75 | Ctrl+C on the configurator | Daemon threads for prompts; terminal restored; exit 130 (2026-10-18, SPEC §9.2, DESIGN §5) |
| Q76 | Device signals | SIGTERM like Ctrl+C; newline after the LED line (2026-10-18, SPEC §9.1) |
| Q77 | Sim state dir | `~/.local/state/teton-device-sim`; `--state-dir` overrides (2026-10-18, SPEC §3) |
| Q78 | Fake Wi-Fi delays | Scan 2 s, connect 3 s; tests inject instant sleep (2026-10-18, SPEC §10) |
| Q79 | CLI test seam | `parse_args`, `run_device(args, buttons=None)`, `run_configurator(args, io=None)`; startup errors → one line, exit 1 (2026-10-18, DESIGN §5) |
| Q80 | Two-process smoke test | Device subprocess on a pseudo-terminal, scripted `run_flow` over the socket, no sleeps; import check in a fresh subprocess (2026-10-18, DESIGN §7) |
| Q81 | BLE local name | The CLI loads `Storage` and passes `GattServerTransport(name)`; `protocol.local_name()` shared with `Info.name` (2026-10-19, DESIGN §5) |
| Q82 | Register without advertising | `start()` = `BlessServer.start()` minus advertising, with `bless`'s own objects; `set_advertising` idempotent (2026-10-19, DESIGN §5) |
| Q83 | Incoming BLE writes | Queued by the synchronous handler, one task awaits the receivers in order (2026-10-19, DESIGN §5) |
| Q84 | BLE startup failures | `bless` imported and the server created in `start()`; failures → `TransportError`, one line, exit 1 (2026-10-19, SPEC §9.1) |
| Q85 | BLE `close()` | Step by step, each failure logged, never raises, idempotent (2026-10-19, DESIGN §5) |
| Q86 | `info` limit | `transport.check_info()` in every device transport's `set_info` (2026-10-19, DESIGN §5) |
| Q87 | BLE tests without a radio | Pure helpers plus a fake server via `make_server`, standing in for `_BlessGattServer`, the only code touching `bless` (2026-10-19, DESIGN §7) |
| Q88 | Device CLI wiring | Step 6.1 wires `GattServerTransport(name)` + `NmcliBackend(state_dir=…)`; configurator stays "not available" until 6.2 (2026-10-19) |
| Q89 | Notification pacing | None; revisit only if the hardware runs lose chunks (2026-10-19) |
| Q90 | `bless` side effects | Adapter alias and the `Released!` line accepted, in the README troubleshooting (2026-10-19) |
| Q91 | RSSI to signal | `clamp(2 × (rssi + 100), 0, 100)`, NetworkManager's scale (2026-10-19, SPEC §9.2) |
| Q92 | BLE discovery | 10 s, service UUID filter, strongest first, advertised local name (2026-10-19, DESIGN §5) |
| Q93 | Connecting to what was found | `BleDiscovery` keeps the `BLEDevice`s for `connect(found)` (2026-10-19, DESIGN §5) |
| Q94 | Reassembly timeout | 10 s from the last chunk, not the first; write-without-response in reserve (2026-10-19, SPEC §4.1) |
| Q95 | BLE `connect()` | Connect (10 s), subscribe, check the service; errors → `TransportError` (2026-10-19, DESIGN §5) |
| Q96 | BLE notifications | Sync callback → `Inbox.put()`, delivered in order (2026-10-19, DESIGN §5) |
| Q97 | BLE disconnects | `disconnected_callback` only marks not connected; idempotent `close()` (2026-10-19, DESIGN §5) |
| Q98 | BLE writes | `response=True`, public `bleak` API only (2026-10-19) |
| Q99 | Configurator CLI, Bluetooth off | `BleDiscovery` without `--simulate`; `ui_text.BLUETOOTH_UNAVAILABLE`, exit 1 (2026-10-19, SPEC §9.2) |
| Q100 | BLE client tests | Pure helpers, no-`bleak` import check, fake `bleak`-shaped client via `make_client` (2026-10-19, DESIGN §7) |
| Q101 | Pending hardware checks | Step 0.3 before 6.2's Verify; central spike superseded (2026-10-19) |

Post-spike simplifications, recorded in plan_v5 ("v5 additions after the BLE spike"): **S1** no `busy`, **S2** fixed 20-byte chunks, **S3** no connection tracking (a new `hello` starts a new session), **S4** the device exits after success. Laptop roles fixed: laptop 1 = device, laptop 2 = configurator.

Added on 2026-10-13 (plan_v5): **D1** device on Ubuntu 24.04 / Python 3.12, configurator on 24.04 or 26.04 (laptop 2: Python 3.14), CI on both; **D2** no text input on the device, simulated buttons and LED, pairing window, the device stays on after success.

## Open questions

These are the questions still open after the first review and the spike. Each one is self-contained: it says which step it blocks, what the docs say and the option I would propose. I haven't resolved any of them. Answers that change a decision go into `plan_v5.md` (a new changelog row) + SPEC/DESIGN.

### Gaps (the docs don't say)

**Q16. CI earlier? (minor).** CI is SHOULD (Step 7.1) but takes about 15 minutes, and it would guard every PR from Phase 1 on. *Proposal:* move it to right after Step 0.2 (the spike). Otherwise it stays in Phase 7.
