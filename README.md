# Iron Pride Display

Native Linux client and visual editor for the built-in IPS screen of the
**Iron Pride Invader Q9MX** PC case. The stock software (APEXSTORM) is
Windows-only — this is a from-scratch reimplementation built by
reverse-engineering its USB serial protocol.

![status](https://img.shields.io/badge/platform-Linux-blue)
![python](https://img.shields.io/badge/python-3.11%2B-green)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

## Download

**[→ Latest release (v1.0)](https://github.com/GitFlowLink/ironpride-display/releases/tag/v1.0)**

Pre-built binary for Linux x86-64 — no Python required, just `ffmpeg` on the system.

## Features

- **Layers**: images, GIFs, looping videos, animated text, system sensor gauges
- **Manipulate**: move, resize from edges/corners (Photoshop-style), free rotation per layer
- **Text**: custom color or animated flowing rainbow gradient
- **Effects**: per-layer opacity, auto-spin with speed control, GIF/video playback speed
- **Sensors**: CPU / GPU / RAM load and CPU / GPU temperature ring gauges (live, 1 s refresh)
- **Color filters** over the whole composition (mono / red / pink / green / blue tint)
- **Themes**: save named layouts and switch between them
- **Tray + autostart**: minimizes to system tray, can launch on login and auto-resume the last layout
- **Drag & drop** files straight from the file manager onto the canvas

## How it works — reverse-engineered protocol

The display enumerates as a CDC ACM serial device (`33c3:8001`) on `/dev/ttyACM0`.
Commands use a simple framed format:

```
5A A5 | CMD 00 | SIZE (uint32 LE) | PAYLOAD
```

| CMD  | Meaning     | Payload           |
|------|-------------|-------------------|
| 0x90 | Hello       | `01`              |
| 0x80 | Brightness  | one byte `00..FF` |
| 0x81 | Orientation | one byte          |
| 0x85 | Frame       | H.264 Annex B NAL |

The panel contains a hardware H.264 decoder. Frames are encoded at **462×1920**
(Constrained Baseline, level 3.1) and the panel rotates them to its native
landscape orientation. A persistent `ffmpeg` pipe encodes the composed canvas
and each emitted keyframe is wrapped in a `0x85` command — the firmware blanks
the screen if the stream stops.

The protocol was recovered from USB captures (`usbmon` + `tcpdump`) of the
official software running in a Windows VM with USB passthrough.

## Requirements

```bash
# Fedora / RHEL
sudo dnf install ffmpeg
sudo usermod -aG dialout "$USER"   # re-login afterwards
```

## Run from source

```bash
pip install -r requirements.txt
python3 ironpride_editor.py

# headless / login mode (tray only, starts streaming automatically):
python3 ironpride_editor.py --background
```

## Build a standalone binary

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name ironpride-display \
            --collect-all PyQt6 ironpride_editor.py
# result: dist/ironpride-display
```

## Notes

- Tested on Fedora 44, KDE Plasma 6 (Wayland), AMD Ryzen 7 8700G / Radeon 780M.
- GPU load/temp are read from `amdgpu` sysfs; on other GPUs the sensor paths may differ.
- This is an unofficial project, not affiliated with Iron Pride or APEXSTORM.

## License

MIT — see [LICENSE](LICENSE).
