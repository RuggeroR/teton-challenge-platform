# Teton Wi-Fi Provisioning: Specification

Version 1.1, 2026-10-11. Derived from [plan_v5.md](plan_v5.md), which records why each decision was made. 1.1 applies the v4 → v5 changes (redaction keys, secrets off the command line, success rule, per-interface checks). If this spec and the plan disagree, the plan wins. Fix the spec, and record any new decision in a new `plan_vN.md`.

Key words: **MUST** = required for the submission, **SHOULD** = planned, **COULD** = only if time allows.

---

## 1. Scope

A technician uses a **configurator** (laptop B) to send Wi-Fi credentials to a **Teton Device** (laptop A) over **Bluetooth Low Energy (BLE)**. The device joins the network, verifies internet access and reports the result back.

| ID | Constraint (from the challenge) | How this spec meets it |
|---|---|---|
| C1 | No internet, cloud relay or shared network at the start | Point-to-point BLE GATT link (§4) |
| C2 | Real Wi-Fi connection | NetworkManager joins the network, then IP and internet are checked (§8) |
| C3 | No plaintext credentials over the air | Authenticated key exchange + AES-256-GCM at the application layer (§5) |
| C4 | Usable by a non-developer | Guided terminal flow, typed setup code, plain-language errors (§9) |

Out of scope for code (covered in the writeup instead): manufacturer certificates, PAKE, fleet enrollment with Smith, `.deb` packaging, metrics.

## 2. Environment

- Both laptops: **Ubuntu 24.04 LTS**, Python 3.12, BlueZ 5.72, NetworkManager 1.46, a Bluetooth 4.2+ adapter.
- Device: the Wi-Fi interface is managed by NetworkManager. Runs as the logged-in desktop user (polkit lets the active user manage connections). `sudo` is a documented fallback if BlueZ refuses to register the GATT app.
- Test networks: Android phone hotspot (WPA2-PSK / WPA3-SAE). Optionally `hostapd` with its built-in EAP server on laptop B for WPA2-Enterprise PEAP.
- Install: `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`. No other tooling required.

## 3. Device identity and setup code

| Item | Spec |
|---|---|
| `device_id` | `TETON-` + 6 uppercase hex characters, random, generated on first start, persisted |
| BLE local name | `Teton-` + last 4 characters of `device_id` |
| Setup code | 16 characters of **Crockford base32** (`0-9 A-Z` without `I L O U`), i.e. 80 random bits from `secrets`, displayed as `XXXX-XXXX-XXXX-XXXX` |
| Code input normalization | uppercase; remove spaces and `-`; map `O→0`, `I→1`, `L→1`; reject if not exactly 16 valid characters |
| Storage | `state.json` in the state dir (default `~/.local/state/teton-device/`, dir `0700`, file `0600`) holding `device_id` and `setup_code` |
| Display | On `run`, the device prints the code and a terminal QR with payload `TETON:v1;id=<device_id>;code=<code>`, plus the line: *"For demo purposes the code is shown on screen; in production it is printed on the device label."* |
| Rotation | `teton-device reset` generates a new setup code |

**Why 80 bits.** An attacker who actively poses as the configurator receives the device's confirmation tag (§5.2) and could then guess codes offline. 80 random bits makes that infeasible. A passive eavesdropper cannot attack the code at all, because it never learns the ECDH shared secret. A short PIN would require a PAKE (writeup: upgrade path).

## 4. BLE GATT interface

Service UUID: `18388b82-f3e5-40bb-a9b9-fd0f9d789b62`, included in advertisements. The device advertises only while unprovisioned.

| Characteristic | UUID | Properties | Content |
|---|---|---|---|
| `info` | `a95e3738-82e8-4b9f-bdae-54b2ad44b843` | read | Plaintext JSON (§6.1). Never contains secrets |
| `session` | `3904babf-186b-4914-b120-559051a7400d` | write, notify | Plaintext handshake messages (§5.1) |
| `command` | `8f3ed082-9bcf-4ea0-8794-785bf52deb1f` | write | Encrypted envelopes, configurator → device |
| `status` | `7e81670a-c5d4-459d-8e6d-0925e5eaa9da` | notify | Encrypted envelopes, device → configurator |

