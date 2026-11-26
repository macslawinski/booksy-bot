"""SQLite state: monitors and the slots seen in the last check (to notify only about newly appeared slots)."""
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

DAY_END = 24 * 60

SCHEMA = """
create table if not exists monitor(
    id integer primary key,
    chat_id integer not null,
    business_id integer not null,
    business_name text not null,
    business_url text not null,
    variant_id integer not null,
    service_name text not null,
    staffer_id integer not null default -1,
    days integer not null default 14,         -- rolling window from today, used when date_from is null
    active integer not null default 1,
    checked_at text,
    created_at text not null default current_timestamp
);
create table if not exists current_slot(
    monitor_id integer not null references monitor(id),
    slot text not null,
    primary key (monitor_id, slot)
);
create table if not exists booking_attempt(   -- one booking per salon and start time, ever (no double booking)
    business_id integer not null,
    slot text not null,                       -- ISO start, Europe/Warsaw
    status text not null,                     -- pending / confirmed / failed / unknown / cancelled
    appointment_uid text,
    updated_at text not null default current_timestamp,
    primary key (business_id, slot)
);
create table if not exists user(              -- friends (joined with BOT_PASSWORD); the owner only after their own /token
    chat_id integer primary key,
    name text not null default '',
    token text,                               -- their own Booksy X-Access-Token (one account per person: Booksy terms)
    fingerprint text not null,                -- their own X-Fingerprint, so accounts do not share one device id
    token_set_at text,                        -- UTC; to learn how long a Booksy token lives
    created_at text not null default current_timestamp
);
create table if not exists blocked(           -- strangers locked out after too many wrong bot passwords
    chat_id integer primary key,
    created_at text not null default current_timestamp
);
"""
# Added after the first release; added to older DBs by Storage._migrate.
NEW_COLUMNS = {
    "date_from": "text",                                  # fixed window (ISO dates), null = rolling `days`
    "date_to": "text",
    "time_from": "integer not null default 0",            # slot start, minutes after midnight, inclusive
    "time_to": f"integer not null default {DAY_END}",
    "autobook": "integer not null default 0",             # 1 = book the first free slot without asking
}


@dataclass
class Monitor:
    id: int
    chat_id: int
    business_id: int
    business_name: str
    business_url: str
    variant_id: int
    service_name: str
    staffer_id: int
    days: int
    date_from: str | None = None
    date_to: str | None = None
    time_from: int = 0
    time_to: int = DAY_END
    autobook: int = 0


COLUMNS = ("id, chat_id, business_id, business_name, business_url, variant_id, service_name, staffer_id, days, "
           "date_from, date_to, time_from, time_to, autobook")


