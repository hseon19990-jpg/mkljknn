"""بناء نصوص الإحصائيات والتقارير (مشتركة بين الأعضاء والمالك والتقرير اليومي)."""
from datetime import datetime, timedelta

import database as db
from userbot import ub_manager

BARS = "▁▂▃▄▅▆▇█"


def spark(values):
    """رسم مصغّر نصي: ▁▂▅▇"""
    if not values:
        return ""
    mx = max(values)
    if mx <= 0:
        return BARS[0] * len(values)
    return "".join(BARS[min(len(BARS) - 1, int(v / mx * (len(BARS) - 1)))] for v in values)


def fmt_local(utc_str, tz, fmt="%m-%d %H:%M"):
    if not utc_str:
        return "—"
    try:
        dt = datetime.strptime(utc_str[:19], "%Y-%m-%d %H:%M:%S") + timedelta(hours=tz)
        return dt.strftime(fmt)
    except Exception:
        return utc_str


def ago(utc_str):
    """منذ كم؟"""
    if not utc_str:
        return "—"
    try:
        dt = datetime.strptime(utc_str[:19], "%Y-%m-%d %H:%M:%S")
        sec = int((db.now_utc() - dt).total_seconds())
    except Exception:
        return utc_str
    if sec < 60:
        return f"منذ {max(sec, 0)} ث"
    if sec < 3600:
        return f"منذ {sec // 60} د"
    if sec < 86400:
        return f"منذ {sec // 3600} س"
    return f"منذ {sec // 86400} يوم"


