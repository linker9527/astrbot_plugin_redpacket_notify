"""
AstrBot 红包提醒插件
"""

import sys
import re
import asyncio
import json

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, BaseMessageComponent, ComponentType


class GroupCard(BaseMessageComponent):
    """自定义群名片组件。

    AstrBot 自带的 Contact 组件用 _type 字段存类型，但 pydantic v2 会丢弃
    下划线开头的字段，导致序列化后 data 里缺 type，NapCat 会报
    `field "type" must be a non-empty string`。这里自定义组件绕开该问题。
    """

    type: ComponentType = ComponentType.Contact
    id: int = 0

    def __init__(self, group_id: int, **_) -> None:
        super().__init__(id=group_id)

    def toDict(self) -> dict:
        return {"type": "contact", "data": {"type": "group", "id": self.id}}

    async def to_dict(self) -> dict:
        return self.toDict()


def _fix_session(s: str) -> str:
    """自动转换 群聊→GroupMessage, 私聊→FriendMessage"""
    s = re.sub(r"群聊", "GroupMessage", re.sub(r"私聊", "FriendMessage", s))
    return s


def _parse_targets(raw) -> list[str]:
    """解析多个通知目标：兼容旧字符串（逗号/分号/换行分隔）与新列表格式"""
    if not raw:
        return []
    if isinstance(raw, list):
        parts = [str(p).strip() for p in raw]
    else:
        parts = re.split(r"[,\n;]+", str(raw).strip())
    return [p.strip() for p in parts if p.strip()]


@register(
    "红包提醒插件",
    "linker9527",
    "QQ红包提醒插件：群里有红包时通知你",
    "1.0.11",
    "https://github.com/linker9527/astrbot_plugin_redpacket_notify",
)
class RedPacketNotifyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        raw_targets = self.config.get("notify_target", [])
        self.notify_targets = [_fix_session(t) for t in _parse_targets(raw_targets)]
        self.monitor_all_groups = bool(self.config.get("monitor_all_groups", False))
        self._recent_redpackets = {}  # 去重：避免多平台重复触发
        self.watch_groups = self.config.get("watch_groups", [])
        self.notify_template = str(self.config.get("notify_template", ""))
        # 兼容字面 \n 与真实换行：统一转为真实换行
        self.notify_template = self.notify_template.replace("\\n", "\n")
        self.reset_template = str(self.config.get("reset_template", "不确定"))
        if not self.notify_template or self.reset_template == "确定":
            self.notify_template = "🧧红包提醒！\n群：{group_name} ({group_id})\n发送者：{sender_name} ({sender_id})\n\n快去抢！"
            # 如果是用户选了"确定"，用默认模板后把配置写回"不确定"
            if self.reset_template == "确定":
                logger.info("[红包提醒] 检测到重置请求，使用默认模板")
                try:
                    import os
                    config_path = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "config", "astrbot_plugin_redpacket_notify_config.json"
                    )
                    if os.path.exists(config_path):
                        with open(config_path, "r", encoding="utf-8-sig") as f:
                            cfg = json.load(f)
                        if cfg.get("reset_template") == "确定":
                            cfg["reset_template"] = "不确定"
                            # 同时恢复模板为默认值
                            cfg["notify_template"] = self.notify_template
                            with open(config_path, "w", encoding="utf-8-sig") as f:
                                json.dump(cfg, f, ensure_ascii=False, indent=2)
                            logger.info("[红包提醒] 已将重置选项恢复为「不确定」，模板已恢复默认")
                except Exception as e:
                    logger.warning(f"[红包提醒] 自动恢复重置选项失败: {e}")

        # 打印可用平台列表（诊断用）
        platforms = []
        for p in self.context.platform_manager.platform_insts:
            try:
                platforms.append(f"{p.meta().id}({p.meta().name})")
            except Exception:
                pass
        logger.info(f"[红包提醒] 加载完成，notify_targets={self.notify_targets}，可用平台: {platforms}")

    async def terminate(self):
        logger.info("[红包提醒] 停止")

    @filter.event_message_type(EventMessageType.ALL, priority=sys.maxsize - 5)
    async def on_message(self, event: AstrMessageEvent):
        msg = event.get_message_str()
        if not msg or "QQ红包" not in msg:
            return

        logger.info(f"[红包提醒] ★ 检测到红包: {msg}")

        # 去重：同一条红包消息 10 秒内只处理一次（避免多平台重复触发）
        import time
        now = time.time()
        dedup_key = msg
        if dedup_key in self._recent_redpackets and now - self._recent_redpackets[dedup_key] < 10:
            logger.info(f"[红包提醒] 去重跳过（{now - self._recent_redpackets[dedup_key]:.1f}秒内已处理过）")
            return
        self._recent_redpackets[dedup_key] = now
        # 清理过期记录
        self._recent_redpackets = {k: v for k, v in self._recent_redpackets.items() if now - v < 60}

        gid = event.get_group_id()
        sname = event.get_sender_name()
        sid = event.get_sender_id()

        if not self.monitor_all_groups and self.watch_groups and str(gid) not in [str(g) for g in self.watch_groups]:
            return

        # 获取群名
        group_name = "未知群"
        try:
            g = event.message_obj.group
            if g and g.group_name:
                group_name = g.group_name
        except Exception:
            pass

        notify_text = self.notify_template.format(
            group_name=group_name, group_id=gid, sender_name=sname, sender_id=sid
        )

        if self.notify_targets:
            for target in self.notify_targets:
                try:
                    from astrbot.core.platform.message_session import MessageSession
                    s = MessageSession.from_str(target)
                    logger.info(f"[红包提醒] 尝试发送到 {target}")
                    # 先发文字通知
                    ok = await asyncio.wait_for(
                        self.context.send_message(s, MessageChain([Plain(notify_text)])),
                        timeout=10.0
                    )
                    if ok:
                        logger.info(f"[红包提醒] 文字通知发送成功: {target}")
                        # 再发群名片（点击可跳转到群）
                        try:
                            gid_int = int(gid) if gid and gid.isdigit() else None
                            if gid_int:
                                await asyncio.wait_for(
                                    self.context.send_message(s, MessageChain([GroupCard(gid_int)])),
                                    timeout=10.0
                                )
                                logger.info(f"[红包提醒] 群名片发送成功: {target}")
                        except Exception as ce:
                            logger.warning(f"[红包提醒] 群名片发送失败(不影响通知): {ce}")
                    else:
                        logger.warning(f"[红包提醒] 发送失败: 未找到平台 '{s.platform_name}'，请检查 notify_target 配置中的平台名是否正确")
                except asyncio.TimeoutError:
                    logger.warning(f"[红包提醒] 发送超时(10s): {target}")
                except Exception as e:
                    logger.warning(f"[红包提醒] 发送异常: {target} -> {e}")

            # 方案A：发送失败不降级回复，静默处理