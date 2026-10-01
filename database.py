import aiosqlite
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


DB_PATH = Path(__file__).parent / "data" / "bot.db"
DB_PATH.parent.mkdir(exist_ok=True)

# Dallas/Fort Worth timezone.
# This automatically changes between CDT and CST as daylight saving changes.
EVENT_TIMEZONE = ZoneInfo("America/Chicago")


async def init_db():
    """
    Create every table used by the bot.

    Call this once during bot startup before any Cog tries to use these tables.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Enforce SQLite foreign-key relationships for this connection.
        await db.execute("PRAGMA foreign_keys = ON")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                message_count INTEGER DEFAULT 0,
                last_seen TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_name TEXT,
                run_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                details TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS dm_sequences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                messages_sent INTEGER NOT NULL DEFAULT 1,
                total_messages INTEGER NOT NULL DEFAULT 5,
                next_send_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (guild_id, user_id)
            )
        """)

        # Makes the recurring "find due DMs" query faster.
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_dm_sequences_due
            ON dm_sequences (active, next_send_at)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS premium_users (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                message_count INTEGER DEFAULT 0,
                last_seen TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                premium_at TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS verification_reminder_sent (
                user_id TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # Keyword-DM event table.
        # created_at, ends_at, and finished_at are stored as ISO 8601 strings
        # in Dallas/Fort Worth local time, including the active UTC offset.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS keyword_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                keywords TEXT NOT NULL,
                normalized_keywords TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                winner_count INTEGER NOT NULL DEFAULT 1,
                winner_id INTEGER,
                finished_at TEXT
            )
        """)

        # Migration for databases created before winner_count existed.
        # CREATE TABLE IF NOT EXISTS does not modify existing tables.
        event_columns_cursor = await db.execute(
            "PRAGMA table_info(keyword_events)"
        )
        event_columns = await event_columns_cursor.fetchall()
        event_column_names = {column[1] for column in event_columns}

        if "winner_count" not in event_column_names:
            await db.execute("""
                ALTER TABLE keyword_events
                ADD COLUMN winner_count INTEGER NOT NULL DEFAULT 1
            """)

        # A user gets one entry per event because of UNIQUE(event_id, user_id).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS keyword_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                message_content TEXT NOT NULL,
                entered_at TEXT NOT NULL,
                UNIQUE(event_id, user_id),
                FOREIGN KEY(event_id) REFERENCES keyword_events(id)
                    ON DELETE CASCADE
            )
        """)

        # Stores every winner for events that choose one or more winners.
        # position 1 is the first draw, position 2 is the second, etc.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS keyword_event_winners (
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                selected_at TEXT NOT NULL,

                PRIMARY KEY (event_id, user_id),
                UNIQUE (event_id, position),

                FOREIGN KEY(event_id) REFERENCES keyword_events(id)
                    ON DELETE CASCADE
            )
        """)

        # Helpful when the background task regularly asks for active events.
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_keyword_events_status
            ON keyword_events (status)
        """)

        # Helpful when loading all entries for a completed event.
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_keyword_entries_event
            ON keyword_entries (event_id)
        """)

        # Helpful when displaying saved winners in draw order.
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_keyword_event_winners_event
            ON keyword_event_winners (event_id, position)
        """)

        await db.commit()

        # Populate verification_reminder_sent table with existing recipients
        # from task_log. INSERT OR IGNORE prevents duplicates.
        await db.execute("""
            INSERT OR IGNORE INTO verification_reminder_sent (user_id, sent_at)
            SELECT
                CAST(
                    SUBSTR(
                        details,
                        INSTR(details, '(') + 1,
                        INSTR(details, ')') - INSTR(details, '(') - 1
                    )
                    AS TEXT
                ),
                run_at
            FROM task_log
            WHERE task_name = 'verification_reminder_sent'
              AND details LIKE '%(%)'
        """)

        await db.commit()


# -----------------------------------------------------------------------------
# Existing user/message functions
# -----------------------------------------------------------------------------


