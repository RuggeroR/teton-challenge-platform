# Provisioning channel options

Research notes, 2026-10-07. Our setup: two Ubuntu laptops (one is the **device**, one the **configurator**) and an Android phone. The constraints are no internet or shared network at the start, a real Wi-Fi join afterwards, nothing in plaintext over the air, and a flow a non-developer can use.

Each option is described the same way: what it is, where it's used, its pros and cons, and how well it fits this challenge.

---

## 1. Bluetooth Low Energy (BLE, GATT)

**How it works.** The device advertises a small radio beacon ("I'm Teton-1234, unprovisioned"). The configurator connects to it and reads or writes named data slots called *characteristics*, grouped into a *service* (GATT = Generic Attribute Profile). The configurator writes the credentials into one slot, and the device answers through another slot ("connecting…", "connected, IP 10.0.0.5"). Range is about 10 to 30 m.

**Where it's used.** This is the de facto standard for headless IoT onboarding: Espressif ESP32 "BLE provisioning", Matter smart-home commissioning, Apple HomeKit, Google Home and Amazon devices, fitness trackers, and medical wearables.

**Pros**
- It doesn't use Wi-Fi, so the device can join the target network while still talking to the configurator, and it can report success or a failure reason back.
- Every phone and nearly every laptop has it. Linux supports it through BlueZ, and Python has libraries for both sides (`bless` for the device, `bleak` for the configurator).
- Chrome on Android supports *Web Bluetooth*, so a plain web page can act as the configurator without an app.
- It is the channel the industry already uses, which is easy to defend in the interview.

**Cons**
- Bluetooth's own built-in pairing security is weak in "Just Works" mode, so we encrypt the credentials ourselves at the application layer. That is the standard practice anyway.
- Making a Linux laptop act as the BLE device (the "peripheral" role) through BlueZ can be finicky, depending on the adapter and the BlueZ version.
- Each write carries only about 20 to 500 bytes, so larger messages have to be split into chunks.
- Web Bluetooth on desktop Linux sits behind a Chrome flag. It works out of the box on Android.

**Fit: strong.** This is my recommended option.

---

## 2. Classic Bluetooth (RFCOMM / serial port profile)

**How it works.** It is the older Bluetooth mode, a virtual serial cable between two paired devices. You open a socket and stream bytes.

**Where it's used.** Legacy industrial gear, OBD car dongles, older medical devices and POS printers.

**Pros**
- It is a simple stream with no chunking.
- Throughput is higher than BLE's.

**Cons**
- It requires OS-level pairing, which is an extra, confusing step for a technician.
- There is no browser support, and iOS restricts it heavily.
- It is falling out of use for new IoT designs.

**Fit: weak.** BLE does the same job better.

---

## 3. SoftAP + captive portal

**How it works.** The device turns its Wi-Fi into a temporary hotspot ("Teton-1234-Setup"). The technician joins it with a phone, a web page opens, and they type in the hospital network credentials. The device then shuts the hotspot down and joins the real network.

**Where it's used.** Very common in consumer IoT: smart plugs, cameras, Chromecast (early versions), printers and ESP32 "WiFiManager".

**Pros**
- No app or special radio is needed; any phone browser works.
- It is easy to understand and easy to build.

**Cons**
- A single Wi-Fi radio can't be a hotspot and a client at the same time, so the device drops the setup link right when it tries to connect. If the password is wrong, the technician gets no feedback.
- The phone also has to leave the hotspot and go back to its normal network, which is awkward.
- It arguably creates a "shared local network", which the challenge forbids at the start.
- The hotspot itself needs WPA2 with a password, or the credentials travel in the clear. Plain HTTP on the portal adds the same problem.
- Hospital IT often flags rogue access points, and some sites run wireless intrusion detection that shuts them down.

**Fit: medium.** It is a good runner-up to discuss in the writeup.

---

