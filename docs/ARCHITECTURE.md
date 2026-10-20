# Architecture and design writeup

Wi-Fi provisioning for a headless Ubuntu "Teton Device" over Bluetooth Low Energy, with credentials encrypted end to end at the application layer.

This is the submission writeup. It explains what was built, why each choice was made, and what would change for a fleet. The detailed documents behind it are [SPEC.md](SPEC.md) (what), [DESIGN.md](DESIGN.md) (how) and [plan_v5.md](plan_v5.md) (every decision with its reason, numbered Q1–Q121). The run evidence is in [evidence/](evidence/README.md).

## Contents

1. [Summary](#1-summary)
2. [Problem and constraints](#2-problem-and-constraints)
3. [Channel choice: BLE GATT](#3-channel-choice-ble-gatt)
4. [Trust bootstrap: how the configurator knows it talks to the right device](#4-trust-bootstrap)
5. [Protecting credentials in transit](#5-protecting-credentials-in-transit)
6. [System design](#6-system-design)
7. [Joining Wi-Fi and reporting back](#7-joining-wi-fi-and-reporting-back)
8. [Testing and evidence](#8-testing-and-evidence)
9. [Known limitations](#9-known-limitations)
10. [Scaling to 200 devices in a hospital wing](#10-scaling-to-200-devices-in-a-hospital-wing)
11. [Fleet and OTA notes](#11-fleet-and-ota-notes)

## 1. Summary

- **Channel:** BLE GATT. The device is a GATT server (BlueZ via `bless`), and the configurator is a GATT client: the Python CLI with `bleak` on a laptop, or a web page in Chrome on Android using Web Bluetooth.
- **Trust:** each device has a random 80-bit setup code, printed on its label (shown on screen in the demo). The technician types it on the configurator. The code salts the key derivation, so only someone holding it can complete the handshake.
- **Crypto:** ephemeral X25519 key exchange, HKDF-SHA256 salted with the setup code, HMAC key confirmation in both directions, then AES-256-GCM with per-direction keys and sequence numbers. It uses only the `cryptography` library in Python and WebCrypto in the browser.
- **Physical gate:** the device advertises only for 5 minutes after someone presses its pairing button [P], and it never accepts typed input.
- **Feedback:** the device joins Wi-Fi through NetworkManager, checks for an IP address, the interface's connectivity and the gateway, then reports `connected` (IP, internet status) or `failed` with one of seven reasons. Each reason becomes one plain-language sentence for the technician, and the same session allows a retry.
- **Proven on hardware** (2026-10-19): laptop 1 (Ubuntu 24.04) as the device and an Android phone running the phone page as the configurator. A wrong setup code was rejected. A wrong hotspot password gave `wrong_password` and a retry in the same session. The correct password gave `connected` with internet `full`. A `btmon` capture of the whole run contains no password, SSID or setup code ([evidence](evidence/README.md)).
- **Tests:** about 800 pytest tests, including Node tests for the phone page. They run without hardware, using an in-memory transport, a Unix-socket simulation and a fake Wi-Fi backend.

## 2. Problem and constraints

A configurator (a phone or a second laptop) must send Wi-Fi credentials to an Ubuntu device that then joins a real network. The challenge sets four hard constraints:

| # | Constraint | What it rules in or out |
|---|---|---|
| C1 | No internet, cloud relay or shared LAN at the start | A point-to-point channel: BLE, a SoftAP, optical, NFC, audio or USB |
| C2 | A real Wi-Fi connection | Drive NetworkManager and verify association, DHCP and reachability, not just "the command returned 0" |
| C3 | No plaintext credentials over any wireless channel | Encryption with an *authenticated* key exchange. BLE "Just Works" pairing alone doesn't qualify |
| C4 | Usable by a non-developer | Guided steps, plain-language errors, no flags or terminals on the configurator side |

The role is Software Engineer, Fleet, so I also treated "what happens at 200 devices" and "how does this hand off to the fleet platform" as first-class questions (§10, §11).

## 3. Channel choice: BLE GATT

The full comparison of ten channels is in [research/channels.md](research/channels.md).

**Why BLE.** It is the only option that meets all four constraints on stock laptop hardware **and** keeps a live feedback path while the device joins Wi-Fi. The setup link doesn't borrow the Wi-Fi radio, so the device can tell the technician *why* a join failed and take a corrected password in the same session. Phones support it natively, and Chrome on Android drives it from a web page with no app install, which is what rescued the hardware run when the second laptop's disk failed (§9). It is also the industry default for headless onboarding (Matter commissioning, ESP-IDF provisioning, HomeKit).

**What it costs.** Range is a few metres, which in a hospital is arguably a feature. Throughput is small, so messages are split into 20-byte chunks. Peripheral mode on Linux depends on the adapter and BlueZ version, so the riskiest part was tested first: a hardware spike on day one, before any protocol code.

**Alternatives considered and rejected:**

| Option | Why not here | Where it would win |
|---|---|---|
| SoftAP + captive portal | A single radio has to leave the setup network exactly when it tries to join the target, so the technician gets no failure feedback. It also creates a temporary open-ish network, and a rogue-AP alarm in a hospital | Zero-app consumer devices |
| Wi-Fi Easy Connect (DPP) | Not exposed by NetworkManager on Ubuntu (raw `wpa_supplicant` only) and driver-dependent. Android's configurator can't send Enterprise credentials | The production direction once Linux support matures. A strong standard |
| WPS | The PIN mode is broken, the push button is unauthenticated, it's disabled on enterprise APs and deprecated with WPA3 | Nowhere |
| Optical QR (configurator shows, device camera reads) | Laptop webcams are unreliable, and it's one-way with no feedback | Teton's camera-equipped devices: a good fit for a later version, with BLE kept for feedback |
| NFC | No reader on the laptops | Future hardware: tap to bootstrap trust, BLE for the data |
| Classic Bluetooth, Wi-Fi Direct, audio, USB | Legacy, fragile on Linux, weak, or not wireless | USB as a factory or recovery path |

## 4. Trust bootstrap

Encryption alone isn't enough. An unauthenticated key exchange can be man-in-the-middled, so the configurator needs a way to know it's talking to *this* device, and the device needs a way to know the configurator is allowed to configure it.

**Demo choice: a per-device setup code.** On first start the device generates 16 Crockford base32 characters (80 random bits, e.g. `7K2M-QX9D-4HTR-B8WZ`) and stores them with mode `0600`. It shows the code and a QR code with the note *"for demo purposes the code is shown on screen; in production it is printed on the device label"*. The technician types it on the configurator (the input is case-insensitive, ignores dashes and maps `O→0`, `I/L→1`). The device itself never accepts typed text.

**Why 80 bits and not a 6-digit PIN.** An attacker who poses as the configurator receives the device's confirmation tag and can then guess codes offline. With 80 random bits that is infeasible. A 6-digit PIN would need a PAKE (SPAKE2 or SRP) to be safe, which is the documented upgrade path. A passive eavesdropper can't attack the code at all, because it never learns the ECDH secret.

**On top: a physical pairing window.** The device advertises only for 300 s after [P] is pressed. On its own a window doesn't stop a MITM, but it shrinks the attack surface: an attacker needs the code *and* must be in radio range during the few minutes after someone physically touched the device. A double press of [R] resets the device and **rotates the code**, which is the answer to "the label was photographed".

**Alternatives for devices without pre-shared material:**

| Option | MITM-safe? | Trade-off |
|---|---|---|
| Numeric comparison (both screens show 6 digits) | Yes, if the human really compares | Needs a display; people click "yes" without looking |
| Passkey shown by the device, typed on the configurator | Yes | Needs a display; 20 bits, so it must be one-time and rate-limited |
| Physical window only | No | WPS-like; easy but weak |
| Trust on first use | No | Acceptable only in a controlled room |
| **Manufacturer certificate** (device key + certificate signed by a Teton CA) | Yes | Strongest, and scales best. It's pre-shared material, but not secret to the technician. **The answer for 200 devices** (§10) |

## 5. Protecting credentials in transit

### 5.1 Handshake and session

Over the plaintext `session` characteristic:

```
configurator                                              device
hello {pub_c, nonce_c}  ───────────────────────────────►  new X25519 key, nonce_d
                        ◄───────────────────────────────  hello_reply {device_id, pub_d, nonce_d, confirm_d}
verify confirm_d (constant time); wrong → abort, send nothing
confirm {confirm_c}     ───────────────────────────────►  verify confirm_c → session established
                        ◄───────────────────────────────  enc(ready)
```

```
transcript = SHA256("teton-prov-v1" || device_id || pub_c || pub_d || nonce_c || nonce_d)
okm        = HKDF-SHA256(ikm = X25519 shared secret, salt = setup code, info = transcript, 96 bytes)
k_c2d | k_d2c | k_confirm = okm
confirm_d  = HMAC(k_confirm, "device" || transcript),  confirm_c = HMAC(k_confirm, "configurator" || transcript)
```

After that, every command and status is an envelope `{"seq", "ct"}`:
- AES-256-GCM with a separate key for each direction;
- the nonce is built from `seq`;
- the AAD is `teton-prov-v1|c2d|<seq>` (or `d2c`);
- the receiver accepts only the next `seq`.

Replay, reordering and reflection all fail decryption, and a decryption failure ends the session.

- **Why X25519 + HKDF instead of BLE pairing.** LE pairing in "Just Works" mode has no MITM protection, and passkey or numeric comparison need a display on the device. Pairing also brings OS dialogs and bonding state. App-layer crypto works the same over BLE, a socket or a web page, and is testable in pytest. The BLE spike captures show 0 SMP (pairing) packets.
- **Why not a PAKE.** With 80 bits of entropy, a salted HKDF plus key confirmation is enough, and every primitive is in `cryptography` and WebCrypto. A PAKE only becomes necessary with short codes.
- **Forward secrecy.** The keys are ephemeral per session, so a setup code that leaks later doesn't decrypt recorded sessions. The attacker would still need the ECDH secret.
- **The same protocol in two languages.** The phone page reimplements the client side in JavaScript (WebCrypto only, no crypto library). It is tested against vectors generated by the Python code (fixed keys, transcripts, both confirmations, envelopes both ways), so the two can't drift.

### 5.2 Threats

| Threat | What stops it |
|---|---|
| Passive sniffer near the device | Everything after the handshake is AES-GCM. The handshake carries only public keys and nonces. The hardware capture was checked by rejoining the 20-byte chunks per characteristic, with 0 hits for password, SSID or code (`scripts/check_capture.py`) |
| Fake device (evil twin of the Teton device) | It can't compute `confirm_d` without the code, so the configurator aborts **before sending anything** (shown on hardware, MUST #4) |
| Rogue configurator or online guessing | It can't compute `confirm_c`. Handshakes are accepted only while the pairing window is open. After 5 failures new handshakes are refused for 30 s, doubling to 600 s |
| Offline guessing from a captured `confirm_d` | 80-bit code; infeasible |
| Replay, reorder, reflection | Per-direction keys, a strict `seq`, and the direction in the AAD |
| Tampering | GCM authentication; any failure ends the session |
| Secrets leaking on the device | No secret on a command line (`nmcli` gets them through stdin, enforced by a test on argv). Logs pass through a redaction processor, enforced by a test that renders a full run and searches for every secret. NetworkManager stores the profile root-only (`0600`) |
| Evil-twin Wi-Fi access point (Enterprise) | PEAP takes an optional CA certificate and domain match. Without them the device connects but the technician sees a "couldn't verify the network" warning. Production policy would refuse |

**Out of scope, deliberately:**
- Radio jamming or denial of service.
- A compromised configurator laptop or phone.
- Someone who has the setup code. That's the trust anchor by design.
- An evil twin of a PSK network: anyone with the PSK can be the AP. That's a property of WPA2-Personal.
- Physical attacks on the device's storage (production: TPM or secure element, §11).

## 6. System design

### 6.1 Components

```
Configurator (laptop CLI or phone page)            Teton Device (laptop A)
  flow.py / index.html   guided steps, texts         cli.py → service.py   orchestrator, buttons, LED
  client.py / protocol.js  handshake, commands       session.py            handshake, rate limit, dispatch
  teton_common: crypto, framing, messages            state.py, storage.py  5-state machine, state.json
  ble_client (bleak) | Web Bluetooth | sim socket    gatt_server (bless) | sim socket
                                                     wifi/: WifiBackend → nmcli.py | fake.py
```

- **One protocol implementation, many transports.** The handshake, crypto, framing and messages live in `teton_common` and never know whether the bytes go over BLE, a Unix socket (`--simulate`) or memory (tests). The transport interface is deliberately thin: start, advertise on/off, `info`, send and receive 20-byte chunks. One contract test suite runs against each implementation.
- **Side effects behind interfaces.** The radio, Wi-Fi (`WifiBackend`), buttons, the clock and randomness are injected. The device's whole behaviour runs in pytest, including the 5-minute window and the 45 s timeouts, with no real sleeps.
- **The device is the authority.** It owns the state machine, verifies everything and decides success. The configurator is a thin client, which is why a second, very different configurator (the phone page) could be added in a day without touching the device.
- **Small, boring choices.** JSON messages, fixed 20-byte chunks (the minimum every BLE link supports), `nmcli` instead of D-Bus, a terminal status line as the "LED". Each is easy to explain and debug. §9 says what production would change.

### 6.2 Device state machine

```
UNPROVISIONED ──[P]──► ADVERTISING ──handshake ok──► SESSION_ESTABLISHED ──set_credentials──► CONNECTING
      ▲                    │                                ▲                                     │
      │          window expires, no session                 └───────── failed(reason) ────────────┤
      │                    ▼                                                                      │ ok
      └──────────── UNPROVISIONED                         PROVISIONED ◄───────────────────────────┘
      └──────────────────────[R][R] reset: delete Teton profiles, new code ◄───────── PROVISIONED
```

There are five states and nothing else:
- A failure isn't a state: the device goes back to `SESSION_ESTABLISHED`, and the LED shows the reason in red.
- The pairing window is a timer, not a state. When it expires mid-session, the session may still finish.
- On success the device sends `connected`, saves `provisioned` and stops advertising **before** the configurator disconnects. The spike showed that BlueZ re-enables advertising on disconnect otherwise.
- After that the device stays on with a green LED. A restart doesn't advertise again; only [R][R] re-enters setup.

**One configurator at a time is enforced by the radio, not by code.** A connected BLE peripheral stops advertising, so a second configurator can't find the device. The simulation copies that behaviour. A new `hello` simply starts a new session, which is also how "retype the code" works.

## 7. Joining Wi-Fi and reporting back

1. **Scan** with `nmcli … device wifi list --rescan yes`, then deduplicate, sort by signal and cap at 30. Networks the device can't join (open, WEP, OWE, WPA3-Enterprise) are left out. The list travels encrypted.
2. **Apply** by creating a dedicated profile `teton-<ssid>` *without* secrets, then piping the secret into `nmcli connection edit` through stdin. PEAP gets MSCHAPv2, plus the CA and domain match when given.
3. **Activate** with `nmcli --wait 45 connection up`, with `LC_ALL=C` so the error text can be classified.
4. **Verify on the Wi-Fi interface:** an IPv4 address, then the interface's own connectivity (`full`, `limited`, `portal`, `none`), then a ping to the gateway on that interface when it isn't `full`.
5. **Decide.** Success means activated + IP + (full internet, or a reachable gateway). Less than `full` is still success, with a `limited_internet` warning, because hospital networks often block Ubuntu's check URL. A portal or an unreachable gateway is a failure.
6. **On failure**, delete the new profile so NetworkManager falls back to the previous connection, and keep the BLE session open for a retry. **On success**, set the profile to autoconnect and mark the device provisioned.

| Reason | Detected from | What the technician reads |
|---|---|---|
| `wrong_password` | NM "secrets required", or NM asking for the key again (seen on NM 1.46 hardware, Q119) | "The Wi-Fi password was not accepted. Check it and try again." |
| `ssid_not_found` | "No network with SSID" | "The device can't see this network. Move closer or check the name." |
| `auth_failed` | 802.1X failure | "The username or password was rejected by the network. Check them and try again." |
| `no_ip` | Activated but no IPv4 | "Connected to Wi-Fi but the network gave no address. Contact IT." |
| `captive_portal` | Interface connectivity `portal` | "This network needs a login page; devices can't use it. Contact IT." |
| `no_internet` | Not `full` and the gateway is silent | "Connected, but there is no internet access. Contact IT." |
| `timeout` | Activation over 45 s or verification over 15 s; unknown errors | "Connecting took too long. Try again." |

All the texts live in one file (`ui_text.py`), which the phone page copies under test. Another test fails if any reason, error code or warning has no text, or if a text contains jargon such as "nmcli", "GATT" or "exception".

## 8. Testing and evidence

| Layer | How it's tested |
|---|---|
| Crypto, framing, messages, setup code, state machine, buttons | Unit tests with injected clocks: tamper, replay and wrong-code cases; window open, expire and restart; reset needing a double press |
| Wi-Fi backend | `nmcli` output fixtures (including real NM 1.46 output from the hardware run); a fake runner proves no secret ever reaches argv |
| Device and configurator together | End-to-end tests with the real `DeviceService` and the real guided flow over the in-memory transport: PSK, PEAP with and without a CA, hidden network, wrong password then retry, wrong code then retype, portal, rate limit, window expiry |
| No secrets in logs | One full run rendered by the real logging setup at DEBUG; asserts that no password, setup code, key (caught by spies in the crypto module), ciphertext or CA line appears, and that error codes and SSIDs stay visible |
| Transports | One contract suite against the memory and socket pairs; the BLE transports against fakes shaped like `bless` and `bleak` |
| Phone page | Node runs `protocol.js` against vectors generated by the Python code, and runs the page's client against the real device service over the simulation socket |
| Hardware | The BLE spike, BLE transport checks with nRF Connect, and the provisioning run with the phone. See [evidence/README.md](evidence/README.md) |

`--simulate` runs the whole product in two terminals on any Linux machine. It's the fastest way for a reviewer to try it (see the [README](../README.md)).

## 9. Known limitations

Stated plainly, because they matter more than the polish:

- **The laptop configurator never ran against real hardware.** Laptop 2's disk failed on the day of the hardware runs. `ble_client.py` is unit-tested against a `bleak`-shaped fake and the CLI is tested end to end over the simulation socket, but no real BLE run. The phone page took its place and ran the full flow on hardware.
- **WPA2-Enterprise (PEAP) is tested only in software**, with the fake backend and `nmcli` fixtures. The `hostapd` test AP needed the lost laptop. The phone page doesn't offer Enterprise or hidden networks; the laptop CLI does.
- **Not tested: Python 3.14 and Ubuntu 26.04 on the configurator side. There's also no CI** (deferred with the rest of Phase 7 to get the hardware run done first). The code uses nothing newer than 3.12.
- **Demo-grade operations:**
  - The device runs as a foreground command as the desktop user, with polkit's defaults; no systemd unit or dedicated service user.
  - The setup code is shown on screen instead of a label.
  - The rate limiter lives in memory, so a restart resets it.
- **The phone page is served over plain HTTP** from laptop A with a Chrome flag. Anyone on that network could swap the page. Production would serve it over HTTPS or ship an app. Loading it is a one-time install step on the home network; the provisioning itself shares no network between phone and device (C1): the tab stays open, the phone runs the hotspot, and everything goes over BLE.
- **Slow by choice.**
  - 20-byte chunks make a scan list or a CA certificate take seconds. Production would use the negotiated MTU (517 measured).
  - A wrong password can take up to the 45 s `--wait` on some NM paths before it's reported.
- **Failure reasons come from parsing `nmcli` text**, which is a heuristic. Unknown errors become `timeout`. Production would talk to NetworkManager over D-Bus and use its numeric state reasons.
- **Python memory hygiene is best effort:** credentials are dropped after use, but Python can't reliably wipe memory.

## 10. Scaling to 200 devices in a hospital wing

The demo flow is "one technician, one device, type a code, type a password". At 200 devices run by a non-technical team, three things break first:
- typing a 16-character code and a Wi-Fi password 200 times;
- the technician seeing the hospital's Wi-Fi secret at all;
- nobody knowing which of the 200 devices failed and why.

What I would change, in order:

1. **Identity from the factory, not a typed code.** Each device gets a key pair and a certificate signed by a Teton device CA at manufacture, with the private key in a TPM or secure element. The configurator verifies the certificate during the handshake instead of salting with a typed code. The handshake shape stays the same: ECDH plus confirmation, now with a signature. The device in turn verifies that the configurator holds a Teton-issued operator credential. The label carries only the device ID and a QR code for *which* device, not a secret.
2. **Pre-staged site profiles, so nobody types the Wi-Fi password.** IT creates a "site profile" once in Smith: SSID, EAP settings, the CA certificate, and a Smith enrollment token. The configurator app downloads it encrypted ahead of time and forwards it to each device. On Enterprise networks, per-device certificates (EAP-TLS) replace a shared password, so one compromised device doesn't leak the hospital's credentials, and revoking one device doesn't touch the others.
3. **A batch flow in the app.** The technician walks the wing scanning each device's QR code (or picks devices by RSSI with the room number shown). The app provisions several devices in parallel, since one BLE central can hold a handful of connections, and queues the rest. In the hardware run the join itself took about 4 s after the password was sent, so radio time per device is dominated by the human steps, not the protocol. The pairing button stays: only the devices being set up right now advertise, so 200 devices don't flood the 2.4 GHz band with advertisements.
4. **An operator dashboard instead of terminal output.** Every device shows pending, connecting, connected or failed, with the same plain-language reason and next action as today. Retries can be done from the list. An audit log records who provisioned which device, in which room, when, and with which profile. No secret is ever displayed.
5. **Bulk alternatives, depending on the site:**
   - *Zero-touch:* devices ship with a staging profile for a provisioning SSID that IT sets up with 802.1X and per-device certificates. The device joins it, enrolls with Smith, and Smith pushes the real configuration. No technician interaction beyond powering on.
   - *Wi-Fi Easy Connect (DPP)* with QR bootstrapping, once NetworkManager and the drivers support it well.
   - *Provisioned peers:* a device that's already online fetches the profile for its neighbours over BLE, as Matter and Thread commissioners do.
6. **Hospital-network realities:**
   - MAC address allowlisting: the device reports its MAC so IT can pre-register it.
   - Restricted egress: the connectivity check should target Smith's endpoint, not Ubuntu's URL.
   - Proxies, hidden SSIDs, 2.4 vs 5 GHz and DFS channels.
   - Captive portals, already detected and reported as such.

The protocol already supports most of this: the transport is pluggable, `set_credentials` carries Enterprise settings and a CA, and the failure reasons are the ones a dashboard would show. The big changes are identity (certificates instead of a typed code) and where credentials come from (Smith instead of the technician's keyboard).

## 11. Fleet and OTA notes

- **The hand-off to Smith.** Provisioning ends when the device is on Wi-Fi, but that's when fleet management begins. The next step is an enrollment call to Smith (MQTT over TLS, or HTTPS) with the device ID, the provisioning result, which network, the RSSI and who provisioned it. Smith then owns credential rotation: it pushes a new profile over the management channel, so no technician has to revisit the device.
- **Running it as a service.** A hardened systemd unit (`ProtectSystem=strict`, `NoNewPrivileges`, `CapabilityBoundingSet=CAP_NET_ADMIN`, D-Bus access to BlueZ and NetworkManager only), `Type=notify` with a watchdog, and a dedicated user with a polkit rule instead of root. The buttons and LED would map to real GPIO. Packaging would be a `.deb` (or part of the image build), installing the unit and the polkit rule.
- **OTA.** With A/B root filesystems (RAUC or Mender), Wi-Fi profiles and the device identity live on the persistent data partition, so they survive updates and rollbacks. A health check after an update must include "still on Wi-Fi and reachable from Smith"; if that fails, it rolls back automatically.
- **Observability.** Count provisioning attempts by result and reason, and record handshake and join latency and RSSI. Expose them as Prometheus metrics, or forward them to Smith. Logs are already structured JSON with secrets redacted, so they can be shipped as they are.
- **Re-provisioning in the field.** Today it's a physical double press of [R], which deletes the Teton profiles and rotates the code. For a fleet, Smith should also trigger re-entry into setup mode remotely, for example when moving a device to another wing. It would still require a physical button press to open the pairing window, so remote access alone can never expose the device over BLE.
