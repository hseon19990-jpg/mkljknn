import aiosqlite
from datetime import datetime, timedelta, timezone
from config import (
    DB_PATH, DEFAULT_MIN_INTERVAL, DEFAULT_MAX_INTERVAL, DEFAULT_TZ_OFFSET,
    DEFAULT_MAX_BUNDLES, DEFAULT_MAX_NUMBERS, DEFAULT_MAX_CLICHES,
)


# ---------------- أدوات الوقت (كل التخزين الجديد بتوقيت UTC) ----------------
def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_str(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def utc_now_str():
    return _utc_str(now_utc())


def day_start_utc_str(tz_offset, days_back=0):
    """بداية اليوم المحلي (بحسب فرق التوقيت) محوّلة إلى UTC كنص."""
    local_now = now_utc() + timedelta(hours=tz_offset)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight -= timedelta(days=days_back)
    return _utc_str(midnight - timedelta(hours=tz_offset))


def hours_ago_str(hours):
    return _utc_str(now_utc() - timedelta(hours=hours))


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            is_blocked INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS blocked_entities (
            entity_id INTEGER PRIMARY KEY,
            entity_type TEXT,
            blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS forced_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT UNIQUE,
            channel_link TEXT
        );
        CREATE TABLE IF NOT EXISTS admin_cliches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_limits (
            user_id INTEGER PRIMARY KEY,
            max_numbers INTEGER DEFAULT 1,
            max_groups INTEGER DEFAULT 1,
            max_cliches INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            session_name TEXT UNIQUE NOT NULL,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS groups_tbl (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            group_title TEXT
        );
        CREATE TABLE IF NOT EXISTS cliches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bundles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT,
            session_id INTEGER,
            group_id INTEGER,
            cliche_id INTEGER,
            interval INTEGER DEFAULT 60,
            is_active INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            coma_until TEXT,
            flood_until TEXT
        );
        CREATE TABLE IF NOT EXISTS bundle_cliches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bundle_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS post_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            bundle_id INTEGER,
            user_id INTEGER,
            session_id INTEGER,
            status TEXT NOT NULL,
            detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_post_ts ON post_log(ts);
        CREATE INDEX IF NOT EXISTS idx_post_bundle ON post_log(bundle_id, ts);
        CREATE INDEX IF NOT EXISTS idx_post_user ON post_log(user_id, ts);
        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            user_id INTEGER,
            bundle_id INTEGER,
            session_id INTEGER,
            detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_event_ts ON event_log(ts);
        CREATE INDEX IF NOT EXISTS idx_event_kind ON event_log(kind, ts);
        """)
        await db.commit()

        # إعادة بناء إذا كان session_id NOT NULL أو UNIQUE
        cur = await db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='bundles'")
        row = await cur.fetchone()
        if row and row[0]:
            schema = row[0]
            need_rebuild = (
                "session_id INTEGER NOT NULL" in schema or
                "group_id INTEGER NOT NULL" in schema or
                "cliche_id INTEGER NOT NULL" in schema or
                "session_id INTEGER UNIQUE" in schema
            )
            if need_rebuild:
                await db.executescript("""
                ALTER TABLE bundles RENAME TO bundles_old;
                CREATE TABLE bundles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT,
                    session_id INTEGER,
                    group_id INTEGER,
                    cliche_id INTEGER,
                    interval INTEGER DEFAULT 60,
                    is_active INTEGER DEFAULT 0,
                    failure_count INTEGER DEFAULT 0,
                    coma_until TEXT,
                    flood_until TEXT
                );
                INSERT INTO bundles (id,user_id,name,session_id,group_id,cliche_id,interval,is_active)
                    SELECT id,user_id,NULL,session_id,group_id,cliche_id,interval,is_active FROM bundles_old;
                DROP TABLE bundles_old;
                """)
                await db.commit()

        # Migrations (بعد إعادة البناء حتى لا تضيع الأعمدة الجديدة)
        async def _add_cols(table, cols):
            for col, definition in cols:
                try:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
                    await db.commit()
                except aiosqlite.OperationalError:
                    pass

        await _add_cols("bundles", [
            ("name", "TEXT"),
            ("failure_count", "INTEGER DEFAULT 0"),
            ("coma_until", "TEXT"),
            ("flood_until", "TEXT"),
            ("mode", "TEXT DEFAULT 'auto'"),          # reply | auto | plain
            ("auto_join", "INTEGER DEFAULT 1"),
            ("hours_from", "INTEGER DEFAULT -1"),     # ساعات العمل (-1 = معطّل)
            ("hours_to", "INTEGER DEFAULT -1"),
            ("daily_limit", "INTEGER DEFAULT 0"),     # 0 = بلا حد
            ("total_sent", "INTEGER DEFAULT 0"),
            ("total_fail", "INTEGER DEFAULT 0"),
            ("last_post_at", "TEXT"),
            ("last_error", "TEXT"),
            ("created_at", "TEXT"),
        ])
        await _add_cols("users", [
            ("username", "TEXT"),
            ("full_name", "TEXT"),
            ("last_seen", "TEXT"),
            ("notify", "INTEGER DEFAULT 1"),
        ])
        await _add_cols("user_limits", [("is_custom", "INTEGER DEFAULT 0")])

        await db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('min_interval',?)",
                         (str(DEFAULT_MIN_INTERVAL),))
        await db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('max_interval',?)",
                         (str(DEFAULT_MAX_INTERVAL),))
        await db.commit()


async def _fetchone(query, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, args)
        row = await cur.fetchone()
        return dict(row) if row else None


async def _fetchall(query, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, args)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def _exec(query, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, args)
        await db.commit()
        return cur.lastrowid


# -------- Users --------
async def ensure_user(user_id, username=None, full_name=None):
    await _exec("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    await _exec("INSERT OR IGNORE INTO user_limits(user_id) VALUES(?)", (user_id,))
    if username is not None or full_name is not None:
        await touch_user(user_id, username, full_name)


async def is_blocked(entity_id):
    r = await _fetchone("SELECT 1 FROM blocked_entities WHERE entity_id=?", (entity_id,))
    return r is not None


async def block_entity(entity_id, etype):
    await _exec("INSERT OR IGNORE INTO blocked_entities(entity_id, entity_type) VALUES(?,?)",
                (entity_id, etype))


async def unblock_entity(entity_id):
    await _exec("DELETE FROM blocked_entities WHERE entity_id=?", (entity_id,))


# -------- Admin cliches --------
async def add_admin_cliche(text):
    await _exec("INSERT INTO admin_cliches(text) VALUES(?)", (text,))


async def get_all_admin_cliches():
    return await _fetchall("SELECT * FROM admin_cliches")


async def delete_admin_cliche(cid):
    await _exec("DELETE FROM admin_cliches WHERE id=?", (cid,))


# -------- Forced channel --------
async def set_forced_channel(channel_id, channel_link):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM forced_channels")
        await db.execute(
            "INSERT INTO forced_channels(channel_id, channel_link) VALUES(?,?)",
            (str(channel_id), channel_link)
        )
        await db.commit()


async def get_forced_channel():
    return await _fetchone("SELECT * FROM forced_channels LIMIT 1")


# -------- Settings --------
async def get_setting(key, default=None):
    r = await _fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


async def set_setting(key, value):
    await _exec("INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=?",
                (key, str(value), str(value)))


async def get_min_interval():
    v = await get_setting("min_interval", str(DEFAULT_MIN_INTERVAL))
    try:
        return int(v)
    except Exception:
        return DEFAULT_MIN_INTERVAL


async def get_max_interval():
    v = await get_setting("max_interval", str(DEFAULT_MAX_INTERVAL))
    try:
        return int(v)
    except Exception:
        return DEFAULT_MAX_INTERVAL


# -------- Sessions --------
async def add_session(user_id, phone, session_name):
    return await _exec("INSERT INTO sessions(user_id,phone,session_name) VALUES(?,?,?)",
                       (user_id, phone, session_name))


async def get_user_sessions(user_id):
    return await _fetchall("SELECT * FROM sessions WHERE user_id=? AND is_active=1", (user_id,))


async def get_session(sid):
    return await _fetchone("SELECT * FROM sessions WHERE id=?", (sid,))


async def get_all_active_sessions():
    return await _fetchall("SELECT * FROM sessions WHERE is_active=1")


async def session_phone_exists(phone):
    """الرقم يعتبر مسجلاً فقط إذا كانت جلسته مرتبطة فعلاً بمجموعة (bundle)."""
    r = await _fetchone(
        "SELECT 1 FROM sessions s JOIN bundles b ON b.session_id = s.id "
        "WHERE s.phone=? AND s.is_active=1", (phone,))
    return r is not None


async def is_session_in_use(session_id):
    r = await _fetchone("SELECT 1 FROM bundles WHERE session_id=?", (session_id,))
    return r is not None


async def deactivate_session(session_id):
    await _exec("UPDATE sessions SET is_active=0 WHERE id=?", (session_id,))


async def get_orphan_sessions_by_phone(phone):
    """جلسات نشطة لهذا الرقم لا ترتبط بأي مجموعة (بقايا حذف سابق)."""
    return await _fetchall(
        "SELECT s.* FROM sessions s WHERE s.phone=? AND s.is_active=1 "
        "AND NOT EXISTS (SELECT 1 FROM bundles b WHERE b.session_id = s.id)", (phone,))


# -------- Groups --------
async def add_group(user_id, gid, title):
    return await _exec("INSERT INTO groups_tbl(user_id,group_id,group_title) VALUES(?,?,?)",
                       (user_id, gid, title))


async def get_group(gid):
    return await _fetchone("SELECT * FROM groups_tbl WHERE id=?", (gid,))


# -------- Cliches --------
async def add_cliche(user_id, text):
    return await _exec("INSERT INTO cliches(user_id,text) VALUES(?,?)", (user_id, text))


async def get_cliche(cid):
    return await _fetchone("SELECT * FROM cliches WHERE id=?", (cid,))


# -------- Bundles --------
async def add_bundle(user_id, name):
    return await _exec(
        "INSERT INTO bundles(user_id,name,is_active,created_at) VALUES(?,?,0,?)",
        (user_id, name, utc_now_str())
    )


async def get_user_bundles(user_id):
    return await _fetchall("SELECT * FROM bundles WHERE user_id=? ORDER BY id", (user_id,))


async def get_bundle(bid):
    return await _fetchone("SELECT * FROM bundles WHERE id=?", (bid,))


async def get_active_bundles():
    return await _fetchall("SELECT * FROM bundles WHERE is_active=1")


async def delete_bundle(bid):
    await _exec("DELETE FROM bundles WHERE id=?", (bid,))


async def update_bundle_session(bid, session_id):
    await _exec("UPDATE bundles SET session_id=? WHERE id=?", (session_id, bid))


async def clear_bundle_session(bid):
    await _exec("UPDATE bundles SET session_id=NULL, is_active=0 WHERE id=?", (bid,))


async def update_bundle_group(bid, group_id):
    await _exec("UPDATE bundles SET group_id=? WHERE id=?", (group_id, bid))


async def update_bundle_cliche(bid, cliche_id):
    await _exec("UPDATE bundles SET cliche_id=? WHERE id=?", (cliche_id, bid))


async def set_bundle_interval(bid, interval):
    await _exec("UPDATE bundles SET interval=? WHERE id=?", (interval, bid))


async def set_bundle_active(bid, active: bool):
    await _exec("UPDATE bundles SET is_active=? WHERE id=?", (1 if active else 0, bid))


async def is_session_used_elsewhere(session_id, exclude_bundle_id=None):
    if exclude_bundle_id:
        r = await _fetchone(
            "SELECT 1 FROM bundles WHERE session_id=? AND id!=? AND is_active=1",
            (session_id, exclude_bundle_id)
        )
    else:
        r = await _fetchone("SELECT 1 FROM bundles WHERE session_id=? AND is_active=1",
                            (session_id,))
    return r is not None


# -------- Failure / Coma --------
async def increment_bundle_failure(bid):
    await _exec("UPDATE bundles SET failure_count = failure_count + 1 WHERE id=?", (bid,))
    r = await _fetchone("SELECT failure_count FROM bundles WHERE id=?", (bid,))
    return r["failure_count"] if r else 0


async def reset_bundle_failures(bid):
    await _exec("UPDATE bundles SET failure_count = 0 WHERE id=?", (bid,))


async def set_bundle_coma(bid, hours=6):
    until = (datetime.now() + timedelta(hours=hours)).isoformat()
    await _exec("UPDATE bundles SET coma_until=? WHERE id=?", (until, bid))


async def clear_bundle_coma(bid):
    await _exec("UPDATE bundles SET coma_until=NULL WHERE id=?", (bid,))


async def set_bundle_flood(bid, seconds):
    until = (datetime.now() + timedelta(seconds=seconds)).isoformat()
    await _exec("UPDATE bundles SET flood_until=? WHERE id=?", (until, bid))


async def clear_bundle_flood(bid):
    await _exec("UPDATE bundles SET flood_until=NULL WHERE id=?", (bid,))


# -------- Tutorial video --------
async def set_tutorial_video(file_id, caption=""):
    await set_setting("tutorial_video_file_id", file_id)
    await set_setting("tutorial_video_caption", caption or "")


async def get_tutorial_video():
    fid = await get_setting("tutorial_video_file_id", "")
    cap = await get_setting("tutorial_video_caption", "")
    if not fid:
        return None
    return {"file_id": fid, "caption": cap}


# =====================================================================
#                          إضافات جديدة
# =====================================================================
async def scalar(query, args=(), default=0):
    r = await _fetchone(query, args)
    if not r:
        return default
    v = list(r.values())[0]
    return default if v is None else v


# -------- Settings helpers --------
async def get_setting_bool(key, default=False):
    v = await get_setting(key, "1" if default else "0")
    return str(v) == "1"


async def toggle_setting(key, default=False):
    cur = await get_setting_bool(key, default)
    await set_setting(key, "0" if cur else "1")
    return not cur


async def get_tz_offset():
    v = await get_setting("tz_offset", str(DEFAULT_TZ_OFFSET))
    try:
        return int(float(v))
    except Exception:
        return DEFAULT_TZ_OFFSET


async def clear_forced_channel():
    await _exec("DELETE FROM forced_channels")


# -------- Users --------
async def touch_user(user_id, username, full_name):
    await _exec(
        "UPDATE users SET username=COALESCE(?,username), full_name=COALESCE(?,full_name), "
        "last_seen=? WHERE user_id=?",
        (username, full_name, utc_now_str(), user_id))


async def get_user(user_id):
    return await _fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))


async def get_all_user_ids():
    rows = await _fetchall(
        "SELECT user_id FROM users WHERE user_id NOT IN "
        "(SELECT entity_id FROM blocked_entities)")
    return [r["user_id"] for r in rows]


async def get_active_user_ids():
    rows = await _fetchall(
        "SELECT DISTINCT user_id FROM bundles WHERE is_active=1 AND user_id NOT IN "
        "(SELECT entity_id FROM blocked_entities)")
    return [r["user_id"] for r in rows]


async def get_recent_users(limit=15):
    return await _fetchall("SELECT * FROM users ORDER BY created_at DESC, user_id DESC LIMIT ?", (limit,))


async def get_blocked_list(limit=50):
    return await _fetchall(
        "SELECT * FROM blocked_entities ORDER BY blocked_at DESC LIMIT ?", (limit,))


async def set_user_notify(user_id, on: bool):
    await _exec("UPDATE users SET notify=? WHERE user_id=?", (1 if on else 0, user_id))


async def wants_notify(user_id):
    r = await get_user(user_id)
    return True if r is None else bool(r.get("notify", 1) in (None, 1))


async def find_user(query):
    """بحث بالـ ID أو اليوزر."""
    q = (query or "").strip().lstrip("@")
    if q.lstrip("-").isdigit():
        return await get_user(int(q))
    return await _fetchone("SELECT * FROM users WHERE lower(username)=lower(?)", (q,))


# -------- Limits --------
async def get_default_limits():
    mb = await get_setting("default_max_bundles", str(DEFAULT_MAX_BUNDLES))
    mn = await get_setting("default_max_numbers", str(DEFAULT_MAX_NUMBERS))
    mc = await get_setting("default_max_cliches", str(DEFAULT_MAX_CLICHES))
    return {"max_groups": int(mb), "max_numbers": int(mn), "max_cliches": int(mc)}


async def get_limits(user_id):
    """الحدود الفعلية: المخصّصة إن ضبطها المالك، وإلا الافتراضية العامة."""
    r = await _fetchone("SELECT * FROM user_limits WHERE user_id=?", (user_id,))
    if r and r.get("is_custom"):
        return {"max_numbers": r["max_numbers"], "max_groups": r["max_groups"],
                "max_cliches": r["max_cliches"], "custom": True}
    d = await get_default_limits()
    d["custom"] = False
    return d


async def set_limits(user_id, max_numbers, max_groups, max_cliches):
    await _exec("INSERT OR IGNORE INTO user_limits(user_id) VALUES(?)", (user_id,))
    await _exec(
        "UPDATE user_limits SET max_numbers=?, max_groups=?, max_cliches=?, is_custom=1 "
        "WHERE user_id=?", (max_numbers, max_groups, max_cliches, user_id))


async def reset_limits(user_id):
    await _exec("UPDATE user_limits SET is_custom=0 WHERE user_id=?", (user_id,))


async def count_user_bundles(user_id):
    return await scalar("SELECT COUNT(*) FROM bundles WHERE user_id=?", (user_id,))


async def count_user_sessions(user_id):
    return await scalar(
        "SELECT COUNT(*) FROM sessions WHERE user_id=? AND is_active=1", (user_id,))


# -------- Bundle: generic update + stats --------
_BUNDLE_FIELDS = {
    "name", "mode", "auto_join", "hours_from", "hours_to", "daily_limit",
    "interval", "last_error",
}


async def update_bundle_fields(bid, **kw):
    sets, args = [], []
    for k, v in kw.items():
        if k not in _BUNDLE_FIELDS:
            raise ValueError(f"field not allowed: {k}")
        sets.append(f"{k}=?")
        args.append(v)
    if not sets:
        return
    args.append(bid)
    await _exec(f"UPDATE bundles SET {', '.join(sets)} WHERE id=?", tuple(args))


async def record_post_result(bid, ok: bool, error=None):
    if ok:
        await _exec(
            "UPDATE bundles SET total_sent=COALESCE(total_sent,0)+1, last_post_at=?, "
            "last_error=NULL WHERE id=?", (utc_now_str(), bid))
    else:
        await _exec(
            "UPDATE bundles SET total_fail=COALESCE(total_fail,0)+1, last_error=? WHERE id=?",
            ((error or "")[:200], bid))


async def get_all_bundles():
    return await _fetchall("SELECT * FROM bundles ORDER BY id")


async def count_bundles(only_active=False):
    q = "SELECT COUNT(*) FROM bundles" + (" WHERE is_active=1" if only_active else "")
    return await scalar(q)


async def get_bundles_page(offset=0, limit=10):
    return await _fetchall(
        "SELECT * FROM bundles ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))


# -------- Extra cliches per bundle --------
async def add_bundle_cliche(bid, text):
    return await _exec(
        "INSERT INTO bundle_cliches(bundle_id,text,created_at) VALUES(?,?,?)",
        (bid, text, utc_now_str()))


async def get_bundle_cliches(bid):
    return await _fetchall("SELECT * FROM bundle_cliches WHERE bundle_id=? ORDER BY id", (bid,))


async def get_bundle_cliche(cid):
    return await _fetchone("SELECT * FROM bundle_cliches WHERE id=?", (cid,))


async def delete_bundle_cliche(cid):
    await _exec("DELETE FROM bundle_cliches WHERE id=?", (cid,))


async def count_bundle_cliches(bid):
    return await scalar("SELECT COUNT(*) FROM bundle_cliches WHERE bundle_id=?", (bid,))


async def delete_bundle_cliches(bid):
    await _exec("DELETE FROM bundle_cliches WHERE bundle_id=?", (bid,))


# -------- Post log --------
async def log_post(bid, user_id, session_id, status, detail=None):
    await _exec(
        "INSERT INTO post_log(ts,bundle_id,user_id,session_id,status,detail) VALUES(?,?,?,?,?,?)",
        (utc_now_str(), bid, user_id, session_id, status, (str(detail)[:300] if detail else None)))


async def post_counts(since=None, bundle_id=None, user_id=None):
    q = "SELECT status, COUNT(*) AS c FROM post_log WHERE 1=1"
    args = []
    if since:
        q += " AND ts>=?"; args.append(since)
    if bundle_id is not None:
        q += " AND bundle_id=?"; args.append(bundle_id)
    if user_id is not None:
        q += " AND user_id=?"; args.append(user_id)
    q += " GROUP BY status"
    rows = await _fetchall(q, tuple(args))
    return {r["status"]: r["c"] for r in rows}


async def daily_ok_series(tz_offset, days=7, bundle_id=None, user_id=None):
    """قائمة بعدد الرسائل الناجحة لكل يوم محلي (الأقدم أولاً)."""
    since = day_start_utc_str(tz_offset, days_back=days - 1)
    q = ("SELECT date(ts, ?) AS d, COUNT(*) AS c FROM post_log "
         "WHERE status='ok' AND ts>=?")
    args = [f"{tz_offset:+d} hours", since]
    if bundle_id is not None:
        q += " AND bundle_id=?"; args.append(bundle_id)
    if user_id is not None:
        q += " AND user_id=?"; args.append(user_id)
    q += " GROUP BY d"
    rows = await _fetchall(q, tuple(args))
    m = {r["d"]: r["c"] for r in rows}
    out = []
    local_today = (now_utc() + timedelta(hours=tz_offset)).date()
    for i in range(days - 1, -1, -1):
        d = (local_today - timedelta(days=i)).isoformat()
        out.append((d, m.get(d, 0)))
    return out


async def get_recent_posts(bid, limit=10):
    return await _fetchall(
        "SELECT * FROM post_log WHERE bundle_id=? ORDER BY id DESC LIMIT ?", (bid, limit))


async def get_recent_failed_posts(limit=20):
    return await _fetchall(
        "SELECT * FROM post_log WHERE status NOT IN ('ok','no_target') "
        "ORDER BY id DESC LIMIT ?", (limit,))


async def top_users_by_posts(since, limit=5):
    return await _fetchall(
        "SELECT user_id, COUNT(*) AS c FROM post_log WHERE status='ok' AND ts>=? "
        "GROUP BY user_id ORDER BY c DESC LIMIT ?", (since, limit))


async def count_ok_posts(bundle_id, since):
    return await scalar(
        "SELECT COUNT(*) FROM post_log WHERE bundle_id=? AND status='ok' AND ts>=?",
        (bundle_id, since))


# -------- Event log --------
async def log_event(kind, user_id=None, bundle_id=None, session_id=None, detail=None):
    await _exec(
        "INSERT INTO event_log(ts,kind,user_id,bundle_id,session_id,detail) VALUES(?,?,?,?,?,?)",
        (utc_now_str(), kind, user_id, bundle_id, session_id,
         (str(detail)[:300] if detail else None)))


async def get_recent_events(limit=25, kinds=None):
    if kinds:
        ph = ",".join("?" * len(kinds))
        return await _fetchall(
            f"SELECT * FROM event_log WHERE kind IN ({ph}) ORDER BY id DESC LIMIT ?",
            tuple(kinds) + (limit,))
    return await _fetchall("SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (limit,))


async def count_events(kind, since=None, user_id=None):
    q = "SELECT COUNT(*) FROM event_log WHERE kind=?"
    args = [kind]
    if since:
        q += " AND ts>=?"; args.append(since)
    if user_id is not None:
        q += " AND user_id=?"; args.append(user_id)
    return await scalar(q, tuple(args))


async def prune_logs(post_days, event_days):
    await _exec("DELETE FROM post_log WHERE ts < ?", (hours_ago_str(post_days * 24),))
    await _exec("DELETE FROM event_log WHERE ts < ?", (hours_ago_str(event_days * 24),))


# -------- Aggregates for dashboards --------
async def global_counts(tz_offset):
    today = day_start_utc_str(tz_offset)
    week = day_start_utc_str(tz_offset, days_back=6)
    h24 = hours_ago_str(24)
    out = {
        "users": await scalar("SELECT COUNT(*) FROM users"),
        "blocked": await scalar("SELECT COUNT(*) FROM blocked_entities"),
        "new_today": await scalar("SELECT COUNT(*) FROM users WHERE created_at>=?", (today,)),
        "new_week": await scalar("SELECT COUNT(*) FROM users WHERE created_at>=?", (week,)),
        "active_24h": await scalar("SELECT COUNT(*) FROM users WHERE last_seen>=?", (h24,)),
        "bundles": await count_bundles(),
        "bundles_active": await count_bundles(True),
        "sessions": await scalar("SELECT COUNT(*) FROM sessions WHERE is_active=1"),
        "sent_total": await scalar("SELECT COALESCE(SUM(total_sent),0) FROM bundles"),
        "sent_today": await scalar(
            "SELECT COUNT(*) FROM post_log WHERE status='ok' AND ts>=?", (today,)),
        "sent_24h": await scalar(
            "SELECT COUNT(*) FROM post_log WHERE status='ok' AND ts>=?", (h24,)),
        "fail_today": await scalar(
            "SELECT COUNT(*) FROM post_log WHERE status NOT IN ('ok','no_target') AND ts>=?",
            (today,)),
        "no_target_today": await scalar(
            "SELECT COUNT(*) FROM post_log WHERE status='no_target' AND ts>=?", (today,)),
        "joins_today": await count_events("join_ok", today),
        "joins_total": await count_events("join_ok"),
        "join_flood_today": await count_events("join_flood", today),
        "coma_now": await scalar(
            "SELECT COUNT(*) FROM bundles WHERE is_active=1 AND coma_until IS NOT NULL "
            "AND coma_until > ?", (datetime.now().isoformat(),)),
        "flood_now": await scalar(
            "SELECT COUNT(*) FROM bundles WHERE is_active=1 AND flood_until IS NOT NULL "
            "AND flood_until > ?", (datetime.now().isoformat(),)),
    }
    return out


async def user_counts(user_id, tz_offset):
    today = day_start_utc_str(tz_offset)
    week = day_start_utc_str(tz_offset, days_back=6)
    return {
        "bundles": await count_user_bundles(user_id),
        "bundles_active": await scalar(
            "SELECT COUNT(*) FROM bundles WHERE user_id=? AND is_active=1", (user_id,)),
        "sessions": await count_user_sessions(user_id),
        "sent_total": await scalar(
            "SELECT COALESCE(SUM(total_sent),0) FROM bundles WHERE user_id=?", (user_id,)),
        "sent_today": await scalar(
            "SELECT COUNT(*) FROM post_log WHERE user_id=? AND status='ok' AND ts>=?",
            (user_id, today)),
        "sent_week": await scalar(
            "SELECT COUNT(*) FROM post_log WHERE user_id=? AND status='ok' AND ts>=?",
            (user_id, week)),
        "fail_week": await scalar(
            "SELECT COUNT(*) FROM post_log WHERE user_id=? AND status NOT IN ('ok','no_target') "
            "AND ts>=?", (user_id, week)),
        "joins_total": await count_events("join_ok", None, user_id),
    }
