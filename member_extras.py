"""ميزات الأعضاء الإضافية: إحصائيات، سجل، إرسال تجريبي، ساعات العمل، حد يومي، كليشات إضافية..."""
import time

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
import reports
from config import OWNER_ID
from userbot import ub_manager
from scheduler import scheduler_mgr
from bot_handlers import check_access, render_bundle, build_main_menu

router = Router()


class MemberFlow(StatesGroup):
    rename = State()
    hours = State()
    daily = State()
    add_cliche = State()


# ---------------------------------------------------------------- helpers
async def _own_bundle(cb: CallbackQuery, bid: int):
    """يتحقق من الحظر ومن أن المجموعة للمستخدم. يعيد المجموعة أو None."""
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True)
        return None
    b = await db.get_bundle(bid)
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("ليست لك.", show_alert=True)
        return None
    return b


async def _edit(cb: CallbackQuery, text, kb=None):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        if "not modified" in str(e).lower():
            return                      # لا تغيير في المحتوى
        try:
            await cb.message.answer(text, reply_markup=kb)
        except Exception:
            pass


def _kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


# ---------------------------------------------------------------- إحصائياتي
@router.callback_query(F.data == "m_stats")
async def cb_m_stats(cb: CallbackQuery):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    text = await reports.build_user_stats_text(cb.from_user.id)
    await _edit(cb, text, _kb([_btn("🔄 تحديث", "m_stats")], [_btn("🔙 رجوع", "back_main")]))
    await cb.answer()


# ---------------------------------------------------------------- إعداداتي
async def _settings_view(uid):
    u = await db.get_user(uid) or {}
    lim = await db.get_limits(uid)
    notify_on = u.get("notify", 1) in (None, 1)
    text = (
        "⚙️ إعداداتي\n\n"
        f"🆔 معرّفك: {uid}\n"
        f"🔔 الإشعارات: {'✅ مفعّلة' if notify_on else '❌ معطّلة'}\n\n"
        "حدودك:\n"
        f"📦 مجموعات: {lim['max_groups']}\n"
        f"📱 أرقام: {lim['max_numbers']}\n"
        f"💬 كليشات لكل مجموعة: {lim['max_cliches']}"
        + ("\n(حدود مخصّصة من المالك)" if lim.get("custom") else "")
        + "\n\nالإشعارات تصلك عند: سبات المجموعة، فلود طويل، أو انتهاء جلسة الرقم."
    )
    kb = _kb(
        [_btn("🔕 تعطيل الإشعارات" if notify_on else "🔔 تفعيل الإشعارات", "m_notify")],
        [_btn("🔙 رجوع", "back_main")],
    )
    return text, kb


@router.callback_query(F.data == "m_settings")
async def cb_m_settings(cb: CallbackQuery):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    text, kb = await _settings_view(cb.from_user.id)
    await _edit(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data == "m_notify")
async def cb_m_notify(cb: CallbackQuery):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    u = await db.get_user(cb.from_user.id) or {}
    now_on = u.get("notify", 1) in (None, 1)
    await db.set_user_notify(cb.from_user.id, not now_on)
    text, kb = await _settings_view(cb.from_user.id)
    await _edit(cb, text, kb)
    await cb.answer("تم")


# ---------------------------------------------------------------- إحصائيات مجموعة
@router.callback_query(F.data.startswith("x_stats_"))
async def cb_x_stats(cb: CallbackQuery):
    bid = int(cb.data.split("_")[2])
    b = await _own_bundle(cb, bid)
    if not b:
        return
    text = await reports.build_bundle_stats_text(b)
    await _edit(cb, text, _kb(
        [_btn("🔄 تحديث", f"x_stats_{bid}"), _btn("📋 السجل", f"x_log_{bid}")],
        [_btn("🔙 رجوع", f"openb_{bid}")],
    ))
    await cb.answer()


# ---------------------------------------------------------------- سجل المجموعة
_STATUS_ICON = {
    "ok": "✅", "flood": "⏳", "slowmode": "🐢", "no_target": "⏭",
    "restricted": "🚫", "dead": "💀", "conn": "📡", "error": "❌",
}


