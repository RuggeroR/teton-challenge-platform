# Implementation plan

Version 2, 2026-10-11. This plan turns [SPEC.md](SPEC.md) and [DESIGN.md](DESIGN.md) into small steps, in the order we build them. Why each decision was made is in [plan_v5.md](plan_v5.md). Version 2 applies the answers to review questions Q1, Q2, Q3, Q7, Q8 and Q9. If this plan disagrees with those documents, they win.

## How to read this plan

- We build **bottom-up**: each step depends only on steps already finished.
- Scope follows SPEC §12: **MUST** first, then **SHOULD**, then **COULD**. Two exceptions: the BLE hardware spike runs right after project tooling (Step 0.2), because it can change the transport interface; and simulation mode (SHOULD) comes before the real BLE transports, because it is cheap and lets us debug the full protocol without radios.
- Every step lists its goal, the files it touches, the tests to write, how you can verify it yourself, and the SPEC/DESIGN sections it implements.
- 🔧 **HARDWARE** marks steps that need the laptops or the phone. Every other step runs on any machine.
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
| 0. Tooling + BLE spike | 0.1–0.2 | MUST | 🔧 0.2 |
| 1. Shared pure code (`teton_common`) | 1.1–1.7 | MUST | no |
| 2. Device core (`teton_device`) | 2.1–2.7 | MUST | 2.5 (scan only, one laptop) |
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
  - Limits: max message 8192, default chunk 20, reassembly timeout 10 s, session idle 300 s, connect timeout 45 s, rate limit 5 failures / 30 s doubling to 600 s.
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

- **Goal:** split a message into chunks that fit the BLE MTU and put it back together, separately per channel.
- **Files:** `src/teton_common/framing.py`, `tests/test_framing.py`.
- **Content:**
  - `frame(payload, chunk_size) -> list[bytes]` adds the 2-byte big-endian length prefix and splits the result.
  - `Reassembler(clock=time.monotonic)` has `feed(chunk) -> bytes | None` and `reset()`. It rejects a declared length over 8192, and drops a partial message older than 10 s.
  - `chunk_size_for_mtu(mtu)` returns `mtu - 3`, or 20 when the MTU is unknown.
- **Tests:** chunk sizes 20, 185 and 512 round trip; exactly 8192 works and 8193 is rejected; interleaved channels using separate reassemblers; the timeout reset, with a fake clock; `reset()` on disconnect; an empty payload.
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
- **Tests:** each listed key is redacted at the top level and when nested; non-secret keys are kept; an error event with `code="busy"` still shows `busy` while `setup_code` is redacted (review Q7); the output is valid JSON; `--verbose` switches the level to DEBUG. The full-run "no secret in logs" test comes in Step 4.1, once a full run exists.
- **Verify:** `pytest tests/test_log.py -v`
- **Implements:** SPEC §5.5, §11.

### Step 1.7: Transport interfaces + in-memory transport

- **Goal:** the abstraction that lets one protocol implementation run over BLE, a Unix socket or memory.
- **Files:** `src/teton_common/transport.py`, `src/teton_common/memory_transport.py`, `tests/test_memory_transport.py`.
- **Content:**
  - `DeviceTransport` (`on_connect`, `on_disconnect`, `set_info`, `on_receive`, `send`, `chunk_size`) and `ClientTransport` (`connect`, `read_info`, `send`, `on_receive`, `chunk_size`, `close`), as in DESIGN §5 (review Q2), adjusted if the spike (Step 0.2) found something.
  - `info` is a plain read of raw JSON and is not framed (SPEC §4.1).
  - A small `FramedChannel` helper that combines a transport, `framing.frame` and one `Reassembler` per channel, so framing sits above the transport.
  - `memory_transport.pair()` returns a connected device end and client end.
