from aiogram import Router, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from config import OWNER_ID
from userbot import ub_manager
from scheduler import scheduler_mgr

router = Router()


# ---------------- FSM States ----------------
class NewBundle(StatesGroup):
    waiting_name = State()
    waiting_number_phone = State()
    waiting_number_code = State()
    waiting_number_password = State()
    waiting_group_link = State()
    waiting_cliche_text = State()
    waiting_interval = State()


class EditBundle(StatesGroup):
    add_number_phone = State()
    add_number_code = State()
    add_number_password = State()
    add_group_link = State()
    add_cliche_text = State()
    waiting_new_interval = State()


class OwnerFlow(StatesGroup):
    block_wait = State()
    unblock_wait = State()
    add_admin_cliche = State()
    add_forced_channel = State()
    set_limits_wait = State()
    set_timer_bounds = State()
    add_tutorial = State()
    test_join = State()


# ---------------- Helpers ----------------
async def check_access(user_id):
    if await db.is_blocked(user_id):
        return False
    await db.ensure_user(user_id)
    return True


def _normalize_status(status):
    if status is None:
        return None
    if hasattr(status, "value"):
        return status.value
    return str(status)


async def _bot_is_admin(bot, chat_id):
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
    except Exception as e:
        return False, f"get_chat_member failed: {e}"
    status = _normalize_status(getattr(me, "status", None))
    if status in ("administrator", "creator"):
        return True, f"status={status}"
    return False, f"status={status}"


async def check_forced_channel(bot, user_id):
    fc = await db.get_forced_channel()
    if not fc:
        return True
    raw_id = str(fc["channel_id"]).strip()
    try:
        cid = int(raw_id)
    except (ValueError, TypeError):
        cid = raw_id
    try:
        member = await bot.get_chat_member(cid, user_id)
        status = _normalize_status(getattr(member, "status", None))
        if status in ("member", "administrator", "creator"):
            return True
        return False
    except Exception:
        pass
    try:
        chat = await bot.get_chat(cid)
        member = await bot.get_chat_member(chat.id, user_id)
        status = _normalize_status(getattr(member, "status", None))
        return status in ("member", "administrator", "creator")
    except Exception:
        pass
    print(f"[ForcedCheck] WARNING: could not verify {user_id}. Allowing.")
    return True


async def send_forced_subscribe(bot, message_or_cb):
    fc = await db.get_forced_channel()
    if not fc:
        return
    link = fc["channel_link"]
    if not link.startswith("http"):
        link = "https://t.me/" + link.lstrip("@")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 اشترك في القناة", url=link)],
        [InlineKeyboardButton(text="✅ تحققت", callback_data="check_sub")],
    ])
    text = "⚠️ للاستفادة من البوت، يجب الاشتراك في قناتنا أولاً:"
    if isinstance(message_or_cb, CallbackQuery):
        await message_or_cb.message.answer(text, reply_markup=kb)
    else:
        await message_or_cb.answer(text, reply_markup=kb)


# ---------------- Menus ----------------
async def build_main_menu(user_id):
    bundles = await db.get_user_bundles(user_id)
    kb = []
    for b in bundles:
        name = b.get("name") or f"المجموعة #{b['id']}"
        status_icon = "🟢" if b["is_active"] else "🔴"
        kb.append([InlineKeyboardButton(
            text=f"{status_icon} {name}",
            callback_data=f"openb_{b['id']}"
        )])
    kb.append([InlineKeyboardButton(text="➕ إضافة مجموعة", callback_data="add_bundle")])
    kb.append([InlineKeyboardButton(text="🎬 عرض شرح البوت", callback_data="show_tutorial")])
    if user_id == OWNER_ID:
        kb.append([InlineKeyboardButton(text="⚙️ لوحة المالك", callback_data="owner_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def bundle_detail_menu(bid, has_session, has_group, has_cliche, is_active):
    num_label = "📱 تغيير الرقم" if has_session else "📱 إضافة رقم"
    grp_label = "👥 تغيير الكروب" if has_group else "👥 إضافة كروب"
    cli_label = "💬 تغيير الكليشة" if has_cliche else "💬 إضافة كليشة"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=num_label, callback_data=f"e_num_{bid}")],
        [InlineKeyboardButton(text="▶️ بدء المجموعة", callback_data=f"e_start_{bid}")],
        [InlineKeyboardButton(text="⏸️ إيقاف المجموعة", callback_data=f"e_stop_{bid}")],
        [InlineKeyboardButton(text=cli_label, callback_data=f"e_cli_{bid}")],
        [InlineKeyboardButton(text=grp_label, callback_data=f"e_grp_{bid}")],
        [InlineKeyboardButton(text="🗑️ حذف الرقم", callback_data=f"e_delnum_{bid}")],
        [InlineKeyboardButton(text="⏱️ المؤقت", callback_data=f"e_timer_{bid}")],
        [InlineKeyboardButton(text="🗑 حذف المجموعة", callback_data=f"e_delb_{bid}")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="back_main")],
    ])


