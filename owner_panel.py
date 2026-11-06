"""لوحة المالك الموسّعة: إحصائيات، إدارة الأعضاء والمجموعات والحسابات، إذاعة، سجلات، تحكم، نسخ احتياطي."""
import asyncio
import csv
import io
import os
import sqlite3
import tempfile
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile,
)
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError

import database as db
import reports
from config import OWNER_ID, DB_PATH
from userbot import ub_manager
from scheduler import scheduler_mgr
from bot_handlers import (
    owner_menu, stop_user_bundles, delete_user_bundles, release_session,
    _bundle_detail_text,
)

router = Router()
PAGE = 8


class OwnerPanel(StatesGroup):
    user_lookup = State()
    user_dm = State()
    user_limits = State()
    broadcast_wait = State()
    welcome_wait = State()
    tz_wait = State()
    deflim_wait = State()


# ------------------------------------------------------------------ helpers
def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


BACK_OWNER = [_btn("🔙 لوحة المالك", "owner_menu")]


async def _edit(cb: CallbackQuery, text, kb=None):
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        try:
            await cb.message.answer(text, reply_markup=kb)
        except Exception:
            pass


def _is_owner(user_id):
    return user_id == OWNER_ID


def _user_label(u, uid=None):
    if u:
        if u.get("username"):
            return f"@{u['username']}"
        if u.get("full_name"):
            return u["full_name"][:20]
        return str(u["user_id"])
    return str(uid)