No OS-level Bluetooth pairing or bonding is used (decision Q2).

### 4.1 Framing

Every message on `session`, `command` and `status` is UTF-8 JSON, framed as `[u16 big-endian total length][payload]`, split into chunks of at most `chunk_size` bytes. The receiver reassembles the chunks per characteristic. `info` is a single GATT read of raw JSON and is not framed.
- `chunk_size` = negotiated ATT MTU − 3, falling back to 20.
- Maximum message size: 8192 bytes (fits a CA certificate PEM). Larger messages are an error.
- A reassembly buffer is reset on disconnect, or when a message is incomplete for 10 s.

## 5. Security protocol

Primitives (Python `cryptography`): X25519, HKDF-SHA256, HMAC-SHA256, AES-256-GCM. Binary fields in JSON are standard base64.

### 5.1 Handshake (on `session`)

1. Configurator → device:
   `{"type":"hello","v":1,"pub":<32 B>,"nonce":<16 B>}`
2. Device generates an ephemeral X25519 key and a nonce, then derives keys (§5.2) and replies:
   `{"type":"hello_reply","v":1,"device_id":"TETON-…","pub":<32 B>,"nonce":<16 B>,"confirm":<32 B confirm_d>}`
3. Configurator derives the same keys and verifies `confirm_d`, using a constant-time comparison. If it doesn't match: show "setup code is wrong or this is not the right device" and abort. Otherwise it sends:
   `{"type":"confirm","confirm":<32 B confirm_c>}`
4. Device verifies `confirm_c`. If it matches, the session is established and the device sends an encrypted `{"type":"ready"}` on `status`. If not, it sends `{"type":"handshake_failed"}` on `session`, counts a failure (§5.4) and disconnects.

### 5.2 Key derivation

```
shared     = X25519(own_priv, peer_pub)
transcript = SHA256("teton-prov-v1" || device_id || pub_c || pub_d || nonce_c || nonce_d)
okm        = HKDF-SHA256(ikm=shared, salt=normalized_setup_code (ASCII), info=transcript, length=96)
k_c2d, k_d2c, k_confirm = okm[0:32], okm[32:64], okm[64:96]
confirm_d  = HMAC-SHA256(k_confirm, "device" || transcript)
confirm_c  = HMAC-SHA256(k_confirm, "configurator" || transcript)
```

Ephemeral keys are generated per session and discarded when it ends.

### 5.3 Encrypted envelope (on `command` and `status`)

```
{"seq": <int>, "ct": <base64>}
nonce = 4 zero bytes || seq as 8-byte big-endian
aad   = "teton-prov-v1|" + ("c2d" | "d2c") + "|" + str(seq)
ct    = AES-256-GCM(k_dir, nonce, plaintext_json, aad)
```

- `seq` starts at 0 per direction and increments by 1. The receiver rejects any `seq` other than the expected one, which blocks replay and reordering. A decryption failure ends the session.
- The plaintext is a JSON message from §6.

### 5.4 Limits

- One active session at a time. A second connection gets `{"type":"error","code":"busy"}` on `session`.
- A session ends after 300 s without traffic, on disconnect, or on any protocol violation.
- Failed handshakes: after 5 consecutive failures, refuse handshakes for 30 s, doubling each time up to 600 s. The counter resets on success. No permanent lockout.

### 5.5 Secret hygiene

- Passwords, PSKs, the setup code and keys are **never logged**. Log redaction applies to the keys `psk`, `password`, `setup_code`, `confirm`, `pub`, `ct` and `ca_cert_pem`, and a test enforces this. The setup code is only ever stored and logged under `setup_code`, and the QR payload is never logged. The key `code` is **not** redacted, because error messages use it (`busy` must stay visible in logs).
- Secrets **never appear on a command line**, where any local user could read them (`ps`, `/proc/<pid>/cmdline`). Every subprocess goes through one runner, and secrets reach `nmcli` through stdin only (§8). A test with a fake runner asserts that no secret appears in any argv.
- Credentials are passed to NetworkManager and then dropped from memory (best effort in Python).
- NetworkManager stores the profile in `/etc/NetworkManager/system-connections/` (root, `0600`).

