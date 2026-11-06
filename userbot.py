import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.tl.types import (
    Channel, Chat, User, ChannelForbidden, ChatForbidden
)
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    ChannelsTooMuchError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    ChannelPrivateError,
    UsernameNotOccupiedError,
    UsernameInvalidError,
    ChatAdminRequiredError,
    UserAlreadyParticipantError,
    InviteRequestSentError,
)
from telethon.tl.functions.channels import LeaveChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import InputUserSelf, PeerChannel
import telethon.errors as _tgerr

from config import (
    API_ID, API_HASH, SESSIONS_DIR,
    CHANNEL_LIMIT_LEAVE_COUNT, LEAVE_DELAY_MINUTES,
    SEND_TIMEOUT, JOIN_LINK_DEDUP_SECONDS,
)
import database as db
import notify


def _errs(*names):
    """يبني tuple من أخطاء Telethon الموجودة فعلاً في النسخة المثبتة."""
    return tuple(getattr(_tgerr, n) for n in names if hasattr(_tgerr, n))


# الجلسة نفسها انتهت (حذف الحساب/حظره/إنهاء الجلسة من الجهاز/استخدامها من مكانين)
DEAD_ERRORS = _errs(
    "AuthKeyUnregisteredError", "UserDeactivatedError", "UserDeactivatedBanError",
    "SessionRevokedError", "SessionExpiredError", "AuthKeyDuplicatedError",
)
# الحساب لا يستطيع الكتابة في هذا الكروب
RESTRICTED_ERRORS = _errs(
    "ChatWriteForbiddenError", "UserBannedInChannelError", "ChatAdminRequiredError",
    "ChannelPrivateError", "ChatRestrictedError",
)
SLOWMODE_ERRORS = _errs("SlowModeWaitError")


# يستخرج الروابط @username, t.me/xxx, t.me/+hash, t.me/joinchat/hash
_DOMAIN = r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)"
CHANNEL_REGEX = re.compile(
    r"(?:"
    + _DOMAIN + r"/(?:joinchat/|\+)([\w-]{10,})"          # دعوة خاصة
    r"|tg://join\?invite=([\w-]{10,})"                      # tg://join?invite=
    r"|tg://resolve\?domain=([A-Za-z0-9_]{4,})"              # tg://resolve?domain=
    r"|" + _DOMAIN + r"/(?!c/|addstickers|share|proxy|socks|setlanguage|login|addlist|boost)([A-Za-z][\w]{3,})"
    r"|(?<![\w.])@([A-Za-z][\w]{3,})"
    r")",
    re.IGNORECASE,
)


class LoginSession:
    def __init__(self, session_name, phone, client):
        self.session_name = session_name
        self.phone = phone
        self.client = client


def _make_client(path):
    """عميل Telethon مع إعدادات اتصال أكثر صبراً (إعادة المحاولة تلقائياً)."""
    return TelegramClient(
        path, API_ID, API_HASH,
        connection_retries=10, retry_delay=5, request_retries=5, auto_reconnect=True,
    )


def _is_invite_link(link):
    return "t.me/+" in link or "joinchat/" in link