class Storage:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        # shortcut: hand-rolled column migration; switch to a migration tool if the schema keeps changing.
        cols = {r[1] for r in self.db.execute("pragma table_info(monitor)")}
        for name, ddl in NEW_COLUMNS.items():
            if name not in cols:
                self.db.execute(f"alter table monitor add column {name} {ddl}")
        if "hour_from" in cols:  # short-lived whole-hour window -> minutes
            self.db.execute("update monitor set time_from = hour_from * 60, time_to = hour_to * 60")
            self.db.execute("alter table monitor drop column hour_from")
            self.db.execute("alter table monitor drop column hour_to")
        if "chat_id" not in {r[1] for r in self.db.execute("pragma table_info(booking_attempt)")}:
            self.db.execute("alter table booking_attempt add column chat_id integer")   # null = before friends: counts for everyone
        if "token_set_at" not in {r[1] for r in self.db.execute("pragma table_info(user)")}:
            self.db.execute("alter table user add column token_set_at text")
        self.db.commit()

    # ---------- friends ----------

    def add_user(self, chat_id: int, name: str) -> None:
        """Registered friend with their own fingerprint (one device id per Booksy account)."""
        self.db.execute("insert or ignore into user(chat_id, name, fingerprint) values (?, ?, ?)", (chat_id, name, str(uuid.uuid4())))
        self.db.commit()

    def block(self, chat_id: int) -> None:
        self.db.execute("insert or ignore into blocked(chat_id) values (?)", (chat_id,))
        self.db.commit()

    def is_blocked(self, chat_id: int) -> bool:
        return self.db.execute("select 1 from blocked where chat_id = ?", (chat_id,)).fetchone() is not None

    def user(self, chat_id: int) -> tuple[str, str | None, str] | None:
        """(name, token, fingerprint) of a registered friend."""
        return self.db.execute("select name, token, fingerprint from user where chat_id = ?", (chat_id,)).fetchone()

    def users(self) -> list[tuple[int, str, bool]]:
        return [(r[0], r[1], r[2] is not None) for r in self.db.execute("select chat_id, name, token from user order by created_at")]

    def set_token(self, chat_id: int, token: str, fingerprint: str | None = None) -> None:
        """Friends are already registered; the owner gets a row on the first /token (fingerprint from .env)."""
        self.db.execute("insert or ignore into user(chat_id, fingerprint) values (?, ?)", (chat_id, fingerprint or str(uuid.uuid4())))
        self.db.execute("update user set token = ?, token_set_at = current_timestamp where chat_id = ?", (token, chat_id))
        self.db.commit()

    def token_set_at(self, chat_id: int) -> datetime | None:
        row = self.db.execute("select token_set_at from user where chat_id = ?", (chat_id,)).fetchone()
        return datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc) if row and row[0] else None

    def remove_user(self, chat_id: int) -> bool:
        """Forget the friend and their token; their monitors stop (booking history stays: no double booking)."""
        cur = self.db.execute("delete from user where chat_id = ?", (chat_id,))
        self.db.execute("update monitor set active = 0 where chat_id = ?", (chat_id,))
        self.db.commit()
        return cur.rowcount == 1

    def add_monitor(self, chat_id: int, business_id: int, business_name: str, business_url: str, variant_id: int,
                    service_name: str, staffer_id: int, days: int, date_from: str | None = None,
                    date_to: str | None = None, time_from: int = 0, time_to: int = DAY_END, autobook: bool = False) -> Monitor:
        m = Monitor(0, chat_id, business_id, business_name, business_url, variant_id, service_name, staffer_id, days,
                    date_from, date_to, time_from, time_to, int(autobook))
        values = list(vars(m).values())[1:]
        cur = self.db.execute(
            f"insert into monitor({COLUMNS.removeprefix('id, ')}) values ({','.join('?' * len(values))})", values)
        self.db.commit()
        assert cur.lastrowid is not None
        m.id = cur.lastrowid
        return m

    def _monitors(self, where: str, args: tuple) -> list[Monitor]:
        rows = self.db.execute(f"select {COLUMNS} from monitor where {where} order by id", args)
        return [Monitor(*r) for r in rows]

    def active_monitors(self) -> list[Monitor]:
        return self._monitors("active = 1", ())

    def chat_monitors(self, chat_id: int) -> list[Monitor]:
        return self._monitors("active = 1 and chat_id = ?", (chat_id,))

    def deactivate(self, chat_id: int, monitor_id: int) -> bool:
        cur = self.db.execute("update monitor set active = 0 where id = ? and chat_id = ? and active = 1", (monitor_id, chat_id))
        self.db.execute("delete from current_slot where monitor_id = ?", (monitor_id,))
        self.db.commit()
        return cur.rowcount == 1

    def update_slots(self, monitor_id: int, current: set[str]) -> tuple[list[str], bool]:
        """Replace the stored slot set; return (slots not present in the previous check, is_first_check).

        A slot that disappears and later reappears (someone cancelled) counts as new again — that is the point of the bot.
        """
        first = self.db.execute("select checked_at is null from monitor where id = ?", (monitor_id,)).fetchone()[0] == 1
        previous = {r[0] for r in self.db.execute("select slot from current_slot where monitor_id = ?", (monitor_id,))}
        self.db.execute("delete from current_slot where monitor_id = ?", (monitor_id,))
        self.db.executemany("insert into current_slot(monitor_id, slot) values (?, ?)", [(monitor_id, s) for s in current])
        self.db.execute("update monitor set checked_at = current_timestamp where id = ?", (monitor_id,))
        self.db.commit()
        return sorted(current - previous), first

    def start_booking(self, business_id: int, slot: str, chat_id: int | None = None) -> str | None:
        """Claim the slot for one booking attempt. Returns None when claimed, else the blocking status.
        Only a 'failed' or 'cancelled' attempt (no visit exists) may be tried again."""
        row = self.db.execute("select status from booking_attempt where business_id = ? and slot = ?", (business_id, slot)).fetchone()
        if row and row[0] not in ("failed", "cancelled"):
            return row[0]
        self.db.execute("insert or replace into booking_attempt(business_id, slot, status, chat_id) values (?, ?, 'pending', ?)",
                        (business_id, slot, chat_id))
        self.db.commit()
        return None

    def stop_autobook(self, monitor_id: int) -> None:
        self.db.execute("update monitor set autobook = 0 where id = ?", (monitor_id,))
        self.db.commit()

    def booked_months(self, business_id: int, chat_id: int | None = None) -> set[str]:
        """'YYYY-MM' of this person's bot bookings in this salon that did or may have succeeded (unknown counts)."""
        rows = self.db.execute("select substr(slot, 1, 7) from booking_attempt where business_id = ? and status not in ('failed', 'cancelled') "
                               "and (chat_id is ? or chat_id is null)", (business_id, chat_id))
        return {r[0] for r in rows}

    def booking_status(self, business_id: int, slot: str) -> tuple[str, int | None] | None:
        """(status, chat_id) of the bot's attempt for this slot, whoever made it."""
        return self.db.execute("select status, chat_id from booking_attempt where business_id = ? and slot = ?",
                               (business_id, slot)).fetchone()

    def bookings(self, chat_id: int) -> list[tuple[str, str]]:
        """(slot, appointment_uid) of this person's confirmed bot bookings, earliest first."""
        return self.db.execute("select slot, appointment_uid from booking_attempt where chat_id = ? and status = 'confirmed' "
                               "and appointment_uid is not null order by slot", (chat_id,)).fetchall()

    def has_booking(self, chat_id: int, appointment_uid: str) -> bool:
        """Only visits the bot booked for this person can be cancelled through the bot."""
        return self.db.execute("select 1 from booking_attempt where chat_id = ? and appointment_uid = ? and status = 'confirmed'",
                               (chat_id, appointment_uid)).fetchone() is not None

    def cancelled(self, chat_id: int, appointment_uid: str) -> None:
        self.db.execute("update booking_attempt set status = 'cancelled', updated_at = current_timestamp "
                        "where chat_id = ? and appointment_uid = ?", (chat_id, appointment_uid))
        self.db.commit()

    def finish_booking(self, business_id: int, slot: str, status: str, appointment_uid: str | None = None) -> None:
        self.db.execute("update booking_attempt set status = ?, appointment_uid = ?, updated_at = current_timestamp "
                        "where business_id = ? and slot = ?", (status, appointment_uid, business_id, slot))
        self.db.commit()