@router.callback_query(F.data.startswith("x_log_"))
async def cb_x_log(cb: CallbackQuery):
    bid = int(cb.data.split("_")[2])
    b = await _own_bundle(cb, bid)
    if not b:
        return
    tz = await db.get_tz_offset()
    rows = await db.get_recent_posts(bid, 12)
    if not rows:
        text = "📋 لا يوجد سجل نشر بعد.\nابدأ المجموعة أو جرّب 🧪 إرسال تجريبي."
    else:
        lines = ["📋 آخر محاولات النشر (بتوقيتك):", ""]
        for r in rows:
            d = f" — {r['detail']}" if r.get("detail") else ""
            lines.append(f"{reports.fmt_local(r['ts'], tz, '%d/%m %H:%M:%S')} "
                         f"{_STATUS_ICON.get(r['status'], '•')} {r['status']}{d}")
        text = "\n".join(lines)
    await _edit(cb, text, _kb(
        [_btn("🔄 تحديث", f"x_log_{bid}"), _btn("📊 إحصائيات", f"x_stats_{bid}")],
        [_btn("🔙 رجوع", f"openb_{bid}")],
    ))
    await cb.answer()


# ---------------------------------------------------------------- إرسال تجريبي
_last_test = {}

_RESULT_TEXT = {
    "flood": "⏳ تلغرام فرض انتظاراً {extra} ثانية.",
    "slowmode": "🐢 الكروب بوضع بطيء، الانتظار {extra} ثانية.",
    "no_target": "⚠️ لا توجد رسالة من عضو حقيقي للرد عليها. غيّر الوضع إلى 🔁 تلقائي.",
    "restricted": "🚫 الحساب لا يستطيع الكتابة في الكروب ({extra}). تأكد أنه عضو وغير مقيّد.",
    "dead": "💀 جلسة الرقم لم تعد صالحة ({extra}). أعد تسجيل الدخول.",
    "conn": "📡 مشكلة اتصال ({extra}). حاول بعد قليل.",
    "error": "❌ خطأ: {extra}",
}


@router.callback_query(F.data.startswith("x_test_"))
async def cb_x_test(cb: CallbackQuery):
    bid = int(cb.data.split("_")[2])
    b = await _own_bundle(cb, bid)
    if not b:
        return
    if not b.get("session_id") or not b.get("group_id") or not b.get("cliche_id"):
        await cb.answer("⚠️ أكمل الرقم والكروب والكليشة أولاً.", show_alert=True); return
    if time.monotonic() - _last_test.get(cb.from_user.id, 0) < 20:
        await cb.answer("⏱ انتظر 20 ثانية بين كل تجربة.", show_alert=True); return
    if not ub_manager.get_client(b["session_id"]):
        await cb.answer("❌ الحساب غير محمّل في البوت. أعد إضافة الرقم.", show_alert=True); return
    grp = await db.get_group(b["group_id"])
    cli = await db.get_cliche(b["cliche_id"])
    if not grp or not cli:
        await cb.answer("⚠️ بيانات ناقصة.", show_alert=True); return
    _last_test[cb.from_user.id] = time.monotonic()
    await cb.answer("⏳ جارٍ الإرسال...")
    status, extra = await ub_manager.send_with_reply(
        b["session_id"], grp["group_id"], cli["text"], b.get("mode") or "auto")
    await scheduler_mgr._handle_result(b, status, extra)
    if status == "ok":
        how = "كرد على رسالة" if extra == "reply" else "كرسالة عادية"
        msg = f"✅ أُرسلت الكليشة {how} في «{grp['group_title'] or grp['group_id']}»."
    else:
        msg = _RESULT_TEXT.get(status, "❌ {extra}").format(extra=extra)
    await cb.message.answer(msg)


# ---------------------------------------------------------------- تسمية
@router.callback_query(F.data.startswith("x_name_"))
async def cb_x_name(cb: CallbackQuery, state: FSMContext):
    bid = int(cb.data.split("_")[2])
    if not await _own_bundle(cb, bid):
        return
    await state.update_data(x_bid=bid)
    await state.set_state(MemberFlow.rename)
    await cb.message.answer("✏️ أرسل الاسم الجديد للمجموعة (حتى 60 حرفاً):")
    await cb.answer()