## 6. Application messages

### 6.1 `info` (read, plaintext)

```json
{"v":1,"device_id":"TETON-3F9A2C","name":"Teton-9A2C","state":"advertising",
 "caps":["wpa2-psk","wpa3-sae","wpa2-eap-peap"]}
```

### 6.2 Commands (configurator → device, encrypted)

| Message | Fields |
|---|---|
| `{"type":"scan"}` | none |
| `{"type":"set_credentials", …}` | `ssid` (str, 1–32 bytes), `hidden` (bool), `security` (`open` \| `wpa2-psk` \| `wpa3-sae` \| `wpa2-eap-peap`), `psk` (str, 8–63, for PSK/SAE), `eap` (object, for PEAP): `identity`, `password`, optional `anonymous_identity`, `ca_cert_pem`, `domain_suffix_match` |

There is no reset command over BLE. Reset is local only (decision Q7).

### 6.3 Device → configurator (encrypted, on `status`)

| Message | Fields |
|---|---|
| `{"type":"ready"}` | Session established |
| `{"type":"scan_result","networks":[…]}` | Each entry: `ssid`, `signal` (0–100), `security`. Deduplicated by SSID, sorted by signal, max 30 |
| `{"type":"status","state":"connecting"}` | Sent as soon as credentials are accepted |
| `{"type":"status","state":"connected", …}` | `ip`, `internet` (`full` \| `limited` \| `none` \| `unknown`, as reported by NetworkManager for the Wi-Fi interface), `warnings` (list: `server_not_verified`, `limited_internet`) |
| `{"type":"status","state":"failed","reason":…}` | `reason` from §7; the session stays open for a retry |
| `{"type":"error","code":…}` | `bad_request`, `busy`, `unsupported_security` |

## 7. Failure reasons

| `reason` | Detected by | Configurator message (plain language) |
|---|---|---|
| `wrong_password` | `nmcli` reports secrets required / 4-way handshake failure | "The Wi-Fi password was not accepted. Check it and try again." |
| `ssid_not_found` | `nmcli` "No network with SSID" | "The device can't see this network. Move closer or check the name." |
| `auth_failed` | 802.1X failure on Enterprise | "The username or password was rejected by the network." |
| `no_ip` | activated, but no IPv4 address within the timeout | "Connected to Wi-Fi but the network gave no address. Contact IT." |
| `captive_portal` | NM connectivity of the Wi-Fi interface = `portal` | "This network needs a login page; devices can't use it. Contact IT." |
| `no_internet` | NM connectivity is not `full` and the gateway does not answer a ping on the Wi-Fi interface | "Connected, but there is no internet access. Contact IT." |
| `timeout` | no result within 45 s | "Connecting took too long. Try again." |

Mapping `nmcli` output to reasons is a heuristic. Unknown errors map to `timeout`, and the raw message goes to the device log only.

## 8. Device Wi-Fi behaviour

1. **Scan:** `nmcli -t -f SSID,SIGNAL,SECURITY device wifi list --rescan yes`.
2. **Apply:** create a dedicated profile `teton-<ssid>` (`nmcli connection add …`) **without secrets**, with security set per §6.2. For PEAP: `802-1x.eap peap`, `802-1x.phase2-auth mschapv2`, plus `802-1x.ca-cert` and `802-1x.domain-suffix-match` when provided. If neither is provided, add the warning `server_not_verified` (decision Q3). Then set the secret (`wifi-sec.psk` or `802-1x.password`) by piping `set …`, `save`, `quit` into `nmcli connection edit id teton-<ssid>` through stdin (§5.5).
3. **Activate:** `nmcli connection up teton-<ssid>` with a 45 s timeout.
4. **Verify,** always on the Wi-Fi interface: device state `activated` → IPv4 address present (else `no_ip`) → `nmcli networking connectivity check` to refresh, then read the interface's own value with `nmcli -g GENERAL.IP4-CONNECTIVITY device show <iface>` → `portal` fails with `captive_portal` → if not `full`, ping the gateway with `ping -I <iface> -c 2 -W 2` (no answer fails with `no_internet`).
5. **Success:** activated + IP + (`full`, or gateway reachable). If connectivity is not `full`, add the warning `limited_internet` ("joined the network but can't reach the internet; this may be normal on restricted networks"). Report `connected`, stop advertising, keep the profile, and set it to autoconnect.
6. **Failure:** report the reason (§7), delete the new profile, and let NetworkManager return to the previous connection if any. The BLE session stays open for a retry.

