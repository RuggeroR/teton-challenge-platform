# nmcli fixtures

Outputs of `nmcli` used by `tests/test_nmcli_parsing.py`.

**Hand-written (Step 2.4)**, from the NetworkManager 1.46 message texts and the `nmcli -t` format, not yet recorded on a laptop. Step 2.5 checks the scan and device output on laptop 1; Step 6.3 replaces them with real recordings, anonymised so no neighbour's SSID ends up in the repo.

| File | Command | Notes |
|---|---|---|
| `scan.txt` | `nmcli -t -f SSID,SIGNAL,SECURITY device wifi list --rescan yes` | escaped `:` and `\`, a hidden SSID, duplicates, every security string we map |
| `devices.txt` | `nmcli -t -f DEVICE,TYPE,STATE device` | an unmanaged USB Wi-Fi first, then the managed one |
| `connections.txt` | `nmcli -t -f NAME,UUID connection show` | two `teton-*` profiles among the owner's |
| `device-show-*.txt` | `nmcli -t -f GENERAL.IP4-CONNECTIVITY,IP4.ADDRESS,IP4.GATEWAY device show <iface>` | full, limited, portal, no address |
| `up-*.txt` | stderr of `nmcli --wait 45 connection up id teton-<ssid>` | one per failure reason, plus one unknown message |
| `wifi-connect-no-ssid.txt` | stderr of `nmcli device wifi connect <ssid>` | the SPEC §7 wording for `ssid_not_found` |