- **Tests:** `connect()` fires `on_connect` on the device end; `read_info()` returns what `set_info()` set; messages arrive on the right channel in order; large messages go through framing with the reported `chunk_size`; `close()` fires `on_disconnect`; sending after close raises an error.
- **Verify:** `pytest tests/test_memory_transport.py -v`
- **Depends on:** Q19 (stopping advertising is not in the interface yet).
- **Implements:** DESIGN §1 (principles), §5 (`DeviceTransport`, `ClientTransport`).

---

## Phase 2: Device core (`teton_device`)

### Step 2.1: State machine and storage

- **Goal:** the device always knows its state, invalid transitions are impossible, and identity and provisioning survive restarts.
- **Files:** `src/teton_device/state.py`, `src/teton_device/storage.py`, `tests/test_state.py`, `tests/test_storage.py`.
- **Content:**
  - `StateMachine` with the DESIGN §4 transitions. `transition(to)` raises `InvalidTransition` on a forbidden transition, and every transition is logged.
  - `Storage(state_dir)` has `load_or_create()`, which creates `device_id` (`TETON-` + 6 hex) and a setup code, creates the directory `0700` and the file `0600`, and writes atomically (temp file + `os.replace`). It also has `mark_provisioned()` and `rotate_code()`.
  - `state.json` also holds a `provisioned` flag. SPEC §3 lists only `device_id` and `setup_code`, but DESIGN §4 requires it to be persisted.
- **Tests:** every allowed transition; a set of forbidden ones; the state after reload when provisioned; the permissions are 0700 and 0600; the file is created once and reused afterwards; `rotate_code` changes the code but keeps `device_id`; a corrupt `state.json` gives a clear error.
- **Verify:** `pytest tests/test_state.py tests/test_storage.py -v`
- **Depends on:** Q13 (the state names, and CONNECTED vs PROVISIONED).
- **Implements:** SPEC §3 (identity, storage, rotation). DESIGN §4, §7 (`state`).

### Step 2.2: Setup code display (QR)

- **Goal:** print the code, the terminal QR and the "demo only" note.
- **Files:** `src/teton_device/qr.py`, `tests/test_qr.py`.
- **Content:** `qr_payload(device_id, code)` returns `TETON:v1;id=…;code=…`. `render(device_id, code) -> str` returns the formatted code, an ASCII QR (from `qrcode`) and the exact note text from SPEC §3.
- **Tests:** the payload format; the output contains the formatted code and the note.
- **Verify:** `pytest tests/test_qr.py -v`, then `python -c "from teton_device.qr import render; print(render('TETON-3F9A2C','7K2MQX9D4HTRB8WZ'))"` and scan the QR with your phone camera; it should show the payload text.
- **Depends on:** Q14 (whether the code in the QR has dashes).
- **Implements:** SPEC §3 (display).

### Step 2.3: Wi-Fi backend interface + fake backend

- **Goal:** the only door to Wi-Fi, and a fake that behaves like the SPEC §10 networks.
- **Files:** `src/teton_device/wifi/backend.py`, `src/teton_device/wifi/fake.py`, `tests/test_wifi_fake.py`.
- **Content:**
  - The `WifiBackend` protocol (`scan`, `connect`, `forget`) and `Connectivity` (`verify`).
  - Result types: `Network`, `Credentials` (secrets use `repr=False`), `ConnectResult` (`ok`, `reason`, `ip`, `warnings`) and `VerifyResult`.
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
    - `decide_result(...)` applies the success rule (review Q8): `portal` → `captive_portal`; `full` → success; anything else → success with the warning `limited_internet` if the gateway answers, otherwise `no_internet`.
  - **I/O wrapper:** `NmcliBackend(runner=...)` calls `nmcli` through one injectable async `runner` that applies a timeout to every call and can feed stdin. It implements the SPEC §8 sequence: scan, add `teton-<ssid>` **without secrets**, set the secret by piping `set …`/`save`/`quit` into `nmcli connection edit id teton-<ssid>` (review Q9), `up --wait 45`, verify, and on failure delete the profile.
  - **Connectivity:** `NmConnectivity.verify(interface)` checks for an IPv4 address, runs `nmcli networking connectivity check` to refresh, reads the **interface's** connectivity, then pings the gateway **on that interface** (`ping -I <iface> -c 2 -W 2`) when it isn't `full` (review Q8).
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
- **Depends on:** Q10 (timeout budget), Q11 (security mapping), Q17 (autoconnect priority).
- **Implements:** SPEC §5.5 (credentials to NM, no secrets in argv), §6.3 (`internet`, warnings), §7, §8. DESIGN §6 (one runner, timeouts, raw output), §7 (`wifi/nmcli`).

