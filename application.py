"""Part of the SMMMAIN Telegram bot.

This section is loaded with the shared compatibility namespace so existing
handlers can continue to call each other while the code stays separated by
domain.
"""
from .referrals import run_referral_tasks_job
from .reaction_operations import run_reaction_ops_job
from .raksh_system.common import cleanup_expired_raksh_channel_memberships
from .storage_cleanup import cleanup_transient_storage_job
from types import SimpleNamespace
from . import shared as _shared
globals().update({key: value for key, value in vars(_shared).items() if not key.startswith("__")})
from telegram.ext import ExtBot, Updater

class ResilientExtBot(ExtBot):
    """Let polling handle Telegram bootstrap retries instead of blocking on getMe()."""

    @property
    def id(self) -> int:
        # Application.start() uses bot.id only for asyncio task names. Keep
        # startup non-blocking until the background getMe() call succeeds.
        return self._bot_user.id if self._bot_user is not None else 0

    @property
    def username(self) -> str:
        # Some legacy handlers read bot.username while polling is starting.
        # ExtBot normally raises until getMe() has populated _bot_user; return
        # an empty value instead so a temporary Telegram/API delay cannot
        # crash the update handler.
        return self._bot_user.username if self._bot_user is not None else ""

    async def initialize(self) -> None:
        if getattr(self, "_initialized", False):
            return
        if self.rate_limiter:
            await self.rate_limiter.initialize()
        await asyncio.gather(self._request[0].initialize(), self._request[1].initialize())
        self._initialized = True

class ResilientUpdater(Updater):
    """Start polling without blocking on a temporary Telegram outage."""

    async def _bootstrap(self, *args, **kwargs) -> None:
        # Clear the webhook once before polling when Telegram responds quickly.
        # A slow API must not hold the whole application; the background retry
        # task in post_init continues cleanup after polling has started.
        try:
            await asyncio.wait_for(
                self.bot.delete_webhook(
                    drop_pending_updates=True,
                ),
                timeout=10,
            )
            logger.info("✅ Webhook cleanup completed before polling")
        except asyncio.TimeoutError:
            logger.warning("⚠️ Webhook cleanup timed out; polling starts and background retry continues")
        except (TimedOut, NetworkError) as e:
            logger.warning(f"⚠️ Webhook cleanup delayed: {e}; polling starts and background retry continues")
        except Exception as e:
            logger.warning(f"⚠️ Webhook cleanup failed: {e}; polling starts and background retry continues")

async def sync_raksh_bot_commands(bot) -> None:
    """يحدّث اسم أمر الرشق في قائمة تيليجرام فور تغيير الاسم."""
    label = get_raksh_accounts_label()
    await bot.set_my_commands([
        BotCommand("start", "🏠 القائمة الرئيسية"),
        BotCommand("raksh", f"🔥 {label}"),
    ])
    if OWNER_ID:
        await bot.set_my_commands(
            [
                BotCommand("start", "🏠 القائمة الرئيسية"),
                BotCommand("admin", "⚙️ لوحة المالك"),
                BotCommand("addpoints", "💰 إضافة/خصم نقاط لمستخدم"),
                BotCommand("broadcast", "📢 إرسال رسالة جماعية"),
                BotCommand("status", "🔍 فحص حالة طلب"),
                BotCommand("compensate_partial", "💰 تعويض أصحاب الطلبات الجزئية"),
                BotCommand("refund_mandatory", "🔁 استرجاع تمويلات الاشتراك الإجباري"),
                BotCommand("testai", "🧪 اختبار مفاتيح AI"),
                BotCommand("raksh", f"🔥 {label}"),
            ],
            scope=BotCommandScopeChat(chat_id=OWNER_ID),
        )

# ─── استيراد نظام الرشق الجديد من داخل حزمة bot_app ────────────────────────
from .raksh_system import (
    cmd_raksh,
    handle_raksh_callback,
    raksh_pre_checkout,
    raksh_successful_payment,
    resume_raksh_orders_job,
    recover_raksh_orders_on_startup,
)

async def pre_checkout_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يوجّه استعلام الدفع إلى معالجه حسب نوع الفاتورة."""
    payload = update.pre_checkout_query.invoice_payload or ""
    if payload.startswith("raksh_stars:"):
        await raksh_pre_checkout(update, context)
    else:
        await pre_checkout(update, context)


async def successful_payment_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يوجّه إشعار الدفع الناجح إلى معالجه حسب نوع الفاتورة."""
    payload = update.message.successful_payment.invoice_payload or ""
    if payload.startswith("raksh_stars:"):
        await raksh_successful_payment(update, context)
    else:
        await successful_payment(update, context)