async def increment_user_message(user_id: str, username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, message_count, last_seen)
            VALUES (?, ?, 1, datetime('now', 'localtime'))
            ON CONFLICT(user_id) DO UPDATE SET
                username = ?,
                message_count = message_count + 1,
                last_seen = datetime('now', 'localtime')
        """, (user_id, username, username))

        await db.commit()


async def log_task_run(task_name: str, details: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO task_log (task_name, details) VALUES (?, ?)",
            (task_name, details),
        )

        await db.commit()


# -----------------------------------------------------------------------------
# Existing DM sequence functions
# -----------------------------------------------------------------------------


async def start_dm_sequence(
    guild_id: int,
    user_id: int,
    messages_sent: int = 1,
    total_messages: int = 5,
) -> bool:
    """
    Create a DM sequence after Day 1 has successfully been sent.

    Returns True if a new sequence was created.
    Returns False if that user already has a sequence record.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO dm_sequences (
                guild_id,
                user_id,
                messages_sent,
                total_messages,
                next_send_at,
                active
            )
            VALUES (
                ?,
                ?,
                ?,
                ?,
                datetime('now', 'localtime', '+1 day'),
                1
            )
            ON CONFLICT(guild_id, user_id) DO NOTHING
        """, (
            str(guild_id),
            str(user_id),
            messages_sent,
            total_messages,
        ))

        await db.commit()

        return cursor.rowcount == 1


async def has_dm_sequence(
    guild_id: int,
    user_id: int,
) -> bool:
    """
    Return True if this member has ever had a DM sequence record in this guild.

    This includes active, completed, and cancelled sequences. That behavior
    prevents the backfill task from restarting old sequences or re-sending Day 1.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT 1
            FROM dm_sequences
            WHERE guild_id = ?
              AND user_id = ?
            LIMIT 1
        """, (
            str(guild_id),
            str(user_id),
        ))

        row = await cursor.fetchone()

        return row is not None


async def get_due_dm_sequences() -> list[dict]:
    """
    Return every active sequence whose next daily DM is due.

    The returned dictionaries support:
    sequence["id"]
    sequence["guild_id"]
    sequence["user_id"]
    sequence["messages_sent"]
    sequence["total_messages"]
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                id,
                guild_id,
                user_id,
                messages_sent,
                total_messages,
                next_send_at,
                active
            FROM dm_sequences
            WHERE active = 1
              AND messages_sent < total_messages
              AND next_send_at <= datetime('now', 'localtime')
            ORDER BY next_send_at ASC
        """)

        rows = await cursor.fetchall()

        return [dict(row) for row in rows]


async def mark_dm_sequence_sent(
    sequence_id: int,
    messages_sent: int,
    total_messages: int,
) -> bool:
    """
    Update the sequence after a DM successfully sends.

    If the final message was sent, active becomes 0.
    Otherwise, schedule the next message for 24 hours later.
    """
    is_complete = messages_sent >= total_messages

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE dm_sequences
            SET
                messages_sent = ?,
                active = CASE
                    WHEN ? = 1 THEN 0
                    ELSE 1
                END,
                next_send_at = CASE
                    WHEN ? = 1 THEN next_send_at
                    ELSE datetime('now', 'localtime', '+1 day')
                END
            WHERE id = ?
              AND active = 1
        """, (
            messages_sent,
            int(is_complete),
            int(is_complete),
            sequence_id,
        ))

        await db.commit()

        return cursor.rowcount == 1


async def cancel_dm_sequence(
    guild_id: int,
    user_id: int,
) -> bool:
    """
    Stop future DMs for this server/member combination.

    This should be called when the paid subscription role is assigned.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE dm_sequences
            SET active = 0
            WHERE guild_id = ?
              AND user_id = ?
              AND active = 1
        """, (
            str(guild_id),
            str(user_id),
        ))

        await db.commit()

        return cursor.rowcount == 1


