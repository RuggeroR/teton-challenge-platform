# Teton Wi-Fi Provisioning: Specification

Version 1.7, 2026-10-19. Derived from [plan_v5.md](plan_v5.md), which records why each decision was made. 1.1 applied the v4 → v5 changes (redaction keys, secrets off the command line, success rule, per-interface checks); 1.2 applied the post-spike simplifications S1–S4 (no `busy`, fixed 20-byte chunks, no connection tracking, exit after success); 1.3 applies D1 (device on 24.04, configurator on 24.04 or 26.04) and D2 (no text input on the device, simulated buttons and LED, pairing window, the device stays on after success); 1.4 applies the Phase 2 review decisions (Q13: device states; Q14: no dashes in the QR; Q4, Q5, Q6, Q22: handshake counting, `rate_limited`, `bad_request`, `hello` only while the window is open; Q24: `forget_all`; Q10: 45 s + 15 s; Q11: security types; Q17: autoconnect priority; Q21: reset in any state but connecting; Q23: no terminal, [S] shows the code); 1.5 applies the Phase 3 review decisions so far (Q26: `auth_failed` text; Q27, Q28: configurator-side failures and input checks; Q30: network list markers; Q31, Q32: configurator waits; Q38–Q47: the guided flow, CA file limit); 1.6 applies the Phase 5 review decisions (Q64–Q72: the simulation transport, §5.4, §10; Q73, Q74: logs on stderr, §9, §11; Q75, Q76: Ctrl+C and SIGTERM, §9; Q77: the sim state dir, §3; Q78: fake Wi-Fi delays, §10; Q65: what simulation is used for, §10, §12). 1.7 applies the Step 6.1 and 6.2 review decisions (Q90: the adapter alias, §4; Q84: Bluetooth startup errors, §9.1; Q94: the reassembly timeout counts from the last chunk, §4.1; Q91–Q93, Q99: discovery, signal and Bluetooth off on the configurator, §9.2). If this spec and the plan disagree, the plan wins. Fix the spec, and record any new decision in a new `plan_vN.md`.

Key words: **MUST** = required for the submission, **SHOULD** = planned, **COULD** = only if time allows.

---

## 1. Scope

A technician uses a **configurator** (laptop B = laptop 2) to send Wi-Fi credentials to a **Teton Device** (laptop A = laptop 1) over **Bluetooth Low Energy (BLE)**. The device joins the network, verifies internet access and reports the result back.

| ID | Constraint (from the challenge) | How this spec meets it |
|---|---|---|
| C1 | No internet, cloud relay or shared network at the start | Point-to-point BLE GATT link (§4) |
| C2 | Real Wi-Fi connection | NetworkManager joins the network, then IP and internet are checked (§8) |
| C3 | No plaintext credentials over the air | Authenticated key exchange + AES-256-GCM at the application layer (§5) |
| C4 | Usable by a non-developer | Guided terminal flow, typed setup code, plain-language errors, physical-style buttons on the device (§9) |

Out of scope for code (covered in the writeup instead): manufacturer certificates, PAKE, fleet enrollment with Smith, `.deb` packaging, metrics.

## 2. Environment

- Device (laptop A): **Ubuntu 24.04 LTS**, Python 3.12, BlueZ 5.72, NetworkManager 1.46. This is the reference platform for BlueZ peripheral mode and NetworkManager.
- Configurator (laptop B): **Ubuntu 24.04 or 26.04 LTS**. Laptop 2 runs 26.04 with Python 3.14 and newer BlueZ, NetworkManager and `hostapd` (plan_v5, D1).
- Both: a Bluetooth 4.2+ adapter. The code requires Python ≥ 3.12 and uses no language or standard-library feature newer than 3.12.
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
| Storage | `state.json` in the state dir (default `~/.local/state/teton-device/`, or `~/.local/state/teton-device-sim/` with `--simulate` so a simulation never touches a real device's state (review Q77); dir `0700`, file `0600`) holding `device_id`, `setup_code` and the `provisioned` flag (so a provisioned device doesn't advertise again after a restart) |
| Display | On `run` and after every reset, the device prints the code and a terminal QR with payload `TETON:v1;id=<device_id>;code=<code>`, where `<code>` is the normalised 16 characters without dashes (review Q14), plus the line: *"For demo purposes the code is shown on screen; in production it is printed on the device label."* The code + QR are the stand-in for the printed label |
| Rotation | The [R] reset button (§9.1) generates a new setup code |
| Input | The device never accepts typed text: no passwords, codes or commands at runtime. Its only inputs are the simulated buttons of §9.1 (plan_v5, D2) |

