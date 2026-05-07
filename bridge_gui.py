"""
umiguri-led-bridge  v3.6
UMIGURI -> [UmgrWebSocket] -> bridge -> [Serial COM1] -> host-aprom firmware

Zero non-stdlib dependencies for the GUI + serial side:
  - tkinter   (stdlib)
  - winreg    (stdlib, Windows only)
  - ctypes    (stdlib)
  - asyncio   (stdlib)
  - websockets  <-- only external dep

Changelog v3.6 (on top of v3.5):
  - BUGFIX: Serial LED packet now interleaves cells and guide walls correctly.
            Previously rgb_31 was built as [c0..c15, gw0..gw14] (flat), but
            the firmware's aBRG array expects interleaved order:
              aBRG[1]=cell0, aBRG[2]=div0_1, aBRG[3]=cell1, ...
            matching led_shared.h: LED_CELL_N = N*2, LED_DIVIDER_N_M = N*2+1.
            The visualiser was already correct; only the serial path was wrong.

Changelog v3.5 (on top of v3.4):
  - BUGFIX: Guide-wall LEDs now rendered BETWEEN each main cell (interleaved),
            matching the real Tasoller hardware layout, not top/bottom bands.
            Visual pattern: cell | gw | cell | gw | ... | cell
            (16 cells × 15 guide walls = 31 elements total, alternating)
  - FEATURE: Serial Bypass toggle — run the WebSocket bridge and LED visualiser
             without requiring COM1 to be present (for testing / display only).
"""

import asyncio
import os
import sys
import time
import threading
import queue
import tkinter as tk
from tkinter import ttk
import ctypes
import ctypes.wintypes
import winreg
import websockets.server

# ─────────────────────────────────────────────────────────────────────────────
# Windows DPI awareness — call BEFORE Tk window is created
# ─────────────────────────────────────────────────────────────────────────────
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Per-monitor v2 (Win10+)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()    # System DPI (Vista+)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Windows serial port via ctypes (no pyserial)
# ─────────────────────────────────────────────────────────────────────────────
GENERIC_WRITE         = 0x40000000
OPEN_EXISTING         = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE  = ctypes.wintypes.HANDLE(-1).value

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


def port_path(port: str) -> str:
    """Return the Win32 device path for a COM port.
    Always uses the \\.\ prefix — safe for COM1 through COM256.
    """
    port = port.strip()
    if not port.upper().startswith("COM"):
        raise ValueError(f"Invalid port name: {port!r}")
    suffix = port[3:]
    if not suffix.isdigit():
        raise ValueError(f"Non-numeric COM port suffix: {port!r}")
    return f"\\\\.\\COM{suffix}"