@router.message(MemberFlow.rename)
async def proc_rename(msg: Message, state: FSMContext):
    name = (msg.text or "").strip()
    if not name or len(name) > 60:
        await msg.answer("❌ اسم غير صالح (1-60 حرفاً).")
        return
    d = await state.get_data()
    b = await db.get_bundle(d.get("x_bid"))
    if not b or b["user_id"] != msg.from_user.id:
        await state.clear(); return
    await db.update_bundle_fields(b["id"], name=name)
    await state.clear()
    await msg.answer("✅ تم تغيير الاسم.")
    await render_bundle(msg, b["id"], edit=False)


# ---------------------------------------------------------------- وضع الإرسال / الانضمام
_MODES = ["reply", "auto", "plain"]


@router.callback_query(F.data.startswith("x_mode_"))
async def cb_x_mode(cb: CallbackQuery):
    bid = int(cb.data.split("_")[2])
    b = await _own_bundle(cb, bid)
    if not b:
        return
    cur = b.get("mode") or "auto"
    nxt = _MODES[(_MODES.index(cur) + 1) % len(_MODES)] if cur in _MODES else "auto"
    await db.update_bundle_fields(bid, mode=nxt)
    await render_bundle(cb, bid, edit=True)
    await cb.answer(reports.mode_label(nxt), show_alert=True)


@router.callback_query(F.data.startswith("x_join_"))
async def cb_x_join(cb: CallbackQuery):
    bid = int(cb.data.split("_")[2])
    b = await _own_bundle(cb, bid)
    if not b:
        return
    new = 0 if b.get("auto_join") in (None, 1) else 1
    await db.update_bundle_fields(bid, auto_join=new)
    await render_bundle(cb, bid, edit=True)
    await cb.answer("🤖 الانضمام التلقائي: " + ("✅ مفعّل" if new else "❌ معطّل"))


# ---------------------------------------------------------------- ساعات العمل
@router.callback_query(F.data.startswith("x_hours_"))
async def cb_x_hours(cb: CallbackQuery, state: FSMContext):
    bid = int(cb.data.split("_")[2])
    b = await _own_bundle(cb, bid)
    if not b:
        return
    tz = await db.get_tz_offset()
    await state.update_data(x_bid=bid)
    await state.set_state(MemberFlow.hours)
    await cb.message.answer(
        f"🕒 ساعات العمل الحالية: {reports.hours_label(b)}\n"
        f"(التوقيت المعتمد: UTC{tz:+d})\n\n"
        "أرسل ساعة البداية وساعة النهاية (0-23) مفصولتين بمسافة.\n"
        "مثال: 8 23  ← تنشر من 8 صباحاً حتى 11 مساءً\n"
        "مثال: 22 6  ← تنشر ليلاً فقط\n\n"
        "لتعطيل الميزة (نشر طوال اليوم) أرسل: off"
    )
    await cb.answer()


@router.message(MemberFlow.hours)
async def proc_hours(msg: Message, state: FSMContext):
    d = await state.get_data()
    b = await db.get_bundle(d.get("x_bid"))
    if not b or b["user_id"] != msg.from_user.id:
        await state.clear(); return
    t = (msg.text or "").strip().lower()
    if t in ("off", "تعطيل", "الغاء", "إلغاء", "-1"):
        await db.update_bundle_fields(b["id"], hours_from=-1, hours_to=-1)
    else:
        parts = t.replace("-", " ").replace(":", " ").split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            await msg.answer("❌ أرسل رقمين مثل: 8 23  أو off للتعطيل.")
            return
        hf, ht = int(parts[0]), int(parts[1])
        if not (0 <= hf <= 23 and 0 <= ht <= 23) or hf == ht:
            await msg.answer("❌ الساعات بين 0 و 23 ويجب أن تختلف البداية عن النهاية.")
            return
        await db.update_bundle_fields(b["id"], hours_from=hf, hours_to=ht)
    await state.clear()
    await msg.answer("✅ تم حفظ ساعات العمل.")
    await render_bundle(msg, b["id"], edit=False)


# ---------------------------------------------------------------- حد يومي
@router.callback_query(F.data.startswith("x_daily_"))
async def cb_x_daily(cb: CallbackQuery, state: FSMContext):
    bid = int(cb.data.split("_")[2])
    b = await _own_bundle(cb, bid)
    if not b:
        return
    await state.update_data(x_bid=bid)
    await state.set_state(MemberFlow.daily)
    cur = b.get("daily_limit") or 0
    await cb.message.answer(
        f"📈 الحد اليومي الحالي: {cur if cur else 'بلا حد'}\n\n"
        "أرسل أقصى عدد رسائل في اليوم لهذه المجموعة (يقلّل خطر الحظر).\n"
        "أرسل 0 لإلغاء الحد."
    )
    await cb.answer()