**Why 80 bits.** An attacker who actively poses as the configurator receives the device's confirmation tag (§5.2) and could then guess codes offline. 80 random bits makes that infeasible. A passive eavesdropper cannot attack the code at all, because it never learns the ECDH shared secret. A short PIN would require a PAKE (writeup: upgrade path).

## 4. BLE GATT interface

Service UUID: `18388b82-f3e5-40bb-a9b9-fd0f9d789b62`, included in advertisements. The device advertises only while unprovisioned **and** a pairing window is open: the window lasts 300 s from the last press of the [P] button (§9.1). When it expires, advertising stops; a session already in progress may finish.

| Characteristic | UUID | Properties | Content |
|---|---|---|---|
| `info` | `a95e3738-82e8-4b9f-bdae-54b2ad44b843` | read | Plaintext JSON (§6.1). Never contains secrets |
| `session` | `3904babf-186b-4914-b120-559051a7400d` | write, notify | Plaintext handshake messages (§5.1) |
| `command` | `8f3ed082-9bcf-4ea0-8794-785bf52deb1f` | write | Encrypted envelopes, configurator → device |
| `status` | `7e81670a-c5d4-459d-8e6d-0925e5eaa9da` | notify | Encrypted envelopes, device → configurator |

No OS-level Bluetooth pairing or bonding is used (decision Q2).

The advertisement carries the local name (`Teton-` + last 4 characters of `device_id`) and the service UUID. While it advertises, `bless` also sets the adapter's alias (the laptop's visible Bluetooth name) to the local name; stopping advertising restores it (review Q90).

### 4.1 Framing

Every message on `session`, `command` and `status` is UTF-8 JSON, framed as `[u16 big-endian payload length][payload]` (the length counts the payload only, not the 2-byte prefix), split into chunks of at most **20 bytes**. The receiver reassembles the chunks per characteristic. `info` is a single GATT read of raw JSON and is not framed.
- The chunk size is fixed at 20 bytes, the minimum every BLE link supports, so no MTU negotiation is needed (plan_v5, S2). Production would use the negotiated MTU − 3 (517 measured in the spike).
- `info` must stay under 180 bytes, so it fits in one read with the MTU that Android and BlueZ negotiate automatically.
- Maximum message size: 8192 bytes. Larger messages are an error. The limit applies to the framed envelope, where base64 of the ciphertext adds a third, so a `set_credentials` with a CA certificate allows a PEM of at most 4096 bytes (`MAX_CA_PEM_BYTES`, review Q43); a typical CA PEM is 1.2–2 KB.
- A reassembly buffer is reset when a message has made no progress for 10 s (no chunk since the last one; review Q94), and when a new session starts. Over BLE, write-with-response moves about 20–30 chunks a second, so a large message can take longer than 10 s in total.

## 5. Security protocol

Primitives (Python `cryptography`): X25519, HKDF-SHA256, HMAC-SHA256, AES-256-GCM. Binary fields in JSON are standard base64.

### 5.1 Handshake (on `session`)

1. Configurator → device:
   `{"type":"hello","v":1,"pub":<32 B>,"nonce":<16 B>}`
2. Device generates an ephemeral X25519 key and a nonce, then derives keys (§5.2) and replies:
   `{"type":"hello_reply","v":1,"device_id":"TETON-…","pub":<32 B>,"nonce":<16 B>,"confirm":<32 B confirm_d>}`