async def cmd_set_button_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يحفظ Custom Emoji مدفوعاً كأيقونة لزر معيّن."""
    user = update.effective_user
    if not user or user.id != OWNER_ID:
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك فقط.")
        return

    action_value = (context.args[0] if context.args else "").strip()
    entities = update.effective_message.entities or []
    custom_emoji_ids = [
        str(getattr(entity, "custom_emoji_id", ""))
        for entity in entities
        if getattr(entity, "type", "") == "custom_emoji"
        and str(getattr(entity, "custom_emoji_id", "")).isdigit()
    ]
    if not action_value or not custom_emoji_ids:
        await update.effective_message.reply_text(
            "🎨 الاستخدام:\n"
            "أرسل الأمر ثم اسم الزر وبعده Premium Custom Emoji في نفس الرسالة.\n\n"
            "مثال: /setbuttonemoji services_menu ⭐\n"
            "الأسماء الشائعة: services_menu, collect_points, charge_points, "
            "my_info, my_numbers, referral, main_menu"
        )
        return

    custom_emoji_id = custom_emoji_ids[0]
    _shared.save_button_custom_emoji(action_value, custom_emoji_id)
    await update.effective_message.reply_text(
        f"✅ تم ربط الأيقونة المميزة بالزر: {action_value}\n"
        "أعد فتح القائمة لرؤيتها."
    )


def main():
    # ── إنشاء event loop جديد في كل تشغيل لتفادي RuntimeError: Event loop is closed ──
    import asyncio as _asyncio
    _loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(_loop)

    # ── التحقق من المتغيرات البيئية الضرورية عند الإطلاق ──────────────────
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if not OWNER_ID:
        missing.append("OWNER_ID")
    if missing:
        logger.critical(f"❌ متغيرات بيئية مفقودة: {', '.join(missing)}")
        logger.critical("❌ أضفها في إعدادات Railway ثم أعد التشغيل.")
        raise SystemExit(1)

    start_health_server()
    init_db()
    try:
        from .users import get_setting
        _shared.load_button_custom_emoji_ids(get_setting(_shared.BUTTON_CUSTOM_EMOJI_SETTING_KEY))
    except Exception as exc:
        logger.warning(f"⚠️ تعذّر تحميل أيقونات الأزرار المميزة: {exc}")
    recovered_raksh_orders = recover_raksh_orders_on_startup()
    if recovered_raksh_orders:
        logger.info(f"🔁 تم تجهيز {recovered_raksh_orders} طلب رشق للاستئناف بعد إعادة النشر")

    from telegram.request import HTTPXRequest

    # Railway can occasionally take longer to establish or return a Telegram
    # API connection. Configure both request clients: get_me() and normal bot
    # calls use the default client, while long polling uses the updates client.
    # Pool exhaustion fix: the previous 32-connection pool with 120s read
    # timeout was filled by background Raksh/report sends, so /start and other
    # user replies failed with "Pool timeout" and the bot looked dead.
    # A much larger pool plus short read timeouts frees stuck connections fast.
    telegram_request = HTTPXRequest(
        connection_pool_size=256,
        read_timeout=30,
        connect_timeout=20,
        write_timeout=30,
        pool_timeout=30,
    )
    updates_request = HTTPXRequest(
        connection_pool_size=4,
        read_timeout=60,
        connect_timeout=20,
        write_timeout=30,
        pool_timeout=30,
    )

    telegram_bot = ResilientExtBot(
        token=BOT_TOKEN,
        request=telegram_request,
        get_updates_request=updates_request,
    )
    polling_updater = ResilientUpdater(telegram_bot, asyncio.Queue())
    app = (
        ApplicationBuilder()
        .updater(polling_updater)
        # Bound handler concurrency so Telegram API work cannot grow far
        # beyond the shared request pool under bursts of updates.
        .concurrent_updates(64)
        .build()
    )

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("setbuttonemoji", cmd_set_button_emoji))
    app.add_handler(CommandHandler("admin",     cmd_admin))
    app.add_handler(CommandHandler("addpoints", cmd_addpoints))
    app.add_handler(CommandHandler("grant_ref", cmd_grant_ref))
    app.add_handler(CommandHandler("broadcast",           cmd_broadcast))
    app.add_handler(CommandHandler("status",              cmd_status_order))
    app.add_handler(CommandHandler("compensate_partial",  cmd_compensate_partial))
    app.add_handler(CommandHandler("refund_mandatory",    cmd_refund_mandatory))
    app.add_handler(CommandHandler("cancel",              cmd_cancel))
    app.add_handler(CommandHandler("import_session",      cmd_import_session))
    app.add_handler(CommandHandler("import_sessions",     cmd_import_sessions))
    app.add_handler(CommandHandler("import_hex",          cmd_import_hex))
    app.add_handler(CommandHandler("mass_reset",          cmd_mass_reset))
    app.add_handler(CommandHandler("rotate_sessions",     cmd_rotate_sessions))
    
    # ════════════════════════════════════════════════════════════
    # 🔥 أمر اختبار AI (للمالك فقط)
    # ════════════════════════════════════════════════════════════
    app.add_handler(CommandHandler("testai", cmd_test_ai))
    # ════════════════════════════════════════════════════════════
    
    # ════════════════════════════════════════════════════════════
    # 🔥 نظام الرشق الجديد (RAKSH SYSTEM)
    # ════════════════════════════════════════════════════════════
    app.add_handler(CommandHandler("raksh", cmd_raksh))
    # لا تلتقط أزرار البوت العامة؛ هذا المعالج مخصص لـ Raksh فقط.
    # بدون pattern كان يطابق كل CallbackQuery ويمنع handle_callback من العمل.
    app.add_handler(
        CallbackQueryHandler(
            handle_raksh_callback,
            # Match the menu callback as well as every Raksh step explicitly.
            # The old catch-all handler below must never consume these queries.
            pattern=r"^(?:raksh_menu|raksh_cancel|raksh(?:_|:))",
        )
    )
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_router))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_router))
    # ════════════════════════════════════════════════════════════
    
    # ════════════════════════════════════════════════════════════
    # 🔥 استيراد الخدمات من المواقع
    # ════════════════════════════════════════════════════════════
    app.add_handler(CommandHandler("import_services", cmd_import_services))
    # ════════════════════════════════════════════════════════════
    
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (
            filters.PHOTO
            | filters.Document.MimeType("image/png")
            | filters.Document.MimeType("image/jpeg")
            | filters.Document.MimeType("image/webp")
            | filters.VIDEO
            | filters.Document.MimeType("video/mp4")
            | filters.Document.MimeType("video/quicktime")
            | filters.Document.MimeType("video/x-m4v")
            | filters.Document.FileExtension("mp4")
            | filters.Document.FileExtension("mov")
            | filters.Document.FileExtension("m4v")
        ),
        handle_avatar_photo
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.StatusUpdate.WEB_APP_DATA,
        handle_owner_webapp_data,
    ))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_text
    ))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.User(OWNER_ID) & (
            filters.Document.FileExtension("session")
            | filters.Document.FileExtension("json")
            | filters.Document.FileExtension("zip")
            | filters.Document.MimeType("application/json")
            | filters.Document.MimeType("application/zip")
            | filters.Document.MimeType("application/x-zip-compressed")
        ),
        handle_buyback_document,
    ))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Document.MimeType("application/json"),
        handle_json_file
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Document.FileExtension("session"),
        handle_session_file
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (
            filters.Document.FileExtension("zip")
            | filters.Document.MimeType("application/zip")
            | filters.Document.MimeType("application/x-zip-compressed")
        ),
        handle_zip_file
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.CONTACT,
        handle_contact_share
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND & ~filters.SUCCESSFUL_PAYMENT,
        handle_unsupported_message
    ))
    app.add_handler(ChatMemberHandler(handle_member_leave, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handle_bot_removed_from_channel, ChatMemberHandler.MY_CHAT_MEMBER))
    if ADMIN_GROUP_ID:
        app.add_handler(MessageHandler(
            filters.Chat(ADMIN_GROUP_ID) &
            (filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER),
            delete_group_service_messages
        ))

    # ════════════════════════════════════════════════════════════
    # 🔥 أمر الطلبات المتأخرة (للمالك فقط) - مدمج هنا لضمان العمل
    # ════════════════════════════════════════════════════════════
    async def cmd_delayed_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id != OWNER_ID:
            await update.message.reply_text("⛔ هذا الأمر للمالك فقط.")
            return
        
        await update.message.reply_text("⏳ *جاري فحص الطلبات المتأخرة...*", parse_mode=ParseMode.MARKDOWN)
        
        threshold = datetime.datetime.now(timezone.utc) - datetime.timedelta(hours=6)
        
        with db_conn() as c:
            rows = c.execute("""
                SELECT o.order_code, o.user_id, o.created_at, 
                       s.name_ar AS service_name, s.panel AS panel
                FROM orders o
                LEFT JOIN services s ON s.id = o.service_id
                WHERE o.status = 'pending'
                  AND o.created_at::timestamptz < %s
                  AND o.api_order_id IS NOT NULL
                  AND o.api_order_id != ''
                ORDER BY o.created_at ASC
            """, (threshold,)).fetchall()
        
        orders = [dict(row) for row in rows]
        
        if not orders:
            await update.message.reply_text("📊 *الطلبات المتأخرة*\n\n✅ لا توجد طلبات مضى عليها أكثر من 6 ساعات.")
            return
        
        from collections import defaultdict
        orders_by_panel = defaultdict(list)
        for order in orders:
            orders_by_panel[order['panel'] or 1].append(order)
        
        for panel, order_list in orders_by_panel.items():
            panel_names = {1: "SMMMAIN", 2: "JustAnotherPanel", 3: "SmmFollows"}
            panel_name = panel_names.get(panel, "موقع غير معروف")
            
            lines = [f"📊 *الطلبات المتأخرة - {panel_name}*", f"📦 عدد الطلبات: {len(order_list)}", ""]
            for o in order_list:
                created = o['created_at']
                date_str = created.strftime("%Y-%m-%d %H:%M") if hasattr(created, 'strftime') else str(created)[:16]
                delay = round((datetime.datetime.now(timezone.utc) - created).total_seconds() / 3600, 1)
                lines.append(f"  └ 📌 `{o['order_code']}` — {date_str} (تأخير {delay} ساعة)")
            
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    
    app.add_handler(CommandHandler("delayed_orders", cmd_delayed_orders))
    # ════════════════════════════════════════════════════════════

    async def post_init(application):
        # ─── معالج عالمي للاستثناءات غير المعالجة في asyncio tasks ────────
        def _handle_asyncio_exception(loop, context):
            exc = context.get("exception")
            msg = context.get("message", "")
            if exc is None:
                logger.warning(f"⚠️ asyncio unhandled: {msg}")
            elif isinstance(exc, asyncio.CancelledError):
                pass  # طبيعي عند إغلاق البوت
            else:
                logger.error(f"❌ asyncio task exception: {exc!r} | {msg}")
        try:
            asyncio.get_event_loop().set_exception_handler(_handle_asyncio_exception)
        except Exception:
            pass

        async def _configure_telegram():
            """Configure optional Telegram metadata without blocking polling startup."""
            try:
                await application.bot.set_my_commands([
                    BotCommand("start", "🏠 القائمة الرئيسية"),
                    BotCommand("raksh", f"🔥 {get_raksh_accounts_label()}"),
                ])
                if OWNER_ID:
                    try:
                        await application.bot.set_my_commands(
                            [
                                BotCommand("start",     "🏠 القائمة الرئيسية"),
                                BotCommand("admin",     "⚙️ لوحة المالك"),
                                BotCommand("addpoints", "💰 إضافة/خصم نقاط لمستخدم"),
                                BotCommand("broadcast",          "📢 إرسال رسالة جماعية"),
                                BotCommand("status",             "🔍 فحص حالة طلب"),
                                BotCommand("compensate_partial", "💰 تعويض أصحاب الطلبات الجزئية"),
                                BotCommand("refund_mandatory", "🔁 استرجاع تمويلات الاشتراك الإجباري"),
                                BotCommand("testai",             "🧪 اختبار مفاتيح AI"),
                                BotCommand("raksh",              f"🔥 {get_raksh_accounts_label()}"),
                            ],
                            scope=BotCommandScopeChat(chat_id=OWNER_ID)
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ تعذّر تعيين أوامر المالك الخاصة (ربما لم يبدأ المالك محادثة مع البوت بعد): {e}")

                global _OWN_BOT_USERNAME
                _me = await application.bot.get_me()
                _OWN_BOT_USERNAME = (_me.username or "").lower().strip()
                logger.info(f"✅ Telegram API connected — bot username: @{_OWN_BOT_USERNAME}")
            except (TimedOut, NetworkError) as e:
                logger.warning(f"⚠️ تعذّر مزامنة إعدادات Telegram مؤقتاً؛ polling مستمر: {e}")
            except Exception as e:
                logger.warning(f"⚠️ تعذّر مزامنة إعدادات Telegram: {e}")

        async def _clear_webhook():
            retry_delay = 5
            for attempt in range(1, 7):
                try:
                    await application.bot.delete_webhook(
                        drop_pending_updates=True,
                        read_timeout=10,
                        write_timeout=10,
                        connect_timeout=10,
                        pool_timeout=10,
                    )
                    logger.info("✅ Webhook cleanup completed")
                    return
                except (TimedOut, NetworkError) as e:
                    logger.warning(
                        f"⚠️ Webhook cleanup attempt {attempt}/6 مؤجل بسبب Telegram: {e}"
                    )
                except Exception as e:
                    logger.warning(f"⚠️ Webhook cleanup attempt {attempt}/6 failed: {e}")
                if attempt < 6:
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
            logger.error(
                "❌ Webhook cleanup لم يكتمل بعد 6 محاولات؛ polling مستمر وسيُعاد تنظيفه عند إعادة التشغيل."
            )

        asyncio.create_task(_configure_telegram(), name="telegram-metadata-sync")
        asyncio.create_task(_clear_webhook(), name="telegram-webhook-cleanup")
        logger.info("✅ Telegram metadata synchronization scheduled in background")
        
        # ════════════════════════════════════════════════════════════
        # 🔥 سجل حالة مفاتيح AI عند بدء التشغيل
        # ════════════════════════════════════════════════════════════
        groq_key = os.environ.get("GROQ_API_KEY", "")
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
        openai_key = get_openai_api_key()
        logger.info(f"🔑 GROQ_API_KEY موجود: {bool(groq_key)} | طوله: {len(groq_key)}")
        logger.info(f"🔑 DEEPSEEK_API_KEY موجود: {bool(deepseek_key)} | طوله: {len(deepseek_key)}")
        logger.info(f"🔑 OPENAI_API_KEY موجود: {bool(openai_key)} | طوله: {len(openai_key)}")
        if not groq_key and not deepseek_key and not openai_key:
            logger.warning("⚠️ لا يوجد مفتاح Groq أو DeepSeek أو OpenAI — خدمات التحقق التلقائي لن تعمل.")
        # ════════════════════════════════════════════════════════════
        
        logger.info("ℹ️ Telegram command synchronization scheduled in background")
        # ─── تعويض المبيعات المكررة عند الإقلاع ────
        async def _bg_startup():
            try:
                await compensate_duplicate_sales_job(
                    type("_ctx", (), {"bot": application.bot})()
                )
            except Exception as e:
                logger.warning(f"⚠️ compensate_duplicate_sales (startup): {e}")
        asyncio.create_task(_bg_startup())
        # لا نحذف الأرقام بلا جلسة عند الإقلاع. فقدان الجلسة لا يثبت أن
        # الرقم محظور من Telegram، والمالك وحده يقرر حذف الرقم يدوياً.
        # ─── حذف الأرقام المجمّدة المكتشفة مسبقاً (frozen_at IS NOT NULL) ────
        try:
            with db_conn() as _fzc:
                _fzc.execute(
                    "DELETE FROM number_stock "
                    "WHERE frozen_at IS NOT NULL AND ever_sold IS NOT TRUE AND assigned_to IS NULL"
                )
                _frz_deleted = _fzc.rowcount
            if _frz_deleted:
                logger.warning(f"🧊 حُذفت {_frz_deleted} أرقام مجمّدة تلقائياً عند الإقلاع.")
        except Exception as e:
            logger.warning(f"⚠️ تنظيف الأرقام المجمّدة (startup): {e}")

        # ─── التحقق من وجود GROQ_API_KEY و DEEPSEEK_API_KEY ───
        groq_key = os.environ.get("GROQ_API_KEY", "")
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not groq_key and not deepseek_key:
            logger.warning("⚠️ لا يوجد مفتاح Groq أو DeepSeek — خدمات التحقق التلقائي لن تعمل.")
        elif groq_key:
            logger.info("✅ GROQ_API_KEY موجودة")
        elif deepseek_key:
            logger.info("✅ DEEPSEEK_API_KEY موجودة (بدون Groq)")

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if isinstance(err, RetryAfter):
            logger.warning(f"⚠️ RetryAfter: {err.retry_after}s")
            return
        if isinstance(err, (NetworkError, TimedOut)):
            logger.warning(f"⚠️ خطأ شبكي مؤقت: {err}")
            return
        if isinstance(err, Conflict):
            # نسختان تعملان في نفس الوقت — طبيعي عند redeploy، سيختفي خلال ثوانٍ
            logger.warning("⚠️ Conflict: نسخة أخرى من البوت تعمل — جاري الانتظار حتى تنتهي...")
            return
        # أخطاء Forbidden/BadRequest من handlers — لا تُوقف البوت
        from telegram.error import Forbidden, BadRequest
        if isinstance(err, (Forbidden, BadRequest)):
            logger.warning(f"⚠️ Telegram API error (ignored): {err}")
            return
        logger.error(f"❌ خطأ غير متوقع:\n{traceback.format_exc()}")

    app.add_error_handler(error_handler)
    app.post_init = post_init

    if app.job_queue:
        app.job_queue.run_repeating(check_pending_orders_job, interval=300, first=30)
        app.job_queue.run_repeating(cleanup_transient_storage_job, interval=21600, first=60)
        logger.info("🧹 تم تفعيل تنظيف البيانات المؤقتة بدفعات كل 6 ساعات")
        logger.info("⏱️ تم تفعيل الفحص الدوري لحالة الطلبات (كل 5 دقائق)")
        app.job_queue.run_repeating(retry_pending_session_resets, interval=600, first=90)
        logger.info("🔒 تم تفعيل إعادة المحاولة الدورية لطرد جلسات الأرقام (كل 10 دقائق)")
        app.job_queue.run_repeating(run_referral_tasks_job, interval=3600, first=120)
        app.job_queue.run_repeating(run_reaction_ops_job, interval=30, first=45)
        logger.info("⚡ تم تفعيل تنفيذ حملات تفاعلات Telegram كل 30 ثانية")
        # طلبات التفاعل المستمر تحتاج استئنافاً دورياً بعد إعادة نشر Railway.
        # قد يتزامن الإقلاع مع تأخر قاعدة البيانات أو Telegram؛ المحاولة
        # الدورية تضمن إعادة تشغيل الطلب إذا فشلت محاولة الإقلاع الأولى.
        app.job_queue.run_repeating(
            resume_raksh_orders_job,
            interval=60,
            first=20,
            data={"all_posts_only": True},
        )
        # نفّذ محاولة استئناف قصيرة بعد بدء JobQueue مباشرة، ثم اترك
        # الفحص الدوري كشبكة أمان إذا تأخرت قاعدة البيانات أو Telegram.
        app.job_queue.run_once(
            resume_raksh_orders_job,
            when=5,
            data={"all_posts_only": True},
            name="resume-all-posts-after-startup",
        )
        logger.info("🔔 تم تفعيل استئناف التفاعل المستمر فور الإقلاع وكل دقيقة")
        app.job_queue.run_repeating(
            resume_raksh_orders_job,
            interval=60,
            first=20,
            data={"exclude_all_posts": True},
        )
        logger.info("🔁 تم تفعيل استئناف خدمات الرشق غير المستمرة كل دقيقة")
        app.job_queue.run_repeating(cleanup_expired_raksh_channel_memberships, interval=300, first=60)
        logger.info("👋 تم تفعيل إخراج حسابات الرشق بعد انتهاء مهلة القنوات (كل 5 دقائق)")
        logger.info("🤝 تم تفعيل مهام الإحالة التلقائية (كل ساعة)")
        app.job_queue.run_repeating(compensate_duplicate_sales_job, interval=21600, first=300)
        logger.info("🔁 تم تفعيل فحص البيع المكرر وتعويض المتضررين (كل 6 ساعات)")
        app.job_queue.run_repeating(check_twofa_reset_job, interval=3600, first=60)
        logger.info("🔐 تم تفعيل فحص إكمال إعادة تعيين 2FA (كل ساعة)")
        app.job_queue.run_repeating(enable_pending_2fa_job, interval=3600, first=120)
        logger.info("🔐 تم تفعيل فحص الحسابات التي تحتاج قراءة/تفعيل 2FA (كل ساعة)")
        app.job_queue.run_repeating(process_buyback_quarantine_job, interval=300, first=60)
        logger.info("💰 تم تفعيل فحوص حجر بيع الحسابات (كل 5 دقائق)")
        app.job_queue.run_repeating(_account_fixup_job, interval=30, first=15)
        logger.info("🔧 تم تفعيل حلقة الإصلاح التلقائي للحسابات (كل 30 ثانية)")

    logger.info("🤖 Bot started!")
    app.run_polling(
        drop_pending_updates=True,
        # Keep Telegram bootstrap failures recoverable. The outer runner also
        # retries failures from getMe(), which happens before polling starts.
        timeout=45,
        bootstrap_retries=-1,
        allowed_updates=["message", "callback_query", "pre_checkout_query", "successful_payment", "chat_member", "my_chat_member"],
    )

# ════════════════════════════════════════════════════════════
# 🔥 أمر اختبار AI (للمالك فقط)
# ════════════════════════════════════════════════════════════
async def cmd_test_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ هذا الأمر للمالك فقط.")
        return
    
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
    OPENAI_API_KEY = get_openai_api_key()

    msg = "🧪 فحص مفاتيح الذكاء الاصطناعي\n\n"
    msg += f"🔑 GROQ_API_KEY: {'✅ موجود' if GROQ_API_KEY else '❌ مفقود'}\n"
    msg += f"🔑 DEEPSEEK_API_KEY: {'✅ موجود' if DEEPSEEK_API_KEY else '❌ مفقود'}\n\n"
    msg += f"🔑 OPENAI_API_KEY: {'✅ موجود' if OPENAI_API_KEY else '❌ مفقود'}\n\n"

    def _http_status_message(name: str, status_code: int) -> str:
        if status_code == 200:
            return f"✅ {name}: يعمل بشكل صحيح | HTTP {status_code}"
        if status_code in (401, 403):
            return f"❌ {name}: المفتاح غير صالح أو منتهي الصلاحية | HTTP {status_code}"
        if status_code == 429:
            return f"⚠️ {name}: تم تجاوز حد الطلبات | HTTP {status_code}"
        if 400 <= status_code < 500:
            return f"❌ {name}: طلب مرفوض من الخدمة | HTTP {status_code}"
        if status_code >= 500:
            return f"❌ {name}: عطل مؤقت في خادم الخدمة | HTTP {status_code}"
        return f"⚠️ {name}: استجابة غير متوقعة | HTTP {status_code}"

    providers = [
        ("Groq", GROQ_API_KEY, "https://api.groq.com/openai/v1/chat/completions",
         os.environ.get("GROQ_TEXT_MODEL", "llama-3.1-8b-instant")),
        ("DeepSeek", DEEPSEEK_API_KEY, "https://api.deepseek.com/chat/completions", "deepseek-chat"),
        ("OpenAI", OPENAI_API_KEY, "https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    ]
    for name, key, url, model in providers:
        if not key:
            msg += f"❌ {name}: المفتاح مفقود | HTTP —\n"
            continue
        try:
            # لا نعتمد على اسم نموذج قديم في GROQ_TEXT_MODEL؛ Groq قد
            # يزيل نموذجاً أو لا يتيحه لهذا المفتاح، وعندها يظهر 404 رغم
            # أن المفتاح نفسه صحيح. نختار نموذجاً نصياً متاحاً فعلياً.
            if name == "Groq":
                models_response = requests.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=10,
                )
                if models_response.status_code != 200:
                    msg += (
                        _http_status_message("Groq", models_response.status_code)
                        + "\n"
                    )
                    continue
                available_models = [
                    item.get("id", "")
                    for item in models_response.json().get("data", [])
                ]
                text_models = [
                    item for item in available_models
                    if item and not any(
                        part in item.lower()
                        for part in ("whisper", "embedding", "guard")
                    )
                ]
                if model not in text_models:
                    preferred = [
                        "llama-3.1-8b-instant",
                        "openai/gpt-oss-20b",
                        "qwen/qwen3-32b",
                        "llama-3.3-70b-versatile",
                    ]
                    model = next(
                        (candidate for candidate in preferred if candidate in text_models),
                        text_models[0] if text_models else "",
                    )
                if not model:
                    msg += "❌ Groq: لا يوجد نموذج نصي متاح | HTTP 200\n"
                    continue
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "قل مرحباً"}], "max_tokens": 5},
                timeout=10,
            )
            status = _http_status_message(name, r.status_code)
            if name == "Groq" and r.status_code == 200:
                status += f" | model={model}"
            msg += status + "\n"
        except Exception as e:
            msg += f"❌ {name}: لم يصل رد من الخدمة | HTTP — | {type(e).__name__}: {e}\n"
    
    msg += "\n📌 جرّب الآن طلب إحالة بتحقق للتأكد من عمل مسار الكابتشا."
    
    await update.message.reply_text(msg)

# ════════════════════════════════════════════════════════════
# ═══ استيراد الخدمات من المواقع ═══
# ════════════════════════════════════════════════════════════

_IMPORT_PLATFORM_KEYWORDS = {
    "tg": ("telegram", "t.me", "tg", "تيليجرام", "تلغرام"),
    "ig": ("instagram", "insta", "انستغرام", "انستجرام"),
    "tt": ("tiktok", "tik tok", "تيك توك"),
    "wa": ("whatsapp", "whats app", "واتساب"),
    "fb": ("facebook", "face book", "فيس بوك", "فيسبوك"),
    "yt": ("youtube", "you tube", "يوتيوب"),
    "sc": ("snapchat", "snap", "سناب"),
    "tw": ("twitter", "x.com", "تويتر"),
}

_IMPORT_CATEGORY_KEYWORDS = {
    "followers": ("followers", "follower", "subscribers", "subscriber", "members", "member", "متابع", "مشترك", "عضو"),
    "views": ("views", "view", "visits", "visit", "impressions", "مشاهد", "زيار"),
    "interactions": ("comments", "comment", "likes", "like", "reactions", "reaction", "engagement", "تعليق", "إعجاب", "تفاعل"),
    "story_views": ("story", "stories", "ستوري", "قصة"),
    "start_bot": ("start", "bot", "بدء", "بوت"),
    "boost": ("boost", "تعزيز"),
    "post_stars": ("stars", "star", "نجوم", "نجمة"),
}

_IMPORT_TRANSLATIONS = {
    "followers": "متابعون",
    "follower": "متابع",
    "subscribers": "مشتركون",
    "subscriber": "مشترك",
    "members": "أعضاء",
    "member": "عضو",
    "premium": "مميزون",
    "views": "مشاهدات",
    "view": "مشاهدة",
    "visits": "زيارات",
    "visit": "زيارة",
    "impressions": "ظهور",
    "likes": "إعجابات",
    "like": "إعجاب",
    "comments": "تعليقات",
    "comment": "تعليق",
    "reactions": "تفاعلات",
    "reaction": "تفاعل",
    "engagement": "تفاعل",
    "telegram": "تيليجرام",
    "instagram": "انستغرام",
    "tiktok": "تيك توك",
    "whatsapp": "واتساب",
    "facebook": "فيس بوك",
    "youtube": "يوتيوب",
    "snapchat": "سناب شات",
    "twitter": "تويتر",
    "automatic": "تلقائي",
    "instant": "فوري",
    "fast": "سريع",
    "quality": "الجودة",
    "speed": "السرعة",
    "refill": "إعادة التعبئة",
    "drop": "السقوط",
    "low": "منخفض",
    "high": "مرتفع",
    "future posts": "المنشورات المستقبلية",
    "posts": "المنشورات",
    "post": "منشور",
    "story": "ستوري",
    "stories": "ستوريات",
    "stars": "نجوم",
    "star": "نجمة",
    "bot": "بوت",
    "start": "بدء",
    "boost": "تعزيز",
    "smm": "اس ام ام",
    "api": "واجهة برمجية",
    "seo": "تحسين محركات البحث",
    "real": "حقيقي",
    "accounts": "حسابات",
    "account": "حساب",
    "full": "كامل",
    "profile": "ملف شخصي",
    "profiles": "ملفات شخصية",
    "active": "نشط",
    "refillable": "قابل لإعادة التعبئة",
    "guaranteed": "مضمون",
    "targeted": "مستهدف",
    "country": "دولة",
    "worldwide": "عالمي",
}

_LATIN_LETTER_NAMES = {
    "a": "اي", "b": "بي", "c": "سي", "d": "دي", "e": "اي",
    "f": "اف", "g": "جي", "h": "اتش", "i": "اي", "j": "جاي",
    "k": "كي", "l": "ال", "m": "ام", "n": "ان", "o": "او",
    "p": "بي", "q": "كيو", "r": "ار", "s": "اس", "t": "تي",
    "u": "يو", "v": "في", "w": "دبليو", "x": "اكس", "y": "واي",
    "z": "زي",
}

def _import_service_text(service: dict) -> str:
    raw_name = str(service.get("name", "") or service.get("type", "")).strip()
    try:
        rate = float(service.get("rate", 0) or 0)
    except (TypeError, ValueError):
        rate = 0.0
    cleaned = _strip_price_from_desc(raw_name, rate * 100000) or raw_name

    # رقم الخدمة في بداية الاسم ليس جزءاً من الاسم العربي.
    cleaned = re.sub(r"^\s*(?:service\s*)?(?:id\s*[:#-]?\s*)?\d{1,8}\s*[-:.)]\s*", "", cleaned, flags=re.IGNORECASE)

    # احتفظ بعبارات الجودة ونوع الحساب، واحذف مواصفات التشغيل التي تُحفظ
    # أصلاً في الحقول المنفصلة (السرعة، السقوط، إعادة التعبئة، والحدود).
    noise_pattern = re.compile(
        r"(?:speed|start\s*time|drop(?:\s*rate)?|refill|"
        r"السرعة|وقت\s*البدء|السقوط|معدل\s*السقوط|إعادة\s*التعبئة|"
        r"max(?:imum)?|minimum|min|الحد\s*الأعلى|الحد\s*الأدنى|أقصى|أدنى)",
        flags=re.IGNORECASE,
    )
    parts = re.split(r"\s*(?:\||•|\s+-\s+)\s*", cleaned)
    useful_parts = [part.strip() for part in parts if part.strip() and not noise_pattern.search(part)]
    cleaned = " | ".join(useful_parts)
    return cleaned.strip(" -|/،,;:") or "خدمة جديدة"

def _fallback_service_name_arabic(text: str) -> str:
    result = str(text or "").strip()
    for source, target in sorted(_IMPORT_TRANSLATIONS.items(), key=lambda item: -len(item[0])):
        result = re.sub(rf"(?<![A-Za-z]){re.escape(source)}(?![A-Za-z])", target, result, flags=re.IGNORECASE)
    result = re.sub(r"\s{2,}", " ", result).strip(" -|/،,;:")
    return result or "خدمة جديدة"

def _arabic_only_service_name(text: str) -> str:
    """يضمن أن الاسم المعروض لا يحتوي أي حرف لاتيني حتى مع فشل الترجمة."""
    result = _fallback_service_name_arabic(text)

    def replace_latin(match):
        token = match.group(0).casefold()
        known = _IMPORT_TRANSLATIONS.get(token)
        if known:
            return known
        return " ".join(_LATIN_LETTER_NAMES.get(letter, "") for letter in token).strip()

    result = re.sub(r"[A-Za-z]+", replace_latin, result)
    result = re.sub(r"\s{2,}", " ", result).strip(" -|/،,;:")
    return result or "خدمة جديدة"

def _translate_service_names_with_ai(names: list[str]) -> dict[str, str]:
    """يترجم أسماء الخدمات دفعة واحدة، مع بديل محلي عند غياب الذكاء الاصطناعي."""
    unique_names = list(dict.fromkeys(name for name in names if name))
    translated = {name: _arabic_only_service_name(name) for name in unique_names}
    if not unique_names:
        return translated

    providers = [
        (
            "gemini",
            os.environ.get("GEMINI_API_KEY", ""),
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            "gemini-2.0-flash",
        ),
        (
            "openai",
            os.environ.get("GROQ_API_KEY", ""),
            "https://api.groq.com/openai/v1/chat/completions",
            os.environ.get("GROQ_TEXT_MODEL", "llama-3.1-8b-instant"),
        ),
        (
            "openai",
            os.environ.get("DEEPSEEK_API_KEY", ""),
            "https://api.deepseek.com/chat/completions",
            "deepseek-chat",
        ),
        (
            "openai",
            get_openai_api_key(),
            "https://api.openai.com/v1/chat/completions",
            "gpt-4o-mini",
        ),
    ]
    for start in range(0, len(unique_names), 25):
        batch = unique_names[start:start + 25]
        payload_names = [{"id": index, "name": name} for index, name in enumerate(batch)]
        prompt = (
            "ترجم أسماء خدمات التسويق التالية إلى العربية ترجمة قصيرة وواضحة. "
            "أعد JSON فقط على شكل مصفوفة فيها id وname_ar، وحافظ على الأرقام "
            "والرموز بين الأقواس ولا تضف أسعاراً أو شرحاً جديداً.\n"
            + json.dumps(payload_names, ensure_ascii=False)
        )
        for provider_type, key, url, model in providers:
            if not key:
                continue
            try:
                if provider_type == "gemini":
                    response = requests.post(
                        url,
                        params={"key": key},
                        headers={"Content-Type": "application/json"},
                        json={
                            "contents": [{"parts": [{"text": (
                                "أنت مترجم خدمات SMM إلى العربية. أعد JSON فقط.\n" + prompt
                            )}]}],
                            "generationConfig": {"temperature": 0.1},
                        },
                        timeout=30,
                    )
                else:
                    response = requests.post(
                        url,
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "temperature": 0.1,
                            "messages": [
                                {"role": "system", "content": "أنت مترجم خدمات SMM إلى العربية. أعد JSON فقط."},
                                {"role": "user", "content": prompt},
                            ],
                        },
                        timeout=30,
                    )
                if response.status_code != 200:
                    continue
                response_data = response.json()
                if provider_type == "gemini":
                    content = (
                        response_data.get("candidates", [{}])[0]
                        .get("content", {})
                        .get("parts", [{}])[0]
                        .get("text", "")
                    )
                else:
                    content = response_data.get("choices", [{}])[0].get("message", {}).get("content", "")
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    parsed = [{"id": index, "name_ar": value} for index, value in parsed.items()]
                for item in parsed if isinstance(parsed, list) else []:
                    index = int(item.get("id", -1))
                    value = str(item.get("name_ar", "")).strip()
                    if 0 <= index < len(batch) and value:
                        translated[batch[index]] = _arabic_only_service_name(value)
                break
            except Exception as exc:
                logger.warning("تعذر ترجمة دفعة أسماء الخدمات عبر الذكاء الاصطناعي: %s", type(exc).__name__)
    return translated

def _prepare_import_services(services: list[dict]) -> list[dict]:
    for service in services:
        # اسم الموقع الأصلي لا يُترجم ولا يُعاد تشكيله. الاسم العربي
        # الذي يظهر للأعضاء يحدده المالك لاحقاً من إعدادات الخدمة.
        source_name = str(service.get("name", "") or service.get("type", "")).strip()
        service["source_name"] = source_name
        service["site_name"] = source_name
        service["clean_name"] = _import_service_text(service)
        service["name_ar"] = source_name or service["clean_name"]
    return services

def _service_matches_platform(service: dict, platform: str) -> bool:
    if platform == "ALL":
        return True
    haystack = " ".join(str(service.get(key, "") or "") for key in ("name", "type", "category")).casefold()
    return any(keyword.casefold() in haystack for keyword in _IMPORT_PLATFORM_KEYWORDS.get(platform, ()))

def _service_matches_category(service: dict, category: str) -> bool:
    if category == "other":
        haystack = " ".join(str(service.get(key, "") or "") for key in ("name", "type", "category")).casefold()
        return not any(keyword.casefold() in haystack for values in _IMPORT_CATEGORY_KEYWORDS.values() for keyword in values)
    haystack = " ".join(str(service.get(key, "") or "") for key in ("name", "type", "category")).casefold()
    return any(keyword.casefold() in haystack for keyword in _IMPORT_CATEGORY_KEYWORDS.get(category, ()))

async def _begin_import_services(update, context, q, panel: int, platform: str, category: str | None = None):
    await q.answer("⏳ جاري جلب الخدمات وترجمة أسمائها...", show_alert=False)
    services = await asyncio.to_thread(smm_services_list, panel, True)
    if not services:
        await q.edit_message_text(
            "❌ تعذر جلب الخدمات من الموقع.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data=f"import_services:panel:{panel}")
            ]]),
        )
        return

    services = [service for service in services if _service_matches_platform(service, platform)]
    if category:
        category_services = [service for service in services if _service_matches_category(service, category)]
        if category_services:
            services = category_services

    services = await asyncio.to_thread(_prepare_import_services, services)
    context.user_data["import_services_list"] = services
    context.user_data["import_selected_services"] = []
    context.user_data["import_page"] = 0
    context.user_data["import_panel"] = panel
    context.user_data["import_platform"] = platform
    context.user_data["import_target_category"] = category
    await show_import_services_list(update, context, q, panel)

async def cmd_import_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر المالك: /import_services - استيراد خدمات من مواقع SMM"""
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ هذا الأمر للمالك فقط.")
        return

    # عرض قائمة المواقع المتاحة
    await update.message.reply_text(
        "🌐 *استيراد الخدمات من المواقع*\n\n"
        "اختر الموقع الذي تريد استيراد الخدمات منه:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("1️⃣ SMMMAIN", callback_data="import_services:panel:1")],
            [InlineKeyboardButton("2️⃣ JustAnotherPanel", callback_data="import_services:panel:2")],
            [InlineKeyboardButton("3️⃣ SmmFollows", callback_data="import_services:panel:3")],
        ])
    )