All `nmcli` calls go through one backend interface, so tests and simulation use a fake.

## 9. User interfaces

### 9.1 Device CLI: `teton-device`

| Command | Behaviour |
|---|---|
| `teton-device run` | Load or create the state, print the code + QR + demo note, advertise, serve sessions, log to stdout |
| `teton-device run --simulate` | Same, but over a local Unix socket with the fake Wi-Fi backend (§10) |
| `teton-device reset` | Delete Teton profiles, generate a new setup code, mark as unprovisioned |
| `teton-device show-code` | Print the code + QR again |
| `--state-dir PATH` | Override the state directory (all commands) |

### 9.2 Configurator CLI: `teton-configurator`

A guided flow with numbered choices and no flags needed:
1. Scan for devices for 10 s, then list them (`Teton-9A2C`, signal).
2. Ask for the setup code ("printed on the device label").
3. Run the handshake (§5.1). On failure, explain and offer to retype the code.
4. Ask the device to scan, then show its networks (name, signal bars, lock icon or "enterprise") plus "Other (hidden) network".
5. Ask for credentials based on security: password, or username + password + optional CA file path + optional server domain.
6. Show progress, then success ("Connected. IP 192.168.1.23, internet OK") or the plain-language failure with an option to retry from step 4 or 5.

`--simulate` connects to the simulated device instead of BLE.

### 9.3 Phone page (COULD)

A single `web/index.html` using Web Bluetooth and WebCrypto that implements the same protocol, for Chrome on Android. No framework.

## 10. Simulation mode (SHOULD)

- Transport: a Unix domain socket at `$XDG_RUNTIME_DIR/teton-sim.sock`. It carries the same four logical channels (`info`, `session`, `command`, `status`) with the same framing and crypto, so only the radio is simulated.
- Wi-Fi: a fake backend with a fixed network list (`Home-PSK` with password `correct-horse-battery`, `Hospital-EAP` with user `nurse01` and password `teton-demo`, `Cafe-Portal` → `captive_portal`, `Weak-Signal` → `timeout`).
- Lets evaluators run the full flow in two terminals on any machine, and is used by the end-to-end test.

## 11. Logging

`structlog` JSON lines to stdout with `ts`, `level`, `event`, `device_id`, `session_id` (random per session) and `state`, all passed through the redaction processor (§5.5). Default level `INFO`; use `--verbose` for `DEBUG`.

## 12. Acceptance criteria

**MUST**
1. On two Ubuntu 24.04 laptops, a technician following the README provisions laptop A onto a phone hotspot (WPA2-PSK) using only the guided configurator.
2. The configurator shows `connected` with an IP and confirmed internet access.
3. A wrong password produces `wrong_password`, and the retry works within the same session.
4. A wrong setup code fails the handshake with a clear message. No credentials are sent.
5. A `btmon` capture of a run shows no plaintext SSID password. The capture is saved in `docs/evidence/`.
6. Unit tests pass: setup code, crypto (including tamper, replay and wrong-code cases), framing, messages, state machine, nmcli output parsing, and log redaction.
7. README, ARCHITECTURE.md (writeup) and evidence (logs and screenshots) are in the repo.

**SHOULD**
8. `--simulate` runs the full flow on one machine, and an end-to-end test runs it in CI (GitHub Actions: ruff + pytest).
9. WPA2-Enterprise PEAP is verified against `hostapd` on laptop B, with the config files in `scripts/hostapd-enterprise/`.
10. A `deploy/` systemd unit and polkit rule, plus `scripts/preflight.sh` that checks the adapter, BlueZ, NetworkManager and Python versions.

**COULD**
11. The phone page (§9.3).
12. EAP-TLS, `.deb` packaging, Prometheus metrics and a Smith enrollment stub.