### Step 2.5: 🔧 HARDWARE (one laptop, read-only): real scan smoke test

- **Goal:** prove that the real backend can scan on this laptop before we build on it.
- **Files:** none. Fixtures may be updated if real output differs from the hand-written ones.
- **You do (either laptop):**
  ```bash
  python -c "import asyncio; from teton_device.wifi.nmcli import NmcliBackend; [print(n) for n in asyncio.run(NmcliBackend().scan())]"
  ```
  Expected: a list of nearby networks with signal and security type, the strongest first, and no duplicates. This command doesn't change any connection.
- **Also confirm the stdin approach of review Q9 (this one creates and deletes a throwaway profile, so you run it):**
  ```bash
  nmcli connection add type wifi con-name teton-stdin-test ssid teton-stdin-test wifi-sec.key-mgmt wpa-psk autoconnect no
  printf 'set wifi-sec.psk dummy-password-123\nsave\nquit\n' | nmcli connection edit id teton-stdin-test
  nmcli -s -g 802-11-wireless-security.psk connection show teton-stdin-test   # dummy-password-123
  nmcli connection delete teton-stdin-test
  ```
  If the secret isn't saved, we fall back to `passwd-file`, as your answer to Q9 suggests.
- **Implements:** SPEC §5.5, §8.1, §8.2.

### Step 2.6: Device session (protocol handler)

- **Goal:** one connection's worth of protocol on the device: handshake, rate limit, encrypted commands, status notifications, idle timeout.
- **Files:** `src/teton_device/session.py`, `tests/test_session.py`.
- **Content:**
  - `DeviceSession(channels, wifi, storage, state, limiter, clock)`:
    - `handle_session_msg`: `hello` → `hello_reply`, then `confirm` → keys + encrypted `ready`.
    - `handle_command`: opens the envelope, then dispatches `scan` or `set_credentials` and sends `status connecting`, then `connected` or `failed`.
  - `HandshakeLimiter` gives 5 failures, then 30 s doubling to 600 s, with an injected clock, and resets on success.
  - Protocol violations end the session and are logged with a reason. Wi-Fi failures do not end it.
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
- **Depends on:** Q4 (how failures are counted, and retyping the code), Q5 (rate-limit reply), Q6 (`bad_request` vs ending the session).
- **Implements:** SPEC §5.1, §5.4, §6.2, §6.3. DESIGN §3, §5 (`DeviceSession`), §6.

### Step 2.7: Device service (orchestrator)

- **Goal:** tie the pieces together: one session at a time, the state machine, persistence, and advertising on and off.
- **Files:** `src/teton_device/service.py`, `tests/test_service.py`.
- **Content:**
  - `DeviceService(transport, wifi, storage, clock)`:
    - `run()` loads state, shows the code, starts the transport and serves connections.
    - A second connection gets `busy`.
    - A disconnect while `CONNECTING` lets the attempt finish (DESIGN §4).
    - On success it persists `provisioned` and stops advertising (Q19).
  - Transport errors never crash the service.
- **Tests (memory transport):** the happy path ends `PROVISIONED` with advertising off; a second client gets `busy`; a disconnect mid-`CONNECTING` still finishes, and a failure rolls back; a restart after provisioning doesn't advertise.
- **Verify:** `pytest tests/test_service.py -v`
- **Depends on:** Q12 (what `run` does after success, or when already provisioned), Q19 (how the service stops advertising).
- **Implements:** SPEC §5.4 (one session), §8.5, §8.6. DESIGN §4, §6.

