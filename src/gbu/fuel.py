"""Session fuel: tokens in the live Grok Build session, Cursor chat, Grok Bot, or a mix.

The amount is the fuel already on the gauge. The pace (tokens per minute over
the last minute) is how hard that work is going. Build totals come from
``~/.grok/sessions/*/usage.json``, which the TUI writes. Cursor does not write
that file, so an open Cursor chat contributes the context size Cursor stores
on the composer (``promptTokenBreakdown.totalUsedTokens``). Bot has no
per-conversation ledger, so its contribution is the rise in today's Bot token
counter since this sitting started. A counter we have only just seen is a
baseline, not a burn.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

# A session still being written counts on its own. After it goes quiet, keep
# the newest one on the gauge until this linger expires.
ACTIVE_SEC = 180.0
LINGER_SEC = 20 * 60.0
FRESH_SEC = 90.0
# Pace uses the sample closest to a minute ago, once we have 20s of span.
RATE_WINDOW_SEC = 60.0
RATE_MIN_SPAN_SEC = 20.0

_EASY_PER_MIN = 5_000
_STEADY_PER_MIN = 40_000
_HARD_PER_MIN = 200_000


@dataclass(frozen=True)
class SessionBurn:
    """Cumulative tokens for one live session.

    ``source`` is ``Build`` for a Grok Build TUI session and ``Cursor`` for an
    open Cursor chat. Cursor's number is the chat's current context size.
    """

    session_id: str
    tokens: int
    updated_at: datetime
    last_turn_tokens: Optional[int] = None
    source: str = "Build"


@dataclass(frozen=True)
class FuelReading:
    """One gauge: fuel burned, and the pace of that burn."""

    tokens: int
    rate_per_min: Optional[float]
    level: str
    sources: tuple[str, ...]
    last_turn_tokens: Optional[int]
    fresh: bool

    @classmethod
    def none(cls) -> "FuelReading":
        return cls(0, None, "quiet", (), None, False)

    def amount_text(self) -> str:
        if not self.sources:
            return "—"
        return format_tokens(self.tokens)

    def detail_text(self) -> str:
        if not self.sources:
            return "no live session"
        text = f"{self.level} · {' + '.join(self.sources)}"
        if self.rate_per_min is not None and self.rate_per_min >= _EASY_PER_MIN:
            return f"{text} · {format_tokens(int(self.rate_per_min))}/min"
        if self.fresh and self.last_turn_tokens:
            return f"{text} · last turn {format_tokens(self.last_turn_tokens)}"
        return text

    def menu_text(self) -> str:
        if not self.sources:
            return "Fuel  —"
        return f"Fuel  {self.amount_text()} · {self.detail_text()}"

    def fill(self) -> float:
        """Bar fill for pace, from 0 to 1. Not a fraction of a token tank."""
        if self.tokens <= 0 and self.level == "quiet":
            return 0.0
        rate = self.rate_per_min
        if rate is not None and rate > 0:
            span = (math.log10(rate) - 3.0) / 3.2
            return max(0.08, min(1.0, span))
        return {"hard": 0.92, "steady": 0.62, "easy": 0.34, "quiet": 0.06}[self.level]


def format_tokens(n: int) -> str:
    """Compact token count: 9400 → 9.4k, 840000 → 840k, 9785228 → 9.8M."""
    n = max(0, int(n))
    if n < 1000:
        return str(n)
    if n < 10_000:
        text = f"{n / 1000:.1f}k"
        return text.replace(".0k", "k")
    if n < 1_000_000:
        return f"{round(n / 1000)}k"
    if n < 10_000_000:
        text = f"{n / 1_000_000:.1f}M"
        return text.replace(".0M", "M")
    return f"{round(n / 1_000_000)}M"


def sessions_root() -> Path:
    base = os.environ.get("GROK_HOME") or str(Path.home() / ".grok")
    return Path(base) / "sessions"


def cursor_state_db() -> Path:
    override = os.environ.get("CURSOR_STATE_DB")
    if override:
        return Path(override)
    return Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"


def burn_from_usage(data: dict, *, session_id: str = "") -> Optional[SessionBurn]:
    """Map one usage.json object to a burn. Ignores everything but counters."""
    if not isinstance(data, dict):
        return None
    session = data.get("session")
    if not isinstance(session, dict):
        return None
    updated = _parse_time(data.get("updatedAt"))
    if updated is None:
        return None
    try:
        tokens = int(session.get("totalTokens") or 0)
    except (TypeError, ValueError):
        return None
    if tokens <= 0:
        return None
    sid = session_id or data.get("sessionId") or ""
    if not isinstance(sid, str):
        sid = ""
    last_turn = _last_turn_tokens(data.get("turns"))
    return SessionBurn(
        session_id=sid,
        tokens=tokens,
        updated_at=updated,
        last_turn_tokens=last_turn,
    )


def load_build_burns(
    root: Optional[Path] = None,
    *,
    now: Optional[datetime] = None,
) -> list[SessionBurn]:
    """Read Build sessions touched inside the linger window."""
    root = root if root is not None else sessions_root()
    if not root.is_dir():
        return []
    moment = _aware(now or datetime.now().astimezone())
    cutoff = moment.timestamp() - (LINGER_SEC + 60.0)
    burns: list[SessionBurn] = []
    try:
        paths = root.glob("*/*/usage.json")
    except OSError:
        return []
    for path in paths:
        try:
            if path.stat().st_mtime < cutoff:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        burn = burn_from_usage(data, session_id=path.parent.name)
        if burn is not None:
            burns.append(burn)
    return burns


def _composer_updated(data: object, header_ms: object) -> Optional[datetime]:
    """Newest clock on a Cursor chat.

    ``composerHeaders.lastUpdatedAt`` stays at open time for a long turn.
    ``conversationCheckpointLastUpdatedAt`` moves while that turn is running.
    """
    stamps: list[float] = []
    if isinstance(header_ms, (int, float)) and not isinstance(header_ms, bool):
        stamps.append(float(header_ms))
    if isinstance(data, dict):
        for key in ("lastUpdatedAt", "conversationCheckpointLastUpdatedAt"):
            raw = data.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 1_000_000_000_000:
                stamps.append(float(raw))
    if not stamps:
        return None
    try:
        return datetime.fromtimestamp(max(stamps) / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def burn_from_composer(
    data: dict,
    *,
    session_id: str,
    updated_at: datetime,
) -> Optional[SessionBurn]:
    """Map one Cursor composer to a burn. Uses context size, not message text."""
    if not isinstance(data, dict) or data.get("isDraft"):
        return None
    if not session_id or session_id == "empty-state-draft" or session_id.startswith("draft-"):
        return None
    breakdown = data.get("promptTokenBreakdown")
    if not isinstance(breakdown, dict):
        return None
    try:
        tokens = int(breakdown.get("totalUsedTokens") or 0)
    except (TypeError, ValueError):
        return None
    if tokens <= 0:
        return None
    return SessionBurn(
        session_id=session_id,
        tokens=tokens,
        updated_at=_aware(updated_at),
        source="Cursor",
    )


def load_cursor_burns(
    db_path: Optional[Path] = None,
    *,
    now: Optional[datetime] = None,
) -> list[SessionBurn]:
    """Read non-archived Cursor chats touched inside the linger window.

    The state database stays open in Cursor, so this is a read-only snapshot.
    A missing or locked database is an empty list, not a failure of Build fuel.
    """
    path = db_path if db_path is not None else cursor_state_db()
    if not path.is_file():
        return []
    moment = _aware(now or datetime.now().astimezone())
    cutoff = moment.timestamp() - (LINGER_SEC + 60.0)
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return []
    try:
        try:
            headers = con.execute(
                """
                SELECT composerId, lastUpdatedAt
                FROM composerHeaders
                WHERE ifnull(isArchived, 0) = 0
                """
            ).fetchall()
        except sqlite3.Error:
            return []
        burns: list[SessionBurn] = []
        for composer_id, header_ms in headers:
            if not isinstance(composer_id, str):
                continue
            try:
                row = con.execute(
                    "SELECT value FROM cursorDiskKV WHERE key = ?",
                    (f"composerData:{composer_id}",),
                ).fetchone()
            except sqlite3.Error:
                continue
            if row is None:
                continue
            raw = row[0]
            try:
                text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                data = json.loads(text)
            except (UnicodeError, json.JSONDecodeError, AttributeError, TypeError):
                continue
            updated = _composer_updated(data, header_ms)
            if updated is None or updated.timestamp() < cutoff:
                continue
            burn = burn_from_composer(data, session_id=composer_id, updated_at=updated)
            if burn is not None:
                burns.append(burn)
        return burns
    finally:
        con.close()


def load_fuel_burns(
    *,
    now: Optional[datetime] = None,
    sessions: Optional[Path] = None,
    cursor_db: Optional[Path] = None,
) -> list[SessionBurn]:
    """TUI sessions plus Cursor chats. Either source can be absent."""
    moment = now or datetime.now().astimezone()
    burns = load_build_burns(sessions, now=moment)
    burns.extend(load_cursor_burns(cursor_db, now=moment))
    return burns


def select_build_burns(
    burns: Sequence[SessionBurn],
    now: datetime,
    *,
    active_sec: float = ACTIVE_SEC,
    linger_sec: float = LINGER_SEC,
) -> list[SessionBurn]:
    """Sessions that are the live work.

    Every session written in the last few minutes counts, so two concurrent
    Build sessions add together. Once they go quiet, only the newest remains.
    """
    moment = _aware(now)

    def age(burn: SessionBurn) -> float:
        return (moment - burn.updated_at).total_seconds()

    active = [b for b in burns if b.tokens > 0 and -5.0 <= age(b) <= active_sec]
    if active:
        return active
    lingering = [b for b in burns if b.tokens > 0 and -5.0 <= age(b) <= linger_sec]
    if not lingering:
        return []
    return [max(lingering, key=lambda b: b.updated_at)]


class FuelMeter:
    """Turns session totals and the Bot daily counter into one reading.

    The first time a session or the Bot counter is seen, that value is a
    baseline. Only tokens that arrive after that move the pace.
    """

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}
        self._consumed = 0
        self._rate_points: list[tuple[float, int]] = []
        self._bot_baseline: Optional[int] = None
        self._bot_value: Optional[int] = None
        self._bot_changed: Optional[float] = None

    def observe(
        self,
        burns: Sequence[SessionBurn],
        bot_daily: Optional[int],
        now: datetime,
        mono: float,
    ) -> FuelReading:
        selected = select_build_burns(burns, now)
        selected_ids = {b.session_id for b in selected}
        for burn in burns:
            prev = self._seen.get(burn.session_id)
            if prev is not None and burn.session_id in selected_ids and burn.tokens > prev:
                self._consumed += burn.tokens - prev
            self._seen[burn.session_id] = burn.tokens

        bot_tokens = self._note_bot(bot_daily, mono)
        rate = self._note_rate(mono)
        return _reading(selected, bot_tokens, rate, _aware(now))

    def _note_bot(self, daily: Optional[int], mono: float) -> int:
        if daily is None or daily < 0:
            if (
                self._bot_value is not None
                and self._bot_baseline is not None
                and self._bot_changed is not None
                and mono - self._bot_changed <= LINGER_SEC
            ):
                return self._bot_value - self._bot_baseline
            return 0
        if self._bot_baseline is None or daily < self._bot_baseline:
            self._bot_baseline = daily
            self._bot_value = daily
            self._bot_changed = mono
            return 0
        assert self._bot_value is not None
        if daily > self._bot_value:
            self._consumed += daily - self._bot_value
            self._bot_value = daily
            self._bot_changed = mono
        elif self._bot_changed is not None and mono - self._bot_changed > LINGER_SEC:
            self._bot_baseline = daily
            self._bot_value = daily
            self._bot_changed = mono
            return 0
        return max(0, self._bot_value - self._bot_baseline)

    def _note_rate(self, mono: float) -> Optional[float]:
        self._rate_points.append((mono, self._consumed))
        cutoff = mono - 180.0
        self._rate_points = [p for p in self._rate_points if p[0] >= cutoff]
        target = mono - RATE_WINDOW_SEC
        base_mono, base_tokens = min(self._rate_points, key=lambda p: abs(p[0] - target))
        span = mono - base_mono
        if span < RATE_MIN_SPAN_SEC:
            return None
        return max(0.0, (self._consumed - base_tokens) / span * 60.0)


def _reading(
    selected: Sequence[SessionBurn],
    bot_tokens: int,
    rate: Optional[float],
    now: datetime,
) -> FuelReading:
    sources: list[str] = []
    tokens = 0
    last_turn: Optional[int] = None
    fresh = False
    if selected:
        for name in ("Build", "Cursor"):
            if any(b.source == name for b in selected):
                sources.append(name)
        for burn in selected:
            if burn.source not in sources:
                sources.append(burn.source)
        tokens += sum(b.tokens for b in selected)
        newest = max(selected, key=lambda b: b.updated_at)
        last_turn = newest.last_turn_tokens
        fresh = (now - newest.updated_at).total_seconds() <= FRESH_SEC
    if bot_tokens > 0:
        sources.append("Bot")
        tokens += bot_tokens
        fresh = True
    if rate is not None:
        level = _level_from_rate(rate)
    elif fresh and last_turn:
        level = _level_from_turn(last_turn)
    else:
        level = "quiet"
    return FuelReading(
        tokens=tokens,
        rate_per_min=rate,
        level=level,
        sources=tuple(sources),
        last_turn_tokens=last_turn,
        fresh=fresh,
    )


def _level_from_rate(rate: float) -> str:
    if rate >= _HARD_PER_MIN:
        return "hard"
    if rate >= _STEADY_PER_MIN:
        return "steady"
    if rate >= _EASY_PER_MIN:
        return "easy"
    return "quiet"


def _level_from_turn(tokens: int) -> str:
    if tokens >= 1_000_000:
        return "hard"
    if tokens >= 200_000:
        return "steady"
    if tokens >= 20_000:
        return "easy"
    return "quiet"


def _last_turn_tokens(turns: object) -> Optional[int]:
    if not isinstance(turns, list) or not turns:
        return None
    last = turns[-1]
    if not isinstance(last, dict):
        return None
    try:
        tokens = int(last.get("totalTokens") or 0)
    except (TypeError, ValueError):
        return None
    return tokens or None


def _parse_time(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment
