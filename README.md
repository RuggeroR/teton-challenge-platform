# teton-challenge-platform
Secure headless Wi-Fi provisioning for Ubuntu devices over BLE, with encrypted credential exchange, built in Python.

> The Bluetooth demo steps and troubleshooting will be added as the implementation lands. The simulation below runs on any Linux machine today.

## Try it without hardware (simulation)

The simulation runs the real device and configurator code over a local socket instead of Bluetooth, with simulated Wi-Fi networks. It needs only Python 3.12+ and changes nothing on your machine's Bluetooth or Wi-Fi.

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

**Terminal 1, the device:**

```bash
. .venv/bin/activate
teton-device run --simulate
```

It prints its setup code and a QR code (the stand-in for the label on a real device), then a status line at the bottom: its "LED", now `LED off`. Press **P**: the LED blinks blue with a 5:00 countdown. Only now can a configurator find the device.

**Terminal 2, the configurator:**

```bash
. .venv/bin/activate
teton-configurator --simulate
```

Press Enter to pick the device, type the setup code shown in terminal 1, pick `Home-PSK` and type the password `correct-horse-battery`. You see `Connected. IP 192.168.1.23, internet OK.`, and terminal 1's LED turns green.

The simulated networks:

| Network | Login | What happens |
|---|---|---|
| `Home-PSK` | password `correct-horse-battery` | connects; any other password is rejected |
| `Hospital-EAP` | username `nurse01`, password `teton-demo` (leave the certificate and domain empty) | connects, with a warning that the device couldn't check it is the real network |
| `Cafe-Portal` | any password | fails: the network needs a login page |
| `Weak-Signal` | any password | fails after 45 s: connecting took too long |

Things to try:
- A wrong password first: you get a plain-language failure and can try again on the same connection.
- In terminal 1, while the device is running: **S** shows its state, IP and setup code; **P** on a connected device says it is already connected; **R** twice within 3 seconds resets it (new setup code, LED off); **Q** or Ctrl+C turns it off.
- Restart the device after connecting it: it stays green and doesn't advertise. The simulation keeps its state in `~/.local/state/teton-device-sim` (change it with `--state-dir PATH`), apart from a real device's.
- A second configurator can't find or connect to the device while the first is connected, as with Bluetooth.

The device's logs are JSON lines on stderr (the screen is stdout). To keep them apart: `teton-device run --simulate 2> device.log`. Add `--verbose` to either command for debug logs. Without `--simulate`, both commands use Bluetooth, and the device uses NetworkManager for Wi-Fi.

## Requirements

### Hardware

| Role | What you need |
|---|---|
| Teton Device (laptop A) | Bluetooth 4.2+ adapter that supports the LE **peripheral** role, and a Wi-Fi card managed by NetworkManager |
| Configurator (laptop B) | Bluetooth 4.2+ adapter that supports the LE **central** role |
| Test network (WPA2/WPA3-Personal) | An Android phone hotspot (any access point works) |
| Test network (WPA2-Enterprise, optional) | A laptop whose Wi-Fi card supports **AP mode**, running `hostapd` |

### Roles and Ubuntu versions

| Laptop | Role | Ubuntu | Python |
|---|---|---|---|
| Laptop A (laptop 1) | **Teton Device**: receives the credentials and joins Wi-Fi | **24.04 LTS** (the reference platform for BlueZ peripheral mode and NetworkManager) | 3.12 |
| Laptop B (laptop 2) | **Configurator**, and the optional WPA2-Enterprise test AP | **24.04 or 26.04 LTS** | 3.12 on 24.04, 3.14 on 26.04 |

The code needs Python 3.12 or newer. Older Ubuntu releases are not supported.

The device has no keyboard input beyond a few simulated buttons, pressed as single keys in the `teton-device run` terminal: **P** opens a 5-minute pairing window (the device is only visible over Bluetooth during it), **R** twice within 3 seconds resets it, **S** shows its status, and **Q** or Ctrl+C turns it off. A status line at the bottom acts as its LED.

### Software

The table lists the Ubuntu 24.04 versions. On a 26.04 configurator, BlueZ, NetworkManager, Python and `hostapd` are newer, which is fine.