def _remaining_minutes(iso_local):
    try:
        until = datetime.fromisoformat(iso_local)
    except Exception:
        return 0
    delta = (until - datetime.now()).total_seconds()
    return int(delta // 60) + 1 if delta > 0 else 0


def in_work_hours(b, tz):
    """هل نحن ضمن ساعات العمل المحددة للمجموعة؟ (بالتوقيت المحلي)"""
    hf, ht = b.get("hours_from", -1), b.get("hours_to", -1)
    if hf is None or ht is None or hf < 0 or ht < 0 or hf == ht:
        return True
    h = (db.now_utc() + timedelta(hours=tz)).hour
    if hf < ht:
        return hf <= h < ht
    return h >= hf or h < ht


def hours_label(b):
    hf, ht = b.get("hours_from", -1), b.get("hours_to", -1)
    if hf is None or ht is None or hf < 0 or ht < 0 or hf == ht:
        return "طوال اليوم"
    return f"{hf:02d}:00 → {ht:02d}:00"


def mode_label(mode):
    return {
        "reply": "↩️ رد فقط (يتوقف إن لم يجد رسالة)",
        "auto": "🔁 تلقائي (رد، وإلا رسالة عادية)",
        "plain": "✉️ رسالة عادية بدون رد",
    }.get(mode or "auto", "🔁 تلقائي")


async def bundle_state_label(b):
    if not b["is_active"]:
        return "🔴 متوقفة"
    m = _remaining_minutes(b.get("coma_until")) if b.get("coma_until") else 0
    if m > 0:
        return f"😴 في سبات (متبقي ~{m} د)"
    m = _remaining_minutes(b.get("flood_until")) if b.get("flood_until") else 0
    if m > 0:
        return f"⏳ فلود تلغرام (متبقي ~{m} د)"
    tz = await db.get_tz_offset()
    if not in_work_hours(b, tz):
        return "🌙 خارج ساعات العمل"
    if await db.get_setting_bool("global_pause", False):
        return "⏸ إيقاف شامل من المالك"
    return "🟢 تعمل"


# ---------------------------------------------------------------- بطاقة المجموعة
async def build_bundle_stats_text(b):
    tz = await db.get_tz_offset()
    bid = b["id"]
    today = db.day_start_utc_str(tz)
    h24 = db.hours_ago_str(24)
    c_today = await db.post_counts(since=today, bundle_id=bid)
    c_week = await db.post_counts(since=db.day_start_utc_str(tz, 6), bundle_id=bid)
    series = await db.daily_ok_series(tz, 7, bundle_id=bid)
    ok_w = c_week.get("ok", 0)
    bad_w = sum(v for k, v in c_week.items() if k not in ("ok", "no_target"))
    nt_w = c_week.get("no_target", 0)
    rate = f"{ok_w * 100 // (ok_w + bad_w)}%" if (ok_w + bad_w) else "—"
    name = b.get("name") or f"#{bid}"
    lines = [
        f"📊 إحصائيات: {name}",
        "",
        f"الحالة: {await bundle_state_label(b)}",
        f"✅ إجمالي المرسل: {b.get('total_sent') or 0}",
        f"📅 اليوم: {c_today.get('ok', 0)}"
        + (f" / {b['daily_limit']}" if b.get("daily_limit") else ""),
        f"🕐 آخر 24 ساعة: {await db.count_ok_posts(bid, h24)}",
        f"🗓 آخر 7 أيام: {ok_w}   نسبة النجاح: {rate}",
        f"⚠️ أخطاء (7 أيام): {bad_w}   ⏭ بلا رسالة للرد: {nt_w}",
        "",
        "آخر 7 أيام:",
        f"{spark([v for _, v in series])}  ({' · '.join(str(v) for _, v in series)})",
        "",
        f"📨 آخر نشر: {fmt_local(b.get('last_post_at'), tz)} ({ago(b.get('last_post_at'))})",
    ]
    if b.get("last_error"):
        lines.append(f"❗ آخر خطأ: {b['last_error']}")
    return "\n".join(lines)


async def build_user_stats_text(uid):
    tz = await db.get_tz_offset()
    c = await db.user_counts(uid, tz)
    lim = await db.get_limits(uid)
    series = await db.daily_ok_series(tz, 7, user_id=uid)
    bundles = await db.get_user_bundles(uid)
    rate_base = c["sent_week"] + c["fail_week"]
    rate = f"{c['sent_week'] * 100 // rate_base}%" if rate_base else "—"
    lines = [
        "📊 إحصائياتي",
        "",
        f"📦 المجموعات: {c['bundles']} / {lim['max_groups']}  (تعمل: {c['bundles_active']})",
        f"📱 الأرقام: {c['sessions']} / {lim['max_numbers']}",
        f"✅ إجمالي الرسائل: {c['sent_total']}",
        f"📅 اليوم: {c['sent_today']}   🗓 الأسبوع: {c['sent_week']}",
        f"🎯 نسبة النجاح (أسبوع): {rate}",
        f"🔗 قنوات/كروبات انضممت لها: {c['joins_total']}",
        "",
        f"آخر 7 أيام: {spark([v for _, v in series])}",
    ]
    if bundles:
        lines.append("")
        lines.append("تفصيل المجموعات:")
        for b in bundles:
            name = b.get("name") or f"#{b['id']}"
            lines.append(f"• {name}: {b.get('total_sent') or 0} رسالة — {await bundle_state_label(b)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- لوحة المالك
async def build_global_stats_text():
    tz = await db.get_tz_offset()
    g = await db.global_counts(tz)
    series = await db.daily_ok_series(tz, 7)
    top = await db.top_users_by_posts(db.day_start_utc_str(tz, 6), 5)
    loaded = len(ub_manager.clients)
    connected = sum(1 for c in ub_manager.clients.values() if _is_conn(c))
    total_post = g["sent_today"] + g["fail_today"]
    rate = f"{g['sent_today'] * 100 // total_post}%" if total_post else "—"
    paused = await db.get_setting_bool("global_pause", False)
    maint = await db.get_setting_bool("maintenance", False)
    lines = [
        "📊 لوحة الإحصائيات العامة",
        "",
        f"👥 الأعضاء: {g['users']}  (جدد اليوم: {g['new_today']} · الأسبوع: {g['new_week']})",
        f"🟢 نشطون آخر 24 ساعة: {g['active_24h']}   🚫 محظورون: {g['blocked']}",
        "",
        f"📦 المجموعات: {g['bundles']}  (تعمل: {g['bundles_active']})",
        f"😴 في سبات: {g['coma_now']}   ⏳ فلود: {g['flood_now']}",
        f"📱 الحسابات: {g['sessions']} (محمّلة: {loaded} · متصلة: {connected})",
        "",
        f"✅ رسائل اليوم: {g['sent_today']}   آخر 24س: {g['sent_24h']}",
        f"📈 الإجمالي: {g['sent_total']}",
        f"⚠️ أخطاء اليوم: {g['fail_today']}   نسبة النجاح: {rate}",
        f"⏭ بلا هدف للرد اليوم: {g['no_target_today']}",
        f"🔗 انضمامات اليوم: {g['joins_today']} (الإجمالي {g['joins_total']})   فلود الانضمام: {g['join_flood_today']}",
        "",
        f"آخر 7 أيام: {spark([v for _, v in series])}",
        f"({' · '.join(str(v) for _, v in series)})",
    ]
    if top:
        lines.append("")
        lines.append("🏆 الأكثر نشاطاً (أسبوع):")
        for i, r in enumerate(top, 1):
            u = await db.get_user(r["user_id"])
            label = f"@{u['username']}" if u and u.get("username") else str(r["user_id"])
            lines.append(f"{i}. {label} — {r['c']}")
    if paused or maint:
        lines.append("")
        if paused:
            lines.append("⏸ الإيقاف الشامل: مفعّل")
        if maint:
            lines.append("🛠 وضع الصيانة: مفعّل")
    return "\n".join(lines)


def _is_conn(client):
    try:
        return client.is_connected()
    except Exception:
        return False
