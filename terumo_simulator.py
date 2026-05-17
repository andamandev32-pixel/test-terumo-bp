"""
Terumo BR-500 simulator.

Sends fake blood-pressure measurements out a serial port so the reader
program can be tested without the real device.

Typical setup on Windows:
    1. Install com0com (free virtual null-modem driver).
    2. Create a pair, e.g. COM8  <->  COM9.
    3. Run terumo_reader.py    on  COM8
       Run terumo_simulator.py on  COM9
    4. Press "Send" in the simulator — the reader will pick it up.

Frame format mirrors the BR-500 receipt (date, SYS, DIA, PULSE, PRP).
Edit FRAME_TEMPLATE below if you want to match a different format.
"""

import random
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import serial
import serial.tools.list_ports


DEFAULT_PORT = "COM9"
DEFAULT_BAUD = 19200

# ASCII frame the simulator will send. Tokens {date} {time} {sys} {dia}
# {pulse} {prp} are filled in before transmit. \r\n terminates each line.
FRAME_TEMPLATE = (
    "{date} {time}\r\n"
    "SYSTOLIC,{sys},mmHg\r\n"
    "DIASTOLIC,{dia},mmHg\r\n"
    "PULSE,{pulse},bpm\r\n"
    "PRP,{prp}\r\n"
)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Terumo BR-500 Simulator")
        self.geometry("560x600")

        self.ser = None
        self.auto_thread = None
        self.auto_stop = threading.Event()

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        ttk.Combobox(top, textvariable=self.port_var, width=10,
                     values=[p.device for p in serial.tools.list_ports.comports()]
                     ).grid(row=0, column=1, padx=4)

        ttk.Label(top, text="Baud:").grid(row=0, column=2, sticky="w")
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        ttk.Combobox(top, textvariable=self.baud_var, width=8,
                     values=["9600", "19200", "38400", "57600", "115200"]
                     ).grid(row=0, column=3, padx=4)

        self.connect_btn = ttk.Button(top, text="Open", command=self.toggle_connect)
        self.connect_btn.grid(row=0, column=4, padx=4)

        # Values
        vals = ttk.LabelFrame(self, text="Measurement values", padding=15)
        vals.pack(fill="x", padx=10, pady=8)

        self.sys_var   = tk.StringVar(value="130")
        self.dia_var   = tk.StringVar(value="85")
        self.pulse_var = tk.StringVar(value="78")
        self.prp_var   = tk.StringVar(value="11050")
        self._field(vals, "SYSTOLIC",  self.sys_var,   "mmHg", 0)
        self._field(vals, "DIASTOLIC", self.dia_var,   "mmHg", 1)
        self._field(vals, "PULSE",     self.pulse_var, "bpm",  2)
        self._field(vals, "PRP",       self.prp_var,   "",     3)

        ttk.Button(vals, text="Random",
                   command=self.randomize).grid(row=4, column=0, pady=(8, 0))

        # Send controls
        ctrls = ttk.Frame(self, padding=10)
        ctrls.pack(fill="x")

        self.send_btn = ttk.Button(ctrls, text="Send once",
                                   command=self.send_once, state="disabled")
        self.send_btn.pack(side="left", padx=4)

        self.auto_var = tk.BooleanVar(value=False)
        self.auto_chk = ttk.Checkbutton(ctrls, text="Auto-send every",
                                        variable=self.auto_var,
                                        command=self.toggle_auto,
                                        state="disabled")
        self.auto_chk.pack(side="left", padx=4)
        self.interval_var = tk.StringVar(value="5")
        ttk.Entry(ctrls, textvariable=self.interval_var, width=4
                  ).pack(side="left")
        ttk.Label(ctrls, text="sec").pack(side="left", padx=(2, 0))

        self.auto_rand_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrls, text="สุ่มค่าใหม่ทุกครั้งที่ส่ง", variable=self.auto_rand_var).pack(side="left", padx=10)

        # Log
        logf = ttk.LabelFrame(self, text="Sent frames", padding=5)
        logf.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log = tk.Text(logf, height=12, font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

        self.status = tk.StringVar(value="closed")
        ttk.Label(self, textvariable=self.status, anchor="w",
                  relief="sunken").pack(fill="x", side="bottom")

    def _field(self, parent, label, var, unit, row):
        ttk.Label(parent, text=label, width=11).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=var, width=8,
                  font=("Segoe UI", 14)).grid(row=row, column=1, padx=8)
        ttk.Label(parent, text=unit).grid(row=row, column=2, sticky="w")

    def _log(self, msg):
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log.see("end")

    # ---------- actions ----------
    def randomize(self):
        sys_ = random.randint(105, 150)
        dia  = random.randint(65, 95)
        self.sys_var.set(str(sys_))
        self.dia_var.set(str(dia))
        self.pulse_var.set(str(random.randint(60, 100)))
        self.prp_var.set(str(sys_ * random.randint(70, 100)))

    def toggle_connect(self):
        if self.ser and self.ser.is_open:
            if self.auto_var.get():
                self.auto_var.set(False)
                self.toggle_auto()
            self.ser.close()
            self.ser = None
            self.status.set("closed")
            self.connect_btn.config(text="Open")
            self.send_btn.config(state="disabled")
            self.auto_chk.config(state="disabled")
            self._log("port closed")
            return
        try:
            self.ser = serial.Serial(
                port=self.port_var.get(),
                baudrate=int(self.baud_var.get()),
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.5,
            )
        except Exception as e:
            messagebox.showerror("Open failed", str(e))
            return
        self.status.set(f"open {self.ser.port} @ {self.ser.baudrate}")
        self.connect_btn.config(text="Close")
        self.send_btn.config(state="normal")
        self.auto_chk.config(state="normal")
        self._log(f"opened {self.ser.port}")

    def _build_frame(self):
        now = time.localtime()
        date_str = time.strftime("%y%m%d", now)
        time_str = time.strftime("%H%M%S", now)
        sys_str = str(self.sys_var.get()).zfill(3)
        dia_str = str(self.dia_var.get()).zfill(3)
        pulse_str = str(self.pulse_var.get()).zfill(3)
        
        try:
            sys = int(self.sys_var.get())
            dia = int(self.dia_var.get())
            map_val = round((sys + 2 * dia) / 3)
            map_str = str(map_val).zfill(3)
        except ValueError:
            map_str = "000"

        # R1,ID,YYMMDD,HHMMSS,SYS,MAP,DIA,PULSE,0000,0000,00000,000
        body = f"R1,000000000,{date_str},{time_str},{sys_str},{map_str},{dia_str},{pulse_str},0000,0000,00000,000".encode('ascii')
        
        # STX = 0x02, ETX = 0x03
        inner = bytes([0x02]) + body + bytes([0x03])
        bcc = sum(inner) & 0xFF
        return inner + bytes([bcc])

    def send_once(self):
        if not (self.ser and self.ser.is_open):
            return
        if getattr(self, 'auto_rand_var', None) and self.auto_rand_var.get():
            self.randomize()
        try:
            frame = self._build_frame()
            self.ser.write(frame)
            self.ser.flush()
            self._log(f"-> {frame!r}")
        except Exception as e:
            self._log(f"send error: {e}")

    def toggle_auto(self):
        if self.auto_var.get():
            try:
                interval = max(0.5, float(self.interval_var.get()))
            except ValueError:
                self.auto_var.set(False)
                messagebox.showerror("Bad interval", "interval must be a number")
                return
            self.auto_stop.clear()
            self.auto_thread = threading.Thread(
                target=self._auto_loop, args=(interval,), daemon=True)
            self.auto_thread.start()
            self._log(f"auto-send started ({interval:g}s)")
        else:
            self.auto_stop.set()
            if self.auto_thread:
                self.auto_thread.join(timeout=1.0)
            self._log("auto-send stopped")

    def _auto_loop(self, interval):
        while not self.auto_stop.is_set():
            self.after(0, self.send_once)
            self.auto_stop.wait(interval)

    def _on_close(self):
        self.auto_stop.set()
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