---

## Phase 3: Configurator (`teton_configurator`)

### Step 3.1: Plain-language text

- **Goal:** every message the technician can see, in one file: one sentence plus a next action.
- **Files:** `src/teton_configurator/ui_text.py`, `tests/test_ui_text.py`.
- **Content:** the prompts, the progress lines, `failure_text(reason)` with the SPEC §7 texts, error-code texts (`busy`, `unsupported_security`, `bad_request`, rate-limited), wrong code / wrong device, and "no device found".
- **Tests:** every `FailureReason` and `ErrorCode` has a text (so a newly added enum value fails the test until it gets one); no text contains jargon such as "nmcli", "GATT" or "exception".
- **Verify:** `pytest tests/test_ui_text.py -v`
- **Implements:** SPEC §7, §9.2. CLAUDE.md "plain language".

### Step 3.2: Protocol client

- **Goal:** the configurator's half of the protocol, independent of UI and radio.
- **Files:** `src/teton_configurator/client.py`, `tests/test_client.py`.
- **Content:**
  - `ProvisioningClient(transport)` has:
    - `read_info()`;
    - `handshake(code) -> DeviceInfo`, which also checks that `hello_reply.device_id` matches `info.device_id`;
    - `scan() -> list[Network]`;
    - `provision(creds) -> AsyncIterator[Status]`.
  - Typed errors: `WrongCodeError`, `DeviceBusyError`, `RateLimitedError`, `ProtocolError` and `ConnectionLostError`.
- **Tests (against the real `DeviceService` over the memory transport, with the fake Wi-Fi):** a full success; a wrong code raises `WrongCodeError` and no `set_credentials` is ever sent (checked on the wire); busy; a retry after `failed`.
- **Verify:** `pytest tests/test_client.py -v`
- **Implements:** SPEC §5.1 (configurator side), §6. DESIGN §5 (`ProvisioningClient`).

### Step 3.3: Guided flow

- **Goal:** the six-step technician experience of SPEC §9.2, testable without a terminal.
- **Files:** `src/teton_configurator/flow.py`, `tests/test_flow.py`.
- **Content:**
  - `run_flow(discover, connect, io)`, where `io` wraps `input` and `print`, so tests can script the answers.
  - The steps:
    1. Pick a device.
    2. Type the code. On a wrong code, explain and offer to retype.
    3. Pick a network, or "Other (hidden) network".
    4. Enter the credentials for that security type (for Enterprise: an optional CA file path and an optional domain).
    5. Watch the progress.
    6. See the result, and on failure retry from the network or credentials step.
  - Signal bars, and a lock or "enterprise" label next to each network.
  - The password is read with `getpass`.
- **Tests (scripted io + memory transport + fake Wi-Fi):** the happy path output contains "Connected" and the IP; a wrong code, then retype, then success; a wrong password, then retry, then success; a hidden network; the server-not-verified warning is shown for PEAP without a CA.
- **Verify:** `pytest tests/test_flow.py -v`
- **Implements:** SPEC §9.2, §7.

---

## Phase 4: End-to-end test (in memory)

### Step 4.1: Full-flow tests + "no secrets in logs"

- **Goal:** the whole system in one process, plus the test that enforces the secret-hygiene rule.
- **Files:** `tests/test_end_to_end.py`, `tests/test_no_secrets_in_logs.py`, `tests/conftest.py` (shared fixtures).
- **Tests:**
  - **End to end:** the DESIGN §7 cases (PSK ok, wrong password then retry, wrong code, portal), plus Enterprise with and without a CA, busy, and the rate limit.
  - **No secrets in logs:** a full simulated run, including a wrong password and a wrong code, with logging captured at DEBUG. The test asserts that none of the PSK, the EAP password, the setup code (raw or formatted), the session keys or the base64 ciphertext appears anywhere in the output.
- **Verify:** `pytest tests/test_end_to_end.py tests/test_no_secrets_in_logs.py -v`
- **Implements:** SPEC §12 MUST #6 (unit tests list complete). DESIGN §7 (end to end, `log`).