async def remove_departed_user(
    guild_id: int,
    user_id: int,
) -> dict[str, int]:
    """
    Permanently remove a user's data when they leave, are kicked, or are banned.

    dm_sequences is scoped to one guild.
    users and premium_users are globally scoped by user_id in the current schema.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        dm_cursor = await db.execute("""
            DELETE FROM dm_sequences
            WHERE guild_id = ?
              AND user_id = ?
        """, (
            str(guild_id),
            str(user_id),
        ))

        users_cursor = await db.execute("""
            DELETE FROM users
            WHERE user_id = ?
        """, (str(user_id),))

        premium_cursor = await db.execute("""
            DELETE FROM premium_users
            WHERE user_id = ?
        """, (str(user_id),))

        await db.commit()

        return {
            "dm_sequences": dm_cursor.rowcount,
            "users": users_cursor.rowcount,
            "premium_users": premium_cursor.rowcount,
        }


# -----------------------------------------------------------------------------
# Existing verification-reminder functions
# -----------------------------------------------------------------------------


async def has_received_verification_reminder(user_id: int) -> bool:
    """
    Check if a user has already received a verification reminder DM.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT 1
            FROM verification_reminder_sent
            WHERE user_id = ?
            LIMIT 1
        """, (str(user_id),))

        row = await cursor.fetchone()

        return row is not None


async def mark_verification_reminder_sent(user_id: int) -> bool:
    """
    Record that a user has received a verification reminder.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT INTO verification_reminder_sent (user_id, sent_at)
                VALUES (?, datetime('now', 'localtime'))
            """, (str(user_id),))

            await db.commit()

            return True

        except aiosqlite.IntegrityError:
            return False


# -----------------------------------------------------------------------------
# Keyword DM event time helpers
# -----------------------------------------------------------------------------


def keyword_event_local_now() -> datetime:
    """
    Return the current time in Dallas/Fort Worth.

    The returned datetime is timezone-aware and automatically uses CDT or CST.
    """
    return datetime.now(EVENT_TIMEZONE)


def keyword_event_datetime_to_db(value: datetime) -> str:
    """
    Convert a datetime to a Dallas/Fort Worth ISO 8601 database value.

    Example while Central Daylight Time is active:
        2026-10-02T00:00:00-05:00

    Example while Central Standard Time is active:
        2026-12-02T00:00:00-06:00
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=EVENT_TIMEZONE)

    return value.astimezone(EVENT_TIMEZONE).isoformat()


def keyword_event_datetime_from_db(value: str) -> datetime:
    """
    Read an ISO 8601 database value as a Dallas/Fort Worth datetime.

    This supports current local-time records and any earlier UTC records,
    provided the earlier records include an offset such as +00:00.
    """
    parsed = datetime.fromisoformat(value)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=EVENT_TIMEZONE)

    return parsed.astimezone(EVENT_TIMEZONE)


# -----------------------------------------------------------------------------
# Keyword DM event database functions
# -----------------------------------------------------------------------------


async def create_keyword_event(
    guild_id: int,
    channel_id: int,
    created_by: int,
    keywords: str,
    normalized_keywords: str,
    created_at: datetime,
    ends_at: datetime,
    winner_count: int = 1,
) -> int:
    """
    Create an active keyword event and return its database event ID.

    winner_count is the number of distinct winners requested for the event.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO keyword_events (
                guild_id,
                channel_id,
                created_by,
                keywords,
                normalized_keywords,
                created_at,
                ends_at,
                winner_count,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
        """, (
            guild_id,
            channel_id,
            created_by,
            keywords,
            normalized_keywords,
            keyword_event_datetime_to_db(created_at),
            keyword_event_datetime_to_db(ends_at),
            winner_count,
        ))

        await db.commit()

        return cursor.lastrowid


async def get_keyword_event(event_id: int) -> Optional[dict]:
    """
    Return one event by ID, regardless of guild.

    For Discord moderator commands, prefer get_guild_keyword_event() so staff
    cannot retrieve an event created in a different server.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT *
            FROM keyword_events
            WHERE id = ?
        """, (event_id,))

        row = await cursor.fetchone()

        return dict(row) if row is not None else None


async def get_guild_keyword_event(
    event_id: int,
    guild_id: int,
) -> Optional[dict]:
    """
    Return an event only if it belongs to the provided Discord guild.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT *
            FROM keyword_events
            WHERE id = ?
              AND guild_id = ?
        """, (
            event_id,
            guild_id,
        ))

        row = await cursor.fetchone()

        return dict(row) if row is not None else None


async def get_active_keyword_events() -> list[dict]:
    """
    Return every currently active keyword event.

    The Cog should compare ends_at to keyword_event_local_now() before adding
    an entry, so no entry is accepted after the deadline.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT *
            FROM keyword_events
            WHERE status = 'active'
            ORDER BY ends_at ASC
        """)

        rows = await cursor.fetchall()

        return [dict(row) for row in rows]


async def get_expired_keyword_events(now: datetime) -> list[dict]:
    """
    Return active events whose deadline has passed.

    Time comparisons happen in Python because ISO timestamps that include
    Central Time's seasonal -05:00 and -06:00 offsets should not be compared
    as raw lexicographical SQLite strings.
    """
    active_events = await get_active_keyword_events()

    return [
        event
        for event in active_events
        if keyword_event_datetime_from_db(event["ends_at"]) <= now
    ]