3. Configurator derives the same keys and verifies `confirm_d`, using a constant-time comparison. If it doesn't match: show "setup code is wrong or this is not the right device" and abort. Otherwise it sends:
   `{"type":"confirm","confirm":<32 B confirm_c>}`
4. Device verifies `confirm_c`. If it matches, the session is established and the device sends an encrypted `{"type":"ready"}` on `status`. If not, it sends `{"type":"handshake_failed"}` on `session` and ends the session (the attempt was already counted when its `hello` arrived, §5.4).

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

- One active session at a time, enforced by the radio: while a configurator is connected, the device does not advertise, so no other configurator can find or connect to it (plan_v5, S1). Simulation mode does the same: while a configurator is connected it refuses other connections and can't be discovered (review Q64).
- The device does not track BLE connections (plan_v5, S3). A new `hello` ends any previous session and starts a new one; retyping the setup code is simply a new `hello` on the same connection. A session also ends after 300 s without traffic, or on any protocol violation (§6.3).
- A `hello` is accepted only while the pairing window is open; otherwise it gets no reply and a current session continues. A handshake whose window closes before `confirm` arrives is dropped (review Q22). After a disconnect, BlueZ restarts advertising by itself if the pairing window is still open (the advertisement is still registered). After the window has expired, the advertisement is unregistered and nothing comes back.
- Failed handshakes: every accepted `hello` counts as a failure until its `confirm` succeeds (a wrong code never reaches `confirm`: the configurator aborts first). After 5 consecutive failures, refuse new `hello`s for 30 s, doubling with each further failure up to 600 s. The counter resets on success. No permanent lockout. A refused `hello` gets `{"type":"error","code":"rate_limited","retry_after_s":<int>}` on `session`, and a current session continues (review Q4, Q5).

### 5.5 Secret hygiene

- Passwords, PSKs, the setup code and keys are **never logged**. Log redaction applies to the keys `psk`, `password`, `setup_code`, `confirm`, `pub`, `ct` and `ca_cert_pem`, and a test enforces this. The setup code is only ever stored and logged under `setup_code`, and the QR payload is never logged. The key `code` is **not** redacted, because error messages use it (error codes such as `bad_request` must stay visible in logs). The device logs every `error` it sends with its `code` (review Q56).
- Secrets **never appear on a command line**, where any local user could read them (`ps`, `/proc/<pid>/cmdline`). Every subprocess goes through one runner, and secrets reach `nmcli` through stdin only (§8). A test with a fake runner asserts that no secret appears in any argv.
- Credentials are passed to NetworkManager and then dropped from memory (best effort in Python).
- NetworkManager stores the profile in `/etc/NetworkManager/system-connections/` (root, `0600`).

## 6. Application messages

### 6.1 `info` (read, plaintext)

```json
{"v":1,"device_id":"TETON-3F9A2C","name":"Teton-9A2C","state":"advertising",
 "caps":["wpa2-psk","wpa3-sae","wpa2-eap-peap"]}
```

`state` is the device state in lowercase: `unprovisioned`, `advertising`, `session_established`, `connecting` or `provisioned` (DESIGN §4, review Q13). A configurator normally reads `advertising`, because it can only connect while the pairing window is open.

### 6.2 Commands (configurator → device, encrypted)

| Message | Fields |
|---|---|
| `{"type":"scan"}` | none |
| `{"type":"set_credentials", …}` | `ssid` (str, 1–32 bytes), `hidden` (bool), `security` (`wpa2-psk` \| `wpa3-sae` \| `wpa2-eap-peap`; `open` is valid on the wire but out of scope and gets `unsupported_security`, review Q11), `psk` (str, 8–63, for PSK/SAE), `eap` (object, for PEAP): `identity`, `password`, optional `anonymous_identity`, `ca_cert_pem`, `domain_suffix_match` |