**At this point:** the protocol, crypto and device logic are complete and tested without any hardware. This is a good milestone for a PR.

---

## Phase 5: Simulation mode + CLIs

### Step 5.1: Unix-socket transports

- **Goal:** the same protocol between two processes, with only the radio simulated.
- **Files:** `src/teton_device/sim_server.py`, `src/teton_configurator/sim_client.py`, `tests/test_sim_transport.py`.
- **Content:**
  - The socket is a byte stream, so each chunk is wrapped as `[u8 channel][u16 length][chunk]`. This is a small multiplexing layer under our normal framing.
  - `info` is a request/response pair.
  - The socket lives at `$XDG_RUNTIME_DIR/teton-sim.sock`, falling back to a temp directory when the variable is unset (CI).
  - A stale socket file is removed at start.
  - The simulated chunk size is 20, so chunking is exercised exactly as on BLE.
- **Tests:** the server and client in one event loop on a socket in `tmp_path`; a full handshake + scan; disconnect detection; a second client gets `busy`.
- **Verify:** `pytest tests/test_sim_transport.py -v`
- **Implements:** SPEC §10 (transport).

### Step 5.2: CLIs

- **Goal:** the real commands of SPEC §9.
- **Files:** `src/teton_device/cli.py`, `src/teton_configurator/cli.py`, `tests/test_cli.py`.
- **Content:**
  - `teton-device run [--simulate] | reset | show-code`, with `--state-dir` and `--verbose`.
  - `teton-configurator [--simulate] [--verbose]`.
  - `run` without `--simulate` imports BLE lazily. Until Phase 6 it prints a clear "BLE transport not available yet".
  - `reset` deletes the `teton-*` profiles through `WifiBackend` (never `nmcli` directly) and rotates the code.
  - Ctrl+C exits cleanly.
- **Tests:** argument parsing; `show-code` prints the code and the note; `reset` with the fake backend rotates the code and calls `forget`; `--state-dir` is respected.
- **Verify (two terminals, any machine):**
  ```bash
  # terminal 1
  teton-device run --simulate --state-dir /tmp/teton-sim
  # terminal 2
  teton-configurator --simulate
  # use the code printed in terminal 1; pick Home-PSK; password correct-horse-battery → "Connected"
  # try a wrong password first → plain-language failure, then retry works
  ```
- **Depends on:** Q12.
- **Implements:** SPEC §9.1, §9.2, §10. SPEC §12 SHOULD #8 (first half).

---

## Phase 6: Real BLE + real Wi-Fi (MUST)

### Step 6.1: BLE device transport (`gatt_server.py`)

- **Goal:** the device transport over BLE, implementing the same interface as the memory and socket transports.
- **Files:** `src/teton_device/gatt_server.py`, `tests/test_gatt_server.py`.
- **Content:**
  - A `bless` server with the 4 characteristics from SPEC §4, no encryption flags (no pairing), the local name `Teton-XXXX` and the service UUID in the advertisement.
  - Chunk size from the MTU (or 20).
  - Implements `DeviceTransport`: `on_connect`/`on_disconnect`, `set_info`, `chunk_size`, plus stopping advertising (Q19), based on what the spike found.
  - `bless` is imported lazily.
- **Tests (no radio):** importing `teton_device` and running the sim and memory paths doesn't import `bless`; the pure helpers (characteristic table, chunk size) are tested.
- **Verify (🔧 laptop A + phone):** `teton-device run`, then in nRF Connect: the service is visible, `info` reads as JSON, and the other characteristics are present.
- **Implements:** SPEC §4. DESIGN §2 (`gatt_server.py`).

### Step 6.2: BLE configurator transport (`ble_client.py`)