def owner_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 حظر عضو/بوت", callback_data="own_block")],
        [InlineKeyboardButton(text="✅ إلغاء حظر", callback_data="own_unblock")],
        [InlineKeyboardButton(text="💬 كليشات الأدمن", callback_data="own_cliches")],
        [InlineKeyboardButton(text="📢 تعيين القناة الإجبارية", callback_data="own_forced")],
        [InlineKeyboardButton(text="📊 تحديد حدود عضو", callback_data="own_limits")],
        [InlineKeyboardButton(text="⏱️ حدود المؤقت", callback_data="own_timer_bounds")],
        [InlineKeyboardButton(text="🎬 فيديو الشرح", callback_data="own_tutorial")],
        [InlineKeyboardButton(text="🧪 اختبار انضمام", callback_data="own_test_join")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="back_main")],
    ])


def admin_cliches_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ إضافة كليشة", callback_data="own_cliche_add")],
        [InlineKeyboardButton(text="🗑 حذف كليشة", callback_data="own_cliche_del_list")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="owner_menu")],
    ])


async def _bundle_detail_text(b):
    sess = await db.get_session(b["session_id"]) if b.get("session_id") else None
    grp = await db.get_group(b["group_id"]) if b.get("group_id") else None
    cli = await db.get_cliche(b["cliche_id"]) if b.get("cliche_id") else None
    sess_txt = sess["phone"] if sess else "❌ لا يوجد"
    grp_txt = (grp["group_title"] or str(grp["group_id"])) if grp else "❌ لا يوجد"
    cli_txt = (cli["text"][:40]) if cli else "❌ لا يوجد"
    state_txt = "🟢 تعمل" if b["is_active"] else "🔴 متوقفة"
    name = b.get("name") or f"المجموعة #{b['id']}"
    return (
        f"📦 {name}\n\n"
        f"📱 الرقم: {sess_txt}\n"
        f"👥 الكروب: {grp_txt}\n"
        f"💬 الكليشة: {cli_txt}\n"
        f"⏱️ المؤقت: {b['interval']} ثانية\n"
        f"الحالة: {state_txt}\n"
    )


async def render_bundle(cb_or_msg, bid, edit=True):
    b = await db.get_bundle(bid)
    if not b:
        return
    text = await _bundle_detail_text(b)
    kb = bundle_detail_menu(
        bid,
        bool(b.get("session_id")),
        bool(b.get("group_id")),
        bool(b.get("cliche_id")),
        bool(b["is_active"]),
    )
    if edit and hasattr(cb_or_msg, "message"):
        try:
            await cb_or_msg.message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    if hasattr(cb_or_msg, "message"):
        await cb_or_msg.message.answer(text, reply_markup=kb)
    else:
        await cb_or_msg.answer(text, reply_markup=kb)


# ---------------- /start ----------------
async def release_session(sid):
    """إذا لم تعد الجلسة مرتبطة بأي مجموعة: نعطّلها ونفصل الحساب ليمكن إضافة الرقم من جديد."""
    if not sid:
        return
    if await db.is_session_in_use(sid):
        return
    await db.deactivate_session(sid)
    await ub_manager.drop_client(sid)


async def release_stale_phone(phone):
    """بقايا جلسات قديمة لنفس الرقم (من حذف سابق) تُنظَّف قبل التحقق."""
    for o in await db.get_orphan_sessions_by_phone(phone):
        await db.deactivate_session(o["id"])
        await ub_manager.drop_client(o["id"])


# ---------------- Reset / Start shortcuts ----------------
START_ALIASES = {
    "start", "ستارت", "ابدأ", "ابدا", "بدء", "القائمة", "القائمه",
    "menu", "الغاء", "إلغاء", "cancel", "🏠 ستارت",
}


def _is_reset_text(text):
    if not text:
        return False
    t = text.strip()
    if t.startswith("/debug_bundles"):
        return False
    if t.lstrip("/").strip().lower() in START_ALIASES:
        return True
    # أي أمر من كلمة واحدة يبدأ بـ / يلغي العملية الحالية
    return t.startswith("/") and len(t) <= 30 and not any(c.isspace() for c in t)


def _reply_shortcut():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🏠 ستارت")]],
        resize_keyboard=True, is_persistent=True,
    )


class ResetStateOnCallback(BaseMiddleware):
    """أي زر يُضغط يلغي العملية القديمة ويبدأ من الزر الجديد."""
    async def __call__(self, handler, event, data):
        state = data.get("state")
        if state is not None:
            await state.clear()
        try:
            await ub_manager.cancel_login(event.from_user.id)
        except Exception:
            pass
        return await handler(event, data)


router.callback_query.outer_middleware(ResetStateOnCallback())