class UserbotManager:
    def __init__(self):
        self.clients = {}
        self.login_sessions = {}
        self.scheduler = None
        self._leave_jobs = {}
        # --- حماية الانضمام من الفلود ---
        self._join_block = {}          # (session_id, kind) -> وقت انتهاء الحظر (monotonic)
        self._seen_links = {}          # (session_id, link) -> آخر وقت جرّبناه
        self.join_skipped = {}         # session_id -> عدد الروابط المتجاوزة بسبب الفلود
        # --- حالة الحسابات ---
        self._dialogs_refreshed = {}
        self.last_resolve_flood = {}   # session_id -> ثواني الفلود عند آخر محاولة إضافة كروب
        self._dead_reported = set()
        self._sess_user = {}

    # ---------- Events / helpers ----------
    async def _user_of_session(self, session_id):
        if session_id not in self._sess_user:
            s = await db.get_session(session_id)
            self._sess_user[session_id] = s["user_id"] if s else None
        return self._sess_user[session_id]

    async def _ev(self, kind, session_id, detail=None):
        try:
            await db.log_event(kind, await self._user_of_session(session_id), None,
                               session_id, detail)
        except Exception as e:
            print(f"[Event] {kind}: {e}")

    def _note_flood(self, session_id, link, err):
        """يسجّل الفلود (إن كان الخطأ فلود) كي تُعرض للعضو رسالة واضحة بدل "تعذّر الوصول"."""
        if isinstance(err, FloodWaitError):
            kind, secs = self._set_join_block(session_id, link, err)
            self.last_resolve_flood[session_id] = secs
            print(f"[Resolve] FloodWait {secs}s ({kind}) [session {session_id}]")

    def join_block_remaining(self, session_id, kind):
        return max(0, int(self._join_block.get((session_id, kind), 0) - time.monotonic()))

    def _set_join_block(self, session_id, link, err):
        req = type(getattr(err, "request", None)).__name__
        if "ResolveUsername" in req:
            kind = "resolve"
        elif "Invite" in req:
            kind = "invite"
        else:
            kind = "invite" if _is_invite_link(link) else "resolve"
        secs = int(getattr(err, "seconds", 60) or 60)
        until = time.monotonic() + secs + 5
        if until > self._join_block.get((session_id, kind), 0):
            self._join_block[(session_id, kind)] = until
        return kind, secs

    # ---------- Login flow ----------
    async def cancel_login(self, user_id):
        """يلغي عملية تسجيل دخول معلّقة (نسي المستخدم الكود مثلاً) وينظّف بقاياها."""
        ls = self.login_sessions.pop(user_id, None)
        if not ls:
            return
        try:
            await ls.client.disconnect()
        except Exception:
            pass
        try:
            path = os.path.join(SESSIONS_DIR, ls.session_name) + ".session"
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    async def start_login(self, user_id, phone):
        await self.cancel_login(user_id)
        session_name = f"u{user_id}_{int(asyncio.get_event_loop().time())}"
        path = os.path.join(SESSIONS_DIR, session_name)
        client = _make_client(path)
        await client.connect()
        if await client.is_user_authorized():
            return ("already_authorized", client)
        try:
            await client.send_code_request(phone)
        except Exception as e:
            await client.disconnect()
            return (f"error:{e}", None)
        self.login_sessions[user_id] = LoginSession(session_name, phone, client)
        return ("code_sent", client)

    async def verify_code(self, user_id, code):
        ls = self.login_sessions.get(user_id)
        if not ls:
            return ("no_login", None)
        try:
            await ls.client.sign_in(ls.phone, code)
            return await self._finalize_login(user_id, ls)
        except SessionPasswordNeededError:
            return ("password_needed", ls.client)
        except Exception as e:
            return (f"error:{e}", ls.client)

    async def verify_password(self, user_id, password):
        ls = self.login_sessions.get(user_id)
        if not ls:
            return ("no_login", None)
        try:
            await ls.client.sign_in(password=password)
            return await self._finalize_login(user_id, ls)
        except Exception as e:
            return (f"error:{e}", ls.client)

    async def _finalize_login(self, user_id, ls):
        sid = await db.add_session(user_id, ls.phone, ls.session_name)
        self.clients[sid] = ls.client
        self.login_sessions.pop(user_id, None)
        try:
            self._register_listeners(sid, ls.client)
        except Exception as e:
            # فشل تسجيل المستمعين لا يجب أن يُفشل تسجيل الدخول نفسه
            print(f"[Userbot] register listeners failed for {sid}: {e}")
        return ("success", sid)

    # ---------- Load existing sessions ----------
    async def load_all_sessions(self):
        sessions = await db.get_all_active_sessions()
        for s in sessions:
            path = os.path.join(SESSIONS_DIR, s["session_name"])
            if not os.path.exists(path + ".session"):
                continue
            try:
                client = _make_client(path)
                await client.connect()
                if await client.is_user_authorized():
                    self.clients[s["id"]] = client
                    self._register_listeners(s["id"], client)
                    print(f"[Userbot] Loaded session {s['id']} ({s['phone']})")
                else:
                    await client.disconnect()
            except Exception as e:
                print(f"[Userbot] Failed to load {s['id']}: {e}")

    def get_client(self, session_id):
        return self.clients.get(session_id)

    async def drop_client(self, session_id, delete_file=True):
        """يفصل الحساب ويزيله من الذاكرة (بدون تسجيل خروج، فقد يكون الرقم مفقوداً)."""
        client = self.clients.pop(session_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception as e:
                print(f"[Drop] disconnect {session_id}: {e}")
        if delete_file:
            try:
                s = await db.get_session(session_id)
                if s:
                    path = os.path.join(SESSIONS_DIR, s["session_name"]) + ".session"
                    if os.path.exists(path):
                        os.remove(path)
            except Exception as e:
                print(f"[Drop] file {session_id}: {e}")

    # ---------- Listeners ----------
    def _register_listeners(self, session_id, client):
        @client.on(events.NewMessage(incoming=True)) # <--- إضافة incoming=True لتجاهل رسائل الحساب نفسه
        async def _on_msg(event):
            try:
                await self._process_event(session_id, event)
            except Exception as e:
                print(f"[Userbot listen] {e}")

        @client.on(events.MessageEdited(incoming=True))
        async def _on_edit(event):
            try:
                await self._process_event(session_id, event)
            except Exception as e:
                print(f"[Userbot edit] {e}")

        @client.on(events.Album())  # Album لا يقبل incoming=True
        async def _on_album(event):
            try:
                if event.messages and getattr(event.messages[0], "out", False):
                    return  # تجاهل رسائل الحساب نفسه
                await self._process_event(session_id, event)
            except Exception as e:
                print(f"[Userbot album] {e}")

    async def _process_event(self, session_id, event):
        """
        المنطق: كل رسالة في أي كروب مرتبط بهذه الجلسة → استخراج الروابط → فتحها.
        الحساب هو "العين" التي ترى وتنضم، وليس البوت.
        """
        # 1. طباعة أسماء الكروبات التي يراها الحساب (للتشخيص)
        chat = await event.get_chat()
        chat_title = getattr(chat, "title", str(event.chat_id))
        chat_id = event.chat_id
        
        if not chat_id:
            return

        # 2. هل هذه الجلسة مرتبطة بأي bundle في هذا الكروب؟
        bundles = await db._fetchall(
            "SELECT * FROM bundles WHERE session_id=?", (session_id,)
        )
        matched = False
        allow_join = False
        for b in bundles:
            if b.get("group_id"):
                grp = await db.get_group(b["group_id"])
                if grp:
                    # المقارنة تاخذ بالحسبان الصيغ المختلفة للـ ID
                    if int(grp["group_id"]) == int(chat_id) or str(grp["group_id"]) in str(chat_id):
                        matched = True
                        if b.get("auto_join") in (None, 1):
                            allow_join = True
                        
        # إذا لم يكن مرتبطاً بـ bundle، نتخطاه (إلا إذا كنا نريد طباعة كل شيء للتشخيص)
        if not matched or not allow_join:
            return

        # 3. تجاهل المحظورين
        try:
            sender = await event.get_sender()
            if sender and await db.is_blocked(sender.id):
                return
        except Exception:
            pass

        # 4. نجمع النص + الروابط المخفية + روابط الأزرار
        msgs = getattr(event, "messages", None) or [getattr(event, "message", event)]
        text = "\n".join(self._gather_text(m) for m in msgs)
        if not text:
            return

        # 5. استخراج الروابط
        links = self._extract_links(text)
        if not links:
            return

        print(f"[Listen] Found {len(links)} link(s) in {chat_title}: {links}")
        client = self.get_client(session_id)
        if not client:
            print(f"[Error] Client {session_id} not found!")
            return

        for link in links:
            try:
                await self._open_and_join(session_id, client, link)
            except Exception as e:
                print(f"[Open] {link}: {e}")

    def _extract_links(self, text):
        results = set()
        for m in CHANNEL_REGEX.finditer(text):
            invite_hash = m.group(1) or m.group(2)
            public_name = m.group(3) or m.group(4) or m.group(5)
            if invite_hash:
                results.add("https://t.me/+" + invite_hash)
            elif public_name:
                results.add("@" + public_name)
        return list(results)

    def _gather_text(self, msg):
        """يجمع كل ما يمكن أن يحتويه الرسالة: النص، الروابط المخفية، روابط الأزرار."""
        parts = []
        raw = getattr(msg, "message", None) or getattr(msg, "raw_text", "") or ""
        if raw:
            parts.append(raw)
        # روابط وmentions داخل الـ entities (نص مخفي خلف رابط)
        for ent in (getattr(msg, "entities", None) or []):
            url = getattr(ent, "url", None)
            if url:
                parts.append(url)
            if ent.__class__.__name__ == "MessageEntityMention":
                try:
                    parts.append(raw[ent.offset:ent.offset + ent.length])
                except Exception:
                    pass
        # روابط الأزرار (inline buttons)
        try:
            rows = getattr(msg, "reply_markup", None)
            rows = getattr(rows, "rows", None) or []
            for row in rows:
                for btn in getattr(row, "buttons", []) or []:
                    url = getattr(btn, "url", None)
                    if url:
                        parts.append(url)
        except Exception:
            pass
        # معاينة الرابط (web page preview)
        try:
            wp = getattr(getattr(msg, "media", None), "webpage", None)
            if wp is not None and getattr(wp, "url", None):
                parts.append(wp.url)
        except Exception:
            pass
        return "\n".join(parts)

    async def _open_and_join(self, session_id, client, link):
        """غلاف: يمنع التكرار، ويوقف محاولات الانضمام مؤقتاً عند الفلود بدل إغراق تلغرام."""
        now = time.monotonic()
        key = (session_id, link)
        last = self._seen_links.get(key)
        if last and now - last < JOIN_LINK_DEDUP_SECONDS:
            return                      # نفس الرابط جُرّب قبل قليل
        kind = "invite" if _is_invite_link(link) else "resolve"
        if self.join_block_remaining(session_id, kind) > 0:
            self.join_skipped[session_id] = self.join_skipped.get(session_id, 0) + 1
            return                      # الحساب تحت فلود لهذا النوع من الطلبات
        self._seen_links[key] = now
        if len(self._seen_links) > 5000:   # تنظيف الذاكرة
            cutoff = now - JOIN_LINK_DEDUP_SECONDS
            self._seen_links = {k: v for k, v in self._seen_links.items() if v > cutoff}
        try:
            await self._open_and_join_inner(session_id, client, link)
        except FloodWaitError as e:
            kind, secs = self._set_join_block(session_id, link, e)
            print(f"[Join] FloodWait {secs}s ({kind}) — الانضمام موقوف لهذا الحساب {secs}s [session {session_id}]")
            await self._ev("join_flood", session_id, f"{kind} {secs}s")

    async def _open_and_join_inner(self, session_id, client, link):
        """
        يحاول فتح الرابط. إذا كان قناة/مجموعة → ينضم.
        إذا كان بوت/شخص → يتجاهل.
        """
        ent = None
        print(f"[Join Attempt] {link}")
        try:
            ent = await client.get_entity(link)
        except (UsernameNotOccupiedError, UsernameInvalidError, ValueError):
            # قد يكون رابط دعوة (invite hash)
            if "t.me/+" in link or "joinchat/" in link:
                ent = await self._resolve_invite(client, link)
            if ent is None:
                print(f"[Join] {link}: get_entity failed.")
                return  # غير موجود
        except ChannelPrivateError:
            if "t.me/+" in link:
                ent = await self._resolve_invite(client, link)
            if ent is None:
                return
        except FloodWaitError:
            raise
        except Exception as e:
            print(f"[Open] get_entity({link}): {e}")
            await self._join_direct(session_id, client, link)
            return

        # تجاهل البوتات والأشخاص
        if isinstance(ent, User):
            print(f"[Ignore] {link} is a User/Bot.")
            return  
        if isinstance(ent, (ChatForbidden, ChannelForbidden)):
            return
        if not isinstance(ent, (Channel, Chat)):
            return

        # نحاول الانضمام
        await self._join_direct(session_id, client, link, ent)

    async def _resolve_invite(self, client, link):
        try:
            hash_part = link.split("/")[-1].replace("+", "")
            if link.count("joinchat"):
                hash_part = link.split("joinchat/")[-1]
            try:
                res = await client(CheckChatInviteRequest(hash_part))
                if hasattr(res, "chat"):
                    return res.chat
            except FloodWaitError:
                raise
            except Exception:
                pass
            upd = await client(ImportChatInviteRequest(hash_part))
            if upd.chats:
                return upd.chats[0]
        except FloodWaitError:
            raise
        except (InviteHashExpiredError, InviteHashInvalidError):
            print(f"[Invite] {link}: Expired/Invalid")
            return None
        except Exception as e:
            print(f"[Invite] {link}: {e}")
        return None

    async def _do_join(self, client, link, ent=None):
        """Telethon لا يملك join_chat — نستخدم الطلبات الصحيحة. يعيد الكيان الذي انضممنا له."""
        if "t.me/+" in link or "joinchat/" in link:
            h = link.split("joinchat/")[-1] if "joinchat/" in link else link.split("+")[-1]
            res = await client(ImportChatInviteRequest(h.strip("/")))
            chats = getattr(res, "chats", None)
            return chats[0] if chats else None
        target = ent if ent is not None else await client.get_input_entity(link)
        res = await client(JoinChannelRequest(target))
        chats = getattr(res, "chats", None)
        return chats[0] if chats else ent

    async def _join_direct(self, session_id, client, link, ent=None):
        joined = False
        chat = None
        try:
            chat = await self._do_join(client, link, ent)
            joined = True
            print(f"[Join] OK: {link}")
            await self._ev("join_ok", session_id, link)
        except UserAlreadyParticipantError:
            joined = True
            chat = ent
            print(f"[Join] Already in: {link}")
        except ChannelsTooMuchError:
            print(f"[Join] Limit hit. Leaving {CHANNEL_LIMIT_LEAVE_COUNT} channels...")
            await self._leave_oldest_channels(client, count=CHANNEL_LIMIT_LEAVE_COUNT)
            try:
                chat = await self._do_join(client, link, ent)
                joined = True
                print(f"[Join] OK after leave: {link}")
                await self._ev("join_ok", session_id, link)
            except Exception as e:
                print(f"[Join retry] {link}: {e}")
                await self._ev("join_fail", session_id, f"{link}: {type(e).__name__}")
        except FloodWaitError as e:
            kind, secs = self._set_join_block(session_id, link, e)
            print(f"[Join] FloodWait {secs}s on {link} — الانضمام موقوف {secs}s [session {session_id}]")
            await self._ev("join_flood", session_id, f"{kind} {secs}s")
            return
        except InviteRequestSentError:
            print(f"[Join] Join request sent (needs approval): {link}")
            await self._ev("join_pending", session_id, link)
            return
        except (InviteHashExpiredError, InviteHashInvalidError):
            print(f"[Join] Expired/invalid invite: {link}")
            return
        except Exception as e:
            print(f"[Join] {link}: {type(e).__name__} {e}")
            await self._ev("join_fail", session_id, f"{link}: {type(e).__name__}")
            return

        if joined and self.scheduler:
            chat_id = getattr(chat, "id", None) if isinstance(chat, (Channel, Chat)) else None
            self._schedule_leave_job(session_id, link, chat_id)

    def _schedule_leave_job(self, session_id, link, chat_id=None):
        job_id = f"leave_{session_id}_{hash(link) & 0xffffffff}"
        try:
            self.scheduler.scheduler.remove_job(job_id)
        except Exception:
            pass
        from apscheduler.triggers.date import DateTrigger
        run_at = datetime.now() + timedelta(minutes=LEAVE_DELAY_MINUTES)
        self.scheduler.scheduler.add_job(
            self._leave_channel,
            trigger=DateTrigger(run_date=run_at),
            args=[session_id, link, chat_id],
            id=job_id,
            replace_existing=True,
        )

    async def _leave_oldest_channels(self, client, count=200):
        try:
            dialogs = await client.get_dialogs(limit=None)
            channels = [d for d in dialogs if d.is_channel and not d.is_group]
            fallback = datetime.min.replace(tzinfo=timezone.utc)
            channels.sort(key=lambda d: d.date or fallback)
            to_leave = channels[:count]
            left = 0
            for d in to_leave:
                try:
                    await client(LeaveChannelRequest(d.entity))
                    left += 1
                except Exception as e:
                    print(f"[LeaveOld] {getattr(d, 'name', d.id)}: {e}")
            print(f"[LeaveOld] Left {left}/{len(to_leave)} channels")
        except Exception as e:
            print(f"[LeaveOld] Error: {e}")

    async def _leave_channel(self, session_id, link, chat_id=None):
        client = self.get_client(session_id)
        if not client:
            return
        try:
            ent = None
            if chat_id:
                # نستخدم المعرّف المخزّن محلياً: لا يسبّب ResolveUsername (سبب الفلود الطويل)
                for peer in (PeerChannel(chat_id), chat_id):
                    try:
                        ent = await client.get_input_entity(peer)
                        break
                    except Exception:
                        continue
            if ent is None:
                ent = await client.get_entity(link)
            if isinstance(ent, Chat):
                await client.delete_dialog(ent)
            else:
                await client(LeaveChannelRequest(ent))
            print(f"[Leave] {link}")
        except FloodWaitError as e:
            print(f"[Leave] {link}: FloodWait {e.seconds}s")
        except Exception as e:
            print(f"[Leave] {link}: {e}")

    # ---------- Connection health ----------
    async def _ensure_connected(self, client):
        if client.is_connected():
            return True
        try:
            await asyncio.wait_for(client.connect(), 30)
        except Exception as e:
            print(f"[Connect] {type(e).__name__}: {e}")
        return client.is_connected()

    async def watchdog(self):
        """يفحص كل الحسابات: يعيد الاتصال إذا انقطع ويكشف الجلسات الميتة."""
        rep = {"ok": 0, "reconnected": 0, "failed": 0, "dead": 0}
        for sid, client in list(self.clients.items()):
            try:
                if not client.is_connected():
                    if not await self._ensure_connected(client):
                        rep["failed"] += 1
                        print(f"[Watchdog] session {sid}: فشلت إعادة الاتصال")
                        await self._ev("reconnect_fail", sid, "watchdog")
                        continue
                    rep["reconnected"] += 1
                    print(f"[Watchdog] session {sid}: أُعيد الاتصال")
                    await self._ev("reconnect", sid, "watchdog")
                # ping خفيف يكشف الجلسة الملغاة
                await asyncio.wait_for(client(GetUsersRequest([InputUserSelf()])), 30)
                rep["ok"] += 1
            except DEAD_ERRORS as e:
                rep["dead"] += 1
                await self.handle_dead_session(sid, type(e).__name__)
            except FloodWaitError as e:
                rep["ok"] += 1
                print(f"[Watchdog] session {sid}: FloodWait {e.seconds}s (تجاهل)")
            except Exception as e:
                rep["failed"] += 1
                print(f"[Watchdog] session {sid}: {type(e).__name__}: {e}")
        return rep

    async def handle_dead_session(self, session_id, reason):
        """الجلسة لم تعد صالحة: أوقف مجموعاتها وأبلغ العضو والمالك (مرة واحدة)."""
        if session_id in self._dead_reported:
            return
        self._dead_reported.add(session_id)
        s = await db.get_session(session_id)
        phone = s["phone"] if s else str(session_id)
        user_id = s["user_id"] if s else None
        bundles = await db._fetchall("SELECT * FROM bundles WHERE session_id=?", (session_id,))
        for b in bundles:
            if b["is_active"]:
                await db.set_bundle_active(b["id"], False)
                if self.scheduler:
                    self.scheduler.unschedule_bundle(b["id"])
            await notify.notify_user(
                b["user_id"],
                f"⚠️ توقفت المجموعة «{b.get('name') or b['id']}» لأن جلسة الرقم {phone} "
                f"لم تعد صالحة ({reason}).\n"
                f"افتح المجموعة ← 📱 تغيير الرقم وسجّل الدخول من جديد.", force=True)
        await db.log_event("session_dead", user_id, None, session_id, f"{phone}: {reason}")
        await notify.notify_owner(f"⚠️ جلسة ميتة: #{session_id} ({phone}) — {reason}")
        print(f"[Dead] session {session_id} ({phone}): {reason}")

    # ---------- Sending with reply ----------
    async def _input_peer(self, client, session_id, group_id):
        try:
            return await client.get_input_entity(group_id)
        except (ValueError, TypeError):
            pass
        # الكيان غير موجود في الكاش: نحدّث الحوارات (مرة كل 30 دقيقة كحد أقصى)
        last = self._dialogs_refreshed.get(session_id, 0)
        if time.monotonic() - last > 1800:
            self._dialogs_refreshed[session_id] = time.monotonic()
            try:
                await client.get_dialogs(limit=300)
                return await client.get_input_entity(group_id)
            except (ValueError, TypeError):
                pass
        return None

    async def _send_inner(self, client, session_id, group_id, text, mode):
        peer = await self._input_peer(client, session_id, group_id)
        if peer is None:
            return ("error", "entity_not_found")
        reply_to = None
        if mode in ("reply", "auto"):
            reply_to = await self._find_reply_target(client, peer)
            if reply_to is None and mode == "reply":
                return ("no_target", None)
        await client.send_message(peer, text, reply_to=reply_to)
        return ("ok", "reply" if reply_to else "plain")

    async def send_with_reply(self, session_id, group_id, text, mode="auto"):
        """
        يرسل رسالة للكروب. الحالات:
        ok | flood | slowmode | no_target | restricted | dead | conn | error
        """
        client = self.get_client(session_id)
        if not client:
            return ("error", "no_client")
        if not await self._ensure_connected(client):
            return ("conn", "disconnected")
        try:
            return await asyncio.wait_for(
                self._send_inner(client, session_id, group_id, text, mode), SEND_TIMEOUT)
        except asyncio.TimeoutError:
            return ("conn", "timeout")
        except FloodWaitError as e:
            return ("flood", e.seconds)
        except SLOWMODE_ERRORS as e:
            return ("slowmode", getattr(e, "seconds", 60))
        except DEAD_ERRORS as e:
            return ("dead", type(e).__name__)
        except RESTRICTED_ERRORS as e:
            return ("restricted", type(e).__name__)
        except (ConnectionError, OSError) as e:
            return ("conn", f"{type(e).__name__}: {e}")
        except Exception as e:
            return ("error", f"{type(e).__name__}: {e}")

    async def _find_reply_target(self, client, peer):
        """آخر رسالة من عضو حقيقي (ليس بوتاً ولا محظوراً ولا الحساب نفسه)."""
        messages = await client.get_messages(peer, limit=30)   # الأخطاء الحقيقية تصعد للمُرسِل
        for msg in messages:
            if getattr(msg, "out", False):
                continue
            if not msg.from_id:
                continue
            try:
                sender = await msg.get_sender()
            except Exception:
                continue
            if not sender:
                continue
            if getattr(sender, "bot", False):
                continue
            if await db.is_blocked(sender.id):
                continue
            return msg.id
        return None

    # ---------- Resolve group (لإضافة كروب جديد) ----------
    async def resolve_group(self, session_id, link):
        client = self.get_client(session_id)
        if not client:
            return None
        self.last_resolve_flood.pop(session_id, None)
        link = (link or "").strip()
        if not link:
            return None

        if "t.me/+" in link or "joinchat/" in link:
            ent = await self._resolve_invite(client, link)
            if ent is None:
                try:
                    await self._do_join(client, link)
                except Exception as e:
                    self._note_flood(session_id, link, e)
                    print(f"[Resolve invite join] {e}")
                try:
                    ent = await client.get_entity(link)
                except Exception as e:
                    self._note_flood(session_id, link, e)
                    print(f"[Resolve invite entity] {e}")
                    return None
            title = getattr(ent, "title", str(ent.id))
            return (ent.id, title)

        try:
            if link.startswith("https://t.me/") or link.startswith("t.me/"):
                link = "@" + link.split("t.me/")[-1].split("?")[0].strip("/")
            if link.startswith("@"):
                try:
                    ent = await client.get_entity(link)
                except Exception:
                    try:
                        await self._do_join(client, link)
                        ent = await client.get_entity(link)
                    except Exception as e:
                        self._note_flood(session_id, link, e)
                        print(f"[Resolve @] {e}")
                        return None
            else:
                try:
                    ent = await client.get_entity(int(link))
                except ValueError:
                    ent = await client.get_entity(link)
            title = getattr(ent, "title", str(ent.id))
            return (ent.id, title)
        except Exception as e:
            self._note_flood(session_id, link, e)
            print(f"[Resolve] {e}")
            return None


ub_manager = UserbotManager()
