"""
Terumo BR-500 reader — minimal & robust.

Architecture:
    - Background thread reads from serial and pushes events to a queue.
    - Tk main thread drains the queue at a fixed cadence (every 100 ms).
    - No work is ever done on the serial-read thread that touches Tk.
    - The log widget is capped (last N lines only).

Behaviour:
    - On every R1 poll from the device, we send one byte: ACK (0x06) or
      whatever you typed in the "Reply" entry.
    - Use the "Try next candidate" button to step through alternative
      replies one at a time when ACK doesn't elicit data.
"""

import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import serial
import serial.tools.list_ports


DEFAULT_PORT = "COM8"
DEFAULT_BAUD = 19200

STX = 0x02
ETX = 0x03

CANDIDATES = [
    ("ACK",       b"\x06"),
    ("NAK",       b"\x15"),
    ("ENQ",       b"\x05"),
    ("D1 frame",  None),    # built at runtime
    ("D0 frame",  None),
    ("G1 frame",  None),
    ("S1 frame",  None),
    ("R2 frame",  None),
    ("R1 echo",   None),
    ("CRLF",      b"\r\n"),
    ("OK CRLF",   b"OK\r\n"),
]
CMD_BYTES = {"D1 frame": b"D1,", "D0 frame": b"D0,",
             "G1 frame": b"G1,", "S1 frame": b"S1,", "R2 frame": b"R2,"}

LOG_MAX_LINES = 400


def bcc(data: bytes) -> int:
    return sum(data) & 0xFF


def build_frame(body: bytes) -> bytes:
    inner = bytes([STX]) + body + bytes([ETX])
    return inner + bytes([bcc(inner)])


def parse_escape(text: str) -> bytes:
    out = bytearray()
    i = 0
    while i < len(text):
        if (text[i] == "\\" and i + 3 < len(text) and text[i + 1] == "x"):
            try:
                out.append(int(text[i + 2:i + 4], 16))
                i += 4
                continue
            except ValueError:
                pass
        out.append(ord(text[i]))
        i += 1
    return bytes(out)


# ---------- serial worker ----------
class SerialWorker(threading.Thread):
    """Reads bytes, finds STX..ETX BCC frames, posts events to a queue."""

    def __init__(self, ser, evq, stop_flag):
        super().__init__(daemon=True)
        self.ser = ser
        self.evq = evq
        self.stop_flag = stop_flag

    def run(self):
        buf = bytearray()
        while not self.stop_flag.is_set():
            try:
                chunk = self.ser.read(256)
            except Exception as e:
                self.evq.put(("error", str(e)))
                return
            if not chunk:
                continue
            buf.extend(chunk)
            self._extract_frames(buf)

    def _extract_frames(self, buf: bytearray):
        consumed = 0
        i = 0
        while i < len(buf):
            if buf[i] != STX:
                i += 1
                consumed = i
                continue
            j = buf.find(ETX, i + 1)
            if j == -1 or j + 1 >= len(buf):
                break
            frame = bytes(buf[i:j + 2])
            self.evq.put(("frame", frame))
            i = j + 2
            consumed = i
        if consumed:
            del buf[:consumed]