There is no reset command over BLE. Reset is the physical [R] button only (decision Q7, plan_v5 D2).

### 6.3 Device → configurator (encrypted, on `status`)

| Message | Fields |
|---|---|
| `{"type":"ready"}` | Session established |
| `{"type":"scan_result","networks":[…]}` | Each entry: `ssid`, `signal` (0–100), `security`. Deduplicated by SSID (the strongest kept), sorted by signal, max 30. Hidden SSIDs and networks the device can't join (open, WEP, OWE, WPA1, WPA3-Enterprise) are left out (review Q11) |
| `{"type":"status","state":"connecting"}` | Sent as soon as credentials are accepted |
| `{"type":"status","state":"connected", …}` | `ip`, `internet` (`full` \| `limited` \| `none` \| `unknown`, as reported by NetworkManager for the Wi-Fi interface), `warnings` (list: `server_not_verified`, `limited_internet`) |
| `{"type":"status","state":"failed","reason":…}` | `reason` from §7; the session stays open for a retry |
| `{"type":"error","code":…}` | `bad_request` (a decrypted command is invalid, e.g. a 5-character PSK; the session stays), `unsupported_security` (a security type not in `caps`, e.g. `open`). `rate_limited` with `retry_after_s` is sent in plaintext on `session` instead (§5.4) |

**What ends a session (review Q6).** Anything that fails before a message is proven to come from the configurator ends the session: a broken envelope, an unexpected `seq`, a decryption failure, a command before the handshake, or a `confirm` without a `hello`. Anything that fails after decryption gets `bad_request`, and the session stays open. The device handles one message at a time, so a command sent during a Wi-Fi attempt is answered after its result. After `connected` the session ends.

## 7. Failure reasons

| `reason` | Detected by | Configurator message (plain language) |
|---|---|---|
| `wrong_password` | `nmcli` reports secrets required / 4-way handshake failure | "The Wi-Fi password was not accepted. Check it and try again." |
| `ssid_not_found` | `nmcli` "No network with SSID" | "The device can't see this network. Move closer or check the name." |
| `auth_failed` | 802.1X failure on Enterprise | "The username or password was rejected by the network. Check them and try again." |
| `no_ip` | activated, but no IPv4 address within the timeout | "Connected to Wi-Fi but the network gave no address. Contact IT." |
| `captive_portal` | NM connectivity of the Wi-Fi interface = `portal` | "This network needs a login page; devices can't use it. Contact IT." |
| `no_internet` | NM connectivity is not `full` and the gateway does not answer a ping on the Wi-Fi interface | "Connected, but there is no internet access. Contact IT." |
| `timeout` | activation not done within 45 s, or verification not within 15 s (review Q10). The configurator gives up waiting after 70 s | "Connecting took too long. Try again." |

Mapping `nmcli` output to reasons is a heuristic. Unknown errors map to `timeout`, and the raw message goes to the device log only.

## 8. Device Wi-Fi behaviour

