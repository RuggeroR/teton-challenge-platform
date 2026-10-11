# teton-challenge-platform
Secure headless Wi-Fi provisioning for Ubuntu devices over BLE, with encrypted credential exchange, built in Python.

> Setup, demo steps and troubleshooting will be added as the implementation lands. Check the requirements below first.

## Requirements

### Hardware

| Role | What you need |
|---|---|
| Teton Device (laptop A) | Bluetooth 4.2+ adapter that supports the LE **peripheral** role, and a Wi-Fi card managed by NetworkManager |
| Configurator (laptop B) | Bluetooth 4.2+ adapter that supports the LE **central** role |
| Test network (WPA2/WPA3-Personal) | An Android phone hotspot (any access point works) |
| Test network (WPA2-Enterprise, optional) | A laptop whose Wi-Fi card supports **AP mode**, running `hostapd` |

Both roles can run on either laptop, so it's simplest to set up both laptops the same way.

### Software

Both laptops run **Ubuntu 24.04 LTS**. Older releases are not supported.

| Component | Version | Ships with Ubuntu 24.04? | Install if missing |
|---|---|---|---|
| Python | 3.12 | yes | n/a |
| `venv` module | for Python 3.12 | no, not on a desktop install | `sudo apt install python3-venv` |
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
lsb_release -rs                        # 24.04
python3 --version                      # Python 3.12.x
python3 -m venv --help >/dev/null && echo venv ok
bluetoothctl --version                 # bluetoothctl: 5.72
systemctl is-active bluetooth          # active
bluetoothctl show | grep -E "Powered|Roles"   # Powered: yes, Roles: central, Roles: peripheral
rfkill list                            # no "blocked: yes"
nmcli --version                        # nmcli tool, version 1.46.x
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
