"""Bounded local USB diagnostics. Contains control packets, never audio or tokens."""
import json
import os
from pathlib import Path
import threading
import time


class USBTrace:
    def __init__(self, path, max_bytes=2_000_000, copies=3):
        self.path = Path(path)
        self.max_bytes, self.copies = max_bytes, copies
        self.lock = threading.Lock()

    def record(self, event, **fields):
        row = {'time': time.time(), 'monotonic': time.monotonic(), 'pid': os.getpid(),
               'event': event, **fields}
        line = (json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        with self.lock:
            if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                for number in range(self.copies, 0, -1):
                    source = self.path if number == 1 else self.path.with_name(self.path.name + f'.{number-1}')
                    if source.exists():
                        source.replace(self.path.with_name(self.path.name + f'.{number}'))
            fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                view = memoryview(line)
                while view:
                    written = os.write(fd, view)
                    if not written:
                        raise OSError('无法写入 USB 诊断记录')
                    view = view[written:]
            finally:
                os.close(fd)
