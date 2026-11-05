"""Holds the current graph, loads it in the background and swaps it atomically on change.

Three things change a response, and each is watched:
- the atlas content (database content marker): full snapshot reload;
- the designations file (content digest): graph rebuilt from the current snapshot;
- the server date, which drives imagery age: graph rebuilt from the current snapshot.
Every rebuild has a new effective graph_version.
"""
from __future__ import annotations

import logging
import threading
from datetime import date
from pathlib import Path
from typing import Callable

from atlas.routing import designation
from atlas.routing.graph import Graph
from atlas.routing.policy import DEFAULT_POLICY, Policy
from atlas.routing.router import Router
from atlas.routing.snapshot import Snapshot, SnapshotError, connect_readonly, load_with_retry, read_marker

log = logging.getLogger('atlas.routing')


class State:
    def __init__(self, graph: Graph | None = None, error: str = 'loading'):
        self.graph = graph
        self.router = Router(graph) if graph else None
        self.error = None if graph else error


class Service:
    """Snapshot provider. `load`, `designations`, `today` and `marker` are injectable for tests.

    `designations` is a designations document, or a callable returning the current one.
    """

    def __init__(self, load: Callable[[], Snapshot], designations: dict | None | Callable[[], dict | None] = None,
                 policy: Policy = DEFAULT_POLICY, today: Callable[[], date] = date.today,
                 marker: Callable[[], tuple] | None = None):
        self._load, self._marker = load, marker
        self._designations = designations if callable(designations) else (lambda: designations)
        self.policy, self.today = policy, today
        self.state = State()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def refresh(self) -> None:
        """Full reload of the atlas snapshot, then a graph build."""
        try:
            snapshot = self._load()
        except SnapshotError as exc:
            log.error('snapshot rejected: %s', exc.reason)
            with self._lock:
                if self.state.graph is None or exc.reason == 'integrity_failed':
                    self.state = State(error=exc.reason)
            return
        self._rebuild(snapshot)

    def _rebuild(self, snapshot: Snapshot) -> None:
        graph = Graph(snapshot, self.policy, self._designations(), self.today())
        with self._lock:
            self.state = State(graph)
        log.info('graph ready: release %s, %s', snapshot.release.id, graph.version)

    def check(self) -> str:
        """One watch cycle. Returns what was done: loaded, reloaded, rebuilt or unchanged."""
        graph = self.state.graph
        if graph is None:
            self.refresh()
            return 'loaded'
        if self._marker is not None:
            try:
                if self._marker() != graph.snapshot.marker:
                    self.refresh()
                    return 'reloaded'
            except Exception as exc:  # never log connection details
                log.warning('change check failed: %s', type(exc).__name__)
        try:
            doc = self._designations()
        except Exception as exc:  # a half-written or invalid file: keep serving the current graph
            log.warning('designations unreadable: %s', type(exc).__name__)
            return 'unchanged'
        if self.today() != graph.as_of or designation.content_digest(doc) != graph.designator.digest:
            self._rebuild(graph.snapshot)
            return 'rebuilt'
        return 'unchanged'

    def run_background(self, interval_s: float) -> threading.Thread:
        def loop():
            while not self._stop.is_set():
                try:
                    self.check()
                except Exception as exc:
                    # Keep the last complete state and retry transient input/build failures.
                    log.warning('graph refresh failed: %s', type(exc).__name__)
                if self._stop.wait(interval_s):
                    break
        thread = threading.Thread(target=loop, name='atlas-snapshot', daemon=True)
        thread.start()
        return thread

    def stop(self):
        self._stop.set()


def database_service(url: str, designations_path: Path | None, policy: Policy = DEFAULT_POLICY) -> Service:
    def marker():
        with connect_readonly(url, timeout_s=10) as conn:
            return read_marker(conn)
    return Service(lambda: load_with_retry(url), designation.FileSource(designations_path).current, policy, marker=marker)
