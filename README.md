# UMIGURI LED Bridge

> Real-time WebSocket → Tasoller LED bridge for UMIGURI and host-aprom firmware.

<p align="center">
  <img src="screenshots/screenshot.png" width="800">
</p>

---

## Overview

UMIGURI LED Bridge is a lightweight Windows utility that connects  
UMIGURI's WebSocket LED output to Tasoller / host-aprom firmware
slider hardware over serial communication.

---

## Tested Hardware

- Tasoller on V2 Firmware using host-aprom firmware

---

# Installation

## Requirements

- Windows 10 / 11
- Python 3.10+
- a Tasoller with host-aprom firmware
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
