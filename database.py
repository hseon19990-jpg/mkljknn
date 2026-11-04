import aiosqlite
from datetime import datetime, timedelta
from config import DB_PATH, DEFAULT_MIN_INTERVAL, DEFAULT_MAX_INTERVAL


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
        """)
        await db.commit()

        # Migrations
        for col, definition in [
            ("name", "TEXT"),
            ("failure_count", "INTEGER DEFAULT 0"),
            ("coma_until", "TEXT"),
            ("flood_until", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE bundles ADD COLUMN {col} {definition}")
                await db.commit()
            except aiosqlite.OperationalError:
                pass

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
async def ensure_user(user_id):
    await _exec("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    await _exec("INSERT OR IGNORE INTO user_limits(user_id) VALUES(?)", (user_id,))


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
    r = await _fetchone("SELECT 1 FROM sessions WHERE phone=? AND is_active=1", (phone,))
    return r is not None


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
        "INSERT INTO bundles(user_id,name,is_active) VALUES(?,?,0)",
        (user_id, name)
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