"""SQLite state: monitors and the slots seen in the last check (to notify only about newly appeared slots)."""
import sqlite3
from dataclasses import dataclass

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
    status text not null,                     -- pending / confirmed / failed / unknown
    appointment_uid text,
    updated_at text not null default current_timestamp,
    primary key (business_id, slot)
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
        self.db.commit()

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

    def start_booking(self, business_id: int, slot: str) -> str | None:
        """Claim the slot for one booking attempt. Returns None when claimed, else the blocking status.
        Only a 'failed' attempt (the server surely did not book) may be tried again."""
        row = self.db.execute("select status from booking_attempt where business_id = ? and slot = ?", (business_id, slot)).fetchone()
        if row and row[0] != "failed":
            return row[0]
        self.db.execute("insert or replace into booking_attempt(business_id, slot, status) values (?, ?, 'pending')", (business_id, slot))
        self.db.commit()
        return None

    def stop_autobook(self, monitor_id: int) -> None:
        self.db.execute("update monitor set autobook = 0 where id = ?", (monitor_id,))
        self.db.commit()

    def booking_status(self, business_id: int, slot: str) -> str | None:
        row = self.db.execute("select status from booking_attempt where business_id = ? and slot = ?", (business_id, slot)).fetchone()
        return row and row[0]

    def finish_booking(self, business_id: int, slot: str, status: str, appointment_uid: str | None = None) -> None:
        self.db.execute("update booking_attempt set status = ?, appointment_uid = ?, updated_at = current_timestamp "
                        "where business_id = ? and slot = ?", (status, appointment_uid, business_id, slot))
        self.db.commit()