1. **Scan:** `nmcli -t -f SSID,SIGNAL,SECURITY device wifi list --rescan yes`. `SECURITY` maps as: `WPA2 802.1X` → `wpa2-eap-peap`; any other value with `WPA2` (including `WPA2 WPA3` transition mode) → `wpa2-psk`; `WPA3` alone → `wpa3-sae`; everything else is left out (review Q11).
2. **Apply:** first delete any profile already named `teton-<ssid>` (a leftover from a crash). Then create a dedicated profile `teton-<ssid>` (`nmcli connection add …`) **without secrets**, bound to the Wi-Fi interface, with `connection.autoconnect no` and `connection.autoconnect-priority 10` (review Q17), with security set per §6.2. For PEAP: `802-1x.eap peap`, `802-1x.phase2-auth mschapv2`, plus `802-1x.ca-cert` and `802-1x.domain-suffix-match` when provided. If neither is provided, add the warning `server_not_verified` (decision Q3). Then set the secret (`wifi-sec.psk` or `802-1x.password`) by piping `set …`, `save`, `quit` into `nmcli connection edit id teton-<ssid>` through stdin (§5.5).
3. **Activate:** `nmcli --wait 45 connection up id teton-<ssid>`. Verification (step 4) then has its own 15 s limit (review Q10). Every command runs with `LC_ALL=C`, so the error texts the reasons are mapped from are always English.
4. **Verify,** always on the Wi-Fi interface: device state `activated` → IPv4 address present (else `no_ip`) → `nmcli networking connectivity check` to refresh, then read the interface's own value with `nmcli -t -f GENERAL.IP4-CONNECTIVITY,IP4.ADDRESS,IP4.GATEWAY device show <iface>` → `portal` fails with `captive_portal` → if not `full`, ping the gateway with `ping -I <iface> -c 2 -W 2` (no answer fails with `no_internet`).
5. **Success:** activated + IP + (`full`, or gateway reachable). If connectivity is not `full`, add the warning `limited_internet` ("joined the network but can't reach the internet; this may be normal on restricted networks"). Report `connected`, keep the profile, set it to autoconnect (`connection.autoconnect yes`), mark the device provisioned, then close the pairing window, which stops advertising before the configurator disconnects. The device stays on in `PROVISIONED` (plan_v5, D2, replacing the exit of S4).
6. **Failure:** report the reason (§7), delete the new profile, and let NetworkManager return to the previous connection if any. The BLE session stays open for a retry.

All `nmcli` calls go through one backend interface, so tests and simulation use a fake.

## 9. User interfaces

### 9.1 Device CLI: `teton-device`

The device has no keyboard input beyond a few simulated physical buttons and a status "LED" (plan_v5, D2).

| Command | Behaviour |
|---|---|
| `teton-device run` | Load or create the state, print the code + QR + demo note, then wait for buttons. **An unprovisioned device does not advertise until [P] is pressed.** Serves sessions while the pairing window is open. The screen (code, QR, LED, messages) is on stdout and the logs on stderr (review Q73). After a successful provisioning it stops advertising and stays on in `PROVISIONED` (green LED). Started on an already provisioned device, it stays on without advertising. Exits only on [Q], Ctrl+C or SIGTERM (review Q76). If Bluetooth can't start (no adapter, `bluetoothd` not running), it prints one line and exits 1 (review Q84) |
| `teton-device run --simulate` | Same, with the same buttons, but over a local Unix socket with the fake Wi-Fi backend (§10) |
| `--state-dir PATH` | Override the state directory (default §3) |
| `--verbose` | `DEBUG` logging (§11) |

There are no `reset` or `show-code` subcommands: reset is the [R] button, and the code + QR are printed on start and after every reset.

**Buttons.** While `run` is active, single keypresses (no Enter) act as buttons:

| Key | Button | Behaviour |
|---|---|---|
| `P` | Pairing | Opens a 300 s pairing window and starts advertising. Pressing it again restarts the window at 300 s. When the window expires, advertising stops; a session in progress may finish. On a provisioned device it only shows "Already connected. Hold reset to set up again." |
| `R` | Reset | Needs **two presses within 3 s** (a simulated long press); a single press shows "Press reset again to confirm." Then: delete the Teton NetworkManager profiles, i.e. every profile named `teton-*` (`WifiBackend.forget_all()`, review Q24), generate a new setup code, print the new code + QR, and return to `UNPROVISIONED` with advertising off until [P]. Works in every state except while connecting, where it shows "Busy connecting. Try again in a moment."; a pairing window or session in progress is closed (review Q21) |
| `S` | Status | Prints the state, SSID, IP, internet result and the remaining pairing time, then the setup code + QR again, because log lines scroll them away (review Q23) |
| `Q` or Ctrl+C | Power | Clean shutdown: stop advertising, close the transport, restore the terminal, exit 0. SIGTERM does the same (review Q76) |