async def handle_import_services_callback(update, context, q, data, user, is_own):
    """معالج أزرار استيراد الخدمات"""
    if not is_own:
        await q.answer("⛔ هذا الخيار للمالك فقط.", show_alert=True)
        return

    # ─── اختيار الموقع ───
    if data.startswith("import_services:panel:"):
        panel = int(data.split(":")[2])
        context.user_data["import_panel"] = panel
        site_name = PANEL_MAP.get(panel, PANEL_MAP[1])["name"]
        
        # عرض قائمة التطبيقات (المنصات)
        await q.edit_message_text(
            f"🌐 *استيراد الخدمات من {site_name}*\n\n"
            "اختر المنصة (التطبيق):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📱 تيليجرام", callback_data="import_services:platform:tg")],
                [InlineKeyboardButton("📸 انستغرام", callback_data="import_services:platform:ig")],
                [InlineKeyboardButton("🎵 تيك توك", callback_data="import_services:platform:tt")],
                [InlineKeyboardButton("💬 واتساب", callback_data="import_services:platform:wa")],
                [InlineKeyboardButton("📘 فيس بوك", callback_data="import_services:platform:fb")],
                [InlineKeyboardButton("▶️ يوتيوب", callback_data="import_services:platform:yt")],
                [InlineKeyboardButton("👻 سناب شات", callback_data="import_services:platform:sc")],
                [InlineKeyboardButton("🐦 تويتر", callback_data="import_services:platform:tw")],
                [InlineKeyboardButton("🔄 جميع المنصات", callback_data="import_services:platform:ALL")],
            ])
        )
        return

    # ─── اختيار المنصة ───
    if data.startswith("import_services:platform:"):
        platform = data.split(":")[2]
        panel = context.user_data.get("import_panel", 1)
        context.user_data["import_platform"] = platform
        context.user_data.pop("import_target_category", None)
        await _begin_import_services(update, context, q, panel, platform)
        return

    # ─── اختيار خدمة من القائمة ───
    if data.startswith("import_services:toggle:"):
        parts = data.split(":")
        if len(parts) < 3:
            return
        try:
            service_index = int(parts[2])
        except ValueError:
            return
        
        services = context.user_data.get("import_services_list", [])
        if service_index < 0 or service_index >= len(services):
            await q.answer("⚠️ الخدمة غير موجودة.", show_alert=True)
            return
        
        selected = context.user_data.get("import_selected_services", [])
        if service_index in selected:
            selected.remove(service_index)
        else:
            selected.append(service_index)
        context.user_data["import_selected_services"] = selected
        
        # تحديث عرض القائمة
        await show_import_services_list(update, context, q, context.user_data.get("import_panel", 1))
        return

    # ─── التنقل بين الصفحات ───
    if data == "import_services:page_next":
        context.user_data["import_page"] = context.user_data.get("import_page", 0) + 1
        await show_import_services_list(update, context, q, context.user_data.get("import_panel", 1))
        return

    if data == "import_services:page_prev":
        context.user_data["import_page"] = max(0, context.user_data.get("import_page", 1) - 1)
        await show_import_services_list(update, context, q, context.user_data.get("import_panel", 1))
        return

    # ─── إضافة الخدمات المحددة ───
    if data == "import_services:add_selected":
        selected = context.user_data.get("import_selected_services", [])
        if not selected:
            await q.answer("⚠️ لم تحدد أي خدمة.", show_alert=True)
            return
        
        # عرض تأكيد الإضافة
        await show_import_services_confirmation(update, context, q)
        return

    # ─── تأكيد الإضافة ───
    if data == "import_services:confirm":
        await import_selected_services(update, context, q)
        return

    # ─── إلغاء ───
    if data == "import_services:cancel":
        context.user_data.pop("import_services_list", None)
        context.user_data.pop("import_selected_services", None)
        context.user_data.pop("import_panel", None)
        context.user_data.pop("import_platform", None)
        context.user_data.pop("import_target_category", None)
        context.user_data.pop("import_page", None)
        await q.edit_message_text(
            "❌ تم إلغاء استيراد الخدمات.",
            reply_markup=owner_settings_kb()
        )
        return

    # ─── تعديل اسم خدمة ───
    if data.startswith("import_services:edit_name:"):
        parts = data.split(":")
        try:
            service_index = int(parts[2])
        except ValueError:
            return
        
        context.user_data["edit_import_service_index"] = service_index
        context.user_data["state"] = "os_await_import_service_name"
        
        services = context.user_data.get("import_services_list", [])
        if service_index < 0 or service_index >= len(services):
            await q.answer("⚠️ الخدمة غير موجودة.", show_alert=True)
            return
        
        service = services[service_index]
        current_name = str(service.get("name_ar") or service.get("clean_name") or service.get("name", ""))
        await q.edit_message_text(
            f"✏️ *تعديل اسم الخدمة فقط*\n\n"
            f"الاسم الحالي:\n{current_name}\n\n"
            "أرسل الاسم الجديد فقط.\n"
            "اسم الموقع الأصلي يبقى محفوظاً كما هو، وسيظهر الاسم الذي تكتبه للأعضاء "
            "دون تغيير رقم الخدمة أو السعر أو الحدود:",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if data == "import_services:resume":
        await show_import_services_list(
            update,
            context,
            q,
            context.user_data.get("import_panel", 1),
        )
        return


async def show_import_services_list(update, context, q, panel):
    """عرض قائمة الخدمات مع أزرار الاختيار"""
    services = context.user_data.get("import_services_list", [])
    selected = context.user_data.get("import_selected_services", [])
    platform = context.user_data.get("import_platform", "ALL")
    target_category = context.user_data.get("import_target_category")
    
    if not services:
        await q.edit_message_text(
            "❌ لا توجد خدمات متاحة.",
            reply_markup=owner_settings_kb()
        )
        return
    
    # تقسيم الخدمات إلى صفحات
    page_size = 10
    total_services = len(services)
    total_pages = max(1, (total_services + page_size - 1) // page_size)
    
    # الحصول على الصفحة الحالية من user_data
    current_page = context.user_data.get("import_page", 0)
    current_page = min(current_page, total_pages - 1)
    
    start = current_page * page_size
    end = min(start + page_size, total_services)
    
    category_line = (
        f"📂 الفئة: {CATEGORY_MAP.get(target_category, target_category)}\n"
        if target_category
        else ""
    )
    lines = [
        (
            f"📋 *الخدمات المتاحة* ({total_services} خدمة)\n"
            f"📍 المنصة: {platform}\n"
            f"{category_line}"
            f"📄 الصفحة {current_page + 1}/{total_pages}\n"
            f"✅ المحددة: {len(selected)}\n\n"
            "اضغط على الخدمة لتحديدها أو إلغاء تحديدها:"
        )
    ]
    
    buttons = []
    for i in range(start, end):
        service = services[i]
        cleaned_name = str(
            service.get("name_ar")
            or service.get("clean_name")
            or service.get("name", "")
        )
        marked = "✅" if i in selected else "⬜"
        buttons.append([
            InlineKeyboardButton(
                f"{marked} {cleaned_name}",
                callback_data=f"import_services:toggle:{i}"
            ),
            InlineKeyboardButton(
                "✏️",
                callback_data=f"import_services:edit_name:{i}",
            ),
        ])
    
    # أزرار التنقل
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data="import_services:page_prev"))
    if current_page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data="import_services:page_next"))
    if nav_buttons:
        buttons.append(nav_buttons)
    
    # أزرار الإجراءات
    action_buttons = []
    if selected:
        action_buttons.append(InlineKeyboardButton(f"✅ إضافة المحددة ({len(selected)})", callback_data="import_services:add_selected"))
    action_buttons.append(InlineKeyboardButton("🔙 رجوع للمواقع", callback_data="import_services:panel:" + str(panel)))
    action_buttons.append(InlineKeyboardButton("❌ إلغاء", callback_data="import_services:cancel"))
    
    buttons.append(action_buttons)
    
    await q.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_import_services_confirmation(update, context, q):
    """عرض تأكيد إضافة الخدمات المحددة"""
    selected = context.user_data.get("import_selected_services", [])
    services = context.user_data.get("import_services_list", [])
    panel = context.user_data.get("import_panel", 1)
    
    if not selected:
        return
    
    lines = ["📋 *تأكيد إضافة الخدمات*\n\n"]
    
    for index in selected:
        if index >= len(services):
            continue
        service = services[index]
        cleaned_name = str(
            service.get("name_ar")
            or service.get("clean_name")
            or service.get("name", "")
        )
        rate = float(service.get("rate", 0) or 0)
        min_qty = int(service.get("min", 0) or 0)
        max_qty = int(service.get("max", 0) or 0)
        
        # حساب السعر بالنقاط: 0.01 دولار = 1000 نقطة
        price_per_1000 = rate * 100000
        
        lines.append(f"• {cleaned_name}")
        lines.append(f"  └ 💰 {price_per_1000:.1f} نقطة/1000 | 📉 {min_qty} | 📈 {max_qty}")
        lines.append("")
    
    lines.append("هل تريد إضافة هذه الخدمات؟")
    
    await q.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأكيد الإضافة", callback_data="import_services:confirm")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="import_services:cancel")],
        ])
    )


