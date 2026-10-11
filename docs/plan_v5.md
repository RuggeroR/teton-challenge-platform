# Teton Challenge: plan v5

v5, 2026-10-11. Earlier versions (v1 to v3) live in the project notes, and v4 is kept in `plan_v4.md`. Their changes are summarised in the changelog below. **Status: v4 corrected by the first spec review (implementation planning); open review questions are tracked in `IMPLEMENTATION_PLAN.md`.**

## Changelog

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
| Teton Device | Laptop A (Ubuntu) | Bluetooth adapter (peripheral role), Wi-Fi via NetworkManager |
| Configurator | Laptop B (Ubuntu) | Bluetooth adapter (central role); later its Wi-Fi card can host the Enterprise test AP |
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
| Physical action window | Button press (or power-cycle) opens a 2-minute provisioning window | No, only narrows the time an attacker has | WPS-style; easy UX, weak security |
| Trust on first use (TOFU) | Accept whoever connects first, remember their key afterwards | No, the first contact is unprotected | Simple; acceptable only in a controlled room |
| Manufacturer certificate | Device has a key + certificate signed by Teton's CA at the factory; configurator verifies the signature | Yes | Strongest and scales best, but it *is* pre-shared material, just not secret to the technician |

The writeup should present the QR as the demo choice and the manufacturer certificate as the 200-device answer.

## 4. Security design (decided)

Threats: passive sniffer, active MITM / fake device, rogue configurator, replay of captured messages, evil-twin access point (Enterprise).

**Setup code (trust anchor).** At first start the device generates a random **16-character setup code** (Crockford base32, 80 bits, e.g. `7K2M-QX9D-4HTR-B8WZ`) and stores it in its state directory (mode 0600). It prints the code and a QR containing `{device_id, code}` in the terminal with the note *"For demo purposes the code is shown on screen; in production it is printed on the device label."* The technician types the code into the configurator; the QR is there for the later phone page.

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
- One session at a time; session expires after 5 minutes idle.
- After 5 failed handshakes, back off 30 s, doubling each time (no permanent lockout in the demo).
- Credentials go straight into the NetworkManager profile (root-only, 0600), are never logged, and are dropped from memory after use; a test asserts no secret appears in logs.
- Secrets never appear on a command line, where any local user could read them through `ps`. They are passed to `nmcli` through stdin; a test asserts no secret appears in argv (v5).
- Log redaction is by key name. The setup code is only ever logged under `setup_code`, so the generic key `code` (used for error codes) is not redacted (v5).

Library: `cryptography` (X25519, HKDF, HMAC, AESGCM). Browser page later: WebCrypto.

## 5. Architecture (decided)

```
 Configurator (laptop B, guided CLI)                  Teton Device (laptop A, foreground command)
 ───────────────────────────────────                  ─────────────────────────────────────────
 technician types setup code                          prints setup code + QR ("demo only")
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
        └ plain-language result / retry prompt         └─ on success: stop advertising, keep profile
```

GATT service (custom 128-bit UUID), characteristics:
- `info` (read, plaintext): device ID, protocol version, capabilities. No secrets.
- `session` (write + notify): handshake messages.
- `command` (write): encrypted commands: `scan`, `set_credentials`, `reset`.
- `status` (notify): encrypted state and results.

Messages are **JSON**, length-prefixed and split into chunks that fit the BLE packet size (MTU), then reassembled.

Failure reasons reported (each mapped to a plain-language message on the configurator): `wrong_password`, `ssid_not_found`, `auth_failed` (Enterprise), `no_ip`, `captive_portal`, `no_internet` (gateway unreachable), `timeout`.

Success rule (v5): Wi-Fi activated + IP + gateway reachable on the Wi-Fi interface. If NM's connectivity for that interface is not `full`, the result is still success, with the warning `limited_internet` ("joined the network but can't reach the internet; this may be normal on restricted networks"). Hospital networks often block Ubuntu's check URL; in production the check would target Smith's endpoint instead.

Transport interface (v5): `DeviceTransport` (`on_connect`, `on_disconnect`, `set_info`, `on_receive`, `send`, `chunk_size`) and `ClientTransport` (`connect`, `read_info`, `send`, `on_receive`, `chunk_size`, `close`). Framing is applied above both.

Device state machine: `ADVERTISING → SESSION_ESTABLISHED → CONNECTING → CONNECTED` or `FAILED(reason) → back to SESSION_ESTABLISHED for retry`. On `CONNECTED` the device stops advertising. `teton-device reset` deletes the profile, generates a new setup code and starts advertising again.

## 6. Tech stack (Python first)

Platform: **Ubuntu 24.04 LTS** on both laptops (Python 3.12, BlueZ 5.72, NetworkManager 1.46).

Device side (laptop A):
- BLE peripheral: **`bless`** (BlueZ over D-Bus). Fallback: `dbus-next` against BlueZ directly.
- Wi-Fi: **NetworkManager via `nmcli`**. Profiles: `wifi-psk`, `wifi-sae`, `wifi-eap-peap` (Could: `wifi-eap-tls`).
- Internet check: `nmcli networking connectivity check` to refresh, then the Wi-Fi interface's own value (`nmcli -g GENERAL.IP4-CONNECTIVITY device show <iface>`) + `ping -I <iface>` to the gateway (v5).
- Crypto: `cryptography`. Logging: `structlog` (JSON, secrets redacted).
- QR: `qrcode` (terminal), shown next to the typed setup code with the "demo only" note.

Configurator side:
- **Must: Python CLI on laptop B** with `bleak`, guided prompts (pick device, type setup code, pick SSID from the device's scan or enter a hidden one, enter password or Enterprise login, see result, retry on failure). Keep it non-developer friendly: no flags required, plain-language errors.
- **Could: bare-bones HTML/JS page** (single file, no framework) for Chrome on Android via Web Bluetooth. Same protocol.

Test infrastructure:
- Unit tests (pytest) for crypto, framing, state machine.
- `--simulate` mode: in-memory transport + fake Wi-Fi backend, so the whole flow runs without radios (also used in CI).
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
- **RF realities**: BLE congestion with 200 advertisers, advertise only while unprovisioned, short-lived sessions, physical proximity checks (RSSI), devices labelled by location.

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
| Q7 | After success | Stop advertising, explicit reset to re-enter |
| Q8 | Demo run mode | Foreground command; systemd unit is a Should |
| Q9 | Setup code input | Typed code; QR shown with "demo only" note |
| Q10 | Ubuntu | 24.04 LTS |

Defaults applied (no objection raised): guided terminal configurator, `nmcli`, `venv` + `pip` install, one session at a time with timeouts and handshake rate limiting.
