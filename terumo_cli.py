"""
Terumo BR-500 reader — terminal version.

Usage:
    python terumo_cli.py                  # COM8 @ 19200, reply ACK
    python terumo_cli.py COM3             # different port
    python terumo_cli.py COM8 9600        # different baud
    python terumo_cli.py COM8 19200 \\x02D1,\\x03   # custom reply on R1 poll
"""

import re
import sys
import time

import serial


STX = 0x02
ETX = 0x03


def bcc(data: bytes) -> int:
    return sum(data) & 0xFF


def build_frame(body: bytes) -> bytes:
    inner = bytes([STX]) + body + bytes([ETX])
    return inner + bytes([bcc(inner)])


def parse_escape(text: str) -> bytes:
    out = bytearray()
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 3 < len(text) and text[i + 1] == "x":
            try:
                out.append(int(text[i + 2:i + 4], 16))
                i += 4
                continue
            except ValueError:
                pass
        out.append(ord(text[i]))
        i += 1
    return bytes(out)


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def main():
    port  = sys.argv[1] if len(sys.argv) > 1 else "COM8"
    baud  = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
    reply = parse_escape(sys.argv[3]) if len(sys.argv) > 3 else b"\x06"

    ser = serial.Serial(port, baud, bytesize=8, parity="N",
                        stopbits=1, timeout=0.2)
    print(f"[{stamp()}] opened {port} @ {baud} — Ctrl+C to quit")
    print(f"[{stamp()}] reply on R1 poll = {reply!r}\n")

    buf = bytearray()
    try:
        while True:
            chunk = ser.read(256)
            if not chunk:
                continue
            buf.extend(chunk)

            # extract STX..ETX BCC frames
            while True:
                try:
                    si = buf.index(STX)
                except ValueError:
                    buf.clear()
                    break
                if si:
                    del buf[:si]
                try:
                    ei = buf.index(ETX, 1)
                except ValueError:
                    break                       # incomplete
                if ei + 1 >= len(buf):
                    break                       # waiting for BCC
                frame = bytes(buf[:ei + 2])
                del buf[:ei + 2]

                body = frame[1:-2]
                text = body.decode("ascii", errors="replace")
                got_bcc = frame[-1]
                want_bcc = bcc(frame[:-1])
                bcc_ok = "ok" if got_bcc == want_bcc else \
                    f"BAD got={got_bcc:#04x} want={want_bcc:#04x}"

                is_r1 = text.startswith("R1")
                tag = "[R1 poll]" if is_r1 else "*** DATA ***"
                print(f"[{stamp()}] <- {frame!r}  body={text!r}  bcc={bcc_ok}  {tag}")

                if is_r1:
                    ser.write(reply)
                    ser.flush()
                    print(f"[{stamp()}] -> {reply!r}")
                else:
                    nums = [int(n) for n in re.findall(r"\d+", text)]
                    bp = [n for n in nums if 20 <= n <= 250]
                    if len(bp) >= 3:
                        print(f"\n  >>> SYSTOLIC = {bp[0]} mmHg")
                        print(f"  >>> DIASTOLIC= {bp[1]} mmHg")
                        print(f"  >>> PULSE    = {bp[2]} bpm\n")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