Keys are case-insensitive; other keys are ignored, and so are escape sequences such as arrows or F1 (`ESC O P`), even though they contain button letters. Without a terminal (systemd, redirected stdin) there are no buttons: the device logs a warning and stays on without advertising (review Q23).

**Status line ("LED").** Always visible at the bottom of the terminal:

| LED | Meaning |
|---|---|
| off | idle / unprovisioned, pairing window closed |
| blinking blue | pairing window open, with the countdown (`4:59`) |
| yellow | connecting to Wi-Fi |
| solid green | connected (provisioned) |
| red | the last Wi-Fi attempt failed, with the reason |

### 9.2 Configurator CLI: `teton-configurator`

A guided flow with numbered choices and no flags needed:
1. Tell the technician to press the pairing button on the device, then scan for devices for 10 s and list them, numbered (`Teton-9A2C`, signal bars); Enter picks the only one. If none is found: "No device found. Press the pairing button on the device and try again.", then Enter looks again (review Q39).
2. Ask for the setup code ("printed on the device label").
3. Run the handshake (§5.1). A badly formatted code is explained and asked again without sending anything; a wrong code is explained and retyped (a new `hello`); rate limited shows how many seconds to wait, then asks again (review Q40).
4. Ask the device to scan, then show its networks (name, four-level signal bars `▂▄▆█`, and "password" or "enterprise"; review Q30) plus "Other (hidden) network" and "Scan again" (review Q41). A hidden network asks for its name and a security type from `info.caps`: WPA2/WPA3 Personal, WPA3 Personal only, or WPA2 Enterprise (review Q42).
5. Ask for credentials based on security: the password (hidden while typed), or username + password + optional CA file path + optional server domain. The CA file must be a PEM certificate of at most 4096 bytes (review Q43).
6. Show one progress line, then success ("Connected. IP 192.168.1.23, internet OK." plus any warnings) or the plain-language failure with: try again (retype the password), pick another network, or quit (review Q44, Q47). No reply, a lost connection or an unexpected reply end the run (exit 1); the technician starts again.

Typing q quits at any menu, and so does the end of input (review Q46). PEAP without a CA or domain gets only the device's `server_not_verified` warning after connecting (review Q45).

Every text the technician sees lives in `teton_configurator/ui_text.py` and is one sentence plus a next action. Besides the device's reasons (§7) and error codes (§6.3), the configurator has texts for what it finds itself (review Q27): a wrong code or wrong device, no reply to `hello` (the pairing window is closed, §5.4), no result within 70 s (different from `timeout`: the device always finishes and may have connected, so the text points at its light), a lost connection, and an unexpected reply. It checks input before sending anything (review Q28): the setup code format (a mistyped code sends no `hello`, so it isn't counted by the rate limit), a password of 8–63 characters, a network name of 1–32 bytes, and a non-empty username and password.

How long the configurator waits (review Q31, Q32): 10 s for each handshake reply (no reply means the pairing window is closed: "Press the pairing button on the device and try again."), 40 s for a scan result, 10 s for `connecting` after `set_credentials`, then 70 s for the result (Q10).

Over BLE the device signal is `2 × (RSSI + 100)` clamped to 0–100, the scale NetworkManager uses for Wi-Fi (review Q91). If Bluetooth is off or missing on the configurator, it says so in one sentence and exits 1 (review Q99). `--simulate` connects to the simulated device instead of BLE. `--verbose` shows `DEBUG` logs (on stderr). Ctrl+C stops at any point: the connection is closed, the terminal settings are restored and the exit code is 130 (review Q46, Q75).

### 9.3 Phone page (COULD)

A single `web/index.html` using Web Bluetooth and WebCrypto that implements the same protocol, for Chrome on Android. No framework.

## 10. Simulation mode (SHOULD)