| Component | Version | Ships with Ubuntu 24.04? | Install if missing |
|---|---|---|---|
| Python | 3.12 | yes | n/a |
| `venv` module | for your Python | no, not on a desktop install | `sudo apt install python3-venv` |
| BlueZ (`bluetoothd`, `bluetoothctl`, `btmon`) | 5.72 | yes | `sudo apt install bluez` |
| NetworkManager (`nmcli`) | 1.46 | yes | `sudo apt install network-manager` |
| `ping`, `ip` | any | yes | `sudo apt install iputils-ping iproute2` |
| `git` | any | no, not on a desktop install | `sudo apt install git` |
| `iw` (optional: check AP mode, debug Wi-Fi) | any | no | `sudo apt install iw` |
| `hostapd` (optional: WPA2-Enterprise test AP) | 2.10 | no | `sudo apt install hostapd` |

To install everything at once: `sudo apt install python3-venv git iw hostapd`.

Ubuntu ships the `hostapd` system service **masked**. Leave it that way: the Enterprise test runs `hostapd` in the foreground with its own config file.

### Permissions

- Run everything as the **logged-in desktop user**. You don't need to join the `bluetooth` or `netdev` groups. BlueZ's D-Bus policy lets every user reach `org.bluez`, and polkit lets the active desktop user manage NetworkManager connections.
- Capturing BLE traffic with `btmon` needs root: `sudo btmon -w capture.btsnoop`.

### Check your machine

All of these checks are read-only. Each line shows what you should see.

```bash
lsb_release -rs                        # 24.04 (the configurator may also show 26.04)
python3 --version                      # Python 3.12.x (3.14.x on 26.04)
python3 -m venv --help >/dev/null && echo venv ok
bluetoothctl --version                 # bluetoothctl: 5.72 (newer on 26.04)
systemctl is-active bluetooth          # active
bluetoothctl show | grep -E "Powered|Roles"   # Powered: yes, Roles: central, Roles: peripheral
rfkill list                            # no "blocked: yes"
nmcli --version                        # nmcli tool, version 1.46.x (newer on 26.04)
nmcli device | grep wifi               # a wifi device that is not "unmanaged"
nmcli general permissions | grep -E "settings.modify.system|network-control"   # both "yes"
nmcli networking connectivity check    # full (if you're online)
iw list | grep -A 10 "Supported interface modes" | grep "\* AP$"   # "* AP" if the card can host the Enterprise test AP
command -v hostapd                     # /usr/sbin/hostapd (only needed for the Enterprise test)
```

### Tested on

| | Laptop 1 (Teton Device) |
|---|---|
| OS | Ubuntu 24.04.5 LTS, kernel 7.0.0-38-generic (HWE) |
| Python | 3.12.3 |
| BlueZ | 5.72 (Intel adapter, central + peripheral roles) |
| NetworkManager | 1.46.0 |
| Wi-Fi | Intel Meteor Lake CNVi (`iwlwifi`), AP mode supported |
| iw / hostapd | 6.7 / 2.10 |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The laptop's Bluetooth name shows as `Teton-XXXX` | While the device advertises, `bless` sets the adapter's alias to the device's name, and puts it back when advertising stops. After a crash or `kill -9` it stays: `bluetoothctl reset-alias` restores it |
| A line like `/org/bluez/Teton9A2C/advertisement1: Released!` on the device's screen | BlueZ dropped the advertisement (for example the adapter was turned off) and `bless` printed it. Harmless; press **P** again once Bluetooth is back |
| The phone or configurator can't connect, and the kernel log shows `Opcode 0x2036 failed: -16` | The Ubuntu Bluetooth settings panel made the adapter discoverable, which competes for advertising. Close the settings panel during runs |
| nRF Connect hangs on the first connection attempt, with nothing in the device's logs | Restart the nRF Connect app |
| The configurator says "Bluetooth is off or missing on this computer" | Turn Bluetooth on (`rfkill list`, `bluetoothctl power on`). Run with `--verbose` to see the reason in the logs |
| `teton-device: no Bluetooth adapter found` | No adapter, or it is blocked: check `rfkill list` and `bluetoothctl show` (see "Check your machine") |