# ---------- app ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Terumo BR-500 Reader")
        self.geometry("700x620")

        self.ser = None
        self.worker = None
        self.stop_flag = threading.Event()
        self.evq = queue.Queue()
        self.cand_idx = 0
        self.last_frame = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain)

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Port:").pack(side="left")
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        ttk.Combobox(top, textvariable=self.port_var, width=10,
                     values=[p.device for p in serial.tools.list_ports.comports()]
                     ).pack(side="left", padx=4)

        ttk.Label(top, text="Baud:").pack(side="left")
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        ttk.Combobox(top, textvariable=self.baud_var, width=8,
                     values=["9600", "19200", "38400"]
                     ).pack(side="left", padx=4)

        self.connect_btn = ttk.Button(top, text="Connect", command=self.toggle_connect)
        self.connect_btn.pack(side="left", padx=4)

        ttk.Button(top, text="Clear log",
                   command=lambda: self.log.delete("1.0", "end")
                   ).pack(side="left", padx=4)

        # reply
        rep = ttk.LabelFrame(self, text="Reply on every R1 poll", padding=8)
        rep.pack(fill="x", padx=10)

        self.reply_var = tk.StringVar(value=r"\x06")
        ttk.Label(rep, text="Reply:").grid(row=0, column=0, sticky="w")
        ttk.Entry(rep, textvariable=self.reply_var, width=40
                  ).grid(row=0, column=1, padx=4, sticky="we")
        rep.columnconfigure(1, weight=1)

        ttk.Button(rep, text="Try next candidate",
                   command=self.next_candidate
                   ).grid(row=0, column=2, padx=4)

        self.cand_label = tk.StringVar(value=f"now: {CANDIDATES[0][0]}")
        ttk.Label(rep, textvariable=self.cand_label,
                  foreground="#06c").grid(row=1, column=0, columnspan=3, sticky="w")

        # manual entry
        manual = ttk.LabelFrame(self, text="Manual entry (type values from receipt)", padding=8)
        manual.pack(fill="x", padx=10, pady=(0, 4))
        self.in_sys   = tk.StringVar()
        self.in_dia   = tk.StringVar()
        self.in_pulse = tk.StringVar()
        ttk.Label(manual, text="SYS:").grid(row=0, column=0, sticky="w")
        ttk.Entry(manual, textvariable=self.in_sys, width=6,
                  font=("Segoe UI", 12)).grid(row=0, column=1, padx=4)
        ttk.Label(manual, text="DIA:").grid(row=0, column=2, sticky="w")
        ttk.Entry(manual, textvariable=self.in_dia, width=6,
                  font=("Segoe UI", 12)).grid(row=0, column=3, padx=4)
        ttk.Label(manual, text="PULSE:").grid(row=0, column=4, sticky="w")
        ttk.Entry(manual, textvariable=self.in_pulse, width=6,
                  font=("Segoe UI", 12)).grid(row=0, column=5, padx=4)
        ttk.Button(manual, text="Save", command=self.save_manual
                   ).grid(row=0, column=6, padx=8)
        ttk.Button(manual, text="Clear", command=self.clear_manual
                   ).grid(row=0, column=7)

        # readout
        readout = ttk.LabelFrame(self, text="Latest reading", padding=15)
        readout.pack(fill="x", padx=10, pady=8)
        self.sys_var   = tk.StringVar(value="---")
        self.dia_var   = tk.StringVar(value="---")
        self.pulse_var = tk.StringVar(value="---")
        self.time_var  = tk.StringVar(value="(waiting for data...)")
        self._field(readout, "SYSTOLIC",  self.sys_var,   "mmHg", 0)
        self._field(readout, "DIASTOLIC", self.dia_var,   "mmHg", 1)
        self._field(readout, "PULSE",     self.pulse_var, "bpm",  2)
        ttk.Label(readout, textvariable=self.time_var,
                  foreground="#666").grid(row=3, column=0, columnspan=3,
                                          sticky="w", pady=(8, 0))

        # log
        logf = ttk.LabelFrame(self, text="Log", padding=5)
        logf.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log = tk.Text(logf, height=12, font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

        self.status = tk.StringVar(value="disconnected")
        ttk.Label(self, textvariable=self.status, anchor="w",
                  relief="sunken").pack(fill="x", side="bottom")

    def _field(self, parent, label, var, unit, row):
        ttk.Label(parent, text=label, width=11,
                  font=("Segoe UI", 11)).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Label(parent, textvariable=var, width=7, anchor="e",
                  font=("Segoe UI", 22, "bold")
                  ).grid(row=row, column=1, padx=8)
        ttk.Label(parent, text=unit,
                  font=("Segoe UI", 11)).grid(row=row, column=2, sticky="w")

    # ---------- log ----------
    def _log(self, msg):
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        # cap line count
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > LOG_MAX_LINES:
            self.log.delete("1.0", f"{lines - LOG_MAX_LINES}.0")
        self.log.see("end")

    # ---------- candidate cycling ----------
    def _build_candidate(self, idx):
        label, fixed = CANDIDATES[idx % len(CANDIDATES)]
        if fixed is not None:
            return label, fixed
        if label == "R1 echo":
            return label, self.last_frame or b""
        return label, build_frame(CMD_BYTES.get(label, b""))

    def next_candidate(self):
        self.cand_idx = (self.cand_idx + 1) % len(CANDIDATES)
        label, data = self._build_candidate(self.cand_idx)
        # express data as escape string for the entry box
        self.reply_var.set("".join(f"\\x{b:02x}" if b < 0x20 or b > 0x7e
                                   else chr(b) for b in data))
        self.cand_label.set(f"now: {label}  ({data!r})")
        self._log(f"switched reply to: {label} {data!r}")

    # ---------- connect ----------
    def toggle_connect(self):
        if self.ser and self.ser.is_open:
            self.stop_flag.set()
            if self.worker:
                self.worker.join(timeout=1.0)
            self.ser.close()
            self.ser = None
            self.connect_btn.config(text="Connect")
            self.status.set("disconnected")
            self._log("port closed")
            return
        try:
            self.ser = serial.Serial(
                port=self.port_var.get(),
                baudrate=int(self.baud_var.get()),
                bytesize=8, parity=serial.PARITY_NONE,
                stopbits=1, timeout=0.2,
            )
        except Exception as e:
            messagebox.showerror("Open failed", str(e))
            return
        self.stop_flag.clear()
        self.worker = SerialWorker(self.ser, self.evq, self.stop_flag)
        self.worker.start()
        self.connect_btn.config(text="Disconnect")
        self.status.set(f"listening on {self.ser.port} @ {self.ser.baudrate}")
        self._log(f"opened {self.ser.port} @ {self.ser.baudrate}")

    # ---------- queue drain on Tk thread ----------
    def _drain(self):
        try:
            for _ in range(50):                       # cap per tick
                kind, payload = self.evq.get_nowait()
                if kind == "frame":
                    self._on_frame(payload)
                elif kind == "error":
                    self._log(f"error: {payload}")
        except queue.Empty:
            pass
        finally:
            self.after(100, self._drain)

    def _on_frame(self, frame: bytes):
        if len(frame) < 4 or frame[0] != STX or frame[-2] != ETX:
            self._log(f"<- (bad frame) {frame!r}")
            return
        self.last_frame = frame
        body = frame[1:-2]
        text = body.decode("ascii", errors="replace")
        is_r1 = text.startswith("R1")
        tag = "[R1 poll]" if is_r1 else "*** DATA ***"
        self._log(f"<- {frame!r}  body={text!r}  {tag}")

        # send the configured reply only on R1 polls
        if is_r1:
            self._send(parse_escape(self.reply_var.get()))

        # try to extract numbers from frames
        if is_r1:
            # R1,ID,YYMMDD,HHMMSS,SYS,MAP,DIA,PULSE,...
            parts = text.split(',')
            if len(parts) >= 8:
                try:
                    sys_v = int(parts[4])
                    dia_v = int(parts[6])
                    pulse_v = int(parts[7])
                    self.sys_var.set(str(sys_v))
                    self.dia_var.set(str(dia_v))
                    self.pulse_var.set(str(pulse_v))
                    self.time_var.set("received " + time.strftime("%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    pass
        else:
            nums = [int(n) for n in re.findall(r"\d+", text)]
            bp = [n for n in nums if 20 <= n <= 250]
            if len(bp) >= 3:
                self.sys_var.set(str(bp[0]))
                self.dia_var.set(str(bp[1]))
                self.pulse_var.set(str(bp[2]))
                self.time_var.set("received " + time.strftime("%Y-%m-%d %H:%M:%S"))

    def _send(self, data: bytes):
        if not (self.ser and self.ser.is_open) or not data:
            return
        try:
            self.ser.write(data)
            self.ser.flush()
            self._log(f"-> {data!r}")
        except Exception as e:
            self._log(f"send error: {e}")

    def save_manual(self):
        try:
            s, d, p = int(self.in_sys.get()), int(self.in_dia.get()), int(self.in_pulse.get())
        except ValueError:
            messagebox.showerror("Bad input", "SYS / DIA / PULSE must be integers")
            return
        self.sys_var.set(str(s))
        self.dia_var.set(str(d))
        self.pulse_var.set(str(p))
        self.time_var.set("entered " + time.strftime("%Y-%m-%d %H:%M:%S"))
        self._log(f"manual entry: SYS={s} DIA={d} PULSE={p}")

    def clear_manual(self):
        self.in_sys.set("")
        self.in_dia.set("")
        self.in_pulse.set("")
        self.sys_var.set("---")
        self.dia_var.set("---")
        self.pulse_var.set("---")
        self.time_var.set("(waiting for data...)")

    def _on_close(self):
        self.stop_flag.set()
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
