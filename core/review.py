"""机器人自主发说说前的管理员审核。

开启审核后, 机器人自己决定的说说不会直接发到 QQ 空间:

1. 先以 ``status=PENDING_BOT_STATUS`` 落库, 拿到稿件 ID;
2. 渲染成卡片, 连同「过稿 / 拒稿」提示发到管理群 (没配管理群则私聊管理员);
3. 管理员执行「过稿 <ID>」才真正发布, 「拒稿 <ID> 原因」则作废。

表白墙投稿也走「过稿 / 拒稿」, 但它有自己的刊头与投稿人通知逻辑,
由 main.py 按稿件状态分流, 这里只处理机器人待审稿件。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .config import PluginConfig
from .db import PostDB
from .model import PENDING_BOT_STATUS, Post
from .qzone import QzoneSession
from .sender import Sender
from .service import PostService

# 稿件来源 → 审核消息里的描述
SOURCE_NAMES = {
    "llm": "机器人在聊天中自己决定发说说",
    "auto": "定时自动写说说",
    "command": "管理员用「发说说」命令发说说",
}


class PublishReview:
    """机器人发说说的审核队列。"""

    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        db: PostDB,
        session: QzoneSession,
        sender: Sender,
    ):
        self.cfg = config
        self.service = service
        self.db = db
        self.session = session
        self.sender = sender

    # ==================== 配置 ====================

    @staticmethod
    def _flag(value: object, default: bool) -> bool:
        return default if value is None else bool(value)

    def _review_conf(self):
        """取审核配置节点。

        配置里没有 review 段时, PluginConfig 访问嵌套节点会抛 TypeError
        而不是返回 None, 这里统一兜成 None, 由调用方按默认值处理。
        """
        try:
            return getattr(self.cfg, "review", None)
        except Exception:
            return None

    def enabled(self, source: str) -> bool:
        """这条来源的说说是否需要审核。

        配置项缺失时按“默认开启”处理（llm / auto），避免配置没同步时
        悄悄退回“直接发布”。
        """
        review = self._review_conf()
        if review is None:
            return False
        if not self._flag(getattr(review, "enable", None), True):
            return False
        field = {
            "llm": "apply_llm",
            "auto": "apply_auto",
            "command": "apply_command",
        }.get(source)
        if field is None:
            return False
        return self._flag(getattr(review, field, None), source != "command")

    def notify_origin(self) -> bool:
        review = self._review_conf()
        return self._flag(getattr(review, "notify_origin", None), True)

    def admins_configured(self) -> bool:
        """有没有可以推送审核消息的收件人。"""
        return bool(self.cfg.manage_group or self.cfg.admins_id)

    @staticmethod
    def is_bot_draft(post: Post | None) -> bool:
        return bool(post and (post.status or "") == PENDING_BOT_STATUS)

    # ==================== 提交审核 ====================

    @staticmethod
    def _origin_group_id(event: AiocqhttpMessageEvent | None) -> int:
        if event is None:
            return 0
        try:
            return int(event.get_group_id() or 0)
        except (TypeError, ValueError, AttributeError):
            return 0

    def _review_message(self, post: Post, source: str) -> str:
        lines = [f"【待审核】{SOURCE_NAMES.get(source, '机器人发说说')}（稿件#{post.id}）"]
        if post.gin:
            lines.append(f"来源群：{post.gin}")
        lines.append(
            f"回复「过稿 {post.id}」发布，回复「拒稿 {post.id} 原因」作废"
        )
        return "\n".join(lines)

    async def submit(
        self,
        *,
        text: str = "",
        images: list[str] | None = None,
        event: AiocqhttpMessageEvent | None = None,
        source: str = "llm",
    ) -> str:
        """把待发内容交给管理员审核, 返回给调用方（AI / 命令）的提示文本。"""
        text = (text or "").strip()
        images = [str(img) for img in (images or []) if img]
        if not text and not images:
            return "说说内容为空, 已取消"

        if not self.admins_configured():
            logger.error("QQ空间审核: 未配置管理群或管理员, 无法发送审核消息")
            return "发说说失败: 插件未配置管理群/管理员, 无法提交审核"

        client = getattr(event, "bot", None) or self.cfg.client
        if client is None:
            # 拿不到客户端就没法通知管理员, 宁可不发也不绕过审核
            logger.error("QQ空间审核: 缺少客户端, 无法通知管理员, 已跳过发布")
            return "发说说失败: 机器人未连接, 无法通知管理员审核"

        try:
            uin = int(await self.session.get_uin() or 0)
        except Exception:
            uin = 0
        try:
            name = await self.session.get_nickname() or ""
        except Exception:
            name = ""

        post = Post(
            uin=uin,
            name=name,
            gin=self._origin_group_id(event),
            text=text,
            images=images,
            status=PENDING_BOT_STATUS,
        )
        await self.db.save(post)

        await self.sender.send_admin_post(
            post,
            client=client,
            message=self._review_message(post, source),
        )
        logger.info(f"QQ空间审核: 稿件#{post.id} 已提交管理员审核（来源 {source}）")

        if source == "auto":
            return f"定时说说已提交管理员审核（稿件#{post.id}）"
        return (
            f"已提交管理员审核（稿件#{post.id}）, 目前还没有发到 QQ 空间。\n"
            f"管理员执行「过稿 {post.id}」后才会真正发布, 「拒稿 {post.id} 原因」则作废。\n"
            "请如实告诉用户: 这条说说已交给管理员审核, 不要说已经发布。"
        )

    # ==================== 审核结果 ====================

    @staticmethod
    def _reject_reason(event: AiocqhttpMessageEvent, post_id: object) -> str:
        """从「拒稿 <ID> <原因>」里取原因, 兼容各种命令别名。"""
        parts = (event.message_str or "").strip().split(maxsplit=2)
        if len(parts) >= 3 and parts[1] == str(post_id):
            return parts[2].strip()
        return ""

    async def approve(self, event: AiocqhttpMessageEvent, post: Post) -> str:
        if post.status == "approved":
            return f"稿件#{post.id}已通过，请勿重复通过"
        if post.status == "rejected":
            return f"稿件#{post.id}已被拒绝，无法再通过"
        if not self.is_bot_draft(post):
            return f"稿件#{post.id}不是待审核的机器人说说"

        try:
            published = await self.service.publish_post(post=post)
        except Exception as e:
            logger.error(f"审核通过后发布失败：{e}")
            return f"稿件#{post.id}发布失败：{e}"

        await self._notify_origin(
            published,
            f"管理员已审核通过，说说#{post.id}已发布",
            event=event,
        )
        logger.info(f"QQ空间审核: 稿件#{post.id} 审核通过并已发布")
        return f"稿件#{post.id}已通过并发布"

    async def reject(self, event: AiocqhttpMessageEvent, post: Post) -> str:
        if post.status == "approved":
            return f"稿件#{post.id}已发布，无法拒绝"
        if post.status == "rejected":
            return f"稿件#{post.id}已拒绝，请勿重复拒绝"
        if not self.is_bot_draft(post):
            return f"稿件#{post.id}不是待审核的机器人说说"

        reason = self._reject_reason(event, post.id)
        post.status = "rejected"
        if reason:
            post.extra_text = reason
        await self.db.save(post)

        await self._notify_origin(
            post,
            "管理员拒绝了这条说说，内容不会发布",
            event=event,
        )
        logger.info(f"QQ空间审核: 稿件#{post.id} 已拒绝")
        msg = f"稿件#{post.id}已拒绝"
        if reason:
            msg += f"\n理由：{reason}"
        return msg

    async def _notify_origin(
        self,
        post: Post,
        message: str,
        *,
        event: AiocqhttpMessageEvent | None = None,
    ) -> None:
        """把审核结果发回机器人当初被触发的群聊。"""
        if not self.notify_origin() or not post.gin:
            return
        try:
            approved_in = str(event.get_group_id() or "") if event is not None else ""
        except Exception:
            approved_in = ""
        # 管理员就在这个群里审核的, 卡片已经看过了, 不用重复发
        if approved_in and approved_in == str(post.gin):
            return

        client = getattr(event, "bot", None) or self.cfg.client
        try:
            await self.sender.send_post_to(
                post,
                client=client,
                group_id=int(post.gin),
                message=message,
            )
        except Exception as e:
            logger.error(f"审核结果回发群聊 {post.gin} 失败：{e}")

    # ==================== 待审列表 ====================

    async def list_pending_text(self, limit: int = 10) -> str:
        posts = await self.db.list_bot_drafts(limit=limit)
        if not posts:
            return "当前没有待审核的说说"
        lines = [f"待审核的机器人说说共 {len(posts)} 条："]
        for post in posts:
            preview = " ".join((post.text or "").split())
            if len(preview) > 60:
                preview = preview[:60] + "…"
            if not preview and post.images:
                preview = f"[{len(post.images)} 张图片]"
            lines.append(f"#{post.id}  {preview}")
        lines.append("回复「过稿 <ID>」发布，或「拒稿 <ID> 原因」作废")
        return "\n".join(lines)
