"""Real macOS PTY regression: resize, navigation, pause, monochrome and exit."""
import fcntl
import os
import pathlib
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest


@unittest.skipUnless(sys.platform == "darwin", "requires macOS collectors and curses")
class TerminalTests(unittest.TestCase):
    def test_real_terminal_resize_and_clean_exit(self):
        script = pathlib.Path(__file__).parents[1] / "macvitals" / "macvitals.py"
        for terminal in ("xterm-256color", "xterm", "vt100"):
            with self.subTest(terminal=terminal):
                master, slave = os.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
                proc = subprocess.Popen(
                    [sys.executable, str(script), "-i", "0.5"],
                    stdin=slave, stdout=slave, stderr=slave,
                    env={**os.environ, "TERM": terminal, "LC_ALL": "en_US.UTF-8"},
                )
                os.close(slave)
                output = bytearray()

                def drain(seconds):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], min(.05, max(0, deadline - time.monotonic())))[0]:
                            try:
                                block = os.read(master, 65536)
                            except OSError:
                                break
                            if not block:
                                break
                            output.extend(block)

                try:
                    drain(1.5)
                    for height, width in ((5, 20), (5, 24), (12, 55), (30, 100)):
                        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
                        proc.send_signal(signal.SIGWINCH)
                        os.write(master, b"123456jjkk5mc1pp")
                        drain(.2)
                    os.write(master, b"q")
                    drain(.5)
                    proc.wait(timeout=8)
                    self.assertEqual(proc.returncode, 0, output.decode(errors="replace")[-2000:])
                    self.assertNotIn(b"Traceback", output)
                    self.assertIn(b"MacVitals", output)
                    self.assertIn("终端过小".encode(), output)
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=8)
                    os.close(master)


if __name__ == "__main__":
    unittest.main()