# ------------------------------------------------------------------ الإحصائيات
@router.callback_query(F.data == "ow_stats")
async def cb_ow_stats(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    text = await reports.build_global_stats_text()
    await _edit(cb, text, _kb([_btn("🔄 تحديث", "ow_stats")], BACK_OWNER))
    await cb.answer()


@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not _is_owner(msg.from_user.id):
        return
    await msg.answer(await reports.build_global_stats_text())


@router.message(Command("health"))
async def cmd_health(msg: Message):
    if not _is_owner(msg.from_user.id):
        return
    await msg.answer("🩺 جارٍ فحص الحسابات...")
    rep = await ub_manager.watchdog()
    await msg.answer(
        "🩺 نتيجة الفحص\n"
        f"✅ سليمة: {rep['ok']}\n🔄 أُعيد اتصالها: {rep['reconnected']}\n"
        f"⚠️ فشل الاتصال: {rep['failed']}\n💀 جلسات ميتة: {rep['dead']}\n"
        f"📦 مجموعات مجدولة: {len(scheduler_mgr.bundle_state)}"
    )


# ------------------------------------------------------------------ الأعضاء
@router.callback_query(F.data == "ow_users")
async def cb_ow_users(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    total = await db.scalar("SELECT COUNT(*) FROM users")
    await _edit(cb, f"👥 إدارة الأعضاء (الإجمالي: {total})", _kb(
        [_btn("🔍 بحث / إدارة عضو", "ow_ufind")],
        [_btn("🆕 أحدث الأعضاء", "ow_urecent"), _btn("🏆 الأكثر نشاطاً", "ow_utop")],
        [_btn("🚫 المحظورون", "ow_ublocked")],
        BACK_OWNER,
    ))
    await cb.answer()


async def _user_list_kb(ids, back="ow_users"):
    rows = []
    for uid in ids:
        u = await db.get_user(uid)
        rows.append([_btn(f"{_user_label(u, uid)}  ·  {uid}", f"ow_u_{uid}")])
    rows.append([_btn("🔙 رجوع", back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "ow_urecent")
async def cb_ow_urecent(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    users = await db.get_recent_users(15)
    await _edit(cb, "🆕 أحدث الأعضاء:", await _user_list_kb([u["user_id"] for u in users]))
    await cb.answer()


@router.callback_query(F.data == "ow_utop")
async def cb_ow_utop(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    tz = await db.get_tz_offset()
    top = await db.top_users_by_posts(db.day_start_utc_str(tz, 6), 10)
    if not top:
        await cb.answer("لا توجد بيانات نشر بعد.", show_alert=True); return
    text = "🏆 الأكثر نشاطاً (آخر 7 أيام):\n\n" + "\n".join(
        f"{i}. {r['user_id']} — {r['c']} رسالة" for i, r in enumerate(top, 1))
    await _edit(cb, text, await _user_list_kb([r["user_id"] for r in top]))
    await cb.answer()


@router.callback_query(F.data == "ow_ublocked")
async def cb_ow_ublocked(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    rows = await db.get_blocked_list(30)
    if not rows:
        await cb.answer("لا يوجد محظورون.", show_alert=True); return
    kb = [[_btn(f"✅ فك حظر {r['entity_id']}", f"ow_uub_{r['entity_id']}")] for r in rows]
    kb.append([_btn("🔙 رجوع", "ow_users")])
    await _edit(cb, f"🚫 المحظورون ({len(rows)}):", InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()


@router.callback_query(F.data == "ow_ufind")
async def cb_ow_ufind(cb: CallbackQuery, state: FSMContext):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    await state.set_state(OwnerPanel.user_lookup)
    await cb.message.answer("🔍 أرسل ID العضو أو @username:")
    await cb.answer()


async def _user_card(uid):
    tz = await db.get_tz_offset()
    u = await db.get_user(uid)
    blocked = await db.is_blocked(uid)
    c = await db.user_counts(uid, tz)
    lim = await db.get_limits(uid)
    lines = [
        f"👤 {_user_label(u, uid)}",
        f"🆔 {uid}",
    ]
    if u:
        if u.get("full_name"):
            lines.append(f"الاسم: {u['full_name']}")
        lines.append(f"انضم: {reports.fmt_local(u.get('created_at'), tz, '%Y-%m-%d')}")
        lines.append(f"آخر ظهور: {reports.ago(u.get('last_seen'))}")
        lines.append(f"🔔 الإشعارات: {'✅' if u.get('notify', 1) in (None, 1) else '❌'}")
    else:
        lines.append("⚠️ غير مسجّل في قاعدة البيانات (لم يستخدم البوت).")
    lines += [
        f"الحالة: {'🚫 محظور' if blocked else '✅ نشط'}",
        "",
        f"📦 مجموعات: {c['bundles']} / {lim['max_groups']} (تعمل {c['bundles_active']})",
        f"📱 أرقام: {c['sessions']} / {lim['max_numbers']}",
        f"💬 كليشات/مجموعة: {lim['max_cliches']}" + (" (مخصّصة)" if lim.get("custom") else ""),
        f"✅ رسائل: {c['sent_total']} (اليوم {c['sent_today']} · أسبوع {c['sent_week']})",
        f"🔗 انضمامات: {c['joins_total']}",
    ]
    block_btn = _btn("✅ فك الحظر", f"ow_uub_{uid}") if blocked else _btn("🚫 حظر", f"ow_ublk_{uid}")
    kb = _kb(
        [block_btn, _btn("✉️ مراسلة", f"ow_udm_{uid}")],
        [_btn("📊 تغيير الحدود", f"ow_ulim_{uid}"), _btn("♻️ حدود افتراضية", f"ow_ulimreset_{uid}")],
        [_btn("⏸ إيقاف مجموعاته", f"ow_ustop_{uid}"), _btn("📦 مجموعاته", f"ow_ubund_{uid}")],
        [_btn("🗑 حذف كل مجموعاته", f"ow_udel_{uid}")],
        [_btn("🔙 الأعضاء", "ow_users")],
    )
    return "\n".join(lines), kb


@router.message(OwnerPanel.user_lookup)
async def proc_user_lookup(msg: Message, state: FSMContext):
    if not _is_owner(msg.from_user.id):
        return
    q = (msg.text or "").strip()
    u = await db.find_user(q)
    if u:
        uid = u["user_id"]
    elif q.lstrip("-").isdigit():
        uid = int(q)
    else:
        await msg.answer("❌ لم أجد عضواً بهذا اليوزر. جرّب الـ ID.")
        return
    await state.clear()
    text, kb = await _user_card(uid)
    await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("ow_u_"))
async def cb_ow_u(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    uid = int(cb.data[len("ow_u_"):])
    text, kb = await _user_card(uid)
    await _edit(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("ow_ublk_"))
async def cb_ow_ublk(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    uid = int(cb.data[len("ow_ublk_"):])
    await db.block_entity(uid, "user")
    n = await stop_user_bundles(uid)
    await db.log_event("owner_block", uid, None, None, f"stopped {n}")
    text, kb = await _user_card(uid)
    await _edit(cb, text, kb)
    await cb.answer(f"🚫 حُظر وأُوقفت {n} مجموعة", show_alert=True)


@router.callback_query(F.data.startswith("ow_uub_"))
async def cb_ow_uub(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    uid = int(cb.data[len("ow_uub_"):])
    await db.unblock_entity(uid)
    await db.log_event("owner_unblock", uid)
    text, kb = await _user_card(uid)
    await _edit(cb, text, kb)
    await cb.answer("✅ رُفع الحظر")


@router.callback_query(F.data.startswith("ow_ustop_"))
async def cb_ow_ustop(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    uid = int(cb.data[len("ow_ustop_"):])
    n = await stop_user_bundles(uid)
    text, kb = await _user_card(uid)
    await _edit(cb, text, kb)
    await cb.answer(f"⏸ أُوقفت {n} مجموعة", show_alert=True)


@router.callback_query(F.data.startswith("ow_udel_"))
async def cb_ow_udel(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    uid = int(cb.data[len("ow_udel_"):])
    await _edit(cb, f"⚠️ سيتم حذف كل مجموعات العضو {uid} وتحرير أرقامه نهائياً. متأكد؟", _kb(
        [_btn("✅ نعم، احذف", f"ow_udelok_{uid}"), _btn("❌ إلغاء", f"ow_u_{uid}")]))
    await cb.answer()


@router.callback_query(F.data.startswith("ow_udelok_"))
async def cb_ow_udelok(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    uid = int(cb.data[len("ow_udelok_"):])
    n = await delete_user_bundles(uid)
    await db.log_event("owner_delete_bundles", uid, None, None, str(n))
    text, kb = await _user_card(uid)
    await _edit(cb, text, kb)
    await cb.answer(f"🗑 حُذفت {n} مجموعة", show_alert=True)


@router.callback_query(F.data.startswith("ow_ulimreset_"))
async def cb_ow_ulimreset(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    uid = int(cb.data[len("ow_ulimreset_"):])
    await db.reset_limits(uid)
    text, kb = await _user_card(uid)
    await _edit(cb, text, kb)
    await cb.answer("♻️ رُجعت للافتراضي")


@router.callback_query(F.data.startswith("ow_ulim_"))
async def cb_ow_ulim(cb: CallbackQuery, state: FSMContext):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    uid = int(cb.data[len("ow_ulim_"):])
    await state.update_data(target=uid)
    await state.set_state(OwnerPanel.user_limits)
    await cb.message.answer(
        f"📊 حدود العضو {uid}\nأرسل ثلاثة أرقام: أرقام مجموعات كليشات\nمثال: 3 3 5")
    await cb.answer()


@router.message(OwnerPanel.user_limits)
async def proc_user_limits(msg: Message, state: FSMContext):
    if not _is_owner(msg.from_user.id):
        return
    parts = (msg.text or "").split()
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        await msg.answer("❌ أرسل ثلاثة أرقام: أرقام مجموعات كليشات")
        return
    d = await state.get_data()
    uid = d["target"]
    mn, mg, mc = map(int, parts)
    await db.set_limits(uid, mn, mg, mc)
    await state.clear()
    text, kb = await _user_card(uid)
    await msg.answer("✅ حُفظت الحدود.\n\n" + text, reply_markup=kb)


@router.callback_query(F.data.startswith("ow_udm_"))
async def cb_ow_udm(cb: CallbackQuery, state: FSMContext):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    uid = int(cb.data[len("ow_udm_"):])
    await state.update_data(target=uid)
    await state.set_state(OwnerPanel.user_dm)
    await cb.message.answer(f"✉️ أرسل الرسالة التي تريد إيصالها للعضو {uid}:")
    await cb.answer()


@router.message(OwnerPanel.user_dm)
async def proc_user_dm(msg: Message, state: FSMContext):
    if not _is_owner(msg.from_user.id):
        return
    d = await state.get_data()
    uid = d["target"]
    try:
        await msg.bot.copy_message(uid, msg.chat.id, msg.message_id)
        await msg.answer("✅ أُرسلت.")
    except Exception as e:
        await msg.answer(f"❌ تعذّر الإرسال: {e}")
    await state.clear()


@router.callback_query(F.data.startswith("ow_ubund_"))
async def cb_ow_ubund(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    uid = int(cb.data[len("ow_ubund_"):])
    bundles = await db.get_user_bundles(uid)
    if not bundles:
        await cb.answer("لا توجد مجموعات.", show_alert=True); return
    rows = []
    for b in bundles:
        rows.append([_btn(f"{_icon(b)} #{b['id']} {(b.get('name') or '')[:20]}", f"ow_b_{b['id']}")])
    rows.append([_btn("🔙 رجوع", f"ow_u_{uid}")])
    await _edit(cb, f"📦 مجموعات {uid}:", InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


# ------------------------------------------------------------------ المجموعات
def _future(iso):
    try:
        return bool(iso) and datetime.fromisoformat(iso) > datetime.now()
    except Exception:
        return False


def _icon(b):
    if not b["is_active"]:
        return "🔴"
    if _future(b.get("coma_until")):
        return "😴"
    if _future(b.get("flood_until")):
        return "⏳"
    if b.get("session_id") not in ub_manager.clients:
        return "⚠️"
    return "🟢"


@router.callback_query(F.data.startswith("ow_bundles_"))
async def cb_ow_bundles(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    page = int(cb.data[len("ow_bundles_"):])
    total = await db.count_bundles()
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = min(max(page, 0), pages - 1)
    rows_db = await db.get_bundles_page(page * PAGE, PAGE)
    rows = [[_btn(f"{_icon(b)} #{b['id']} {(b.get('name') or '')[:14]} · {b['user_id']}", f"ow_b_{b['id']}")]
            for b in rows_db]
    nav = []
    if page > 0:
        nav.append(_btn("◀️", f"ow_bundles_{page - 1}"))
    nav.append(_btn(f"{page + 1}/{pages}", f"ow_bundles_{page}"))
    if page < pages - 1:
        nav.append(_btn("▶️", f"ow_bundles_{page + 1}"))
    rows.append(nav)
    rows.append(BACK_OWNER)
    await _edit(cb, f"📦 كل المجموعات ({total})\n🟢 تعمل  🔴 متوقفة  😴 سبات  ⏳ فلود  ⚠️ الحساب غير محمّل",
                InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


async def _bundle_admin_card(bid):
    b = await db.get_bundle(bid)
    if not b:
        return None, None
    text = await _bundle_detail_text(b)
    loaded = b.get("session_id") in ub_manager.clients
    text += f"\n\n👤 العضو: {b['user_id']}\n🔌 الحساب محمّل: {'✅' if loaded else '❌'}"
    if b.get("failure_count"):
        text += f"\n⚠️ إخفاقات متتالية: {b['failure_count']}"
    kb = _kb(
        [_btn("⏸ إيقاف", f"ow_bstop_{bid}"), _btn("▶️ تشغيل", f"ow_bstart_{bid}")],
        [_btn("🧹 تصفير السبات/الفلود", f"ow_bclear_{bid}")],
        [_btn("📊 إحصائيات", f"ow_bstats_{bid}"), _btn("👤 العضو", f"ow_u_{b['user_id']}")],
        [_btn("🗑 حذف", f"ow_bdel_{bid}")],
        [_btn("🔙 المجموعات", "ow_bundles_0")],
    )
    return text, kb


@router.callback_query(F.data.startswith("ow_b_"))
async def cb_ow_b(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    bid = int(cb.data[len("ow_b_"):])
    text, kb = await _bundle_admin_card(bid)
    if not text:
        await cb.answer("غير موجودة.", show_alert=True); return
    await _edit(cb, text, kb)
    await cb.answer()


async def _refresh_b(cb, bid, note):
    text, kb = await _bundle_admin_card(bid)
    if text:
        await _edit(cb, text, kb)
    await cb.answer(note)


@router.callback_query(F.data.startswith("ow_bstop_"))
async def cb_ow_bstop(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    bid = int(cb.data[len("ow_bstop_"):])
    await db.set_bundle_active(bid, False)
    scheduler_mgr.unschedule_bundle(bid)
    await _refresh_b(cb, bid, "⏸ أُوقفت")


@router.callback_query(F.data.startswith("ow_bstart_"))
async def cb_ow_bstart(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    bid = int(cb.data[len("ow_bstart_"):])
    b = await db.get_bundle(bid)
    if not b:
        await cb.answer("غير موجودة.", show_alert=True); return
    if not b.get("session_id") or not b.get("group_id") or not b.get("cliche_id"):
        await cb.answer("⚠️ بياناتها ناقصة.", show_alert=True); return
    if await db.is_session_used_elsewhere(b["session_id"], exclude_bundle_id=bid):
        await cb.answer("⚠️ الرقم مستخدم في مجموعة نشطة أخرى.", show_alert=True); return
    await db.set_bundle_active(bid, True)
    scheduler_mgr.schedule_bundle(bid, b["interval"])
    await _refresh_b(cb, bid, "▶️ شُغّلت")


@router.callback_query(F.data.startswith("ow_bclear_"))
async def cb_ow_bclear(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    bid = int(cb.data[len("ow_bclear_"):])
    await db.clear_bundle_coma(bid)
    await db.clear_bundle_flood(bid)
    await db.reset_bundle_failures(bid)
    await _refresh_b(cb, bid, "🧹 صُفّرت")


@router.callback_query(F.data.startswith("ow_bstats_"))
async def cb_ow_bstats(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    bid = int(cb.data[len("ow_bstats_"):])
    b = await db.get_bundle(bid)
    if not b:
        await cb.answer("غير موجودة.", show_alert=True); return
    await _edit(cb, await reports.build_bundle_stats_text(b),
                _kb([_btn("🔙 رجوع", f"ow_b_{bid}")]))
    await cb.answer()


@router.callback_query(F.data.startswith("ow_bdel_"))
async def cb_ow_bdel(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    bid = int(cb.data[len("ow_bdel_"):])
    await _edit(cb, f"⚠️ حذف المجموعة #{bid} نهائياً؟", _kb(
        [_btn("✅ نعم", f"ow_bdelok_{bid}"), _btn("❌ لا", f"ow_b_{bid}")]))
    await cb.answer()


@router.callback_query(F.data.startswith("ow_bdelok_"))
async def cb_ow_bdelok(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    bid = int(cb.data[len("ow_bdelok_"):])
    b = await db.get_bundle(bid)
    scheduler_mgr.unschedule_bundle(bid)
    sid = b.get("session_id") if b else None
    await db.delete_bundle(bid)
    await db.delete_bundle_cliches(bid)
    await release_session(sid)
    await _edit(cb, f"🗑 حُذفت المجموعة #{bid}.", _kb([_btn("🔙 المجموعات", "ow_bundles_0")]))
    await cb.answer("🗑 حُذفت")


# ------------------------------------------------------------------ الحسابات
@router.callback_query(F.data == "ow_sessions")
async def cb_ow_sessions(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    sessions = await db.get_all_active_sessions()
    if not sessions:
        await cb.answer("لا توجد حسابات.", show_alert=True); return
    lines = [f"📱 الحسابات ({len(sessions)})", "✅ متصل  ⚠️ غير متصل  ❌ غير محمّل", ""]
    for s in sessions[:40]:
        c = ub_manager.clients.get(s["id"])
        if c is None:
            st = "❌"
        else:
            st = "✅" if reports._is_conn(c) else "⚠️"
        used = "🔗" if await db.is_session_in_use(s["id"]) else "·"
        fl = ub_manager.join_block_remaining(s["id"], "resolve")
        fl_txt = f" ⏳انضمام {fl // 60}د" if fl > 0 else ""
        lines.append(f"{st}{used} #{s['id']} {s['phone']} (user {s['user_id']}){fl_txt}")
    if len(sessions) > 40:
        lines.append(f"… و{len(sessions) - 40} أخرى")
    await _edit(cb, "\n".join(lines), _kb([_btn("🔄 فحص وإعادة اتصال الكل", "ow_reconnect")], BACK_OWNER))
    await cb.answer()


@router.callback_query(F.data == "ow_reconnect")
async def cb_ow_reconnect(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    await cb.answer("⏳ جارٍ الفحص...")
    rep = await ub_manager.watchdog()
    await cb.message.answer(
        "🩺 نتيجة الفحص\n"
        f"✅ سليمة: {rep['ok']}\n🔄 أُعيد اتصالها: {rep['reconnected']}\n"
        f"⚠️ فشل: {rep['failed']}\n💀 ميتة: {rep['dead']}")


# ------------------------------------------------------------------ الإذاعة
_pending_bc = {}
_bc_running = False
_bg_tasks = set()


@router.callback_query(F.data == "ow_broadcast")
async def cb_ow_broadcast(cb: CallbackQuery, state: FSMContext):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    if _bc_running:
        await cb.answer("⏳ هناك إذاعة جارية الآن.", show_alert=True); return
    await state.set_state(OwnerPanel.broadcast_wait)
    await cb.message.answer(
        "📣 أرسل الآن الرسالة التي تريد إذاعتها (نص / صورة / فيديو / ملف…).\n"
        "ستُنسخ كما هي لكل الأعضاء. للإلغاء أرسل /start")
    await cb.answer()


@router.message(OwnerPanel.broadcast_wait)
async def proc_broadcast_msg(msg: Message, state: FSMContext):
    if not _is_owner(msg.from_user.id):
        return
    _pending_bc[msg.from_user.id] = (msg.chat.id, msg.message_id)
    await state.clear()
    n_all = len(await db.get_all_user_ids())
    n_act = len(await db.get_active_user_ids())
    await msg.answer(
        "👆 هذه الرسالة ستُذاع. اختر الجمهور:",
        reply_markup=_kb(
            [_btn(f"📣 للجميع ({n_all})", "ow_bc_all")],
            [_btn(f"🟢 لأصحاب المجموعات العاملة ({n_act})", "ow_bc_active")],
            [_btn("❌ إلغاء", "ow_bc_cancel")],
        ))


@router.callback_query(F.data == "ow_bc_cancel")
async def cb_bc_cancel(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    _pending_bc.pop(cb.from_user.id, None)
    await _edit(cb, "❎ أُلغيت الإذاعة.", _kb(BACK_OWNER))
    await cb.answer()


@router.callback_query(F.data.in_({"ow_bc_all", "ow_bc_active"}))
async def cb_bc_go(cb: CallbackQuery):
    global _bc_running
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    src = _pending_bc.pop(cb.from_user.id, None)
    if not src:
        await cb.answer("انتهت صلاحية الطلب. ابدأ من جديد.", show_alert=True); return
    if _bc_running:
        await cb.answer("⏳ هناك إذاعة جارية.", show_alert=True); return
    ids = await (db.get_all_user_ids() if cb.data == "ow_bc_all" else db.get_active_user_ids())
    if not ids:
        await cb.answer("لا يوجد مستلمون.", show_alert=True); return
    _bc_running = True
    status_msg = await cb.message.edit_text(f"📣 بدأت الإذاعة إلى {len(ids)} عضو...")
    task = asyncio.create_task(_run_broadcast(cb.bot, status_msg, ids, src))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    await cb.answer()


async def _run_broadcast(bot, status_msg, ids, src):
    global _bc_running
    ok = blocked = failed = 0
    total = len(ids)
    started = datetime.now()
    try:
        for i, uid in enumerate(ids, 1):
            for attempt in (1, 2):
                try:
                    await bot.copy_message(uid, src[0], src[1])
                    ok += 1
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    if attempt == 2:
                        failed += 1
                except TelegramForbiddenError:
                    blocked += 1
                    break
                except Exception:
                    failed += 1
                    break
            await asyncio.sleep(0.05)
            if i % 100 == 0:
                try:
                    await status_msg.edit_text(f"📣 الإذاعة جارية… {i}/{total}")
                except Exception:
                    pass
    finally:
        _bc_running = False
    secs = int((datetime.now() - started).total_seconds())
    await db.log_event("broadcast", OWNER_ID, None, None, f"ok={ok} blocked={blocked} failed={failed}")
    try:
        await status_msg.edit_text(
            f"✅ انتهت الإذاعة ({secs} ث)\n\n"
            f"📨 وصلت: {ok}\n🚷 حاظرو البوت: {blocked}\n❌ فشلت: {failed}\n👥 المجموع: {total}",
            reply_markup=_kb(BACK_OWNER))
    except Exception:
        pass


# ------------------------------------------------------------------ السجلات
_EVENT_ICON = {
    "join_ok": "🔗", "join_flood": "⏳", "join_fail": "❌", "join_pending": "🕓",
    "coma": "😴", "send_flood": "⏳", "session_dead": "💀", "reconnect": "🔄",
    "reconnect_fail": "📡", "broadcast": "📣", "owner_block": "🚫",
    "owner_unblock": "✅", "owner_delete_bundles": "🗑",
}


@router.callback_query(F.data == "ow_logs")
async def cb_ow_logs(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    tz = await db.get_tz_offset()
    rows = await db.get_recent_events(25)
    lines = ["📜 آخر الأحداث:", ""]
    if not rows:
        lines.append("(لا توجد أحداث بعد)")
    for r in rows:
        who = f" u{r['user_id']}" if r.get("user_id") else ""
        b = f" b#{r['bundle_id']}" if r.get("bundle_id") else ""
        s = f" s#{r['session_id']}" if r.get("session_id") else ""
        d = f" — {r['detail']}" if r.get("detail") else ""
        lines.append(f"{reports.fmt_local(r['ts'], tz)} {_EVENT_ICON.get(r['kind'], '•')} {r['kind']}{who}{b}{s}{d}")
    await _edit(cb, "\n".join(lines), _kb(
        [_btn("❌ أخطاء النشر", "ow_logs_err"), _btn("🔄 تحديث", "ow_logs")], BACK_OWNER))
    await cb.answer()


@router.callback_query(F.data == "ow_logs_err")
async def cb_ow_logs_err(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    tz = await db.get_tz_offset()
    rows = await db.get_recent_failed_posts(20)
    lines = ["❌ آخر أخطاء النشر:", ""]
    if not rows:
        lines.append("✨ لا توجد أخطاء مسجّلة.")
    for r in rows:
        d = f" — {r['detail']}" if r.get("detail") else ""
        lines.append(f"{reports.fmt_local(r['ts'], tz)} b#{r['bundle_id']} u{r['user_id']} {r['status']}{d}")
    await _edit(cb, "\n".join(lines), _kb(
        [_btn("📜 الأحداث", "ow_logs"), _btn("🔄 تحديث", "ow_logs_err")], BACK_OWNER))
    await cb.answer()


# ------------------------------------------------------------------ التحكم والإعدادات
_TOGGLES = [
    ("global_pause", "⏸ إيقاف النشر شامل", False),
    ("maintenance", "🛠 وضع الصيانة", False),
    ("admin_cliches_on", "💬 كليشات الأدمن بين النشرات", True),
    ("daily_report", "🗓 التقرير اليومي", True),
    ("notify_owner", "🔔 إشعارات المالك (سبات/جلسات)", True),
]


async def _control_view():
    rows = []
    for key, label, default in _TOGGLES:
        on = await db.get_setting_bool(key, default)
        rows.append([_btn(f"{'✅' if on else '❌'} {label}", f"ow_t_{key}")])
    tz = await db.get_tz_offset()
    fc = await db.get_forced_channel()
    d = await db.get_default_limits()
    rows += [
        [_btn(f"🕰 فرق التوقيت: UTC{tz:+d}", "ow_tz"), _btn("👋 رسالة الترحيب", "ow_welcome")],
        [_btn("📏 الحدود الافتراضية", "ow_deflim")],
    ]
    if fc:
        rows.append([_btn("📢 حذف القناة الإجبارية", "ow_fc_clear")])
    rows.append(BACK_OWNER)
    text = (
        "🎛 التحكم والإعدادات\n\n"
        f"📢 القناة الإجبارية: {fc['channel_link'] if fc else 'غير مفعّلة'}\n"
        f"📏 الحدود الافتراضية: مجموعات {d['max_groups']} · أرقام {d['max_numbers']} · كليشات {d['max_cliches']}\n\n"
        "• الإيقاف الشامل يوقف كل النشر مؤقتاً دون تغيير حالة المجموعات.\n"
        "• وضع الصيانة يمنع الأعضاء من استخدام البوت."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "ow_control")
async def cb_ow_control(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    text, kb = await _control_view()
    await _edit(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("ow_t_"))
async def cb_ow_toggle(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    key = cb.data[len("ow_t_"):]
    defaults = {k: d for k, _, d in _TOGGLES}
    if key not in defaults:
        await cb.answer(); return
    new = await db.toggle_setting(key, defaults[key])
    text, kb = await _control_view()
    await _edit(cb, text, kb)
    await cb.answer("✅ مفعّل" if new else "❌ معطّل")


@router.callback_query(F.data == "ow_fc_clear")
async def cb_ow_fc_clear(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    await db.clear_forced_channel()
    text, kb = await _control_view()
    await _edit(cb, text, kb)
    await cb.answer("🗑 حُذفت القناة الإجبارية")


@router.callback_query(F.data == "ow_tz")
async def cb_ow_tz(cb: CallbackQuery, state: FSMContext):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    await state.set_state(OwnerPanel.tz_wait)
    await cb.message.answer("🕰 أرسل فرق التوقيت عن UTC كرقم صحيح.\nمثال: 3 (العراق/السعودية) · 2 (مصر) · 4 (الإمارات)")
    await cb.answer()


@router.message(OwnerPanel.tz_wait)
async def proc_tz(msg: Message, state: FSMContext):
    if not _is_owner(msg.from_user.id):
        return
    t = (msg.text or "").strip().replace("+", "")
    if not t.lstrip("-").isdigit() or not -12 <= int(t) <= 14:
        await msg.answer("❌ أرسل رقماً بين -12 و 14.")
        return
    await db.set_setting("tz_offset", int(t))
    await state.clear()
    await msg.answer(f"✅ فرق التوقيت: UTC{int(t):+d}")


@router.callback_query(F.data == "ow_welcome")
async def cb_ow_welcome(cb: CallbackQuery, state: FSMContext):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    cur = await db.get_setting("welcome_text", "👋 مرحبًا بك!")
    await state.set_state(OwnerPanel.welcome_wait)
    await cb.message.answer(f"👋 رسالة الترحيب الحالية:\n\n{cur}\n\nأرسل النص الجديد، أو reset للرجوع للافتراضي.")
    await cb.answer()


@router.message(OwnerPanel.welcome_wait)
async def proc_welcome(msg: Message, state: FSMContext):
    if not _is_owner(msg.from_user.id):
        return
    t = (msg.text or "").strip()
    if not t or len(t) > 1000:
        await msg.answer("❌ نص غير صالح (1-1000 حرف).")
        return
    await db.set_setting("welcome_text", "👋 مرحبًا بك!" if t.lower() == "reset" else t)
    await state.clear()
    await msg.answer("✅ حُفظت رسالة الترحيب.")


@router.callback_query(F.data == "ow_deflim")
async def cb_ow_deflim(cb: CallbackQuery, state: FSMContext):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    d = await db.get_default_limits()
    await state.set_state(OwnerPanel.deflim_wait)
    await cb.message.answer(
        f"📏 الحدود الافتراضية الحالية: مجموعات {d['max_groups']} · أرقام {d['max_numbers']} · كليشات {d['max_cliches']}\n\n"
        "أرسل ثلاثة أرقام: مجموعات أرقام كليشات\nمثال: 5 5 3\n(تُطبّق على كل من لم تُخصَّص له حدود).")
    await cb.answer()


@router.message(OwnerPanel.deflim_wait)
async def proc_deflim(msg: Message, state: FSMContext):
    if not _is_owner(msg.from_user.id):
        return
    parts = (msg.text or "").split()
    if len(parts) != 3 or not all(p.isdigit() and int(p) >= 1 for p in parts):
        await msg.answer("❌ أرسل ثلاثة أرقام (≥1): مجموعات أرقام كليشات")
        return
    mg, mn, mc = map(int, parts)
    await db.set_setting("default_max_bundles", mg)
    await db.set_setting("default_max_numbers", mn)
    await db.set_setting("default_max_cliches", mc)
    await state.clear()
    await msg.answer(f"✅ الحدود الافتراضية: مجموعات {mg} · أرقام {mn} · كليشات {mc}")


# ------------------------------------------------------------------ نسخة احتياطية
def _backup_db_bytes():
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


@router.callback_query(F.data == "ow_export")
async def cb_ow_export(cb: CallbackQuery):
    if not _is_owner(cb.from_user.id):
        await cb.answer(); return
    await cb.answer("⏳ جارٍ التجهيز...")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    try:
        data = await asyncio.to_thread(_backup_db_bytes)
        await cb.message.answer_document(
            BufferedInputFile(data, filename=f"bot_backup_{stamp}.db"),
            caption="💾 نسخة احتياطية من قاعدة البيانات.\n⚠️ ملفات الجلسات (sessions/) لا تُرسل هنا؛ احفظها عبر Volume.")
    except Exception as e:
        await cb.message.answer(f"❌ فشل النسخ الاحتياطي: {e}")
    try:
        users = await db._fetchall("SELECT * FROM users ORDER BY created_at")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["user_id", "username", "full_name", "created_at(UTC)", "last_seen(UTC)", "notify", "blocked"])
        for u in users:
            w.writerow([u["user_id"], u.get("username") or "", u.get("full_name") or "",
                        u.get("created_at") or "", u.get("last_seen") or "", u.get("notify"),
                        int(await db.is_blocked(u["user_id"]))])
        await cb.message.answer_document(
            BufferedInputFile(("\ufeff" + buf.getvalue()).encode("utf-8"), filename=f"users_{stamp}.csv"),
            caption=f"👥 قائمة الأعضاء ({len(users)})")
    except Exception as e:
        await cb.message.answer(f"❌ فشل تصدير الأعضاء: {e}")