class WinSerial:
    """Minimal Windows serial writer using CreateFile / WriteFile."""

    def __init__(self, port: str, baud: int = 115200):
        self._handle = None
        self._lock   = threading.Lock()
        self.port    = port
        self.baud    = baud
        self._open()

    def _open(self):
        name = port_path(self.port)
        h = k32.CreateFileW(name,
                             GENERIC_WRITE, 0, None,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if h == INVALID_HANDLE_VALUE:
            self._handle = None
            return False

        class DCB(ctypes.Structure):
            _fields_ = [
                ("DCBlength",           ctypes.c_ulong),
                ("BaudRate",            ctypes.c_ulong),
                ("fBinary",             ctypes.c_uint, 1),
                ("fParity",             ctypes.c_uint, 1),
                ("fOutxCtsFlow",        ctypes.c_uint, 1),
                ("fOutxDsrFlow",        ctypes.c_uint, 1),
                ("fDtrControl",         ctypes.c_uint, 2),
                ("fDsrSensitivity",     ctypes.c_uint, 1),
                ("fTXContinueOnXoff",   ctypes.c_uint, 1),
                ("fOutX",               ctypes.c_uint, 1),
                ("fInX",                ctypes.c_uint, 1),
                ("fErrorChar",          ctypes.c_uint, 1),
                ("fNull",               ctypes.c_uint, 1),
                ("fRtsControl",         ctypes.c_uint, 2),
                ("fAbortOnError",       ctypes.c_uint, 1),
                ("fDummy2",             ctypes.c_uint, 17),
                ("wReserved",           ctypes.c_ushort),
                ("XonLim",              ctypes.c_ushort),
                ("XoffLim",             ctypes.c_ushort),
                ("ByteSize",            ctypes.c_ubyte),
                ("Parity",              ctypes.c_ubyte),
                ("StopBits",            ctypes.c_ubyte),
                ("XonChar",             ctypes.c_char),
                ("XoffChar",            ctypes.c_char),
                ("ErrorChar",           ctypes.c_char),
                ("EofChar",             ctypes.c_char),
                ("EvtChar",             ctypes.c_char),
                ("wReserved1",          ctypes.c_ushort),
            ]

        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        if not k32.GetCommState(h, ctypes.byref(dcb)):
            k32.CloseHandle(h)
            self._handle = None
            return False

        dcb.BaudRate = self.baud
        dcb.ByteSize = 8
        dcb.Parity   = 0   # NOPARITY
        dcb.StopBits = 0   # ONESTOPBIT
        dcb.fBinary  = 1
        if not k32.SetCommState(h, ctypes.byref(dcb)):
            k32.CloseHandle(h)
            self._handle = None
            return False

        self._handle = h
        return True

    def write(self, data: bytes) -> bool:
        with self._lock:
            if self._handle is None:
                if not self._open():
                    return False
            buf     = (ctypes.c_char * len(data))(*data)
            written = ctypes.c_ulong(0)
            ok = k32.WriteFile(self._handle, buf, len(data),
                               ctypes.byref(written), None)
            if not ok:
                k32.CloseHandle(self._handle)
                self._handle = None
                return False
            return True

    def close(self):
        with self._lock:
            if self._handle is not None:
                k32.CloseHandle(self._handle)
                self._handle = None


# ─────────────────────────────────────────────────────────────────────────────
# Slider serial protocol
# ─────────────────────────────────────────────────────────────────────────────
SLIDER_SYNC    = 0xFF
SLIDER_MARK    = 0xFD
SLIDER_CMD_LED = 0x02

UMGR_VERSION         = 0x01
UMGR_OP_SET_LED      = 0x10
UMGR_OP_INITIALIZE   = 0x11
UMGR_OP_PING         = 0x12
UMGR_OP_REQUEST_INFO = 0xD0
UMGR_OP_READY        = 0x19
UMGR_OP_PONG         = 0x1A
UMGR_OP_REPORT_INFO  = 0xD8


def _escape(b: int) -> bytes:
    if b == SLIDER_SYNC:
        return bytes([SLIDER_MARK, 0xFE])
    if b == SLIDER_MARK:
        return bytes([SLIDER_MARK, 0xFC])
    return bytes([b])


def build_led_packet(rgb_31: list, brightness: int) -> bytes:
    """
    Build a serial LED packet for the Tasoller / host-aprom firmware.

    Firmware reference: slider.h  slider_cmd_Rx_led
        typedef struct __packed {
            uint8_t u8Brightness;   // Range: 0~63
            struct __packed { uint8_t u8B; uint8_t u8R; uint8_t u8G; } aBRG[32];
        } slider_cmd_Rx_led;

    rgb_31     : list of 31 (R, G, B) tuples in INTERLEAVED hardware order:
                   index 0,2,4,...,30  → 16 main touch-strip cells
                   index 1,3,5,...,29  → 15 guide-wall dividers
                 (matches LED_CELL_0=0, LED_DIVIDER_0_1=1, LED_CELL_1=2, ...)
    brightness : 0-254 caller range, scaled to 0-63 for firmware.

    Payload layout (97 bytes):
        [0]       u8Brightness  (0-63)
        [1..96]   32 × 3 bytes BRG   (index 0 = obscured/zero, indices 1-31 = logical[0..30])
    """
    rgb_31 = list(rgb_31)
    if len(rgb_31) < 31:
        rgb_31 += [(0, 0, 0)] * (31 - len(rgb_31))
    rgb_31 = rgb_31[:31]

    # Firmware brightness is 0-63; caller passes 0-254
    brt63 = max(0, min(63, round(brightness * 63 / 254)))
    payload = bytearray([brt63])

    # Direct 1:1 LED mapping
    # Your firmware expects LEDs starting at aBRG[0], not a hidden offset LED.
    for r, g, b in rgb_31:
        payload += bytes([b, r, g])
    
    # Pad remaining slot to keep 32 entries total
    payload += bytes([0, 0, 0])

    if len(payload) != 97:
        raise RuntimeError(
            f"Payload mismatch: expected 97, got {len(payload)}"
        )

    checksum = (
        -(SLIDER_SYNC + SLIDER_CMD_LED + len(payload) + sum(payload))
    ) & 0xFF

    pkt = bytearray([SLIDER_SYNC])
    pkt += _escape(SLIDER_CMD_LED)
    pkt += _escape(len(payload))
    for byte in payload:
        pkt += _escape(byte)
    pkt += _escape(checksum)
    return bytes(pkt)


# ─────────────────────────────────────────────────────────────────────────────
# Bridge core
# ─────────────────────────────────────────────────────────────────────────────
FIXED_PORT = "COM1"
FIXED_BAUD = 115200


class BridgeCore:
    """
    WebSocket bridge that optionally forwards LED data to serial.

    bypass_serial=True  → accept UMIGURI connections and drive the GUI
                          visualiser, but never touch COM1.
    bypass_serial=False → full operation: serial + WebSocket + visualiser.
    """

    def __init__(self, ws_port, brightness, fps, log_queue, led_queue,
                 bypass_serial: bool = False):
        self.port           = FIXED_PORT
        self.baud           = FIXED_BAUD
        self.ws_port        = ws_port
        self.brightness     = brightness
        self.fps            = fps
        self.bypass_serial  = bypass_serial
        self._log_q         = log_queue
        self._led_q         = led_queue   # carries (main_cells, wall_cells)
        self._loop          = None
        self._stop          = threading.Event()
        self.stats          = {"packets": 0, "clients": 0, "serial_ok": True}

    def log(self, msg, level="INFO"):
        self._log_q.put((level, msg))

    def start(self):
        self._stop.clear()
        self.stats = {"packets": 0, "clients": 0, "serial_ok": True}
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            self.log(f"Bridge crashed: {e}", "ERROR")

    async def _serve(self):
        # ── Serial setup (skipped in bypass mode) ─────────────────────────────
        writer = None
        if self.bypass_serial:
            self.log("Serial BYPASS — visualiser only, COM1 not opened", "WARN")
        else:
            writer = WinSerial(self.port, self.baud)
            if writer._handle is None:
                self.log(f"Cannot open serial port {self.port}", "ERROR")
                return
            self.log(f"Serial opened  →  {self.port} @ {self.baud}")

        self.log(f"WebSocket port      →  {self.ws_port}")

        min_interval = 1.0 / self.fps
        last_send    = 0.0
        clients      = [0]

        async def handle(ws):
            clients[0] += 1
            self.stats["clients"] = clients[0]
            self.log(f"UMIGURI connected  ({clients[0]} active)")
            nonlocal last_send
            try:
                async for msg in ws:
                    if self._stop.is_set():
                        break
                    if not isinstance(msg, bytes) or len(msg) < 3:
                        continue
                    ver, op, plen = msg[0], msg[1], msg[2]
                    pay = msg[3:]
                    if ver != UMGR_VERSION or plen != len(pay):
                        continue

                    if op == UMGR_OP_INITIALIZE:
                        self.log("Initialize → Ready")
                        await ws.send(bytes([UMGR_VERSION, UMGR_OP_READY, 0x00]))

                    elif op == UMGR_OP_PING and plen == 4:
                        await ws.send(bytes([
                            UMGR_VERSION, UMGR_OP_PONG, 6,
                            pay[0], pay[1], pay[2], pay[3], 0x51, 0xED
                        ]))

                    elif op == UMGR_OP_REQUEST_INFO:
                        self.log("ServerInfo request")
                        name    = b"led-bridge\x00\x00\x00\x00\x00\x00"
                        hw_name = b"tasoller-aprom\x00\x00"
                        await ws.send(
                            bytes([UMGR_VERSION, UMGR_OP_REPORT_INFO, 44])
                            + name + bytes([0, 1, 0, 0, 0, 0])
                            + hw_name + bytes([0, 1, 0, 1, 0, 0])
                        )

                    elif op == UMGR_OP_SET_LED and plen == 103:
                        # pay[1..48]  : 16 main cells  (offset 1 + i*3)
                        # pay[49..93] : 15 guide walls  (offset 49 + i*3)
                        main_cells  = []
                        guide_walls = []
                        for i in range(16):
                            p = 1 + i * 3
                            main_cells.append((pay[p], pay[p + 1], pay[p + 2]))
                        for i in range(15):
                            p = 49 + i * 3
                            guide_walls.append((pay[p], pay[p + 1], pay[p + 2]))

                        vis_main_src  = main_cells
                        vis_walls_src = guide_walls

                        # No left-right inversion needed — UMIGURI order matches display
                        hw_main  = list(reversed(main_cells))
                        hw_walls = list(reversed(guide_walls))
                        # Guide walls run top-to-bottom — not reversed

                        # Scale colours by brightness so the visualiser matches
                        # what hardware would display (brightness 0-254 → 0.0-1.0)
                        brt = self.brightness / 254.0

                        def scale(cells):
                            return [
                                (int(r * brt), int(g * brt), int(b * brt))
                                for r, g, b in cells
                            ]

                        vis_main  = scale(vis_main_src)
                        vis_walls = scale(vis_walls_src)

                        # Push both to the GUI queue as a pair
                        try:
                            self._led_q.put_nowait((vis_main, vis_walls))
                        except queue.Full:
                            pass

                        # Serial path — skipped entirely in bypass mode
                        if not self.bypass_serial and writer is not None:
                            now = time.monotonic()
                            if now - last_send >= min_interval:
                                # Interleave cells and guide walls to match firmware's
                                # logical index layout:
                                #   aBRG[1]=cell0, aBRG[2]=div0, aBRG[3]=cell1, ...
                                # (led_shared.h: LED_CELL_0=0, LED_DIVIDER_0_1=1, ...)
                                rgb_31 = []
                                for _i in range(15):
                                    rgb_31.append(hw_main[_i])
                                    rgb_31.append(hw_walls[_i])
                                rgb_31.append(hw_main[15])
                                try:
                                    pkt = build_led_packet(
                                        rgb_31, self.brightness
                                    )
                                except RuntimeError as e:
                                    self.log(f"Packet build error: {e}", "ERROR")
                                    continue
                                ok = writer.write(pkt)
                                self.stats["serial_ok"] = ok
                                if ok:
                                    last_send = now
                                    self.stats["packets"] += 1
                        elif self.bypass_serial:
                            # Bypass mode: count frames reaching the visualiser
                            now = time.monotonic()
                            if now - last_send >= min_interval:
                                last_send = now
                                self.stats["packets"] += 1

            except Exception as e:
                self.log(f"Client disconnected: {e}", "WARN")
            finally:
                clients[0] -= 1
                self.stats["clients"] = clients[0]

        if not (1024 <= self.ws_port <= 65535):
            self.log(
                f"WS port {self.ws_port} out of valid range 1024–65535", "ERROR"
            )
            if writer:
                writer.close()
            return

        try:
            async with websockets.server.serve(
                handle, "127.0.0.1", self.ws_port
            ):
                while not self._stop.is_set():
                    await asyncio.sleep(0.1)
        finally:
            if writer:
                writer.close()
            self.log("Bridge stopped.")


# ─────────────────────────────────────────────────────────────────────────────
# Design tokens
# ─────────────────────────────────────────────────────────────────────────────
BG       = "#0b0c10"
SURFACE  = "#13141a"
SURFACE2 = "#1c1e26"
BORDER   = "#272933"
ACCENT   = "#00e5c8"
RED      = "#ff4560"
GREEN    = "#00e5a0"
YELLOW   = "#f5c518"
TEXT     = "#d4d8e8"
TEXTDIM  = "#5a5f74"
TEXTMID  = "#888fa8"
MONO     = "Courier New"

# ─────────────────────────────────────────────────────────────────────────────
# Slider visualiser geometry
#
# Real Tasoller layout (top view):
#
#   [cell 0] [gw 0] [cell 1] [gw 1] ... [cell 14] [gw 14] [cell 15]
#
# 16 main cells + 15 guide-wall dividers = 31 visual elements.
# Guide-wall segments are narrow vertical lines between each cell,
# matching the actual hardware photo.
#
# Canvas height: main cells are tall rectangles; guide-wall dividers
# are the same height but narrower — a thin coloured vertical bar.
# ─────────────────────────────────────────────────────────────────────────────
_VIS_H       = 56    # canvas height in px
_CELL_Y0     = 2     # main cell top edge
_CELL_Y1     = 54    # main cell bottom edge
_GW_Y0       = 6     # guide-wall top edge (slightly inset)
_GW_Y1       = 50    # guide-wall bottom edge

# Width ratios: guide-wall dividers are ~12 % as wide as a main cell
# The 31 units share the full canvas width:  16*cell_w + 15*gw_w = total_w
# gw_w = cell_w * GW_RATIO  →  cell_w = total_w / (16 + 15*GW_RATIO)
_GW_RATIO    = 0.18

# Idle colours
_CELL_IDLE   = (20, 20, 36)     # near-black blue
_GW_IDLE     = (45, 10, 80)     # dim purple


def hsep(parent, color=BORDER):
    tk.Frame(parent, bg=color, height=1).pack(fill="x")


def _resource(relative: str) -> str:
    """Resolve a path to a bundled resource whether running live or via PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


# ─────────────────────────────────────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            self.iconbitmap(_resource("icon.ico"))
        except Exception:
            pass  # no icon.ico present — fine
        self.title("UMIGURI LED Bridge")
        self.configure(bg=BG)
        self.resizable(False, False)

        self._bridge  = None
        self._running = False
        self._log_q   = queue.Queue()
        self._led_q   = queue.Queue(maxsize=4)

        self._style_ttk()
        self._build()
        self._poll()

    # ── ttk style ─────────────────────────────────────────────────────────────
    def _style_ttk(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox",
                         fieldbackground=SURFACE2, background=SURFACE2,
                         foreground=TEXT, selectbackground=SURFACE2,
                         selectforeground=TEXT, arrowcolor=ACCENT,
                         bordercolor=BORDER, lightcolor=SURFACE2,
                         darkcolor=SURFACE2, padding=(6, 4))
        style.map("TCombobox",
                  fieldbackground=[("readonly", SURFACE2)],
                  foreground=[("readonly", TEXT)],
                  bordercolor=[("focus", ACCENT)])

    # ── Build ─────────────────────────────────────────────────────────────────
    def _build(self):
        self._build_header()
        self._build_slider_vis()
        self._build_status()
        hsep(self)
        self._build_config()
        hsep(self)
        self._build_brightness()
        hsep(self)
        self._build_controls()
        hsep(self)
        self._build_log()
        self._build_hint()

    # ── Header ────────────────────────────────────────────────────────────────
    def _build_header(self):
        h = tk.Frame(self, bg=SURFACE)
        h.pack(fill="x")
        tk.Frame(h, bg=ACCENT, height=2).pack(fill="x")

        inner = tk.Frame(h, bg=SURFACE)
        inner.pack(fill="x", padx=20, pady=(14, 12))

        top_row = tk.Frame(inner, bg=SURFACE)
        top_row.pack(fill="x")
        tk.Label(top_row, text="Created by Mayo Inoue",
                 bg=SURFACE, fg=ACCENT,
                 font=(MONO, 10, "bold"), anchor="w").pack(side="left")
        tk.Label(top_row, text=" v3.6 ",
                 bg=BORDER, fg=TEXTDIM,
                 font=(MONO, 8), relief="flat", bd=0).pack(side="right")

        tk.Label(inner, text="Serial LED Bridge",
                 bg=SURFACE, fg=TEXT,
                 font=(MONO, 16), anchor="w").pack(fill="x", pady=(4, 0))
        tk.Label(inner, text="host-aprom  ·  tasoller  ·  COM1 @ 115200",
                 bg=SURFACE, fg=TEXTDIM,
                 font=(MONO, 8), anchor="w").pack(fill="x", pady=(3, 0))
        hsep(h)

    # ── Tasoller slider LED visualiser ────────────────────────────────────────
    def _build_slider_vis(self):
        """
        Single canvas matching the real hardware layout.

        Elements left → right:
          cell[0] | gw[0] | cell[1] | gw[1] | ... | gw[14] | cell[15]
          (16 main cells, 15 guide-wall dividers, 31 elements total)

        Guide-wall dividers are narrow purple/coloured bars between cells.
        """
        self._vis = tk.Canvas(
            self, height=_VIS_H, bg=BG, highlightthickness=0
        )
        self._vis.pack(fill="x")

        # Lists holding canvas item IDs in left-to-right order
        self._main_rects = []   # 16 items
        self._gw_rects   = []   # 15 items

        self._vis.bind("<Configure>", lambda e: self._redraw_vis())
        self._redraw_vis()

    def _vis_layout(self, total_w: int):
        """
        Return (cell_w, gw_w) float pixel widths for the given canvas width.
        Satisfies:  16*cell_w + 15*gw_w == total_w
                    gw_w == cell_w * _GW_RATIO
        """
        cell_w = total_w / (16 + 15 * _GW_RATIO)
        gw_w   = cell_w * _GW_RATIO
        return cell_w, gw_w

    def _redraw_vis(self, main_colors=None, wall_colors=None):
        cv = self._vis
        cv.update_idletasks()
        w = cv.winfo_width() or 560
        cv.delete("all")
        self._main_rects = []
        self._gw_rects   = []

        main_colors = list(main_colors or [_CELL_IDLE] * 16)[:16]
        wall_colors = list(wall_colors or [_GW_IDLE]   * 15)[:15]

        cell_w, gw_w = self._vis_layout(w)
        x = 0.0

        for i in range(16):
            # ── Main cell ──────────────────────────────────────────────────
            r, g, b = main_colors[i]
            self._main_rects.append(
                cv.create_rectangle(
                    x, _CELL_Y0, x + cell_w, _CELL_Y1,
                    fill=f"#{r:02x}{g:02x}{b:02x}", outline=""
                )
            )
            x += cell_w

            # ── Guide-wall divider (between cells, not after the last) ─────
            if i < 15:
                r, g, b = wall_colors[i]
                self._gw_rects.append(
                    cv.create_rectangle(
                        x, _GW_Y0, x + gw_w, _GW_Y1,
                        fill=f"#{r:02x}{g:02x}{b:02x}", outline=""
                    )
                )
                x += gw_w

    def _update_vis(self, main_colors, wall_colors):
        cv = self._vis
        main_colors = list(main_colors or [])[:16]
        wall_colors = list(wall_colors or [])[:15]

        if len(self._main_rects) != 16 or len(self._gw_rects) != 15:
            self._redraw_vis(main_colors, wall_colors)
            return

        for i, (r, g, b) in enumerate(main_colors):
            cv.itemconfig(self._main_rects[i],
                          fill=f"#{r:02x}{g:02x}{b:02x}")
        for i, (r, g, b) in enumerate(wall_colors):
            cv.itemconfig(self._gw_rects[i],
                          fill=f"#{r:02x}{g:02x}{b:02x}")

    # ── Status strip ──────────────────────────────────────────────────────────
    def _build_status(self):
        row = tk.Frame(self, bg=SURFACE2)
        row.pack(fill="x")
        inner = tk.Frame(row, bg=SURFACE2)
        inner.pack(fill="x", padx=20, pady=8)

        self._dot_lbl = tk.Label(inner, text="●", bg=SURFACE2, fg=TEXTDIM,
                                  font=(MONO, 10))
        self._dot_lbl.pack(side="left")

        self._status_var = tk.StringVar(value="IDLE")
        tk.Label(inner, textvariable=self._status_var,
                 bg=SURFACE2, fg=TEXTMID,
                 font=(MONO, 9, "bold")).pack(side="left", padx=(6, 0))

        self._pkt_var = tk.StringVar(value="0 pkts")
        tk.Label(inner, textvariable=self._pkt_var,
                 bg=SURFACE2, fg=TEXTDIM,
                 font=(MONO, 8)).pack(side="right")

    # ── Config panel ──────────────────────────────────────────────────────────
    def _build_config(self):
        panel = tk.Frame(self, bg=SURFACE)
        panel.pack(fill="x")
        self._section_label(panel, "CONFIGURATION")

        grid = tk.Frame(panel, bg=SURFACE)
        grid.pack(fill="x", padx=20, pady=(0, 14))
        grid.columnconfigure(1, weight=1)

        def lbl(text, row):
            tk.Label(grid, text=text, bg=SURFACE, fg=TEXTDIM,
                     font=(MONO, 8), width=13, anchor="w"
                     ).grid(row=row, column=0, pady=6, sticky="w")

        def entry_widget(row, var, width=10):
            e = tk.Entry(
                grid, textvariable=var, width=width,
                bg=SURFACE2, fg=TEXT, insertbackground=ACCENT,
                relief="flat", bd=0, font=(MONO, 9),
                highlightthickness=1,
                highlightbackground=BORDER,
                highlightcolor=ACCENT
            )
            e.grid(row=row, column=1, pady=6, sticky="w")
            return e

        # Row 0: WebSocket Port
        lbl("WS PORT", 0)
        self._ws_port_var = tk.StringVar(value="5001")
        entry_widget(0, self._ws_port_var, width=10)

        # Row 1: FPS — locked to 120
        self._fps_var = tk.IntVar(value=120)

        # Row 2: Serial Bypass toggle
        lbl("SERIAL", 2)
        bypass_frame = tk.Frame(grid, bg=SURFACE)
        bypass_frame.grid(row=2, column=1, pady=6, sticky="w")

        self._bypass_var = tk.BooleanVar(value=False)

        # Custom toggle-style checkbutton
        self._bypass_cb = tk.Checkbutton(
            bypass_frame,
            text="Bypass COM1  (visualiser only)",
            variable=self._bypass_var,
            command=self._on_bypass_toggle,
            bg=SURFACE, fg=TEXTMID,
            selectcolor=SURFACE2,
            activebackground=SURFACE,
            activeforeground=YELLOW,
            font=(MONO, 8),
            cursor="hand2",
        )
        self._bypass_cb.pack(side="left")

        # Indicator label that changes colour with state
        self._bypass_lbl = tk.Label(
            bypass_frame, text="● LIVE",
            bg=SURFACE, fg=GREEN,
            font=(MONO, 8, "bold")
        )
        self._bypass_lbl.pack(side="left", padx=(10, 0))

    def _on_bypass_toggle(self):
        if self._bypass_var.get():
            self._bypass_lbl.config(text="● BYPASS", fg=YELLOW)
        else:
            self._bypass_lbl.config(text="● LIVE", fg=GREEN)
        # If bridge is running, restart it with new bypass state
        if self._running:
            self._append_log("WARN",
                "Bypass changed — restarting bridge…")
            self._stop_bridge()
            self._start_bridge()

    # ── Brightness 0-254 ─────────────────────────────────────────────────────
    def _build_brightness(self):
        panel = tk.Frame(self, bg=SURFACE)
        panel.pack(fill="x")
        self._section_label(panel, "BRIGHTNESS")

        inner = tk.Frame(panel, bg=SURFACE)
        inner.pack(fill="x", padx=20, pady=(0, 14))

        val_row = tk.Frame(inner, bg=SURFACE)
        val_row.pack(fill="x", pady=(0, 6))
        tk.Label(val_row, text="Level  (0 – 254)",
                 bg=SURFACE, fg=TEXTDIM,
                 font=(MONO, 8)).pack(side="left")
        self._brt_val = tk.StringVar(value="200")
        tk.Label(val_row, textvariable=self._brt_val,
                 bg=SURFACE, fg=ACCENT,
                 font=(MONO, 9, "bold")).pack(side="right")

        self._brt_slider = tk.Scale(
            inner, from_=0, to=254, orient="horizontal",
            bg=SURFACE, fg=TEXT, troughcolor=BORDER,
            activebackground=ACCENT,
            highlightthickness=0, showvalue=False, bd=0,
            command=self._on_brightness
        )
        self._brt_slider.set(200)
        self._brt_slider.pack(fill="x")

    # ── Start/Stop ────────────────────────────────────────────────────────────
    def _build_controls(self):
        panel = tk.Frame(self, bg=BG)
        panel.pack(fill="x", padx=20, pady=14)

        self._start_btn = tk.Button(
            panel, text="⬡  START BRIDGE",
            command=self._toggle,
            bg=SURFACE2, fg=ACCENT,
            activebackground=RED, activeforeground=TEXT,
            relief="flat", bd=0, cursor="hand2",
            font=(MONO, 11, "bold"),
            pady=10,
            highlightthickness=1,
            highlightbackground=ACCENT,
            highlightcolor=ACCENT,
        )
        self._start_btn.pack(fill="x")
        self._start_btn.bind("<Enter>", lambda e: self._start_btn.config(
            bg=ACCENT if not self._running else RED, fg=BG))
        self._start_btn.bind("<Leave>", lambda e: self._start_btn.config(
            bg=SURFACE2, fg=RED if self._running else ACCENT))

    # ── Log ───────────────────────────────────────────────────────────────────
    def _build_log(self):
        hdr = tk.Frame(self, bg=SURFACE)
        hdr.pack(fill="x")
        hdr_inner = tk.Frame(hdr, bg=SURFACE)
        hdr_inner.pack(fill="x", padx=20, pady=(8, 6))
        tk.Label(hdr_inner, text="LOG OUTPUT", bg=SURFACE, fg=TEXTDIM,
                 font=(MONO, 8)).pack(side="left")
        tk.Button(hdr_inner, text="CLEAR", command=self._clear_log,
                   bg=SURFACE, fg=TEXTDIM, relief="flat", bd=0,
                   font=(MONO, 7), cursor="hand2",
                   activebackground=SURFACE, activeforeground=RED
                   ).pack(side="right")
        hsep(hdr)

        frame = tk.Frame(self, bg=BG)
        frame.pack(fill="both", expand=True)

        self._log_text = tk.Text(
            frame, bg=BG, fg=TEXT,
            font=(MONO, 9), bd=0, relief="flat",
            state="disabled", wrap="word",
            height=9, padx=20, pady=8
        )
        scroll = tk.Scrollbar(frame, command=self._log_text.yview,
                               bg=BORDER, troughcolor=BG, bd=0,
                               relief="flat", width=6)
        self._log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._log_text.pack(fill="both", expand=True)
        self._log_text.tag_config("INFO",  foreground=TEXTMID)
        self._log_text.tag_config("WARN",  foreground=YELLOW)
        self._log_text.tag_config("ERROR", foreground=RED)
        self._log_text.tag_config("TS",    foreground=TEXTDIM)

    # ── Hint footer ───────────────────────────────────────────────────────────
    def _build_hint(self):
        foot = tk.Frame(self, bg=SURFACE)
        foot.pack(fill="x")
        hsep(foot)
        self._hint_var = tk.StringVar(
            value="→  Set UMIGURI LED Server port to 5001"
        )
        tk.Label(foot, textvariable=self._hint_var,
                 bg=SURFACE, fg=TEXTDIM, font=(MONO, 8)).pack(
                     padx=20, pady=8, anchor="w")

    # ── Section label helper ──────────────────────────────────────────────────
    def _section_label(self, parent, text):
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x", padx=20, pady=(10, 8))
        tk.Label(row, text=text, bg=SURFACE, fg=TEXTDIM,
                 font=(MONO, 8)).pack(side="left")
        tk.Frame(row, bg=BORDER, height=1).pack(
            side="left", fill="x", expand=True, padx=(8, 0), pady=4)

    # ── Callbacks ─────────────────────────────────────────────────────────────
    def _on_brightness(self, val):
        v = int(float(val))
        self._brt_val.set(str(v))
        if self._bridge:
            self._bridge.brightness = v

    def _toggle(self):
        if self._running:
            self._stop_bridge()
        else:
            self._start_bridge()

    def _start_bridge(self):
        try:
            ws_port = int(self._ws_port_var.get())
            fps     = int(self._fps_var.get())
            bright  = int(self._brt_slider.get())
        except ValueError as e:
            self._append_log("ERROR", f"Invalid config: {e}")
            return

        if not (1024 <= ws_port <= 65535):
            self._append_log("ERROR",
                f"WS port {ws_port} is out of valid range 1024–65535")
            return

        bypass = self._bypass_var.get()

        self._hint_var.set(
            f"→  Set UMIGURI LED Server port to {ws_port}"
        )
        self._bridge = BridgeCore(
            ws_port, bright, fps,
            self._log_q, self._led_q,
            bypass_serial=bypass
        )
        self._bridge.start()
        self._running = True
        self._start_btn.config(
            text="■  STOP BRIDGE",
            fg=RED, highlightbackground=RED, highlightcolor=RED,
        )
        mode_str = "BYPASS" if bypass else f"COM1 @ {FIXED_BAUD}"
        self._set_status(f"WAITING FOR UMIGURI…  [{mode_str}]", ACCENT)

    def _stop_bridge(self):
        if self._bridge:
            self._bridge.stop()
            self._bridge = None
        self._running = False
        self._start_btn.config(
            text="⬡  START BRIDGE",
            fg=ACCENT, highlightbackground=ACCENT, highlightcolor=ACCENT,
        )
        self._set_status("IDLE", TEXTDIM)
        self._update_vis([_CELL_IDLE] * 16, [_GW_IDLE] * 15)

    def _set_status(self, text, color):
        self._dot_lbl.config(fg=color)
        self._status_var.set(text)

    def _append_log(self, level, msg):
        ts = time.strftime("%H:%M:%S")
        self._log_text.config(state="normal")
        self._log_text.insert("end", f"[{ts}] ", "TS")
        self._log_text.insert("end", f"{level:<5}  ", level)
        self._log_text.insert("end", f"{msg}\n", "INFO")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _clear_log(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    # ── 100 ms poll ───────────────────────────────────────────────────────────
    def _poll(self):
        # Drain log queue
        try:
            while True:
                level, msg = self._log_q.get_nowait()
                self._append_log(level, msg)
                if level == "ERROR" and "serial" in msg.lower():
                    self._set_status("SERIAL ERROR", RED)
        except queue.Empty:
            pass

        # Drain LED queue — keep only the latest frame
        latest = None
        try:
            while True:
                latest = self._led_q.get_nowait()
        except queue.Empty:
            pass
        if latest:
            main_c, wall_c = latest
            self._update_vis(main_c, wall_c)

        # Update status / packet counter
        if self._bridge and self._running:
            s = self._bridge.stats
            self._pkt_var.set(f"{s['packets']:,} pkts")
            c = s["clients"]
            bypass = self._bridge.bypass_serial
            if not bypass and not s["serial_ok"]:
                self._set_status("SERIAL ERROR", RED)
            elif c > 0:
                tag = " [BYPASS]" if bypass else ""
                self._set_status(
                    f"CONNECTED  ({c} client{'s' if c > 1 else ''}){tag}",
                    GREEN
                )
            else:
                tag = "  [BYPASS]" if bypass else ""
                self._set_status(f"WAITING FOR UMIGURI…{tag}", ACCENT)

        self.after(100, self._poll)


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.geometry("560x720")
    app.mainloop()