- **Goal:** discovery and the GATT client with `bleak`.
- **Files:** `src/teton_configurator/ble_client.py`, `tests/test_ble_client.py`.
- **Content:**
  - Scan for 10 s filtered by the service UUID, and list devices by name and signal.
  - Connect, read the MTU, subscribe to `session` and `status` **before** writing `hello`, then read `info`.
  - Writes use write-with-response, so chunks arrive in order.
  - `bleak` is imported lazily.
- **Tests (no radio):** the pure helpers (signal to bars, device list formatting); importing without `bleak` used.
- **Verify (🔧 both laptops):** laptop A `teton-device run`; laptop B `teton-configurator`. The device appears; the code is accepted; the scan list shows the real networks around laptop A. Stop before entering credentials. That comes in 6.3.
- **Implements:** SPEC §4, §9.2 (steps 1–4). DESIGN §2 (`ble_client.py`).

### Step 6.3: 🔧 HARDWARE: real Wi-Fi run and evidence

- **Goal:** meet SPEC §12 MUST #1–#5 on real hardware and record the evidence.
- **Files:** `docs/evidence/` (logs, screenshots, `.btsnoop` capture, `README.md` index), and `tests/fixtures/nmcli/` (real recorded outputs, anonymised).
- **You do:**
  1. Phone: turn on the hotspot (WPA2-Personal; also try WPA3 if the phone offers it). Pick a password you don't use anywhere else, because it will appear in evidence notes.
  2. Laptop A: `sudo btmon -w docs/evidence/run-psk.btsnoop` in one terminal, and `teton-device run --verbose 2>&1 | tee docs/evidence/device-run.log` in another.
  3. Laptop B: `teton-configurator 2>&1 | tee docs/evidence/configurator-run.log`.
  4. Run the scenarios:
     - **wrong setup code:** a clear message, and nothing else is sent;
     - **wrong hotspot password:** `wrong_password`, then a retry in the same session;
     - **correct password:** "Connected, IP …, internet OK".
  5. Check the capture: `btmon -r docs/evidence/run-psk.btsnoop | grep -c "<hotspot password>"` gives `0`. Also grep for the SSID; the SSID travels encrypted too, so it should also be 0.
  6. Take screenshots of both terminals.
  7. Record `nmcli` outputs for the fixtures: the wrong-password and unknown-SSID errors, and the scan output.
  8. On laptop A afterwards: `teton-device reset` to rejoin your normal Wi-Fi.
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
- **Content:** `ubuntu-24.04`, Python 3.12, `pip install -e ".[dev]"`, `ruff check .`, `ruff format --check .`, `pytest`. No Bluetooth needed, because of the lazy imports.
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
  - **Preflight script:** the read-only checks from the README "Check your machine" section, each with a clear OK or FIX line.
- **Tests:** `bash -n scripts/preflight.sh`; `systemd-analyze verify deploy/teton-device.service`; a pytest for the `sd_notify` helper, against a temporary socket.
- **Verify:** `scripts/preflight.sh` on both laptops prints all OK. Optionally (🔧, needs `sudo`, done by you): install the unit and run `systemctl start teton-device`.
- **Implements:** SPEC §12 SHOULD #10. plan_v5 §11.1.

---

## Phase 8: Docs and writeup (MUST)

### Step 8.1: README

- **Goal:** someone at Teton can reproduce everything from a stock Ubuntu 24.04 install.
- **Files:** `README.md`. The Requirements section already exists.
- **Content:** the quickstart in simulation (2 terminals); the real demo per laptop; the Enterprise test; a troubleshooting table (adapter off or `rfkill`, the BlueZ GATT cache on the client, `bluetoothctl remove <addr>`, NetworkManager permissions, the `sudo` fallback for BlueZ, `btmon` needs root, a hotspot with no internet); the second laptop's column in "Tested on".
- **Verify:** you follow the README on the second laptop from scratch.
- **Implements:** SPEC §12 MUST #7.

### Step 8.2: ARCHITECTURE.md (the writeup)

- **Goal:** the submission writeup the challenge asks for.
- **Files:** `docs/ARCHITECTURE.md`, `docs/evidence/README.md` (an index of the evidence).
- **Content:**
  - The channel choice and rejected alternatives (from `research/channels.md` and plan_v5 §3).
  - The trust bootstrap and its alternatives (plan_v5 §3b).
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

