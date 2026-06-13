# Iron Pride Display

Native Linux client and visual editor for the built-in IPS screen of the
**Iron Pride Invader Q9MX** PC case. The stock software (APEXSTORM) is
Windows-only — this is a from-scratch reimplementation built by
reverse-engineering its USB serial protocol.

![status](https://img.shields.io/badge/platform-Linux-blue)
![python](https://img.shields.io/badge/python-3.11%2B-green)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

## What it does

A drag-and-drop editor that composes a live scene and streams it to the case
display in real time:

- **Layers**: images, GIFs, looping videos, text, system sensors
- **Manipulate**: move, resize from edges/corners (Photoshop-style), free rotation
- **Text**: custom color or animated flowing rainbow gradient
- **Effects**: per-layer opacity, auto-spin with speed control, GIF/video playback speed
- **Sensors**: CPU / GPU / RAM load and CPU / GPU temperature ring gauges (live)
- **Color filters** over the whole composition (mono / red / pink / green / blue tint)
- **Themes**: save layouts by name and switch between them
- **Tray + autostart**: minimizes to the system tray, can launch on login and
  auto-resume the last layout
- **Drag & drop** files straight from the file manager onto the canvas

## How it works (reverse-engineered protocol)

The display enumerates as a CDC ACM serial device (`33c3:8001`) on
`/dev/ttyACM0`. Commands use a simple framed format:

```
5A A5 | CMD 00 | SIZE (uint32 LE) | PAYLOAD
```

| CMD  | Meaning      | Payload            |
|------|--------------|--------------------|
| 0x90 | Hello        | `01`               |
| 0x80 | Brightness   | one byte `00..FF`  |
| 0x81 | Orientation  | one byte           |
| 0x85 | Frame        | H.264 Annex B NAL  |

The panel contains a hardware H.264 decoder. Frames are encoded at **462x1920**
(Constrained Baseline, level 3.1) and the panel rotates them to its native
landscape orientation. The client keeps a continuous stream alive (the firmware
blanks the screen if frames stop), so a persistent `ffmpeg` pipe encodes the
composed canvas and each emitted keyframe is wrapped in a `0x85` command.

The protocol was recovered from USB captures (`usbmon` + `tcpdump`) of the
official software running in a Windows VM with USB passthrough.

## Requirements

System packages (Fedora example):

```bash
sudo dnf install python3-pyqt6 ffmpeg
```

Python packages:

```bash
pip install -r requirements.txt
```

You also need access to the serial device:

```bash
sudo usermod -aG dialout "$USER"   # re-login afterwards
```

## Run

```bash
python3 ironpride_editor.py
# headless / login mode (tray only, auto-starts streaming):
python3 ironpride_editor.py --background
```

## Build a standalone binary

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name ironpride-display \
            --collect-all PyQt6 ironpride_editor.py
# result: dist/ironpride-display   (ffmpeg must be installed on the target)
```

## Notes

- Tested on Fedora KDE Plasma 6 (Wayland), AMD Ryzen 8700G / Radeon 780M.
- GPU load/temp are read from `amdgpu` sysfs; on other GPUs adjust the sensor paths.
- This is an unofficial project, not affiliated with Iron Pride or APEXSTORM.

## License

MIT — see [LICENSE](LICENSE).
