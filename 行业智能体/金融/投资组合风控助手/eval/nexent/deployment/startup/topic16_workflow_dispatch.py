"""Opt-in fixed tool workflow for a single published Nexent v2.8.3 Agent."""
from contextlib import closing
from contextvars import ContextVar
from functools import wraps
import hashlib
import os
from pathlib import Path
import sqlite3
import time

from topic16_dispatch_protocol import CORE_TOOLS, action_digest, decode_request, read_key


CORE_SHA256 = '0d470f0b687e2b453c9c0e8d8da18827737a4208936ef9cc358dddaf60f36cdf'
active_action = ContextVar('topic16_workflow_action', default=None)


def stamp_receipts(sink):
    def stamped(record):
        action = active_action.get()
        if action and action['tool'] == record.get('tool_name'):
            record = {**record, 'workflow_action_sha256': action_digest(action['nonce'])}
        sink(record)
    return stamped


def claim_action(db, action):
    with closing(sqlite3.connect(db, timeout=4)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        now = int(time.time())
        if action['expires'] < now:
            raise ValueError('workflow action expired before dispatch')
        conn.execute('CREATE TABLE IF NOT EXISTS workflow_actions(nonce TEXT PRIMARY KEY, expires INTEGER NOT NULL)')
        conn.execute('DELETE FROM workflow_actions WHERE expires < ?', (now,))
        claimed = conn.execute('INSERT OR IGNORE INTO workflow_actions VALUES (?, ?)',
                               (action['nonce'], action['expires'])).rowcount
        if claimed != 1:
            raise ValueError('workflow action already used')


def install(agent_name, key_file, state_directory):
    from nexent.core.agents import core_agent as core

    if hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest() != CORE_SHA256:
        raise RuntimeError('workflow requires the pinned Nexent v2.8.3 CoreAgent')
    if not agent_name or getattr(core.CoreAgent, '_topic16_workflow_installed', False):
        raise RuntimeError('invalid workflow agent configuration')
    key = read_key(key_file)
    database = Path(state_directory).resolve(strict=True) / 'workflow-actions.sqlite'
    fd = os.open(database, os.O_CREAT | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    os.close(fd)
    original = core.CoreAgent._run_stream

    @wraps(original)
    def run_stream(self, task, max_steps, images=None):
        action = decode_request(key, agent_name, task)
        if action is None:
            yield from original(self, task=task, max_steps=max_steps, images=images)
            return
        # Nexent may use an invocation alias as the runtime agent name.
        # The published description is passed through to this CoreAgent.
        if self.description != 'topic16-workflow:' + agent_name:
            raise ValueError('workflow published description mismatch')
        if images or self.stop_event.is_set():
            raise ValueError('workflow run is cancelled or carries images')
        if not (CORE_TOOLS | {action['tool']}) <= self.tools.keys():
            raise ValueError('workflow published bindings incomplete')
        tool = self.tools[action['tool']]
        if getattr(tool, '_tool_call_observer_wrapped', False) is not True:
            raise RuntimeError('workflow requires native tool observation')
        claim_action(database, action)
        token = active_action.set(action)
        try:
            # Use the Agent's registered and guarded tool. Its own observer creates
            # the call ID and the managed MCP runtime writes the original receipt.
            result = tool(**action['arguments'])
        finally:
            active_action.reset(token)
        yield core.FinalAnswerStep(core.handle_agent_output_types(result))

    core.CoreAgent._run_stream = run_stream
    core.CoreAgent._topic16_workflow_installed = True
