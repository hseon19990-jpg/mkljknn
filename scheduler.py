import random
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database as db
import notify
import reports
from userbot import ub_manager
from config import (
    FAILURES_BEFORE_COMA, COMA_HOURS, WATCHDOG_SECONDS,
    LOG_RETENTION_DAYS, EVENT_RETENTION_DAYS,
)

# حالات لا نخزّنها في السجل في كل مرة (فقط عند بداية سلسلة التكرار)
_QUIET_STATUSES = {"no_target"}


class SchedulerManager:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.bundle_state = {}      # bundle_id -> "user" | "admin"
        self._last_status = {}      # bundle_id -> آخر حالة (لتقليل الضجيج)
        self._last_skip = {}        # bundle_id -> آخر سبب تخطٍّ

    def start(self):
        self.scheduler.start()
        ub_manager.scheduler = self
        self.scheduler.add_job(
            self._watchdog, "interval", seconds=WATCHDOG_SECONDS,
            id="sys_watchdog", replace_existing=True, max_instances=1, coalesce=True)
        self.scheduler.add_job(
            self._housekeeping, "interval", minutes=30,
            id="sys_housekeeping", replace_existing=True, max_instances=1, coalesce=True)

    # ------------------------------------------------------------- system jobs
    async def _watchdog(self):
        try:
            rep = await ub_manager.watchdog()
            if rep["reconnected"] or rep["failed"] or rep["dead"]:
                print(f"[Watchdog] {rep}")
        except Exception as e:
            print(f"[Watchdog] error: {e}")

    async def _housekeeping(self):
        """تنظيف السجلات + التقرير اليومي للمالك (مرة في اليوم بعد الساعة 23:00 محلياً)."""
        try:
            await db.prune_logs(LOG_RETENTION_DAYS, EVENT_RETENTION_DAYS)
            if not await db.get_setting_bool("daily_report", True):
                return
            tz = await db.get_tz_offset()
            local_now = db.now_utc() + timedelta(hours=tz)
            today = local_now.date().isoformat()
            last = await db.get_setting("last_report_day", "")
            if last == today or local_now.hour < 23:   # يُرسل مساءً (بعد 23:00 محلياً) بأرقام اليوم شبه الكاملة
                return
            await db.set_setting("last_report_day", today)
            text = "🗓 التقرير اليومي\n\n" + await reports.build_global_stats_text()
            await notify.notify_owner(text)
        except Exception as e:
            print(f"[Housekeeping] error: {e}")

    # ------------------------------------------------------------------- tick
    def _skip(self, bid, reason):
        """يطبع سبب التخطي مرة واحدة عند تغيّره فقط."""
        if self._last_skip.get(bid) != reason:
            self._last_skip[bid] = reason
            print(f"[Skip] bundle=#{bid} reason={reason}")

    async def _pick_user_text(self, bundle):
        """الكليشة الأساسية + الإضافية (عشوائياً)."""
        texts = []
        cliche = await db.get_cliche(bundle["cliche_id"])
        if cliche:
            texts.append(cliche["text"])
        for c in await db.get_bundle_cliches(bundle["id"]):
            texts.append(c["text"])
        return random.choice(texts) if texts else None

    async def _tick(self, bundle_id):
        bundle = await db.get_bundle(bundle_id)
        if not bundle or not bundle["is_active"]:
            return
        if not bundle.get("session_id") or not bundle.get("group_id") or not bundle.get("cliche_id"):
            return

        # --- إيقاف شامل من المالك
        if await db.get_setting_bool("global_pause", False):
            self._skip(bundle_id, "global_pause")
            return

        # --- عضو محظور
        if await db.is_blocked(bundle["user_id"]):
            self._skip(bundle_id, "user blocked")
            return

        now = datetime.now()

        coma_until = bundle.get("coma_until")
        if coma_until:
            try:
                until_dt = datetime.fromisoformat(coma_until)
                if now < until_dt:
                    self._skip(bundle_id, f"coma until {coma_until[:16]}")
                    return
                await db.clear_bundle_coma(bundle_id)
                await db.reset_bundle_failures(bundle_id)
                print(f"[Resume] bundle=#{bundle_id} خرجت من السبات")
            except Exception:
                pass

        flood_until = bundle.get("flood_until")
        if flood_until:
            try:
                until_dt = datetime.fromisoformat(flood_until)
                if now < until_dt:
                    self._skip(bundle_id, f"flood until {flood_until[:16]}")
                    return
                await db.clear_bundle_flood(bundle_id)
                print(f"[Resume] bundle=#{bundle_id} انتهى الفلود")
            except Exception:
                pass

        # --- ساعات العمل
        tz = await db.get_tz_offset()
        if not reports.in_work_hours(bundle, tz):
            self._skip(bundle_id, "outside work hours")
            return

        # --- الحد اليومي
        daily = bundle.get("daily_limit") or 0
        if daily > 0:
            done = await db.count_ok_posts(bundle_id, db.day_start_utc_str(tz))
            if done >= daily:
                self._skip(bundle_id, f"daily limit {done}/{daily}")
                return

        # --- اختيار النص (دور العضو ثم دور الأدمن بالتناوب)
        state = self.bundle_state.get(bundle_id, "user")
        if state == "user":
            text = await self._pick_user_text(bundle)
            self.bundle_state[bundle_id] = "admin"
        else:
            self.bundle_state[bundle_id] = "user"
            if not await db.get_setting_bool("admin_cliches_on", True):
                return
            adms = await db.get_all_admin_cliches()
            text = random.choice(adms)["text"] if adms else None

        if not text:
            return
        grp = await db.get_group(bundle["group_id"])
        if not grp:
            self._skip(bundle_id, "group missing")
            return

        self._last_skip.pop(bundle_id, None)
        mode = bundle.get("mode") or "auto"
        status, extra = await ub_manager.send_with_reply(
            bundle["session_id"], grp["group_id"], text, mode)
        await self._handle_result(bundle, status, extra)

    async def _handle_result(self, bundle, status, extra):
        bid = bundle["id"]
        uid = bundle["user_id"]
        sid = bundle["session_id"]
        name = bundle.get("name") or f"#{bid}"

        prev = self._last_status.get(bid)
        self._last_status[bid] = status
        detail = None if extra is None else str(extra)

        # سجل النشر: كل شيء ما عدا تكرار no_target
        if not (status in _QUIET_STATUSES and prev == status):
            await db.log_post(bid, uid, sid, status, detail)
        print(f"[Post] bundle=#{bid} ({name}) status={status}"
              + (f" extra={extra}" if extra is not None else ""))

        if status == "ok":
            await db.record_post_result(bid, True)
            await db.reset_bundle_failures(bid)
            if prev not in (None, "ok", "no_target"):
                print(f"[Post] bundle=#{bid} عادت للنشر بعد: {prev}")

        elif status == "flood":
            secs = int(extra) + 1
            await db.set_bundle_flood(bid, secs)
            await db.record_post_result(bid, False, f"FloodWait {secs}s")
            await db.log_event("send_flood", uid, bid, sid, f"{secs}s")
            if prev != "flood" and secs >= 600:
                await notify.notify_user(
                    uid, f"⏳ تلغرام فرض انتظاراً {secs // 60} دقيقة على المجموعة «{name}». "
                         f"ستستأنف تلقائياً بعد انتهائه.")

        elif status == "slowmode":
            secs = int(extra) + 1
            await db.set_bundle_flood(bid, secs)     # نحترم الوضع البطيء للكروب

        elif status == "no_target":
            pass

        elif status == "conn":
            # مشكلة اتصال: لا تُحسب إخفاقاً حتى لا ندخل السبات بسبب الشبكة
            await db.record_post_result(bid, False, f"اتصال: {extra}")

        elif status == "dead":
            await db.record_post_result(bid, False, f"جلسة ميتة: {extra}")
            await ub_manager.handle_dead_session(sid, str(extra))

        elif status in ("restricted", "error"):
            await db.record_post_result(bid, False, f"{status}: {extra}")
            count = await db.increment_bundle_failure(bid)
            if count >= FAILURES_BEFORE_COMA:
                await db.set_bundle_coma(bid, hours=COMA_HOURS)
                await db.reset_bundle_failures(bid)
                await db.log_event("coma", uid, bid, sid, f"{status}: {extra}")
                print(f"[Coma] bundle=#{bid} سبات {COMA_HOURS} ساعات بسبب {status}: {extra}")
                await notify.notify_user(
                    uid,
                    f"😴 المجموعة «{name}» دخلت سباتاً {COMA_HOURS} ساعات بعد {FAILURES_BEFORE_COMA} "
                    f"إخفاقات متتالية.\nالسبب: {status} — {extra}\n"
                    f"تأكد أن الحساب ما زال عضواً ويستطيع الكتابة في الكروب.")
                await notify.notify_owner(
                    f"😴 سبات: مجموعة #{bid} ({name}) للعضو {uid}\n{status}: {extra}")

    # ------------------------------------------------------------- scheduling
    def schedule_bundle(self, bundle_id, interval):
        job_id = f"bundle_{bundle_id}"
        half = max(int(interval) // 2, 5)
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass
        self.scheduler.add_job(
            self._tick, "interval", seconds=half,
            args=[bundle_id], id=job_id, replace_existing=True,
            max_instances=1, coalesce=True, misfire_grace_time=30,
        )
        self.bundle_state[bundle_id] = "user"

    def unschedule_bundle(self, bundle_id):
        job_id = f"bundle_{bundle_id}"
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass
        self.bundle_state.pop(bundle_id, None)
        self._last_status.pop(bundle_id, None)
        self._last_skip.pop(bundle_id, None)

    async def reload_all(self):
        bundles = await db.get_active_bundles()
        for b in bundles:
            self.schedule_bundle(b["id"], b["interval"])
        print(f"[Scheduler] Reloaded {len(bundles)} bundles")


scheduler_mgr = SchedulerManager()