async def import_selected_services(update, context, q):
    """إضافة الخدمات المحددة إلى قاعدة البيانات"""
    selected = context.user_data.get("import_selected_services", [])
    services = context.user_data.get("import_services_list", [])
    panel = context.user_data.get("import_panel", 1)
    platform = context.user_data.get("import_platform", "ALL")
    target_category = context.user_data.get("import_target_category")
    
    if not selected:
        return
    
    added = 0
    errors = []
    
    for index in selected:
        if index >= len(services):
            continue
        service = services[index]
        
        try:
            api_id = int(service.get("service", 0))
            cleaned_name = str(
                service.get("name_ar")
                or service.get("source_name")
                or service.get("name", "")
            ).strip()
            rate = float(service.get("rate", 0) or 0)
            min_qty = int(service.get("min", 0) or 0)
            max_qty = int(service.get("max", 0) or 0)
            desc = str(service.get("type", "") or service.get("source_name", ""))
            
            # حساب السعر بالنقاط: 0.01 دولار = 1000 نقطة
            price_per_1000 = rate * 100000
            
            # استخدم الفئة التي اختارها المالك، أو خمّنها في مسار الاستيراد العام.
            category = target_category or "other"
            if not target_category:
                service_name_lower = " ".join(
                    str(service.get(key, "") or "")
                    for key in ("name", "type", "category")
                ).casefold()
                for cat, keywords in _IMPORT_CATEGORY_KEYWORDS.items():
                    if any(keyword.casefold() in service_name_lower for keyword in keywords):
                        category = cat
                        break
            
            with db_conn() as c:
                existing = c.execute(
                    "SELECT id FROM services WHERE panel=%s AND api_service_id=%s",
                    (panel, api_id),
                ).fetchone()
                if existing:
                    c.execute(
                        """
                        UPDATE services
                        SET category=%s, platform=%s, source_name=%s, name_ar=%s, description=%s,
                            min_qty=%s, max_qty=%s, price_per_point=%s, active=TRUE
                        WHERE id=%s
                        """,
                        (
                            category,
                            platform,
                            str(service.get("source_name") or service.get("name") or cleaned_name),
                            cleaned_name,
                            desc,
                            min_qty,
                            max_qty,
                            price_per_1000,
                            existing["id"],
                        ),
                    )
                else:
                    c.execute(
                        """
                        INSERT INTO services
                            (category, api_service_id, panel, platform, source_name, name_ar, description,
                             min_qty, max_qty, price_per_point)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            category,
                            api_id,
                            panel,
                            platform,
                            str(service.get("source_name") or service.get("name") or cleaned_name),
                            cleaned_name,
                            desc,
                            min_qty,
                            max_qty,
                            price_per_1000,
                        ),
                    )
            added += 1
            
        except Exception as e:
            errors.append(f"خطأ في الخدمة #{index}: {str(e)}")
    
    # تنظيف حالة الاستيراد
    context.user_data.pop("import_services_list", None)
    context.user_data.pop("import_selected_services", None)
    context.user_data.pop("import_panel", None)
    context.user_data.pop("import_platform", None)
    context.user_data.pop("import_target_category", None)
    context.user_data.pop("import_page", None)
    
    result_text = f"✅ *تمت إضافة {added} خدمة بنجاح!*\n"
    if errors:
        result_text += f"\n⚠️ أخطاء: {len(errors)}\n"
        result_text += "\n".join(f"• {e}" for e in errors[:5])
    
    await q.edit_message_text(
        result_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=owner_settings_kb()
    )


async def handle_import_services_text(update, context):
    """معالجة النصوص الخاصة باستيراد الخدمات"""
    user = update.effective_user
    text = update.message.text
    state = context.user_data.get("state", "")
    
    # تعديل اسم خدمة
    if state == "os_await_import_service_name":
        if user.id != OWNER_ID:
            return False
        
        service_index = context.user_data.get("edit_import_service_index")
        services = context.user_data.get("import_services_list", [])
        
        if service_index is None or service_index >= len(services):
            context.user_data["state"] = "main_menu"
            await update.message.reply_text("⚠️ انتهت جلسة التعديل.")
            return True
        
        # الاسم العربي يكتبه المالك يدوياً؛ لا نترجم اسم الموقع تلقائياً.
        requested_name = text.strip()
        if not requested_name:
            await update.message.reply_text("⚠️ أرسل اسماً جديداً فقط.")
            return True
        new_name = requested_name
        services[service_index]["name_ar"] = new_name
        context.user_data["import_services_list"] = services
        context.user_data.pop("edit_import_service_index", None)
        context.user_data["state"] = "main_menu"
        
        await update.message.reply_text(
            f"✅ تم تعديل الاسم إلى: {new_name}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔙 رجوع لقائمة الخدمات",
                    callback_data="import_services:resume",
                )
            ]])
        )
        return True
    
    return False

if __name__ == "__main__":
    main()