@router.message(MemberFlow.daily)
async def proc_daily(msg: Message, state: FSMContext):
    d = await state.get_data()
    b = await db.get_bundle(d.get("x_bid"))
    if not b or b["user_id"] != msg.from_user.id:
        await state.clear(); return
    t = (msg.text or "").strip()
    if not t.isdigit() or int(t) > 100000:
        await msg.answer("❌ أرسل رقماً صحيحاً (0 = بلا حد).")
        return
    await db.update_bundle_fields(b["id"], daily_limit=int(t))
    await state.clear()
    await msg.answer("✅ تم حفظ الحد اليومي.")
    await render_bundle(msg, b["id"], edit=False)


# ---------------------------------------------------------------- كليشات إضافية
async def _clis_view(bid):
    extras = await db.get_bundle_cliches(bid)
    lines = ["💬 الكليشات الإضافية", "",
             "البوت يختار عشوائياً بين الكليشة الأساسية وهذه الكليشات في كل نشر.", ""]
    rows = []
    if not extras:
        lines.append("(لا توجد كليشات إضافية)")
    for i, c in enumerate(extras, 1):
        preview = c["text"].replace("\n", " ")[:45]
        lines.append(f"{i}. {preview}")
        rows.append([_btn(f"🗑 حذف {i}", f"x_clidel_{c['id']}_{bid}")])
    rows.append([_btn("➕ إضافة كليشة", f"x_cliadd_{bid}")])
    rows.append([_btn("🔙 رجوع", f"openb_{bid}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("x_clis_"))
async def cb_x_clis(cb: CallbackQuery):
    bid = int(cb.data.split("_")[2])
    if not await _own_bundle(cb, bid):
        return
    text, kb = await _clis_view(bid)
    await _edit(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("x_clidel_"))
async def cb_x_clidel(cb: CallbackQuery):
    parts = cb.data.split("_")
    cid, bid = int(parts[2]), int(parts[3])
    if not await _own_bundle(cb, bid):
        return
    c = await db.get_bundle_cliche(cid)
    if c and c["bundle_id"] == bid:
        await db.delete_bundle_cliche(cid)
    text, kb = await _clis_view(bid)
    await _edit(cb, text, kb)
    await cb.answer("🗑 حُذفت")


@router.callback_query(F.data.startswith("x_cliadd_"))
async def cb_x_cliadd(cb: CallbackQuery, state: FSMContext):
    bid = int(cb.data.split("_")[2])
    if not await _own_bundle(cb, bid):
        return
    lim = await db.get_limits(cb.from_user.id)
    total = 1 + await db.count_bundle_cliches(bid)
    if cb.from_user.id != OWNER_ID and total >= lim["max_cliches"]:
        await cb.answer(f"⚠️ وصلت للحد الأقصى من الكليشات ({lim['max_cliches']}).", show_alert=True)
        return
    await state.update_data(x_bid=bid)
    await state.set_state(MemberFlow.add_cliche)
    await cb.message.answer("💬 أرسل نص الكليشة الإضافية:")
    await cb.answer()


@router.message(MemberFlow.add_cliche)
async def proc_add_cliche(msg: Message, state: FSMContext):
    d = await state.get_data()
    b = await db.get_bundle(d.get("x_bid"))
    if not b or b["user_id"] != msg.from_user.id:
        await state.clear(); return
    text = (msg.text or "").strip()
    if not text or len(text) > 4000:
        await msg.answer("❌ نص غير صالح (1-4000 حرف).")
        return
    lim = await db.get_limits(msg.from_user.id)
    total = 1 + await db.count_bundle_cliches(b["id"])
    if msg.from_user.id != OWNER_ID and total >= lim["max_cliches"]:
        await msg.answer(f"⚠️ وصلت للحد الأقصى من الكليشات ({lim['max_cliches']}).")
        await state.clear(); return
    await db.add_bundle_cliche(b["id"], text)
    await state.clear()
    view, kb = await _clis_view(b["id"])
    await msg.answer("✅ أُضيفت الكليشة.\n\n" + view, reply_markup=kb)
