"""Terminator plugin: periodically dump the live layout tree to a JSON cache.

Used by terminator-session-restore to reproduce the exact pane arrangement
(nesting, ratios, order) after reboot, since describe_layout() is not
exposed over DBus.

Install: copy to ~/.config/terminator/plugins/ and add "LayoutSnapshot"
to enabled_plugins in [global_config] of ~/.config/terminator/config.
"""
import json
import os
import time

from gi.repository import GLib

import terminatorlib.plugin as plugin
from terminatorlib.terminator import Terminator

AVAILABLE = ['LayoutSnapshot']

OUTPUT = os.path.expanduser('~/.cache/terminator_layout_snapshot.json')
INTERVAL_SEC = 30


class LayoutSnapshot(plugin.Plugin):
    capabilities = []

    def __init__(self):
        plugin.Plugin.__init__(self)
        GLib.timeout_add_seconds(5, self._dump_once)
        GLib.timeout_add_seconds(INTERVAL_SEC, self._dump)

    def _dump_once(self):
        self._dump()
        return False  # one-shot

    def _dump(self):
        try:
            term = Terminator()
            layout = term.describe_layout()
            terminals = {}
            for t in term.terminals:
                try:
                    terminals[str(t.uuid)] = {
                        'cwd': t.get_cwd(),
                        'pid': t.pid,
                    }
                except Exception:
                    continue
            data = {
                'timestamp': time.time(),
                'layout': layout,
                'terminals': terminals,
            }
            tmp = OUTPUT + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, default=str)
            os.replace(tmp, OUTPUT)
        except Exception:
            pass
        return True  # keep the timer running
