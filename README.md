# UMIGURI LED Bridge

> Real-time WebSocket → Tasoller LED bridge for UMIGURI and host-aprom firmware.

<p align="center">
  <img src="screenshots/screenshot.png" width="800">
</p>

---

## Overview

UMIGURI LED Bridge is a lightweight Windows utility that connects  
UMIGURI's WebSocket LED output to Tasoller / host-aprom compatible
slider hardware over serial communication.

The project was built specifically for rhythm game and arcade controller
setups requiring:

- low latency
- accurate LED mapping
- minimal dependencies
- reliable hardware synchronization

Unlike generic serial forwarding tools, this bridge correctly implements
the Tasoller LED layout including:

- interleaved guide-wall LEDs
- hardware BRG ordering
- host-aprom packet formatting
- proper LED index alignment
- real hardware mirroring behavior

---

# Features

## Real-Time LED Translation

Converts UMIGURI LED packets directly into the
host-aprom slider protocol in real time.

---

## Tested Hardware

- Tasoller on V2 Firmware using host-aprom firmware

---

# Installation

## Requirements

- Windows 10 / 11
- Python 3.10+
- a Tasoller with host-approm
- UMIGURI configured for WebSocket LED output

---

## Install Dependencies

```bash
pip install websockets
```

---

## Run

```bash
python main.py
```

---

# UMIGURI Configuration

Set UMIGURI LED Server Port to whichever port is configured inside the bridge

---

# Credits
Created by Mayo Inoue

Special thanks to:
- UMIGURI developers
- host-aprom contributors
- rhythm game hardware community
- Tasoller reverse-engineering efforts

---

# Disclaimer

This project is an unofficial community tool and is not affiliated with
GAMO2, SEGA, Konami, or any arcade/controller hardware manufacturer.
