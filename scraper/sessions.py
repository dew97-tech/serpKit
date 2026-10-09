"""Reusable scraping sessions: sticky residential IP plus persisted cookies.

A session pins one proxy IP (sessid) and keeps the Google cookies captured
from runs (storage_state). While it works it is reused for every query of its
preset; on TTL expiry or a failed run it rotates and is stored again. One
lock per preset serializes access in this single-process setup.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


def session_reuse_enabled() -> bool:
    return os.environ.get("SESSION_REUSE", "1").lower() not in ("0", "false", "no", "")


def session_ttl_s() -> float:
    try:
        return float(os.environ.get("SESSION_TTL_S", "1500"))
    except ValueError:
        return 1500.0


def _session_dir() -> Path:
    return Path(os.environ.get("SESSION_DIR", ".sessions"))


@dataclass
class Session:
    preset: str
    sessid: str
    created_at: float
    last_used: float
    solve_failures: int = 0
    runs: int = 0
    successes: int = 0
    exit_ip: str = ""
    exit_geo: str = ""
    user_agent: str = ""
    state: dict | None = field(default=None, repr=False)

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > session_ttl_s()

    def meta(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if key != "state"}


class SessionManager:
    """Per-preset session store (single process)."""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    @classmethod
    def lock(cls, preset: str) -> threading.Lock:
        with cls._locks_guard:
            if preset not in cls._locks:
                cls._locks[preset] = threading.Lock()
            return cls._locks[preset]

    @staticmethod
    def _state_path(preset: str) -> Path:
        return _session_dir() / f"{preset}.state.json"

    @staticmethod
    def _meta_path(preset: str) -> Path:
        return _session_dir() / f"{preset}.meta.json"

    def load(self, preset: str) -> Session | None:
        """Return a live (non-expired) session; None when absent/expired/corrupt."""
        try:
            meta = json.loads(self._meta_path(preset).read_text("utf-8"))
            state = json.loads(self._state_path(preset).read_text("utf-8"))
        except Exception:
            return None
        try:
            session = Session(
                preset=preset,
                sessid=str(meta.get("sessid", "")),
                created_at=float(meta.get("created_at", 0)),
                last_used=float(meta.get("last_used", 0)),
                solve_failures=int(meta.get("solve_failures", 0)),
                runs=int(meta.get("runs", 0)),
                successes=int(meta.get("successes", 0)),
                exit_ip=str(meta.get("exit_ip", "")),
                exit_geo=str(meta.get("exit_geo", "")),
                user_agent=str(meta.get("user_agent", "")),
                state=state,
            )
        except (TypeError, ValueError):
            return None
        if not session.sessid or session.expired:
            return None
        return session

    def summary(self, preset: str) -> dict:
        """UI/monitor view of one preset's session (never raises)."""
        session = self.load(preset)
        if session is None:
            return {"preset": preset, "active": False}
        ttl = session_ttl_s()
        age = max(0.0, time.time() - session.created_at)
        cookies = 0
        if isinstance(session.state, dict):
            cookies = len(session.state.get("cookies", []) or [])
        return {
            "preset": preset,
            "active": True,
            "sessid": session.sessid,
            "age_s": int(age),
            "ttl_s": int(ttl),
            "expires_in_s": int(max(0.0, ttl - age)),
            "exit_ip": session.exit_ip,
            "exit_geo": session.exit_geo,
            "user_agent": session.user_agent,
            "runs": session.runs,
            "successes": session.successes,
            "solve_failures": session.solve_failures,
            "cookies": cookies,
        }

    def create(self, preset: str) -> Session:
        now = time.time()
        return Session(preset=preset, sessid=uuid.uuid4().hex[:12],
                       created_at=now, last_used=now)

    def save(self, session: Session) -> None:
        try:
            out_dir = _session_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            self._meta_path(session.preset).write_text(
                json.dumps(session.meta(), indent=2), encoding="utf-8")
            if session.state is not None:
                self._state_path(session.preset).write_text(
                    json.dumps(session.state), encoding="utf-8")
        except Exception:
            pass

    def clear(self, preset: str) -> None:
        for path in (self._meta_path(preset), self._state_path(preset)):
            try:
                path.unlink()
            except OSError:
                pass