@router.message(F.text.func(_is_reset_text))
@router.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await ub_manager.cancel_login(msg.from_user.id)
    if not await check_access(msg.from_user.id):
        await msg.answer("🚫 أنت محظور من استخدام البوت.")
        return
    if not await check_forced_channel(msg.bot, msg.from_user.id):
        await send_forced_subscribe(msg.bot, msg)
        return
    await msg.answer("👋 مرحبًا بك!", reply_markup=_reply_shortcut())
    kb = await build_main_menu(msg.from_user.id)
    await msg.answer("اختر من القائمة:", reply_markup=kb)


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    if not await check_forced_channel(cb.bot, cb.from_user.id):
        await cb.answer("❌ لم تشترك بعد!", show_alert=True)
        return
    kb = await build_main_menu(cb.from_user.id)
    await cb.message.edit_text("✅ تم التحقق!\nاختر من القائمة:", reply_markup=kb)
    await cb.answer("✅")


@router.callback_query(F.data == "back_main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = await build_main_menu(cb.from_user.id)
    await cb.message.edit_text("اختر من القائمة:", reply_markup=kb)
    await cb.answer()


# ---------------- Tutorial (members) ----------------
@router.callback_query(F.data == "show_tutorial")
async def cb_show_tutorial(cb: CallbackQuery):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    video = await db.get_tutorial_video()
    if not video:
        await cb.answer("⚠️ لم يتم إضافة فيديو شرح بعد.", show_alert=True)
        return
    try:
        await cb.message.answer_video(
            video["file_id"],
            caption=video["caption"] or None
        )
        await cb.answer("🎬")
    except Exception as e:
        await cb.answer(f"❌ خطأ: {e}", show_alert=True)


# ---------------- Open bundle ----------------
@router.callback_query(F.data.startswith("openb_"))
async def cb_open_bundle(cb: CallbackQuery):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    bid = int(cb.data.split("_")[1])
    b = await db.get_bundle(bid)
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("ليست لك.", show_alert=True); return
    await render_bundle(cb, bid, edit=True)
    await cb.answer()


# ---------------- Create new bundle ----------------
@router.callback_query(F.data == "add_bundle")
async def cb_add_bundle(cb: CallbackQuery, state: FSMContext):
    if not await check_access(cb.from_user.id):
        await cb.answer(); return
    await cb.message.answer("📝 أرسل اسم المجموعة الجديدة:")
    await state.set_state(NewBundle.waiting_name)
    await cb.answer("📝")


@router.message(NewBundle.waiting_name)
async def new_bundle_name(msg: Message, state: FSMContext):
    name = msg.text.strip()
    if not name or len(name) > 60:
        await msg.answer("❌ اسم غير صالح (1-60 حرفاً).")
        return
    bid = await db.add_bundle(msg.from_user.id, name)
    await state.update_data(new_bid=bid)
    await msg.answer(
        f"✅ تم إنشاء '{name}' (#{bid}).\n\n"
        f"📱 الآن أرسل رقم الهاتف (بصيغة دولية):\n"
        f"⚠️ يجب أن يكون رقماً مختلفاً عن المجموعات الأخرى."
    )
    await state.set_state(NewBundle.waiting_number_phone)


@router.message(NewBundle.waiting_number_phone)
async def new_bundle_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    await release_stale_phone(phone)
    if await db.session_phone_exists(phone):
        await msg.answer("⚠️ هذا الرقم مسجل بالفعل.\nأرسل رقماً آخر.")
        return
    status, _ = await ub_manager.start_login(msg.from_user.id, phone)
    if status == "code_sent":
        await msg.answer("📩 أرسل كود التحقق:")
        await state.set_state(NewBundle.waiting_number_code)
    elif status == "already_authorized":
        await msg.answer("⚠️ الرقم مسجل بالفعل.")
    else:
        await msg.answer(f"❌ خطأ: {status}")


@router.message(NewBundle.waiting_number_code)
async def new_bundle_code(msg: Message, state: FSMContext):
    status, sid = await ub_manager.verify_code(msg.from_user.id, msg.text.strip())
    if status == "success":
        d = await state.get_data()
        await db.update_bundle_session(d["new_bid"], sid)
        await msg.answer("✅ تم حفظ الرقم.\n\n🔗 أرسل رابط الكروب أو @username أو ID:")
        await state.set_state(NewBundle.waiting_group_link)
    elif status == "password_needed":
        await msg.answer("🔐 أرسل كلمة مرور 2FA:")
        await state.set_state(NewBundle.waiting_number_password)
    else:
        await msg.answer(
            f"❌ فشل: {status}\n"
            "🔁 أعد إرسال الكود (لا حاجة لإعادة إدخال الرقم)."
        )


@router.message(NewBundle.waiting_number_password)
async def new_bundle_password(msg: Message, state: FSMContext):
    status, sid = await ub_manager.verify_password(msg.from_user.id, msg.text.strip())
    if status == "success":
        d = await state.get_data()
        await db.update_bundle_session(d["new_bid"], sid)
        await msg.answer("✅ تم حفظ الرقم.\n\n🔗 أرسل رابط الكروب:")
        await state.set_state(NewBundle.waiting_group_link)
    else:
        await msg.answer(
            f"❌ فشل: {status}\n"
            "🔁 أعد إرسال كلمة المرور."
        )


@router.message(NewBundle.waiting_group_link)
async def new_bundle_group(msg: Message, state: FSMContext):
    d = await state.get_data()
    b = await db.get_bundle(d["new_bid"])
    if not b or not b.get("session_id"):
        await msg.answer("❌ المجموعة أو الرقم غير موجود.")
        await state.clear(); return
    result = await ub_manager.resolve_group(b["session_id"], msg.text.strip())
    if not result:
        await msg.answer("❌ لم أتمكن من الوصول للكروب. حاول مرة أخرى (رابط/يوزر/ID):")
        return
    gid, title = result
    gdb_id = await db.add_group(msg.from_user.id, gid, title)
    await db.update_bundle_group(d["new_bid"], gdb_id)
    await msg.answer(f"✅ تم حفظ الكروب: {title}\n\n💬 أرسل نص الكليشة:")
    await state.set_state(NewBundle.waiting_cliche_text)


@router.message(NewBundle.waiting_cliche_text)
async def new_bundle_cliche(msg: Message, state: FSMContext):
    d = await state.get_data()
    cid = await db.add_cliche(msg.from_user.id, msg.text)
    await db.update_bundle_cliche(d["new_bid"], cid)
    mn = await db.get_min_interval()
    mx = await db.get_max_interval()
    await msg.answer(f"✅ تم حفظ الكليشة.\n\n⏱️ أرسل المؤقت بالثواني (بين {mn} و {mx}):")
    await state.set_state(NewBundle.waiting_interval)


@router.message(NewBundle.waiting_interval)
async def new_bundle_interval(msg: Message, state: FSMContext):
    try:
        interval = int(msg.text.strip())
    except ValueError:
        await msg.answer("❌ أرسل رقمًا صحيحًا.")
        return
    mn = await db.get_min_interval()
    mx = await db.get_max_interval()
    if interval < mn or interval > mx:
        await msg.answer(f"❌ المؤقت بين {mn} و {mx}.")
        return
    d = await state.get_data()
    bid = d["new_bid"]
    await db.set_bundle_interval(bid, interval)
    b = await db.get_bundle(bid)
    name = b.get("name") or f"#{bid}"
    await msg.answer(
        f"✅ اكتملت المجموعة '{name}'!\n"
        f"⏱️ المؤقت: {interval} ثانية\n"
        f"🔴 الحالة: متوقفة (اضغط ▶️ بدء المجموعة للتشغيل)."
    )
    kb = await build_main_menu(msg.from_user.id)
    await msg.answer("اختر من القائمة:", reply_markup=kb)
    await state.clear()


# ---------------- In-bundle: start/stop ----------------
@router.callback_query(F.data.startswith("e_start_"))
async def cb_e_start(cb: CallbackQuery):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    bid = int(cb.data.split("_")[2])
    b = await db.get_bundle(bid)
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("ليست لك.", show_alert=True); return
    if not b.get("session_id") or not b.get("group_id") or not b.get("cliche_id"):
        await cb.answer("⚠️ أكمل البيانات أولاً.", show_alert=True); return
    if await db.is_session_used_elsewhere(b["session_id"], exclude_bundle_id=bid):
        await cb.answer("⚠️ هذا الرقم مستخدم في مجموعة أخرى نشطة.", show_alert=True); return
    await db.set_bundle_active(bid, True)
    scheduler_mgr.schedule_bundle(bid, b["interval"])
    await render_bundle(cb, bid, edit=True)
    await cb.answer("✅ تم بدء المجموعة")


@router.callback_query(F.data.startswith("e_stop_"))
async def cb_e_stop(cb: CallbackQuery):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    bid = int(cb.data.split("_")[2])
    b = await db.get_bundle(bid)
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("ليست لك.", show_alert=True); return
    await db.set_bundle_active(bid, False)
    scheduler_mgr.unschedule_bundle(bid)
    await render_bundle(cb, bid, edit=True)
    await cb.answer("⏸️ تم إيقاف المجموعة")


# ------- Change number -------
@router.callback_query(F.data.startswith("e_num_"))
async def cb_e_num(cb: CallbackQuery, state: FSMContext):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    bid = int(cb.data.split("_")[2])
    b = await db.get_bundle(bid)
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("ليست لك.", show_alert=True); return
    await state.update_data(edit_bid=bid)
    await cb.message.answer("📱 أرسل الرقم الجديد (بصيغة دولية):")
    await state.set_state(EditBundle.add_number_phone)
    await cb.answer("📱")


@router.message(EditBundle.add_number_phone)
async def edit_num_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    await release_stale_phone(phone)
    if await db.session_phone_exists(phone):
        await msg.answer("⚠️ هذا الرقم مسجل بالفعل.\nأرسل رقماً آخر.")
        return
    status, _ = await ub_manager.start_login(msg.from_user.id, phone)
    if status == "code_sent":
        await msg.answer("📩 أرسل كود التحقق:")
        await state.set_state(EditBundle.add_number_code)
    elif status == "already_authorized":
        await msg.answer("⚠️ الرقم مسجل.")
    else:
        await msg.answer(f"❌ خطأ: {status}")


@router.message(EditBundle.add_number_code)
async def edit_num_code(msg: Message, state: FSMContext):
    status, sid = await ub_manager.verify_code(msg.from_user.id, msg.text.strip())
    if status == "success":
        d = await state.get_data()
        bid = d.get("edit_bid")
        await db.set_bundle_active(bid, False)
        scheduler_mgr.unschedule_bundle(bid)
        _prev = await db.get_bundle(bid)
        old_sid = _prev.get("session_id") if _prev else None
        await db.update_bundle_session(bid, sid)
        if old_sid != sid:
            await release_session(old_sid)
        await msg.answer("✅ تم استبدال الرقم. المجموعة متوقفة، اضغط ▶️ للتشغيل.")
        kb = await build_main_menu(msg.from_user.id)
        await msg.answer("اختر من القائمة:", reply_markup=kb)
        await state.clear()
    elif status == "password_needed":
        await msg.answer("🔐 أرسل كلمة مرور 2FA:")
        await state.set_state(EditBundle.add_number_password)
    else:
        await msg.answer(f"❌ فشل: {status}\n🔁 أعد إرسال الكود.")


@router.message(EditBundle.add_number_password)
async def edit_num_password(msg: Message, state: FSMContext):
    status, sid = await ub_manager.verify_password(msg.from_user.id, msg.text.strip())
    if status == "success":
        d = await state.get_data()
        bid = d.get("edit_bid")
        await db.set_bundle_active(bid, False)
        scheduler_mgr.unschedule_bundle(bid)
        _prev = await db.get_bundle(bid)
        old_sid = _prev.get("session_id") if _prev else None
        await db.update_bundle_session(bid, sid)
        if old_sid != sid:
            await release_session(old_sid)
        await msg.answer("✅ تم استبدال الرقم. المجموعة متوقفة.")
        kb = await build_main_menu(msg.from_user.id)
        await msg.answer("اختر من القائمة:", reply_markup=kb)
        await state.clear()
    else:
        await msg.answer(f"❌ فشل: {status}\n🔁 أعد إرسال كلمة المرور.")


# ------- Delete number -------
@router.callback_query(F.data.startswith("e_delnum_"))
async def cb_e_delnum(cb: CallbackQuery):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    bid = int(cb.data.split("_")[2])
    b = await db.get_bundle(bid)
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("ليست لك.", show_alert=True); return
    await db.set_bundle_active(bid, False)
    scheduler_mgr.unschedule_bundle(bid)
    old_sid = b.get("session_id")
    await db.clear_bundle_session(bid)
    await release_session(old_sid)
    await render_bundle(cb, bid, edit=True)
    await cb.answer("🗑 تم حذف الرقم وإيقاف المجموعة.")


# ------- Change group -------
@router.callback_query(F.data.startswith("e_grp_"))
async def cb_e_grp(cb: CallbackQuery, state: FSMContext):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    bid = int(cb.data.split("_")[2])
    b = await db.get_bundle(bid)
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("ليست لك.", show_alert=True); return
    if not b.get("session_id"):
        await cb.answer("⚠️ أضف رقم أولاً.", show_alert=True); return
    await state.update_data(edit_bid=bid)
    await cb.message.answer("🔗 أرسل رابط الكروب الجديد أو @username أو ID:")
    await state.set_state(EditBundle.add_group_link)
    await cb.answer("🔗")


@router.message(EditBundle.add_group_link)
async def edit_group_link(msg: Message, state: FSMContext):
    d = await state.get_data()
    bid = d.get("edit_bid")
    b = await db.get_bundle(bid)
    if not b or not b.get("session_id"):
        await msg.answer("❌ المجموعة أو الرقم غير موجود.")
        await state.clear(); return
    result = await ub_manager.resolve_group(b["session_id"], msg.text.strip())
    if not result:
        await msg.answer("❌ لم أتمكن من الوصول للكروب. حاول مرة أخرى:")
        return
    gid, title = result
    gdb_id = await db.add_group(msg.from_user.id, gid, title)
    await db.update_bundle_group(bid, gdb_id)
    await msg.answer(f"✅ تم استبدال الكروب: {title}")
    kb = await build_main_menu(msg.from_user.id)
    await msg.answer("اختر من القائمة:", reply_markup=kb)
    await state.clear()


# ------- Change cliche -------
@router.callback_query(F.data.startswith("e_cli_"))
async def cb_e_cli(cb: CallbackQuery, state: FSMContext):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    bid = int(cb.data.split("_")[2])
    b = await db.get_bundle(bid)
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("ليست لك.", show_alert=True); return
    await state.update_data(edit_bid=bid)
    await cb.message.answer("💬 أرسل نص الكليشة الجديد (سيستبدل القديمة):")
    await state.set_state(EditBundle.add_cliche_text)
    await cb.answer("💬")


@router.message(EditBundle.add_cliche_text)
async def edit_cliche_text(msg: Message, state: FSMContext):
    d = await state.get_data()
    bid = d.get("edit_bid")
    cid = await db.add_cliche(msg.from_user.id, msg.text)
    await db.update_bundle_cliche(bid, cid)
    await msg.answer("✅ تم استبدال الكليشة.")
    kb = await build_main_menu(msg.from_user.id)
    await msg.answer("اختر من القائمة:", reply_markup=kb)
    await state.clear()


# ------- Timer -------
@router.callback_query(F.data.startswith("e_timer_"))
async def cb_e_timer(cb: CallbackQuery, state: FSMContext):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    bid = int(cb.data.split("_")[2])
    b = await db.get_bundle(bid)
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("ليست لك.", show_alert=True); return
    mn = await db.get_min_interval()
    mx = await db.get_max_interval()
    await state.update_data(edit_bid=bid)
    await cb.message.answer(f"⏱️ أرسل المؤقت الجديد (بين {mn} و {mx}):")
    await state.set_state(EditBundle.waiting_new_interval)
    await cb.answer("⏱️")


@router.message(EditBundle.waiting_new_interval)
async def edit_new_interval(msg: Message, state: FSMContext):
    try:
        interval = int(msg.text.strip())
    except ValueError:
        await msg.answer("❌ أرسل رقمًا صحيحًا.")
        return
    mn = await db.get_min_interval()
    mx = await db.get_max_interval()
    if interval < mn or interval > mx:
        await msg.answer(f"❌ المؤقت بين {mn} و {mx}.")
        return
    d = await state.get_data()
    bid = d.get("edit_bid")
    b = await db.get_bundle(bid)
    await db.set_bundle_interval(bid, interval)
    if b and b["is_active"]:
        scheduler_mgr.schedule_bundle(bid, interval)
    await msg.answer(f"✅ تم تحديث المؤقت إلى {interval} ثانية.")
    kb = await build_main_menu(msg.from_user.id)
    await msg.answer("اختر من القائمة:", reply_markup=kb)
    await state.clear()


# ------- Delete bundle -------
@router.callback_query(F.data.startswith("e_delb_"))
async def cb_e_delb(cb: CallbackQuery):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    bid = int(cb.data.split("_")[2])
    b = await db.get_bundle(bid)
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("ليست لك.", show_alert=True); return
    scheduler_mgr.unschedule_bundle(bid)
    old_sid = b.get("session_id")
    await db.delete_bundle(bid)
    await release_session(old_sid)
    kb = await build_main_menu(cb.from_user.id)
    await cb.message.edit_text(f"✅ تم حذف المجموعة #{bid}.", reply_markup=kb)
    await cb.answer("🗑")


# ---------------- Owner Panel ----------------
@router.callback_query(F.data == "owner_menu")
async def cb_owner_menu(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("للمالك فقط.", show_alert=True); return
    await cb.message.edit_text("⚙️ لوحة المالك:", reply_markup=owner_menu())
    await cb.answer()


@router.callback_query(F.data == "own_block")
async def cb_own_block(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    await cb.message.answer("أرسل user_id / bot_id للحظر:")
    await state.set_state(OwnerFlow.block_wait)
    await cb.answer("🚫")


@router.message(OwnerFlow.block_wait)
async def proc_block(msg: Message, state: FSMContext):
    try:
        eid = int(msg.text.strip())
        await db.block_entity(eid, "user")
        await msg.answer(f"✅ تم حظر {eid}.")
    except ValueError:
        await msg.answer("❌ أرسل ID رقمي.")
    await state.clear()


@router.callback_query(F.data == "own_unblock")
async def cb_own_unblock(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    await cb.message.answer("أرسل ID لإلغاء الحظر:")
    await state.set_state(OwnerFlow.unblock_wait)
    await cb.answer("✅")


@router.message(OwnerFlow.unblock_wait)
async def proc_unblock(msg: Message, state: FSMContext):
    try:
        eid = int(msg.text.strip())
        await db.unblock_entity(eid)
        await msg.answer(f"✅ تم إلغاء حظر {eid}.")
    except ValueError:
        await msg.answer("❌ ID غير صحيح.")
    await state.clear()


@router.callback_query(F.data == "own_cliches")
async def cb_own_cliches(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    await cb.message.edit_text("💬 كليشات الأدمن:", reply_markup=admin_cliches_menu())
    await cb.answer()


@router.callback_query(F.data == "own_cliche_add")
async def cb_own_cliche_add(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    await cb.message.answer(
        "أرسل كليشة/كليشات الأدمن.\n💡 كل كليشة في سطر منفصل."
    )
    await state.set_state(OwnerFlow.add_admin_cliche)
    await cb.answer("💬")


@router.message(OwnerFlow.add_admin_cliche)
async def proc_admin_cliche(msg: Message, state: FSMContext):
    lines = [l.strip() for l in (msg.text or "").split("\n") if l.strip()]
    if not lines:
        await msg.answer("❌ لم يتم التعرف على كليشات.")
        return
    for l in lines:
        await db.add_admin_cliche(l)
    await msg.answer(f"✅ تم حفظ {len(lines)} كليشة.", reply_markup=admin_cliches_menu())
    await state.clear()


@router.callback_query(F.data == "own_cliche_del_list")
async def cb_own_cliche_del_list(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    cliches = await db.get_all_admin_cliches()
    if not cliches:
        await cb.message.answer("📭 لا توجد كليشات.")
        await cb.answer(); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 {c['text'][:30]}", callback_data=f"delac_{c['id']}")]
        for c in cliches
    ] + [[InlineKeyboardButton(text="🔙 رجوع", callback_data="own_cliches")]])
    await cb.message.answer("اختر كليشة لحذفها:", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("delac_"))
async def cb_del_admin_cliche(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    cid = int(cb.data.split("_")[1])
    await db.delete_admin_cliche(cid)
    await cb.message.edit_text(f"✅ تم حذف الكليشة #{cid}.")
    await cb.answer("🗑")


# ------- Forced Channel -------
@router.callback_query(F.data == "own_forced")
async def cb_own_forced(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    await cb.message.answer(
        "أرسل @username القناة أو رابطها أو ID.\n⚠️ يجب أن يكون البوت مشرفاً."
    )
    await state.set_state(OwnerFlow.add_forced_channel)
    await cb.answer("📢")


@router.message(OwnerFlow.add_forced_channel)
async def proc_forced(msg: Message, state: FSMContext):
    link = msg.text.strip()
    if not link:
        await msg.answer("❌ أرسل رابطاً صالحاً.")
        return
    try:
        if link.startswith("https://t.me/"):
            username = link.rstrip("/").split("/")[-1]
            if username.startswith("+") or username.startswith("joinchat"):
                await msg.answer("❌ أرسل ID رقمي للقنوات الخاصة.")
                return
            chat = await msg.bot.get_chat("@" + username)
        elif link.startswith("@"):
            chat = await msg.bot.get_chat(link)
        else:
            chat = await msg.bot.get_chat(int(link))
    except Exception as e:
        await msg.answer(f"❌ لم أتمكن من العثور على القناة: {e}")
        return
    chat_type = getattr(chat, "type", None)
    if hasattr(chat_type, "value"):
        chat_type = chat_type.value
    if chat_type and chat_type != "channel":
        await msg.answer(f"❌ هذا ليس قناة (نوعه: {chat_type}).")
        return
    is_admin, info = await _bot_is_admin(msg.bot, chat.id)
    if not is_admin:
        await msg.answer(f"❌ البوت ليس مشرفاً ({info}).")
        return
    await db.set_forced_channel(str(chat.id), link)
    await msg.answer(f"✅ القناة الإجبارية: {chat.title}\n🆔 {chat.id}")
    await state.clear()


# ------- Limits -------
@router.callback_query(F.data == "own_limits")
async def cb_own_limits(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    await cb.message.answer("أرسل: user_id max_numbers max_groups max_cliches")
    await state.set_state(OwnerFlow.set_limits_wait)
    await cb.answer("📊")


@router.message(OwnerFlow.set_limits_wait)
async def proc_limits(msg: Message, state: FSMContext):
    parts = msg.text.split()
    if len(parts) != 4:
        await msg.answer("❌ صيغة غير صحيحة.")
        return
    try:
        uid, mn, mg, mc = map(int, parts)
        await db.set_limits(uid, mn, mg, mc)
        await msg.answer(f"✅ تم تحديث حدود {uid}.")
    except ValueError:
        await msg.answer("❌ قيم غير رقمية.")
    await state.clear()


# ------- Timer bounds (owner) -------
@router.callback_query(F.data == "own_timer_bounds")
async def cb_own_timer_bounds(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    mn = await db.get_min_interval()
    mx = await db.get_max_interval()
    await cb.message.answer(f"⏱️ الحالي: min={mn}, max={mx}\nأرسل: min max")
    await state.set_state(OwnerFlow.set_timer_bounds)
    await cb.answer("⏱️")


@router.message(OwnerFlow.set_timer_bounds)
async def proc_timer_bounds(msg: Message, state: FSMContext):
    parts = msg.text.split()
    if len(parts) != 2:
        await msg.answer("❌ أرسل قيمتين: min max")
        return
    try:
        mn, mx = map(int, parts)
    except ValueError:
        await msg.answer("❌ قيم غير رقمية.")
        return
    if mn < 10 or mx <= mn:
        await msg.answer("❌ min>=10 و max>min.")
        return
    await db.set_setting("min_interval", mn)
    await db.set_setting("max_interval", mx)
    await msg.answer(f"✅ حدود المؤقت: {mn} .. {mx}")
    await state.clear()


# ------- Tutorial video (owner) -------
@router.callback_query(F.data == "own_tutorial")
async def cb_own_tutorial(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("للمالك فقط.", show_alert=True); return
    current = await db.get_tutorial_video()
    status = "✅ مضبوط" if current else "❌ غير مضبوط"
    await cb.message.answer(
        f"🎬 فيديو الشرح: {status}\n\nأرسل الفيديو الآن:"
    )
    await state.set_state(OwnerFlow.add_tutorial)
    await cb.answer("🎬")


@router.message(OwnerFlow.add_tutorial, F.video)
async def proc_tutorial_video(msg: Message, state: FSMContext):
    if msg.from_user.id != OWNER_ID:
        return
    file_id = msg.video.file_id
    caption = msg.caption or ""
    await db.set_tutorial_video(file_id, caption)
    await msg.answer("✅ تم حفظ فيديو الشرح.")
    await state.clear()


@router.message(OwnerFlow.add_tutorial)
async def proc_tutorial_wrong(msg: Message, state: FSMContext):
    if msg.from_user.id != OWNER_ID:
        return
    await msg.answer("❌ أرسل فيديو.")


# ------- Test join (owner) -------
@router.callback_query(F.data == "own_test_join")
async def cb_own_test_join(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    sessions = await db.get_all_active_sessions()
    if not sessions:
        await cb.message.answer("⚠️ لا توجد جلسات محملة.")
        await cb.answer(); return
    lines = ["الجلسات المتاحة:"]
    for s in sessions:
        loaded = "✅" if s["id"] in ub_manager.clients else "❌ (غير محملة)"
        lines.append(f"#{s['id']} - {s['phone']} {loaded}")
    lines.append("\nأرسل: session_id رابط_القناة")
    lines.append("مثال: 1 https://t.me/+xxxxx")
    await cb.message.answer("\n".join(lines))
    await state.set_state(OwnerFlow.test_join)
    await cb.answer("🧪")


@router.message(OwnerFlow.test_join)
async def proc_test_join(msg: Message, state: FSMContext):
    if msg.from_user.id != OWNER_ID:
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) != 2:
        await msg.answer("❌ الصيغة: session_id رابط")
        return
    try:
        sid = int(parts[0])
        link = parts[1].strip()
    except ValueError:
        await msg.answer("❌ session_id رقمي.")
        return
    client = ub_manager.get_client(sid)
    if not client:
        await msg.answer(
            f"❌ لا يوجد client للجلسة {sid}.\n"
            f"الجلسات المحملة فعلياً: {list(ub_manager.clients.keys())}"
        )
        return
    try:
        ent = await client.get_entity(link)
        kind = type(ent).__name__
        await msg.answer(f"ℹ️ get_entity نجح. النوع: {kind}\nمحاولة الانضمام...")
    except Exception as e:
        await msg.answer(f"⚠️ get_entity فشل: {type(e).__name__}: {e}\nمحاولة الانضمام مباشرة...")
    try:
        await client.join_chat(link)
        await msg.answer(f"✅ انضم بنجاح: {link}")
    except Exception as e:
        await msg.answer(f"❌ الانضمام فشل: {type(e).__name__}: {e}")
    await state.clear()


# ------- Debug bundles (owner) -------
@router.message(Command("debug_bundles"))
async def cmd_debug_bundles(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    bundles = await db._fetchall("SELECT * FROM bundles")
    if not bundles:
        await msg.answer("📭 لا توجد bundles.")
        return
    lines = ["📋 تشخيص الباندلات:"]
    for b in bundles:
        sess = await db.get_session(b["session_id"]) if b.get("session_id") else None
        grp = await db.get_group(b["group_id"]) if b.get("group_id") else None
        cli = await db.get_cliche(b["cliche_id"]) if b.get("cliche_id") else None
        active = "🟢" if b["is_active"] else "🔴"
        sess_txt = f"#{b['session_id']} ({sess['phone']})" if sess else f"❌ id={b.get('session_id')}"
        grp_txt = f"#{b['group_id']} '{grp['group_title']}' ({grp['group_id']})" if grp else f"❌ id={b.get('group_id')}"
        cli_txt = "✅" if cli else "❌"
        loaded = "✅" if b.get("session_id") in ub_manager.clients else "❌ غير محملة في ub_manager"
        lines.append(
            f"\n{active} Bundle #{b['id']} '{b.get('name')}'\n"
            f"  session: {sess_txt} | ub_manager: {loaded}\n"
            f"  group: {grp_txt}\n"
            f"  cliche: {cli_txt}"
        )
    await msg.answer("\n".join(lines))