| Step | Laptop A (device) | Laptop B (configurator) | Phone | Blocked by an environment problem? |
|---|---|---|---|---|
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

Answered on 2026-10-11 and recorded in [plan_v5.md](plan_v5.md), SPEC 1.1 and DESIGN 1.1.

| # | Question | Decision |
|---|---|---|
| Q1 | AAD contents | SPEC §5.3 is right: label + direction + `seq`. plan_v4 §4 corrected in plan_v5 |
| Q2 | Transport interface incomplete | Split into `DeviceTransport` and `ClientTransport`; framing stays above; `info` unframed (DESIGN §5, SPEC §4.1) |
| Q3 | BLE spike timing | Right after project tooling: now Step 0.2 |
| Q7 | Redaction key `code` hides error codes | `code` removed from the list; setup code only under `setup_code`; test that `busy` stays visible (SPEC §5.5) |
| Q8 | Connectivity rules | Option A: success = activated + IP + gateway reachable on the Wi-Fi interface; not `full` → warning `limited_internet`; `portal` → `captive_portal`; gateway down → `no_internet`; per-interface checks (SPEC §6.3, §7, §8) |
| Q9 | Secrets in argv | Never. Profile without secrets, then `nmcli connection edit` through stdin; one runner; argv test (SPEC §5.5, §8). Confirmed on hardware in Step 2.5 |

## Open questions

These are the questions still open after the first review, plus Q19, found while applying the answers. Each one is self-contained: it says which step it blocks, what the docs say and the option I would propose. I haven't resolved any of them. Answers that change a decision go into `plan_v5.md` (a new changelog row) + SPEC/DESIGN.

### Contradictions between documents

**Q13. State names (blocks 2.1).**
- plan_v5 §5 has a `FAILED(reason)` state; DESIGN §4 doesn't (failure is a transition back to `SESSION_ESTABLISHED`).
- DESIGN §4 has both `CONNECTED` and `PROVISIONED`, without saying what the difference is.
- SPEC §6.1 shows `info.state = "advertising"` but doesn't list the other values.
- Nobody covers the moment between `hello` and `confirm` (handshake in progress).

*Proposal:* follow DESIGN, with no `FAILED` state. `CONNECTED` is a short-lived state while the result is sent; `PROVISIONED` is persisted and means advertising is off. `info.state` uses the lowercase `DeviceState` values.

**Q6. `bad_request` vs ending the session (blocks 2.6).** SPEC §6.3 defines `error: bad_request`, which implies the session continues. DESIGN §6 says protocol violations (bad JSON, wrong state) end the session. *Proposal:*
- a broken **envelope** (bad JSON, wrong `seq`, decryption failure) ends the session;
- a **decrypted but invalid** command (a PSK of 5 characters, a missing field) or `scan` while `CONNECTING` gets `bad_request`, and the session stays open.

### Gaps (the docs don't say)

**Q4. How failed handshakes are counted, and retyping the code (blocks 2.6, 3.3).** With a wrong code, the *configurator* detects the mismatch first (`confirm_d`) and aborts, so it never sends `confirm`. The device just sees `hello` followed by a disconnect. That is exactly what an attacker collecting tags for offline guessing would do. Two things are open:
- Should "a `hello` not followed by a valid `confirm`" count as a failure for the rate limit? *Proposal:* yes.
- SPEC §9.2 says "offer to retype the code". Should that be a new `hello` on the same connection, or a reconnect? *Proposal:* the configurator disconnects and reconnects, which is simpler and matches "abort".

**Q5. What a rate-limited device replies (blocks 2.6, 3.1).** SPEC §5.4 says "refuse handshakes for 30 s" but defines no message for it. *Proposal:* a new error code `rate_limited` with `retry_after_s`, so the technician sees "Too many wrong codes. Wait 30 seconds and try again." This is a SPEC change.

