import asyncio
import os
import re
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
)
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest

from config import (
    API_ID, API_HASH, SESSIONS_DIR,
    CHANNEL_LIMIT_LEAVE_COUNT, LEAVE_DELAY_MINUTES,
)
import database as db


# يستخرج الروابط @username, t.me/xxx, t.me/+hash, t.me/joinchat/hash
CHANNEL_REGEX = re.compile(
    r"(?:"
    r"(?:https?://)?t\.me/(?:joinchat/|\+)([\w\d_-]{10,})"
    r"|(?:https?://)?t\.me/([\w\d_]{4,})"
    r"|@([\w\d_]{4,})"
    r")"
)


class LoginSession:
    def __init__(self, session_name, phone, client):
        self.session_name = session_name
        self.phone = phone
        self.client = client


class UserbotManager:
    def __init__(self):
        self.clients = {}
        self.login_sessions = {}
        self.scheduler = None
        self._leave_jobs = {}

    # ---------- Login flow ----------
    async def start_login(self, user_id, phone):
        session_name = f"u{user_id}_{int(asyncio.get_event_loop().time())}"
        path = os.path.join(SESSIONS_DIR, session_name)
        client = TelegramClient(path, API_ID, API_HASH)
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
        self._register_listeners(sid, ls.client)
        del self.login_sessions[user_id]
        return ("success", sid)

    # ---------- Load existing sessions ----------
    async def load_all_sessions(self):
        sessions = await db.get_all_active_sessions()
        for s in sessions:
            path = os.path.join(SESSIONS_DIR, s["session_name"])
            if not os.path.exists(path + ".session"):
                continue
            try:
                client = TelegramClient(path, API_ID, API_HASH)
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

    # ---------- Listeners ----------
    def _register_listeners(self, session_id, client):
        @client.on(events.NewMessage(incoming=True)) # <--- إضافة incoming=True لتجاهل رسائل الحساب نفسه
        async def _on_msg(event):
            try:
                await self._process_event(session_id, event)
            except Exception as e:
                print(f"[Userbot listen] {e}")

        @client.on(events.Album(incoming=True))
        async def _on_album(event):
            try:
                for m in event.messages:
                    await self._process_event(session_id, m)
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
        for b in bundles:
            if b.get("group_id"):
                grp = await db.get_group(b["group_id"])
                if grp:
                    # المقارنة تاخذ بالحسبان الصيغ المختلفة للـ ID
                    if int(grp["group_id"]) == int(chat_id) or str(grp["group_id"]) in str(chat_id):
                        matched = True
                        break
                        
        # إذا لم يكن مرتبطاً بـ bundle، نتخطاه (إلا إذا كنا نريد طباعة كل شيء للتشخيص)
        if not matched:
            return

        # 3. تجاهل المحظورين
        try:
            sender = await event.get_sender()
            if sender and await db.is_blocked(sender.id):
                return
        except Exception:
            pass

        # 4. النص من الرسالة (يدعم الميديا والكابشن)
        text = event.raw_text or ""
        if not text and event.message:
            text = getattr(event.message, "message", "") or ""
        
        print(f"[DEBUG] رسالة في {chat_title}: {text[:50]}") # للتشخيص
        
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
            invite_hash, public_name, mention = m.group(1), m.group(2), m.group(3)
            if invite_hash:
                results.add("https://t.me/+" + invite_hash)
            elif public_name:
                # تجاهل الروابط التي قد تكون يوزر للكروب نفسه
                results.add("@" + public_name)
            elif mention:
                results.add(mention)
        return list(results)

    async def _open_and_join(self, session_id, client, link):
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
            return
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
        await self._join_direct(session_id, client, link)

    async def _resolve_invite(self, client, link):
        try:
            hash_part = link.split("/")[-1].replace("+", "")
            if link.count("joinchat"):
                hash_part = link.split("joinchat/")[-1]
            try:
                res = await client(CheckChatInviteRequest(hash_part))
                if hasattr(res, "chat"):
                    return res.chat
            except Exception:
                pass
            upd = await client(ImportChatInviteRequest(hash_part))
            if upd.chats:
                return upd.chats[0]
        except (InviteHashExpiredError, InviteHashInvalidError):
            print(f"[Invite] {link}: Expired/Invalid")
            return None
        except Exception as e:
            print(f"[Invite] {link}: {e}")
        return None

    async def _join_direct(self, session_id, client, link):
        joined = False
        try:
            await client.join_chat(link)
            joined = True
            print(f"[Join] OK: {link}")
        except UserAlreadyParticipantError:
            joined = True
            print(f"[Join] Already in: {link}")
        except ChannelsTooMuchError:
            print(f"[Join] Limit hit. Leaving {CHANNEL_LIMIT_LEAVE_COUNT} channels...")
            await self._leave_oldest_channels(client, count=CHANNEL_LIMIT_LEAVE_COUNT)
            try:
                await client.join_chat(link)
                joined = True
                print(f"[Join] OK after leave: {link}")
            except Exception as e:
                print(f"[Join retry] {link}: {e}")
        except FloodWaitError as e:
            print(f"[Join] FloodWait {e.seconds}s on {link}")
            return
        except (InviteHashExpiredError, InviteHashInvalidError):
            print(f"[Join] Expired/invalid invite: {link}")
            return
        except Exception as e:
            print(f"[Join] {link}: {type(e).__name__} {e}")
            return

        if joined and self.scheduler:
            self._schedule_leave_job(session_id, link)

    def _schedule_leave_job(self, session_id, link):
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
            args=[session_id, link],
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

    async def _leave_channel(self, session_id, link):
        client = self.get_client(session_id)
        if not client:
            return
        try:
            await client.leave_chat(link)
            print(f"[Leave] {link}")
        except Exception as e:
            print(f"[Leave] {link}: {e}")

    # ---------- Sending with reply ----------
    async def send_with_reply(self, session_id, group_id, text):
        client = self.get_client(session_id)
        if not client:
            return ("error", "no_client")
        reply_to = await self._find_reply_target(client, group_id)
        if reply_to is None:
            return ("no_target", None)
        try:
            await client.send_message(group_id, text, reply_to=reply_to)
            return ("ok", None)
        except FloodWaitError as e:
            return ("flood", e.seconds)
        except (ChatWriteForbiddenError, UserBannedInChannelError):
            return ("restricted", None)
        except Exception as e:
            return ("error", str(e))

    async def _find_reply_target(self, client, group_id):
        try:
            messages = await client.get_messages(group_id, limit=30)
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
        except Exception:
            pass
        return None

    # ---------- Resolve group (لإضافة كروب جديد) ----------
    async def resolve_group(self, session_id, link):
        client = self.get_client(session_id)
        if not client:
            return None
        link = (link or "").strip()
        if not link:
            return None

        if "t.me/+" in link or "joinchat/" in link:
            ent = await self._resolve_invite(client, link)
            if ent is None:
                try:
                    await client.join_chat(link)
                except Exception as e:
                    print(f"[Resolve invite join] {e}")
                try:
                    ent = await client.get_entity(link)
                except Exception as e:
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
                        await client.join_chat(link)
                        ent = await client.get_entity(link)
                    except Exception as e:
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
            print(f"[Resolve] {e}")
            return None


ub_manager = UserbotManager()