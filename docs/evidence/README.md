# Evidence

Hardware runs on 2026-10-19 (Step 6.3 of `docs/IMPLEMENTATION_PLAN.md`), and the BLE spike of 2026-10-11 (Step 0.2). Times are UTC, as in the logs.

## The provisioning run (SPEC §12 MUST #1–#5)

**Setup.**
- **Device:** laptop 1, Ubuntu 24.04.5, BlueZ 5.72, NetworkManager 1.46.0, Python 3.12.3, `bless` 0.3.0, running `teton-device run --verbose 2> docs/evidence/device-run.log`.
- **Configurator:** a Pixel 9, Android 17, Chrome 155.0.8059.81, running the phone page (`web/`), served from laptop 1 with `python3 -m http.server`.
- **Network:** the phone's own hotspot `Coldspot`, WPA2-Personal, with the throwaway password `test-Teton` (plan_v5 Q117).
- **Capture:** `sudo btmon -w docs/evidence/run-psk.btsnoop` on laptop 1 for the whole run.
- **Why a phone:** laptop 2's disk failed that day (plan_v5 D3).

The setup code during the run was `JME8-43X3-QC22-ECYY`. The [R][R] at the end of the run replaced it, so it no longer works; it is named here so the checks below can be repeated.

**Timeline** (from `device-run.log`):

| Time | What happened | MUST |
|---|---|---|
| 18:16:45 | Device started, service registered, **not advertising** | #1 |
| 18:17:35 | [P]: pairing window open, advertising | #1 |
| 18:18:28 | A `hello` with the wrong code `0000-0000-0000-0000`: the phone rejects the device's tag and sends nothing more | #4 |
| 18:19:08 | A new `hello` with the right code: `session_established` | |
| 18:19:12 | The device scans: 8 networks, sent encrypted to the phone | |
| 18:19:47 | A wrong password for Coldspot: `connecting`, then at 18:19:57 **`wrong_password`** (NetworkManager: "Secrets were required, but not provided"); the session stays open | #3 |
| 18:20:36 | Try again with the right password: at 18:20:40 **`connected`, IP 10.195.95.40, internet `full`**; the device saves `provisioned` and stops advertising before the phone leaves | #2 |
| 18:22:06 | [R][R]: the `teton-Coldspot` profile is deleted and a new code is made; then [Q]: power off | |

**Files:**

| File | Shows |
|---|---|
| `phone-01-wrong-code-a.png`, `-b.png` | The wrong code typed, then "The setup code is wrong, or this is not the right device…" (MUST #4) |
| `phone-02-networks.png` | "Setup code accepted." and the device's scan list, with Coldspot first; the names of neighbouring networks are blurred |
| `phone-03-wrong-password-a.png`, `-b.png` | The wrong password typed (masked), then "The Wi-Fi password was not accepted. Check it and try again." with *Try again* (MUST #3) |
| `phone-04-connected.png` | "Connected. IP 10.195.95.40, internet OK." (MUST #2) |
| `Led-red-teton-device.png` | Laptop 1's terminal: the code and QR, and the status line "LED red, last attempt failed: wrong_password" |
| `Led-green-teton-device.png` | The same terminal after the retry: "LED green" |
| `nmcli-teton.png` | `nmcli -t -f NAME,DEVICE connection show --active` right after: `teton-Coldspot` active on `wlp0s20f3`, the profile the device created |
| `device-run.log` | The device's JSON log of the run: stderr only, so the setup code shown on screen is not in it (review Q73) |
| `run-psk.btsnoop` | The Bluetooth capture of the whole run. Read it with `btmon -r docs/evidence/run-psk.btsnoop` |

**Checks** (MUST #5, review Q63, Q116). `btmon` prints packet data as hex, and our messages travel in 20-byte chunks, so a plain `grep` would prove nothing. `scripts/check_capture.py` instead rejoins the ATT values of each characteristic and searches them, as well as the raw file, in any letter case:

```bash
python scripts/check_capture.py docs/evidence/run-psk.btsnoop 'test-Teton' 'Coldspot' 'JME843X3QC22ECYY' 'JME8-43X3-QC22-ECYY'
#   42 write requests, 87 notifications, 3 read responses searched -> OK: no secret in the capture
grep -ci -e 'test-Teton' -e 'JME843X3QC22ECYY' -e 'JME8-43X3-QC22-ECYY' docs/evidence/device-run.log
#   0   (the SSID Coldspot is in the log on purpose, review Q62; it is not in the capture)
```

**Earlier runs that day** were repeated, so their files are not kept (plan_v5 Q119–Q121). The first run reported the wrong password as `timeout`: NetworkManager had asked for the key again, and `nmcli connection up` ran into its 45 s `--wait`. That run's real `nmcli` output became the fixture `tests/fixtures/nmcli/up-wrong-password-nm146.txt`, and the classifier now recognises it. The second run's screenshots were mixed up with the first run's.

## The BLE spike (Step 0.2, 2026-10-11)

| File | Shows |
|---|---|
| `spike-peripheral.log`, `spike-peripheral-2.log` | `scripts/spike/ble_peripheral.py` on laptop 1, with `bless` patched to log BlueZ's read and write options (MTU 517, the client's device path) |
| `spike-connect.btsnoop`, `spike-connect-2.btsnoop` | The `btmon` captures of those sessions with nRF Connect on the phone: no pairing (0 SMP packets), advertising ending on connect and coming back after the disconnect (plan_v5 S1–S4) |
