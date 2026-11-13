"""Deployment-only private JSONL sink; no calls are run and no data is invented."""
import fcntl
import json
import os
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path


class PrivateJSONLReceiptSink:
    def __init__(self, state_directory, run_id, *, allowed_tools=("evaluate_portfolio_snapshot",)):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise ValueError("Invalid observation run ID")
        state = Path(state_directory).resolve(strict=True)
        if not state.is_dir():
            raise ValueError("Receipt state directory must already exist")
        self._state = state
        self._run_id = run_id
        self._filename = run_id + ".execution_records.jsonl"
        self._lock = threading.Lock()
        self._allowed_tools = frozenset(allowed_tools)

    def __call__(self, receipt):
        if (not isinstance(receipt, dict)
                or receipt.get("tool_name") not in self._allowed_tools
                or receipt.get("source") != "nexent-execution-record"):
            raise ValueError("Unexpected receipt source or tool")
        record = dict(receipt)
        record["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        record["observation_run_id"] = self._run_id
        encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True,
                              allow_nan=False) + "\n").encode("utf-8")
        with self._lock:
            # Resolve only the preapproved directory; never follow a filename
            # symlink, and refuse existing non-private files.
            directory_fd = os.open(self._state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fd = os.open(self._filename,
                             os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
                try:
                    metadata = os.fstat(fd)
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                        raise PermissionError("Receipt file is not a private regular file")
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    remaining = memoryview(encoded)
                    while remaining:
                        count = os.write(fd, remaining)
                        if count <= 0:
                            raise OSError("Incomplete receipt write")
                        remaining = remaining[count:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                os.close(directory_fd)


def install_receipt_sink(sink):
    """Explicit launch-layer injection before runtime observers are created."""
    from nexent.core.utils.observer import MessageObserver

    if not callable(sink):
        raise TypeError("Receipt sink must be callable")
    if getattr(MessageObserver, "_topic16_receipt_sink_installed", False):
        raise RuntimeError("A receipt sink is already installed in this process")
    original_init = MessageObserver.__init__

    def observed_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.mcp_receipt_sink = sink

    MessageObserver.__init__ = observed_init
    MessageObserver._topic16_receipt_sink_installed = True