async def add_keyword_event_entry(
    event_id: int,
    user_id: int,
    username: str,
    message_id: int,
    message_content: str,
    entered_at: datetime,
) -> bool:
    """
    Enter one user into an event.

    Returns True if the entry was created.
    Returns False if this user already entered this event.

    The UNIQUE(event_id, user_id) constraint prevents duplicate entries even
    if the person DMs the matching phrase more than once.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("PRAGMA foreign_keys = ON")

            await db.execute("""
                INSERT INTO keyword_entries (
                    event_id,
                    user_id,
                    username,
                    message_id,
                    message_content,
                    entered_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                event_id,
                user_id,
                username,
                message_id,
                message_content,
                keyword_event_datetime_to_db(entered_at),
            ))

            await db.commit()

            return True

        except aiosqlite.IntegrityError:
            return False


async def get_keyword_event_entries(event_id: int) -> list[dict]:
    """
    Return all unique entries for an event, oldest entry first.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT *
            FROM keyword_entries
            WHERE event_id = ?
            ORDER BY entered_at ASC
        """, (event_id,))

        rows = await cursor.fetchall()

        return [dict(row) for row in rows]


async def get_keyword_event_winners(event_id: int) -> list[dict]:
    """
    Return all saved winners for an event in draw order.

    Each dictionary contains:
    - event_id
    - user_id
    - position
    - selected_at
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                event_id,
                user_id,
                position,
                selected_at
            FROM keyword_event_winners
            WHERE event_id = ?
            ORDER BY position ASC
        """, (event_id,))

        rows = await cursor.fetchall()

        return [dict(row) for row in rows]


async def get_keyword_event_entry_count(event_id: int) -> int:
    """
    Return the number of unique users entered in an event.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT COUNT(*) AS count
            FROM keyword_entries
            WHERE event_id = ?
        """, (event_id,))

        row = await cursor.fetchone()

        return row[0]


async def user_has_keyword_event_entry(
    event_id: int,
    user_id: int,
) -> bool:
    """
    Return True if this user already has an entry in this event.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT 1
            FROM keyword_entries
            WHERE event_id = ?
              AND user_id = ?
            LIMIT 1
        """, (
            event_id,
            user_id,
        ))

        row = await cursor.fetchone()

        return row is not None


async def finish_keyword_event(
    event_id: int,
    winner_ids: list[int],
    finished_at: datetime,
) -> bool:
    """
    Mark an active event as finished and save zero or more distinct winners.

    winner_ids may be an empty list when nobody submitted a valid matching DM.

    Returns True only when the function changed a row from active to finished.
    The status guard prevents duplicate draws and duplicate announcements if
    background-task iterations overlap or the bot restarts during processing.
    """
    if len(winner_ids) != len(set(winner_ids)):
        raise ValueError("winner_ids must not contain duplicates.")

    selected_at = keyword_event_datetime_to_db(finished_at)
    first_winner_id = winner_ids[0] if winner_ids else None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            # Obtain the SQLite write lock now so the status check and winner
            # records become one transaction.
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute("""
                UPDATE keyword_events
                SET status = 'finished',
                    winner_id = ?,
                    finished_at = ?
                WHERE id = ?
                  AND status = 'active'
            """, (
                first_winner_id,
                selected_at,
                event_id,
            ))

            # Another task/process already completed or cancelled this event.
            if cursor.rowcount != 1:
                await db.rollback()
                return False

            for position, user_id in enumerate(winner_ids, start=1):
                await db.execute("""
                    INSERT INTO keyword_event_winners (
                        event_id,
                        user_id,
                        position,
                        selected_at
                    )
                    VALUES (?, ?, ?, ?)
                """, (
                    event_id,
                    user_id,
                    position,
                    selected_at,
                ))

            await db.commit()
            return True

        except Exception:
            await db.rollback()
            raise


async def cancel_keyword_event(
    event_id: int,
    guild_id: int,
) -> bool:
    """
    Cancel an active event belonging to the supplied server.

    Returns False if the event does not exist, is from a different guild, or
    has already finished/cancelled.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE keyword_events
            SET status = 'cancelled'
            WHERE id = ?
              AND guild_id = ?
              AND status = 'active'
        """, (
            event_id,
            guild_id,
        ))

        await db.commit()

        return cursor.rowcount == 1