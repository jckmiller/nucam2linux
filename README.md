# nucam2linux 📱→🖥️

Stream your Android phone's camera to Ubuntu as a virtual webcam — accessible inside any browser (Chrome, Firefox, etc.) — over USB using ADB and scrcpy.

---

## How It Works

```
Android Camera
      │
   USB cable
      │
  ADB (USB Debugging)
      │
  scrcpy --video-source=camera
      │       ↘
  ffmpeg pipe  └──► /dev/video10  (v4l2loopback)
                           │
                      Browser sees it as
                      a standard webcam ✅
```

---

## Requirements

| Component | Min Version |
|---|---|
| Ubuntu | 20.04+ |
| Android | 8.0+ (API 26+) |
| scrcpy | **2.0+** (required for `--video-source=camera`) |
| ADB | Any recent version |

---

## Installation (Ubuntu)

### 1. Clone or download this repo

```bash
git clone https://github.com/yourname/nucam2linux.git ~/nucam2linux
cd ~/nucam2linux
```

### 2. Run the setup script

```bash
chmod +x setup.sh
./setup.sh
```

The script will:
- Install `adb`, `ffmpeg`, `scrcpy`, `v4l2loopback-dkms`, `v4l-utils`
- Load the `v4l2loopback` kernel module (creates `/dev/video10`)
- Configure it to load on every boot
- Add your user to the `video` group
- Install Python dependency (`rich`)
- Install and enable the systemd user service (auto-start on login)

> **Note:** Log out and back in after setup so the `video` group change takes effect.

---

## Android Setup

1. **Enable Developer Options**
   - Go to **Settings → About Phone**
   - Tap **Build Number** 7 times until you see "You are now a developer!"

2. **Enable USB Debugging**
   - Go to **Settings → Developer Options**
   - Toggle **USB Debugging** ON

3. **Connect via USB**
   - Plug your phone into the Ubuntu PC with a USB cable
   - On the phone, tap **"Allow"** when the ADB authorization dialog appears
   - Verify connection: `adb devices` should show your device as `device` (not `unauthorized`)

---

## Usage

### Interactive (Terminal UI)

```bash
python3 ~/nucam2linux/nucam.py
```

The terminal UI shows:
- 🔗 Connection status
- 📱 Device name & Android version
- 📷 Camera resolution & FPS
- 🎥 Virtual device path (e.g. `/dev/video10`)
- ⏱ Stream uptime

**Keyboard shortcuts:**
| Key | Action |
|---|---|
| `r` | Restart stream |
| `q` / `Ctrl+C` | Quit & clean up |

### Headless (systemd / no UI)

```bash
python3 ~/nucam2linux/nucam.py --no-ui
```

### Systemd service

```bash
# Start now
systemctl --user start nucam.service

# Stop
systemctl --user stop nucam.service

# View live logs
journalctl --user -fu nucam.service

# Disable auto-start
systemctl --user disable nucam.service
```

---

## Configuration

Edit `~/nucam2linux/nucam.conf` to change defaults:

```ini
[camera]
camera_facing = back      # "back" or "front"
resolution    = 1280x720  # WxH
fps           = 30

[device]
v4l2_sink  = auto         # "auto" or e.g. /dev/video10
adb_serial = auto         # "auto" or your device serial

[scrcpy]
extra_flags    =           # Any additional scrcpy flags
reconnect_delay = 3        # Seconds between reconnect attempts
```

---

## Verify the Virtual Camera

```bash
# List all video devices
v4l2-ctl --list-devices

# Inspect the loopback device
v4l2-ctl --device=/dev/video10 --all
```

---

## Using in the Browser

1. Open any app that uses your webcam (Google Meet, Zoom web, Discord, etc.)
2. Go to camera settings and select **"nucam2linux"** (or `v4l2loopback` device)
3. Your Android phone's camera feed will appear 🎉

### Quick test with Chrome

```
chrome://webrtc-internals
```

Or open this one-liner test page:

```bash
python3 -c "
import http.server, webbrowser, threading, time
html = open('/dev/stdin').read() if False else '''<!DOCTYPE html>
<html><body>
<video id=v autoplay playsinline style=\"width:100%\"></video>
<script>
navigator.mediaDevices.getUserMedia({video:true})
  .then(s=>document.getElementById(\"v\").srcObject=s);
</script></body></html>'''
import tempfile, os
f = tempfile.NamedTemporaryFile(suffix='.html', delete=False, mode='w')
f.write(html); f.close()
webbrowser.open('file://'+f.name)
"
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `adb devices` shows `unauthorized` | Unlock phone and tap **Allow** on the USB debugging dialog |
| `scrcpy: --video-source not found` | Upgrade scrcpy to v2.0+: `sudo apt-get install scrcpy` (avoid snap — it causes GPU interface errors) |
| `/dev/video10` doesn't exist | Run `sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="nucam2linux" exclusive_caps=1` |
| Browser doesn't see the camera | Ensure your user is in the `video` group: `groups $USER` |
| Black screen in browser | Try switching `camera_facing` in `nucam.conf` |
| Low FPS | Lower `resolution` or `fps` in `nucam.conf` |

---

## How the USB Connection Works

1. **ADB (Android Debug Bridge)** creates a secure tunnel over the USB cable — no Wi-Fi needed.
2. **scrcpy** connects to the Android device via this ADB tunnel and requests camera frames using the Android `Camera2` API, then streams H.264 video to the host.
3. **v4l2loopback** is a Linux kernel module that creates a fake `/dev/videoX` device. Any app that writes frames to it makes those frames available to all readers (browsers, video apps, etc.).
4. **scrcpy `--v4l2-sink`** writes the decoded video frames directly into the loopback device.

---

## License

MIT