**Q10. The 45 s timeout budget (blocks 2.4).** SPEC §7 says `timeout` = "no result within 45 s", and §8.3 says `connection up` has a 45 s timeout. Verification (the connectivity check + 2 pings) comes after that. Is 45 s the activation limit or the total? *Proposal:* 45 s for activation, plus up to 15 s for verification. The configurator waits up to 70 s before saying "took too long".

**Q11. Which security types (blocks 1.1, 1.3, 2.4).**
- SPEC §6.2 accepts `open`, but `info.caps` (§6.1) and plan_v4 only mention PSK, SAE and PEAP. Is `open` in scope?
- Most phone hotspots now use **WPA2/WPA3 transition mode** (`SECURITY = "WPA2 WPA3"`). *Proposal:* map it to `wpa2-psk`, which works with transition mode.
- Networks we can't join (WEP, OWE, WPA3-Enterprise): hide them from the scan list, or show them as "not supported"? *Proposal:* show them, marked "not supported", so the technician isn't left wondering why the network is missing.

**Q12. What `run` does after success, and with `reset` (blocks 2.7, 5.2).**
- After `connected`, should `teton-device run` exit or keep running idle?
- What should `run` do on an already-provisioned device?
- Can `reset` run while `run` is active? It is a separate process.

*Proposal:*
- After success, keep the process running and print "Provisioned; advertising stopped. Run `teton-device reset` to set up again." Exit on Ctrl+C.
- On an already-provisioned device, print the same message and exit 0.
- `reset` only edits the state and the profiles. If a `run` is active, it tells you to restart it, and uses a lock file to detect that.

**Q14. Setup code format inside the QR (blocks 2.2, minor).** SPEC §3 doesn't say whether `code=` in the QR payload has dashes. *Proposal:* the normalised 16 characters without dashes, because it's for machines.

**Q15. `bless` limitations (conditional on the Step 0.2 spike results).** If the spike shows that `bless` can't report disconnects or tell clients apart, `busy` can't be implemented reliably over BLE. Notifications from a BlueZ GATT server also go to *every* subscribed client, so a `busy` reply would reach the active client too. plan_v5 §6 names `dbus-next` directly against BlueZ as the fallback. `bless` already pulls it in, but using it directly makes it a **direct** dependency that DESIGN §8 doesn't list. *Proposal:* decide after the spike. A middle path is to keep `bless` and add a small D-Bus watch on `org.bluez.Device1.Connected` through `bless`'s own D-Bus connection.

**Q16. CI earlier? (minor).** CI is SHOULD (Step 7.1) but takes about 15 minutes, and it would guard every PR from Phase 1 on. *Proposal:* move it to right after Step 0.2 (the spike). Otherwise it stays in Phase 7.

**Q17. Autoconnect priority (affects the Step 6.3 demo, minor).** Laptop A has several saved networks with autoconnect on. SPEC §8.5 sets the Teton profile to autoconnect but says nothing about priority, so a known home network could win after a reboot. *Proposal:* give the `teton-*` profile `connection.autoconnect-priority 10` (a one-line SPEC addition). In production that matters less, because a fresh device has no other profiles.

**Q18. Logging the Enterprise username (minor).** `identity` (e.g. `nurse01`) isn't in the redaction list. It isn't a secret, but in a hospital it is personal data. *Proposal:* add `identity` and `anonymous_identity` to the redaction keys.

**Q19. Stopping advertising isn't in `DeviceTransport` (blocks 1.7, 2.7, 6.1; new, found while applying Q2).** The interface from your answer to Q2 has no way to start or stop advertising. SPEC §8.5 and decision Q7 of plan_v5 need the device to stop advertising on success (and plan_v5 §5: re-enter setup only via reset). The memory and socket transports can treat it as a no-op. *Proposal:* add `async start()` (begin serving and advertising) and `async stop_advertising()` (keep the current connection, so the final `connected` status still reaches the configurator) to `DeviceTransport`, and update DESIGN §5.
