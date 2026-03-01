"""
Telegram Bot 服务
提供 TG Bot 上车功能，用户通过 Telegram 即可使用所有服务
"""
import logging
import asyncio
from datetime import datetime, date
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import TelegramUser, Team, WaitingRoom, RedemptionCode

logger = logging.getLogger(__name__)


# ──────────── 辅助：获取或自动注册 TG 用户 ────────────

async def _get_or_create_user(
    session: AsyncSession, tg_user
) -> TelegramUser:
    """首次使用自动注册，后续直接返回"""
    result = await session.execute(
        select(TelegramUser).where(
            TelegramUser.tg_user_id == str(tg_user.id)
        )
    )
    user = result.scalar_one_or_none()
    if user:
        # 更新基本信息
        user.username = tg_user.username or user.username
        user.first_name = tg_user.first_name or user.first_name
        user.last_name = tg_user.last_name or user.last_name
        await session.commit()
        return user

    user = TelegramUser(
        tg_user_id=str(tg_user.id),
        username=tg_user.username or "",
        first_name=tg_user.first_name or "",
        last_name=tg_user.last_name or "",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    logger.info("TG 用户自动注册: %s (%s)", tg_user.id, tg_user.username)
    return user


# ──────────── 主菜单键盘 ────────────

def _main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆓 免费上车", callback_data="free_spots"),
            InlineKeyboardButton("🎫 兑换码上车", callback_data="redeem_start"),
        ],
        [
            InlineKeyboardButton("🚌 候车室", callback_data="waiting_room"),
            InlineKeyboardButton("📧 绑定邮箱", callback_data="bind_email"),
        ],
        [
            InlineKeyboardButton("💰 每日签到", callback_data="sign_in"),
            InlineKeyboardButton("👤 我的信息", callback_data="my_info"),
        ],
    ])


# ──────────── 命令处理器 ────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令 — 自动注册并显示主菜单"""
    async with AsyncSessionLocal() as session:
        user = await _get_or_create_user(session, update.effective_user)

    display = user.first_name or user.username or f"用户{user.tg_user_id}"
    await update.message.reply_text(
        f"👋 你好 {display}！欢迎使用 GPT Team 管理系统\n\n"
        "请选择以下操作：",
        reply_markup=_main_menu_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /help 命令"""
    await update.message.reply_text(
        "📖 *可用命令*\n\n"
        "/start \\- 主菜单\n"
        "/free \\- 查看免费车位\n"
        "/redeem \\- 使用兑换码上车\n"
        "/wait \\- 加入候车室\n"
        "/bindmail \\- 绑定邮箱\n"
        "/signin \\- 每日签到\n"
        "/me \\- 我的信息\n"
        "/help \\- 帮助",
        parse_mode="MarkdownV2",
    )


async def cmd_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看免费车位"""
    await _show_free_spots(update.message, update.effective_user)


async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """使用兑换码上车"""
    async with AsyncSessionLocal() as session:
        user = await _get_or_create_user(session, update.effective_user)
    if not user.email:
        await update.message.reply_text(
            "⚠️ 请先绑定邮箱才能使用兑换码\n\n"
            "发送 /bindmail 你的邮箱  来绑定\n"
            "例如: /bindmail user@example.com"
        )
        return
    context.user_data["state"] = "waiting_redeem_code"
    await update.message.reply_text(
        "🎫 请发送你的兑换码：\n（直接输入兑换码文本即可）"
    )


async def cmd_wait(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """加入候车室"""
    await _join_waiting_room(update.message, update.effective_user)


async def cmd_bindmail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """绑定邮箱"""
    args = context.args
    if not args:
        await update.message.reply_text(
            "📧 用法: /bindmail 你的邮箱\n"
            "例如: /bindmail user@example.com"
        )
        return

    email = args[0].strip().lower()
    if "@" not in email or "." not in email:
        await update.message.reply_text("❌ 邮箱格式不正确，请重新输入")
        return

    async with AsyncSessionLocal() as session:
        user = await _get_or_create_user(session, update.effective_user)
        user.email = email
        await session.commit()

    await update.message.reply_text(f"✅ 邮箱已绑定: {email}")


async def cmd_signin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """每日签到"""
    await _do_sign_in(update.message, update.effective_user)


async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """我的信息"""
    await _show_my_info(update.message, update.effective_user)


# ──────────── 回调查询处理器 ────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理内联键盘回调"""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "main_menu":
        await query.edit_message_text(
            "请选择以下操作：",
            reply_markup=_main_menu_keyboard(),
        )
    elif data == "free_spots":
        await _show_free_spots(query.message, update.effective_user, edit=True)
    elif data == "redeem_start":
        async with AsyncSessionLocal() as session:
            user = await _get_or_create_user(session, update.effective_user)
        if not user.email:
            await query.edit_message_text(
                "⚠️ 请先绑定邮箱才能使用兑换码\n\n"
                "发送 /bindmail 你的邮箱  来绑定",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")]
                ]),
            )
            return
        context.user_data["state"] = "waiting_redeem_code"
        await query.edit_message_text(
            "🎫 请发送你的兑换码：\n（直接输入兑换码文本即可）",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")]
            ]),
        )
    elif data == "waiting_room":
        await _join_waiting_room(query.message, update.effective_user, edit=True)
    elif data == "bind_email":
        await query.edit_message_text(
            "📧 请发送绑定邮箱命令：\n/bindmail 你的邮箱\n\n例如: /bindmail user@example.com",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")]
            ]),
        )
    elif data == "sign_in":
        await _do_sign_in(query.message, update.effective_user, edit=True)
    elif data == "my_info":
        await _show_my_info(query.message, update.effective_user, edit=True)
    elif data.startswith("join_free_"):
        team_id = int(data.replace("join_free_", ""))
        await _join_free_team(query.message, update.effective_user, team_id)