- Transport: a Unix domain socket at `$XDG_RUNTIME_DIR/teton-sim.sock`, or `<tempdir>/teton-sim-<uid>.sock` when that variable is unset, with mode `0600` (review Q69). It carries the same four logical channels (`info`, `session`, `command`, `status`) with the same framing, 20-byte chunks and crypto, so only the radio is simulated.
- Wire format (review Q66, Q67): the socket is a byte stream, so each chunk travels as one record `[u8 channel][u16 big-endian length][chunk]` (channels: 0 `info`, 1 `session`, 2 `command`, 3 `status`). This sits **under** the normal framing (§4.1). `info` is a request (an empty record from the configurator) and a response (the info JSON, at most 180 bytes). An unknown channel, a length over the limit (20 bytes, 180 for `info`), a wrong direction, a non-empty info request or a short read drops the connection, with one log line that never contains the bytes.
- One configurator at a time, like BLE (review Q64): new connections are accepted only while the pairing window is open (the simulated "advertising") and no configurator is connected; any other connection is closed at once. `connect()` reads `info` once, so a refused connection fails there, and discovery lists the device only while it accepts connections. A configurator leaving waits for the device to drop the connection, so the next one is never refused by mistake (review Q70).
- Start and stop (review Q68): a leftover socket file nobody listens on is removed at start; if another simulated device is listening, start fails ("another simulated device is already running"); a path that isn't a socket is never touched. The file is removed on power off.
- Buttons and LED: the same as on BLE (§9.1).
- Wi-Fi: a fake backend with a fixed network list (`Home-PSK` with password `correct-horse-battery`, `Hospital-EAP` with user `nurse01` and password `teton-demo`, `Cafe-Portal` → `captive_portal`, `Weak-Signal` → `timeout`). A scan takes 2 s and an attempt 3 s, so the yellow LED and the progress line are visible (review Q78); `Weak-Signal` waits the full 45 s.
- Lets evaluators run the full flow in two terminals on any machine. The in-process end-to-end tests use the memory transport instead; a two-process smoke test runs the simulation (review Q65, Q80).

## 11. Logging

`structlog` JSON lines to **stderr** with `ts`, `level`, `event`, `device_id`, `session_id` (random per session) and `state`, all passed through the redaction processor (§5.5). The device's default level is `INFO` and the configurator's `WARNING`, so log lines don't break up the technician's guided text; `--verbose` gives `DEBUG` on both (review Q73, Q74). stdout is the device's screen and the configurator's guided text.

## 12. Acceptance criteria

**MUST**
1. With laptop A on Ubuntu 24.04 and laptop B on Ubuntu 24.04 or 26.04, a technician following the README provisions laptop A onto a phone hotspot (WPA2-PSK) using only the guided configurator and the device's [P] button. The device advertises nothing until [P] is pressed, and stops when the pairing window expires.
2. The configurator shows `connected` with an IP and confirmed internet access.
3. A wrong password produces `wrong_password`, and the retry works within the same session.
4. A wrong setup code fails the handshake with a clear message. No credentials are sent.
5. A `btmon` capture of a run shows no plaintext SSID password. The capture is saved in `docs/evidence/`.
6. Unit tests pass: setup code, crypto (including tamper, replay and wrong-code cases), framing, messages, state machine, nmcli output parsing, log redaction, and the buttons (pairing window open / expire / restart, reset needing a double press, [P] ignored when provisioned).
7. README, ARCHITECTURE.md (writeup) and evidence (logs and screenshots) are in the repo.

**SHOULD**
8. `--simulate` runs the full flow on one machine, and a two-process smoke test runs it in CI (GitHub Actions: ruff + pytest on Python 3.12 and 3.14).
9. WPA2-Enterprise PEAP is verified against `hostapd` on laptop B, with the config files in `scripts/hostapd-enterprise/`.
10. A `deploy/` systemd unit and polkit rule, plus `scripts/preflight.sh` that checks the adapter, BlueZ, NetworkManager and Python versions.

**COULD**
11. The phone page (§9.3).
12. EAP-TLS, `.deb` packaging, Prometheus metrics and a Smith enrollment stub.
