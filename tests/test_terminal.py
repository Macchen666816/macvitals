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
    def test_action_confirmations_cancel_in_real_terminal_without_mutation(self):
        program = '''
import curses,time
from macvitals.macvitals import MonitorUI,MonitorData
from macvitals.docker_monitor import DockerContainer,DockerSnapshot
from macvitals.docker_actions import ActionResult
class Actions:
 def snapshot(self): return ActionResult()
 def submit(self,*args): raise AssertionError('Unexpected action submission')
def run(screen):
 screen.keypad(True)
 data=MonitorData()
 data.docker=DockerSnapshot(status='ok',context='orbstack',endpoint='unix:///test.sock',
  engine_id='test-engine',sampled_at=time.monotonic(),containers=[DockerContainer(
   id='a'*64,name='test-container',project='test',state='running',auto_remove=False,restart_count=0)])
 ui=MonitorUI(screen,data,Actions());ui.page=ui.PAGE_DOCKER
 ui.render()
 while ui.handle_key(screen.getch()): ui.render()
curses.wrapper(run)
'''
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        proc = subprocess.Popen([sys.executable, "-c", program], stdin=slave, stdout=slave, stderr=slave,
                                cwd=str(pathlib.Path(__file__).parents[1]),
                                env={**os.environ, "TERM": "xterm-256color", "LC_ALL": "en_US.UTF-8"})
        os.close(slave)
        output = bytearray()
        try:
            deadline = time.monotonic() + 2
            sent = False
            while time.monotonic() < deadline:
                if select.select([master], [], [], .05)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break
                if not sent and b"MacVitals" in output:
                    os.write(master, b"ot\norqq")  # stop defaults to cancel; restart dialog q cancels
                    sent = True
            proc.wait(timeout=5)
            self.assertTrue(sent)
            self.assertEqual(proc.returncode, 0, output.decode(errors="replace")[-1500:])
            self.assertNotIn(b"Traceback", output)
            self.assertIn("停止确认".encode(), output)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            os.close(master)

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
                        os.write(master, b"1234567mcgnbjjkk5mn\rvmcv\r1pp")
                        drain(.2)
                    os.write(master, b"q")
                    # More detail panels can fill a PTY before curses finishes
                    # drawing queued input. Keep consuming output during exit.
                    exit_deadline = time.monotonic() + 8
                    while proc.poll() is None and time.monotonic() < exit_deadline:
                        drain(.1)
                    proc.wait(timeout=8)
                    self.assertEqual(proc.returncode, 0, output.decode(errors="replace")[-2000:])
                    self.assertNotIn(b"Traceback", output)
                    self.assertIn(b"MacVitals", output)
                    self.assertIn("终端过小".encode(), output)
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=3)
                    os.close(master)


if __name__ == "__main__":
    unittest.main()