# ──────────── 文本消息处理器 ────────────

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理普通文本消息（兑换码输入等）"""
    state = context.user_data.get("state")

    if state == "waiting_redeem_code":
        context.user_data.pop("state", None)
        code = update.message.text.strip()
        await _do_redeem(update.message, update.effective_user, code)
        return

    # 默认回复
    await update.message.reply_text(
        "请使用菜单或命令操作 👇\n发送 /start 打开主菜单",
    )


# ──────────── 业务逻辑函数 ────────────

async def _show_free_spots(message, tg_user, edit=False):
    """显示免费车位列表"""
    from app.services.team import TeamService
    team_service = TeamService()

    async with AsyncSessionLocal() as session:
        await _get_or_create_user(session, tg_user)
        result = await team_service.get_free_spot_teams(session)

    if not result.get("success") or not result.get("teams"):
        text = "😔 当前没有可用的免费车位\n\n可以加入候车室等待通知"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚌 加入候车室", callback_data="waiting_room")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")],
        ])
    else:
        lines = ["🆓 *可用免费车位*\n"]
        buttons = []
        for t in result["teams"]:
            avail = t["max_members"] - t["current_members"]
            lines.append(
                f"• {t['team_name']}  ({t['current_members']}/{t['max_members']}，"
                f"剩余 {avail})"
            )
            buttons.append([InlineKeyboardButton(
                f"🚀 上车 {t['team_name']}",
                callback_data=f"join_free_{t['id']}",
            )])
        buttons.append([InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")])
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(buttons)

    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.reply_text(text, reply_markup=kb)


async def _join_free_team(message, tg_user, team_id: int):
    """加入免费车位"""
    from app.services.team import team_service

    async with AsyncSessionLocal() as session:
        user = await _get_or_create_user(session, tg_user)

        if not user.email:
            await message.edit_text(
                "⚠️ 请先绑定邮箱才能上车\n\n"
                "发送 /bindmail 你的邮箱",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")]
                ]),
            )
            return

        # 检查 Team
        team_result = await session.execute(select(Team).where(Team.id == team_id))
        team = team_result.scalar_one_or_none()

        if not team:
            await message.edit_text("❌ 该车位不存在",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ 返回", callback_data="free_spots")]
                ]))
            return

        if not team.is_free_spot or team.is_exclusive:
            await message.edit_text("❌ 该车位不可用",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ 返回", callback_data="free_spots")]
                ]))
            return

        if team.current_members >= team.max_members:
            await message.edit_text("❌ 该车位已满",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ 返回", callback_data="free_spots")]
                ]))
            return

        join_result = await team_service.add_team_member(team.id, user.email, session)

    if join_result.get("success"):
        await message.edit_text(
            f"✅ 上车成功！请查收 {user.email} 的邀请邮件\n\n"
            f"Team: {team.team_name}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")]
            ]),
        )
    else:
        await message.edit_text(
            f"❌ 上车失败: {join_result.get('error', '未知错误')}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 返回", callback_data="free_spots")]
            ]),
        )


async def _join_waiting_room(message, tg_user, edit=False):
    """加入候车室"""
    from app.services.waiting_room import waiting_room_service

    async with AsyncSessionLocal() as session:
        user = await _get_or_create_user(session, tg_user)

        if not user.email:
            text = "⚠️ 请先绑定邮箱才能加入候车室\n\n发送 /bindmail 你的邮箱"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")]
            ])
            if edit:
                await message.edit_text(text, reply_markup=kb)
            else:
                await message.reply_text(text, reply_markup=kb)
            return

        result = await waiting_room_service.join(session, user.email)

    text = f"{'✅' if result.get('success') else '❌'} {result.get('message', '')}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")]
    ])
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.reply_text(text, reply_markup=kb)


async def _do_redeem(message, tg_user, code: str):
    """使用兑换码上车"""
    from app.services.redeem_flow import redeem_flow_service

    async with AsyncSessionLocal() as session:
        user = await _get_or_create_user(session, tg_user)

        if not user.email:
            await message.reply_text("⚠️ 请先绑定邮箱: /bindmail 你的邮箱")
            return

        # 验证兑换码
        verify = await redeem_flow_service.verify_code_and_get_teams(code, session)
        if not verify.get("success") or not verify.get("valid"):
            reason = verify.get("reason") or verify.get("error") or "兑换码无效"
            await message.reply_text(f"❌ {reason}")
            return

        # 自动选择 Team 并兑换
        result = await redeem_flow_service.redeem_and_join_team(
            user.email, code, None, session
        )

    if result.get("success"):
        team_info = result.get("team_info", {})
        await message.reply_text(
            f"✅ 上车成功！\n\n"
            f"Team: {team_info.get('team_name', '-')}\n"
            f"邮箱: {user.email}\n\n"
            f"请查收邀请邮件",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")]
            ]),
        )
    else:
        await message.reply_text(
            f"❌ 兑换失败: {result.get('error', '未知错误')}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")]
            ]),
        )


async def _do_sign_in(message, tg_user, edit=False):
    """每日签到"""
    from app.config import settings as app_settings
    daily_points = int(app_settings.user_daily_signin_points)

    async with AsyncSessionLocal() as session:
        user = await _get_or_create_user(session, tg_user)

        today = date.today()
        if user.last_sign_in_at and user.last_sign_in_at.date() == today:
            text = f"⏰ 今天已经签到过了\n当前积分: {user.points}"
        else:
            user.points += daily_points
            user.last_sign_in_at = datetime.now()
            await session.commit()
            text = f"✅ 签到成功！+{daily_points} 积分\n当前积分: {user.points}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")]
    ])
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.reply_text(text, reply_markup=kb)


async def _show_my_info(message, tg_user, edit=False):
    """显示用户信息"""
    async with AsyncSessionLocal() as session:
        user = await _get_or_create_user(session, tg_user)

    display = user.first_name or user.username or str(user.tg_user_id)
    email_str = user.email or "未绑定"
    sign_in_str = (
        user.last_sign_in_at.strftime("%Y-%m-%d %H:%M")
        if user.last_sign_in_at else "从未签到"
    )
    reg_str = (
        user.created_at.strftime("%Y-%m-%d %H:%M")
        if user.created_at else "-"
    )

    text = (
        f"👤 *{display}*\n\n"
        f"TG ID: `{user.tg_user_id}`\n"
        f"邮箱: {email_str}\n"
        f"积分: {user.points}\n"
        f"最后签到: {sign_in_str}\n"
        f"注册时间: {reg_str}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="main_menu")]
    ])
    if edit:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


# ──────────── Bot 生命周期管理 ────────────

_bot_app: Optional[Application] = None
_bot_task: Optional[asyncio.Task] = None


async def start_bot(token: str):
    """启动 Telegram Bot（后台任务）"""
    global _bot_app, _bot_task

    if _bot_app is not None:
        logger.warning("TG Bot 已在运行，跳过重复启动")
        return

    try:
        app = Application.builder().token(token).build()

        # 注册命令处理器
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("free", cmd_free))
        app.add_handler(CommandHandler("redeem", cmd_redeem))
        app.add_handler(CommandHandler("wait", cmd_wait))
        app.add_handler(CommandHandler("bindmail", cmd_bindmail))
        app.add_handler(CommandHandler("signin", cmd_signin))
        app.add_handler(CommandHandler("me", cmd_me))

        # 回调查询
        app.add_handler(CallbackQueryHandler(callback_handler))

        # 文本消息
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

        _bot_app = app

        # 设置命令菜单
        await app.bot.set_my_commands([
            BotCommand("start", "主菜单"),
            BotCommand("free", "查看免费车位"),
            BotCommand("redeem", "使用兑换码上车"),
            BotCommand("wait", "加入候车室"),
            BotCommand("bindmail", "绑定邮箱"),
            BotCommand("signin", "每日签到"),
            BotCommand("me", "我的信息"),
            BotCommand("help", "帮助"),
        ])

        logger.info("Telegram Bot 正在启动 (polling)...")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram Bot 已启动")

    except Exception as e:
        logger.error("Telegram Bot 启动失败: %s", e)
        _bot_app = None


async def stop_bot():
    """停止 Telegram Bot"""
    global _bot_app
    if _bot_app is None:
        return
    try:
        logger.info("正在停止 Telegram Bot...")
        await _bot_app.updater.stop()
        await _bot_app.stop()
        await _bot_app.shutdown()
        logger.info("Telegram Bot 已停止")
    except Exception as e:
        logger.error("停止 Telegram Bot 出错: %s", e)
    finally:
        _bot_app = None


def is_running() -> bool:
    """Bot 是否正在运行"""
    return _bot_app is not None