## 4. Wi-Fi Direct (Wi-Fi P2P)

**How it works.** Two Wi-Fi devices form a direct link without a router, with one acting as a mini access point (the "group owner").

**Where it's used.** Printers, Miracast screen mirroring, Android file sharing.

**Pros**
- Throughput is high.
- It is WPA2-protected by default.

**Cons**
- It has the same single-radio problem as SoftAP.
- Linux support (wpa_supplicant P2P with NetworkManager) is fragile.
- There is no browser API, and on iOS it isn't usable at all.
- It is effectively a temporary shared network.

**Fit: weak.**

---

## 5. Wi-Fi Easy Connect (DPP, Device Provisioning Protocol)

**How it works.** It is a Wi-Fi Alliance standard built for exactly this problem. The device has a QR code containing its public key. The configurator (an Android phone running 10 or later, under *Settings → Wi-Fi → Add device*) scans it. The two sides then run an authenticated, encrypted exchange in Wi-Fi management frames, and the device receives the network credentials. No network or app is needed.

**Where it's used.** Newer smart-home devices, enterprise Wi-Fi onboarding, Wi-Fi Alliance certified IoT. It is mandatory with WPA3 certification for some device classes.

**Pros**
- It is a purpose-built, standardized and cryptographically strong protocol, with the QR code as its trust anchor.
- Android ships a configurator out of the box.
- It looks very "production-grade" in a writeup.

**Cons**
- On Ubuntu, it means driving `wpa_supplicant` directly (DPP mode), which bypasses NetworkManager. NetworkManager has no DPP support.
- Support depends on the Wi-Fi driver and firmware, and is hard to debug.
- Android's configurator only sends WPA2-PSK and WPA3-SAE networks, not Enterprise.
- If we use Android's built-in configurator, we write no configurator code, but the challenge asks for code on both sides.
- Feedback to the technician is minimal.

**Fit: medium for the demo, strong as the "production direction".** We mention it in the writeup and in the 200-device section.

---

## 6. WPS (Wi-Fi Protected Setup)

**How it works.** You press a button on the router (or type a PIN), and the device gets the credentials within a two-minute window.

**Where it's used.** Home routers.

**Pros**
- It is one button.

**Cons**
- The PIN mode is broken (brute-forceable in hours), and the button mode has no authentication.
- It is deprecated by the Wi-Fi Alliance as of WPA3.
- Hospital access points have it disabled.

**Fit: reject.** It is still worth one line in the writeup to show you know why.

---

## 7. Optical: QR code shown by the configurator

**How it works.** The configurator shows a QR code containing the (encrypted) credentials, and the device's camera reads it. Wi-Fi network QR codes (`WIFI:S:…;P:…;`) are the familiar example.

**Where it's used.** Phone-to-phone Wi-Fi sharing (Android and iOS), Amazon/Google "scan QR" setup for camera devices (Ring, Blink, Wyze).

**Pros**
- Nothing travels over radio, so there is nothing to sniff.
- It is trivially simple, and the technician understands it instantly.

**Cons**
- The device needs a camera. Many IoT devices have one (Teton's devices are camera-based), but laptops have webcams with mixed QR performance.
- It is one-way, with no feedback to the configurator. You would need a second channel or an LED.
- Anyone who photographs the screen has the credentials, unless they're encrypted to the device's key.

**Fit: medium, and interesting for Teton specifically,** since their devices have cameras. It is worth a paragraph in the writeup.

---

## 8. Audio / ultrasonic ("data over sound")

**How it works.** The configurator plays encoded tones (sometimes inaudible), and the device's microphone decodes them.

**Where it's used.** Some Chinese smart cameras, Chirp.io (discontinued), Google Cast guest mode (formerly).

**Pros**
- It works on any device with a speaker and a microphone.

**Cons**
- It is slow (about 10 to 100 bytes per second) and unreliable in noisy hallways.
- It is one-way and can be eavesdropped.
- It is niche and hard to defend.

**Fit: reject.**

---

## 9. NFC (tap to provision)

**How it works.** The phone is tapped against the device, and a few kilobytes are exchanged at about 4 cm range.

**Where it's used.** Payment terminals, some enterprise IoT (HPE Aruba access points, Philips Hue bridges), Android's "tap to set up".

**Pros**
- The very short range is a strong physical security property.
- The technician experience is excellent.

**Cons**
- Laptops have no NFC reader, so we can't build it with our hardware.
- It is still usually paired with BLE or Wi-Fi for the actual data exchange.

**Fit: not buildable here.** It is worth mentioning as hardware future work.

---

## 10. Wired: USB stick / USB cable / serial console

**How it works.** The technician plugs in a USB stick holding a config file, or a cable from a laptop.

**Where it's used.** Industrial and medical gear, Raspberry Pi "boot partition `wpa_supplicant.conf`", factory provisioning.

**Pros**
- It isn't wireless, so the plaintext rule technically doesn't apply.
- It is extremely simple and robust.

**Cons**
- It is physical, one device at a time, and requires an exposed port. Hospital devices are often sealed or mounted on ceilings.
- A USB stick with a plaintext password is a security problem of its own.
- It sidesteps the spirit of the challenge, which wants a wireless local channel.

**Fit: weak for this challenge.** It is a valid factory or recovery path to mention.

---

## Summary

| Channel | Works with our hardware | Two-way feedback | Credential protection | Industry use for this | Effort |
|---|---|---|---|---|---|
| **BLE GATT** | ✅ | ✅ | App-layer encryption (ours) | Very high | Medium |
| Classic Bluetooth | ✅ | ✅ | Pairing + ours | Low (legacy) | Medium |
| SoftAP + portal | ✅ | ❌ (drops on switch) | WPA2 on the AP, ideally plus ours | High (consumer) | Low to medium |
| Wi-Fi Direct | ⚠️ fragile | ✅ | WPA2 | Low | High |
| Wi-Fi Easy Connect (DPP) | ⚠️ driver-dependent | Minimal | Built in, strong | Growing | Medium to high |
| WPS | ✅ | ❌ | Broken | Deprecated | Low |
| Optical QR | ⚠️ webcam | ❌ | Encrypt to device key | Medium (cameras) | Low |
| Audio | ✅ | ❌ | Weak | Niche | Medium |
| NFC | ❌ | ✅ | Physical range | Medium | n/a |
| USB / wired | ✅ | ✅ | Not wireless | Factory / recovery | Low |

**My recommendation: BLE GATT.** It is the only option that meets all four constraints with our hardware and keeps a feedback path to the technician while the device joins Wi-Fi. It is also what the IoT industry actually uses for this. SoftAP and DPP are the two alternatives worth defending against in the writeup; BLE wins on feedback and Linux support. The decision is yours.

## Sources

- [Android: Wi-Fi Easy Connect (DPP)](https://source.android.com/docs/core/connect/wifi-easy-connect): the configurator role since Android 10, PSK and SAE only.
- [wpa_supplicant README-DPP](https://src.rcs.uwaterloo.ca/xref/freebsd-src/contrib/wpa/wpa_supplicant/README-DPP) and the [DPP2 support announcement](https://lists.infradead.org/pipermail/hostap/2021-August/039809.html).
- [Espressif: Wi-Fi Easy Connect](https://esp-idf.readthedocs.io/en/stable/api-reference/network/esp_dpp.html), for comparison with their BLE provisioning.
- [Web Bluetooth implementation status](https://github.com/WebBluetoothCG/web-bluetooth/blob/99a001958db36e288879eb8ea2e9c28e91480df2/implementation-status.md). It is dated; today Chrome on Android supports Web Bluetooth by default, while desktop Linux still needs a flag.
- Other points come from general knowledge of these protocols and were not re-verified for this note.
