"""
main.py —— Roblox 全功能查询插件（AstrBot 版）

由 NoneBot2 插件 nonebot_plugin_roblox_search (v1.3.5) 迁移并增强而来。
在保留原插件全部查询功能的基础上：
- 重新设计了输出格式：QQ 官方机器人用户/游戏查询使用原生 Markdown，OneBot 等平台使用信息卡
- 修复原插件 bug：群组/游戏名搜索未做 URL 编码（中文/空格关键词会失败）、
  游戏 ID 查询不兼容 universeId、同步 requests 阻塞事件循环等
- 增强：认证徽章、OMNI 游戏搜索、游戏点赞/类型、群主ID、搜索结果候选提示、长度保护

功能命令（均可带 / 前缀触发，无斜杠触发可在配置中关闭）：
    菜单 / 帮助 / menu          查看功能菜单（图片）
    用户名搜索 [用户名]          根据用户名查询用户完整资料
    用户ID搜索 [数字ID]          按用户ID查询用户完整资料
    群组名搜索 [群组名]          模糊搜索群组并展示详情
    群组ID搜索 [数字ID]          查询群组详情与职位列表
    游戏名搜索 [游戏名]          OMNI 搜索游戏并展示详情
    游戏ID搜索 [数字ID]          按 Universe ID / 地点 ID 查询游戏详情
    获取好友列表 [用户ID] [页码]  读取用户好友（每页10个，可翻页）
    获取粉丝列表 [用户ID] [页码]  读取用户粉丝（每页10个）
    获取关注列表 [用户ID] [页码]  读取用户关注（每页10个）
    获取徽章列表 [用户ID]        读取用户获得的 Roblox 官方徽章
"""

import asyncio
import html
import os
import tempfile
import time
import traceback
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from dateutil.relativedelta import relativedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

from .roblox_api import (
    RobloxAPIError,
    close_client,
    download_image,
    get_avatar_url,
    get_follower_count,
    get_followers,
    get_following_count,
    get_followings,
    get_friend_count,
    get_friends,
    get_game_icon,
    get_game_info,
    get_game_info_by_universe,
    get_game_votes,
    get_group_icon,
    get_group_info,
    get_group_roles,
    get_headshot_url,
    get_user_badges,
    get_user_details,
    get_user_groups,
    get_user_presence,
    search_game,
    search_group,
    search_user,
    set_base_domain,
)
from .render_utils import GAME_CARD_TEMPLATE, USER_CARD_TEMPLATE, menu_to_image

# 非白名单群的拒绝提示（与原插件一致）
WHITELIST_MSG = "此群未获得账号所有者的允许，未开放此群白名单，暂时不开使用，请联系账号所有者"

# 单条文本输出最大长度（QQ/Telegram 等平台消息上限约 4000+，留出余量）
MAX_TEXT_LEN = 3500

# 已生成待发送的临时图片文件（发送后由定期清理 / 插件停用时统一删除，避免堆积）
_TEMP_FILES: list[tuple[str, float]] = []
_TEMP_FILE_MAX_AGE = 3600  # 1 小时


def _track_temp_file(path: str) -> None:
    """登记临时文件，并顺带清理超过 1 小时的旧文件"""
    now = time.time()
    _TEMP_FILES.append((path, now))
    for p, created in list(_TEMP_FILES):
        if now - created > _TEMP_FILE_MAX_AGE:
            try:
                os.remove(p)
            except OSError:
                pass
            _TEMP_FILES.remove((p, created))


def _cleanup_temp_files() -> None:
    """删除全部登记的临时文件（插件停用时调用）"""
    for p, _ in _TEMP_FILES:
        try:
            os.remove(p)
        except OSError:
            pass
    _TEMP_FILES.clear()


def _truncate(text: str, max_len: int = MAX_TEXT_LEN) -> str:
    """超长文本截断保护，防止超出平台消息长度限制"""
    if len(text) > max_len:
        return text[:max_len] + "\n...（内容过长，已截断）"
    return text


def _parse_param(event: AstrMessageEvent, keyword: str) -> str:
    """从消息中剥离命令关键词，返回剩余参数。

    兼容两种情况：消息含完整命令（"/用户名搜索 Roblox" / "用户名搜索 Roblox"），
    或框架已剥离命令（只剩 "Roblox"）。
    """
    msg = event.message_str.strip()
    if msg.startswith("/"):
        msg = msg[1:]
    if msg.startswith(keyword):
        return msg[len(keyword):].strip()
    return msg


def _parse_date(s: str):
    """解析 Roblox API 的 ISO 时间字符串（统一视为 UTC），失败返回 None

    兼容：3/6 位小数秒、7 位及以上小数秒（旧版 Python 的 fromisoformat 不认）、
    无小数秒、无 Z 后缀等变体。返回带 UTC 时区信息的 datetime。
    """
    if not s:
        return None
    s = s.strip()
    dt = None
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ")
    except Exception:
        t = s.replace("Z", "")
        try:
            dt = datetime.fromisoformat(t)
        except Exception:
            if "." in t:
                base, frac = t.split(".", 1)
                frac = (frac + "000000")[:6]  # 截断到 6 位小数秒
                try:
                    dt = datetime.fromisoformat(f"{base}.{frac}")
                except Exception:
                    return None
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc)


def _fmt_date(dt) -> str:
    """UTC 时间转本地时区后格式化为日期字符串；无效返回“未知”"""
    return dt.astimezone().strftime("%Y-%m-%d") if dt else "未知"


def _calc_age(created_dt):
    """计算注册时长，返回 (注册日期字符串, 时长描述)；传入 None 返回两个空串。

    日期按本地时区展示，时长用 UTC 对 UTC 计算，避免时区混用导致的偏差。
    """
    if not created_dt:
        return "", ""
    now = datetime.now(timezone.utc)
    created_date = created_dt.astimezone().strftime("%Y-%m-%d")
    delta = relativedelta(now, created_dt)
    total_days = (now - created_dt).days
    age_info = f"{delta.years}年{delta.months}个月{delta.days}天（共{total_days}天）"
    return created_date, age_info


def _parse_presence(status):
    """解析在线状态，返回 (在线状态文本, 位置)"""
    online_status = "离线"
    location = "无"
    if status and isinstance(status, dict):
        ptype = status.get("userPresenceType")
        if ptype == 2:
            online_status = "在线"
        elif ptype == 3:
            online_status = "游戏中"
        elif ptype == 4:
            online_status = "工作室中"
        if status.get("lastLocation"):
            location = status["lastLocation"]
    return online_status, location


def _escape_qq_markdown(value) -> str:
    """转义来自 Roblox API 的动态文本，避免破坏 QQ Markdown 排版。"""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    escaped = "\\`*_{}[]<>()#+-.!|~"
    return "".join(f"\\{char}" if char in escaped else char for char in text)


def _is_safe_markdown_image_url(url: str) -> bool:
    """仅允许可由 QQ 服务端拉取的公网 HTTPS 图片 URL。"""
    if not url or any(ord(char) < 32 for char in url):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or any(char.isspace() for char in url):
        return False
    blocked_hosts = {"localhost", "::1", "0.0.0.0"}
    if host in blocked_hosts or host.startswith(("127.", "10.", "192.168.", "169.254.")):
        return False
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) > 1 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return False
    return True


def _build_qq_user_markdown(
    raw_name: str,
    display_name: str,
    user_id: int,
    created_date: str,
    age_info: str,
    friend_count: str,
    follower_count: str,
    following_count: str,
    online_status: str,
    location: str,
    is_banned: bool,
    verified: bool | None,
    description: str,
    groups: list,
    headshot_image: str = "",
    avatar_image: str = "",
) -> str:
    """构造 QQ 官方机器人原生 Markdown 用户资料。"""
    inline = lambda value: _escape_qq_markdown(value).replace("\n", " ")
    lines = ["# 罗布乐思（Roblox）档案"]
    if headshot_image and avatar_image:
        # 表格内部必须使用单换行；整条 Markdown 消息的其他段落再用空行分隔。
        lines.append("\n".join((
            "| 头像 | Roblox形象 |",
            "| :--: | :--: |",
            f"| {headshot_image} | {avatar_image} |",
        )))
    elif headshot_image:
        lines.append(headshot_image)
    elif avatar_image:
        lines.append(avatar_image)
    if display_name and display_name != raw_name:
        lines.append(f"**用户名：** {inline(display_name)} `(@{inline(raw_name)})`")
    else:
        lines.append(f"**用户名：** `{inline(raw_name)}`")
    lines.extend([
        f"{following_count} 关注 ｜ {follower_count} 粉丝 ｜ {friend_count} 好友",
        f"**用户 ID：** `{user_id}`",
    ])
    if created_date:
        lines.extend([
            f"**注册日期：** `{created_date}`",
            f"**注册时长：** {inline(age_info)}",
        ])
    lines.extend([
        f"**在线状态：** {inline(online_status)}",
        f"**当前位置：** {inline(location)}",
        f"**是否封禁：** {'是' if is_banned else '否'}",
    ])
    if verified is not None:
        lines.append(f"**已认证：** {'是' if verified else '否'}")

    desc = inline(description[:500]) if description else "无"
    if description and len(description) > 500:
        desc += "……"
    lines.append(f"**用户简介：** {desc}")

    lines.append("**群组列表：**")
    if groups:
        for group in groups[:5]:
            group_name = inline(group.get("group", {}).get("name", "未知"))
            role = inline(group.get("role", {}).get("name", "未知"))
            gid = group.get("group", {}).get("id", 0)
            lines.append(f"- **{group_name}**（职位：{role}，ID：`{gid}`）")
    else:
        lines.append("- 暂无公开群组")
    return _truncate("\n\n".join(lines))


async def _download_to_temp(url: str, suffix: str = ".png") -> str | None:
    """下载图片到临时文件，返回路径；失败返回 None"""
    try:
        data = await download_image(url)
        if not data:
            return None
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _track_temp_file(path)
        return path
    except Exception:
        return None


async def _menu_to_temp() -> str | None:
    """生成菜单图片并写入临时文件，返回路径；失败返回 None"""
    try:
        data = await menu_to_image()
        fd, path = tempfile.mkstemp(suffix=".png")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _track_temp_file(path)
        return path
    except Exception as e:
        logger.error(f"[Roblox] 菜单生成失败: {e}")
        return None


class RobloxSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 群白名单：留空 = 所有群全开放；配置后仅白名单内群可用查询功能（菜单始终可用），私聊不受限制
        raw_whitelist = config.get("search_white_list", []) or []
        self.whitelist = [str(x) for x in raw_whitelist]
        # 是否支持无斜杠触发命令
        self.allow_no_slash = bool(config.get("allow_no_slash_command", True))
        # 机器人平台类型：auto 自动识别 / onebot / qq_official
        self.platform_type = str(config.get("platform_type", "auto") or "auto").strip().lower()
        # 数据源代理域名（默认 rotunnel.com，可换 roproxy.com / 自建反代）
        set_base_domain(str(config.get("api_base_domain", "") or ""))
        # QQ Markdown 外链图片代理；留空时直接使用 Roblox CDN URL。
        self.qq_markdown_image_proxy = str(
            config.get("qq_markdown_image_proxy", "https://wsrv.nl/") or ""
        ).strip()
        # 查询冷却：同一群内同一用户两次查询的最小间隔秒数，0 = 不限制
        try:
            self.cooldown = max(0, int(config.get("query_cooldown", 5) or 0))
        except (TypeError, ValueError):
            self.cooldown = 5
        self._last_query: dict[tuple[str, str], float] = {}
        self._no_slash_handlers = {
            "用户名搜索": self.username_search,
            "用户ID搜索": self.user_id_search,
            "群组名搜索": self.group_name_search,
            "群组ID搜索": self.group_id_search,
            "游戏名搜索": self.game_name_search,
            "游戏ID搜索": self.game_id_search,
            "获取好友列表": self.friends_list,
            "获取粉丝列表": self.followers_list,
            "获取关注列表": self.followings_list,
            "获取徽章列表": self.badges_list,
        }
        if self.whitelist:
            logger.info(f"[Roblox全功能查询] 已加载群白名单: {self.whitelist}（仅白名单群可用查询功能）")
        else:
            logger.info("[Roblox全功能查询] 未配置群白名单，所有群均可使用查询功能")

    def _check_whitelist(self, event: AstrMessageEvent) -> bool:
        """私聊放行；群聊时未配置白名单则放行，配置了则须在白名单内"""
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return True  # 私聊放行
        if not self.whitelist:
            return True  # 未配置白名单 = 全开放
        return group_id in self.whitelist

    def _check_cooldown(self, event: AstrMessageEvent) -> float:
        """查询冷却检查；返回剩余冷却秒数（0 表示可查询），通过时记录本次时间。

        按（群号/私聊, 发送者）维度限频，防止刷屏打爆代理接口触发 429。
        """
        if self.cooldown <= 0:
            return 0
        gid = str(event.get_group_id() or "") or "private"
        uid = str(event.get_sender_id() or "") or "unknown"
        key = (gid, uid)
        now = time.time()
        if len(self._last_query) > 4096:  # 防内存无限增长，顺带清理过期记录
            cutoff = now - self.cooldown
            self._last_query = {k: v for k, v in self._last_query.items() if v > cutoff}
        remain = self.cooldown - (now - self._last_query.get(key, 0.0))
        if remain > 0:
            return remain
        self._last_query[key] = now
        return 0

    def _gate(self, event: AstrMessageEvent) -> str | None:
        """查询前置检查（白名单 + 冷却）；通过返回 None，否则返回提示文本"""
        if not self._check_whitelist(event):
            return WHITELIST_MSG
        remain = self._check_cooldown(event)
        if remain > 0:
            return f"查询太频繁啦，请 {remain:.0f} 秒后再试～"
        return None

    def _resolve_platform(self, event: AstrMessageEvent) -> str:
        """返回实际生效的平台类型：auto 时从事件自动识别，否则用配置值。

        支持同一个 AstrBot 同时接入 QQ 官方机器人 + OneBot 等多种平台，
        每条消息各自按来源平台适配，无需手动切换配置。

        QQ 官方机器人（qq_official / qq_official_webhook）一条消息只能带一张图，
        多图会被适配器强制拆成多条；其余平台（OneBot 等）默认按 onebot 处理。
        """
        if self.platform_type in ("onebot", "qq_official"):
            return self.platform_type
        name = (event.get_platform_name() or "").lower()
        if "qq_official" in name or name in ("qqofficial", "qq_official_webhook"):
            return "qq_official"
        return "onebot"

    def _show_progress(self, event: AstrMessageEvent) -> bool:
        """是否发送“正在查询...”中间提示。

        QQ 官方机器人平台对消息条数/频率有限制，省略中间提示，
        只回最终结果一条，减少被限流的风险；其余平台保留提示改善体验。
        """
        return self._resolve_platform(event) != "qq_official"

    def _plain(self, event: AstrMessageEvent, text: str):
        """纯文本输出：强制关闭 Markdown 渲染，避免动态内容里的 # * | 等被平台解析"""
        return event.plain_result(text).use_markdown(False)

    async def _qq_markdown(self, event: AstrMessageEvent, text: str):
        """通过 botpy 官方接口发送 msg_type=2 Markdown；失败时回退 AstrBot 消息链。"""
        if await self._send_qq_native_markdown(event, text):
            return None
        return event.plain_result(text).use_markdown(True)

    async def _send_qq_native_markdown(self, event: AstrMessageEvent, text: str) -> bool:
        """调用 QQ 官方机器人 post_group_message/post_c2c_message 发送原生 Markdown。"""
        try:
            platform_id = ""
            getter = getattr(event, "get_platform_id", None)
            if callable(getter):
                platform_id = str(getter() or "")
            if not platform_id:
                platform_id = str(
                    getattr(event, "platform_id", "")
                    or getattr(event, "platform_name", "")
                    or ""
                )
            platform = self.context.get_platform_inst(platform_id) if platform_id else None
            client_getter = getattr(platform, "get_client", None)
            if not callable(client_getter):
                return False

            from botpy.types.message import MarkdownPayload

            client = client_getter()
            group_id = self._qq_event_value(event, ("group_openid", "group_id", "guild_id", "channel_id"))
            sender_id = self._qq_event_value(
                event, ("member_openid", "user_openid", "openid", "user_id", "sender_id", "author_id")
            )
            message_id = self._qq_event_value(event, ("message_id", "msg_id", "event_id"))
            payload = MarkdownPayload(content=text)
            if group_id:
                await client.api.post_group_message(
                    group_openid=group_id,
                    msg_type=2,
                    markdown=payload,
                    msg_id=message_id or None,
                    msg_seq=1,
                )
            elif sender_id:
                await client.api.post_c2c_message(
                    openid=sender_id,
                    msg_type=2,
                    markdown=payload,
                    msg_id=message_id or None,
                    msg_seq=1,
                )
            else:
                return False
            logger.info("[Roblox] 已通过 QQ 官方原生 Markdown 接口发送用户资料")
            return True
        except Exception as exc:
            logger.warning("[Roblox] QQ 原生 Markdown 发送失败，回退 AstrBot 消息链: %s", exc)
            return False

    @staticmethod
    def _qq_event_value(event: AstrMessageEvent, keys: tuple[str, ...]) -> str:
        """从 AstrBot 事件及 QQ 原始消息的常见层级读取 OpenID/消息 ID。"""
        if "group_openid" in keys:
            getter = getattr(event, "get_group_id", None)
            if callable(getter):
                value = getter()
                if value:
                    return str(value)
        if "openid" in keys:
            getter = getattr(event, "get_sender_id", None)
            if callable(getter):
                value = getter()
                if value:
                    return str(value)
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        sources = (
            event,
            message_obj,
            getattr(message_obj, "sender", None),
            raw_message,
            getattr(raw_message, "raw_data", None),
        )
        for source in sources:
            if isinstance(source, dict):
                nested_sources = (source, source.get("author"), source.get("sender"), source.get("member"))
                for nested in nested_sources:
                    if not isinstance(nested, dict):
                        continue
                    for key in keys:
                        value = nested.get(key)
                        if value:
                            return str(value)
            elif source is not None:
                for key in keys:
                    value = getattr(source, key, "")
                    if value:
                        return str(value)
                for attr in ("author", "sender", "member"):
                    nested = getattr(source, attr, None)
                    if nested is None:
                        continue
                    for key in keys:
                        value = getattr(nested, key, "")
                        if value:
                            return str(value)
        return ""

    def _qq_markdown_image(self, alt: str, url: str, width: int, height: int) -> str:
        """构造 QQ 官方 Markdown 外链图片，不创建 Image 组件。"""
        if not _is_safe_markdown_image_url(url):
            return ""
        display_url = url
        proxy = self.qq_markdown_image_proxy.rstrip("/")
        # 国内网络环境下 wsrv.nl 通常比 images.weserv.nl 更容易被 QQ 拉取。
        if proxy.lower() == "https://images.weserv.nl":
            proxy = "https://wsrv.nl"
        parsed = urlsplit(url)
        if proxy and parsed.hostname and parsed.hostname.endswith("rbxcdn.com"):
            # 按 QQ 官方 Markdown 常见兼容写法，仅编码代理参数中的特殊字符，
            # 保留协议和路径斜杠，避免 QQ 客户端拒绝完整百分号编码的图片源。
            source = url.removeprefix("https://").removeprefix("http://")
            display_url = (
                f"{proxy}/?url={quote(source, safe='/:._~-')}"
                f"&w={width}&h={height}&fit=contain&output=png"
            )
        if not _is_safe_markdown_image_url(display_url):
            return ""
        safe_alt = _escape_qq_markdown(alt).replace("\n", " ")
        return f"![{safe_alt} #{width}px #{height}px]({display_url})"

    def _chain(self, event: AstrMessageEvent, chain):
        """图文消息输出：强制关闭 Markdown 渲染（不影响图片发送）"""
        return event.chain_result(chain).use_markdown(False)

    @staticmethod
    def _game_value(value, fallback="未知") -> str:
        """将游戏字段安全转为展示文本。"""
        return str(value if value not in (None, "") else fallback)

    def _build_qq_game_markdown(self, game: dict) -> str:
        """构造 QQ 官方机器人原生 Markdown 游戏资料，封面以外链图片嵌入。"""
        inline = lambda value: _escape_qq_markdown(self._game_value(value)).replace("\n", " ")
        description = self._game_value(game.get("description"), "无描述")
        desc = _escape_qq_markdown(description[:500]).replace("\n", "  \n")
        if len(description) > 500:
            desc += "……"
        cover = self._qq_markdown_image("游戏封面", game.get("icon_url", ""), 420, 420)
        lines = ["# Roblox 游戏信息"]
        if cover:
            lines.append(cover)
        lines.extend([
            f"**游戏名：** {inline(game.get('name'))}",
            f"**开发者：** {inline(game.get('creator'))}",
            f"**Universe ID：** `{game.get('universe_id', 0)}`",
            f"**地点 ID：** `{game.get('place_id', 0)}`",
            f"**当前游玩：** `{game.get('playing', 0):,}`",
            f"**总访问量：** `{game.get('visits', 0):,}`",
            f"**收藏数：** `{game.get('favorites', 0):,}`",
            f"**点赞数：** `{game.get('likes', 0):,}`",
            f"**类型：** {inline(game.get('genre'))}",
            f"**创建时间：** `{game.get('created', '未知')}`",
            f"**更新时间：** `{game.get('updated', '未知')}`",
            "## 游戏简介",
            desc,
            f"[在 Roblox 中打开](https://www.roblox.com/games/{game.get('place_id', 0)})",
        ])
        return _truncate("\n\n".join(lines))

    def _build_game_plain_text(self, game: dict, title: str) -> str:
        """构造 OneBot HTML 渲染失败时使用的纯文本回退内容。"""
        output = f"【{title}】\n"
        output += f"游戏名：{game['name']}\n"
        output += f"游戏ID：{game['universe_id']}\n"
        output += f"地点ID：{game['place_id']}\n"
        output += f"开发者：{game['creator']}\n"
        output += f"当前游玩：{game['playing']:,}\n"
        output += f"总访问量：{game['visits']:,}\n"
        output += f"收藏数：{game['favorites']:,}\n"
        output += f"点赞数：{game['likes']:,}\n"
        output += f"类型：{game['genre']}\n"
        output += f"创建时间：{game['created']}\n"
        output += f"更新时间：{game['updated']}\n"
        output += f"\n【游戏描述】\n{game['description'][:300]}{'......' if len(game['description']) > 300 else ''}"
        return _truncate(output)

    async def _render_html_card(self, template: str, data: dict) -> str | None:
        """渲染单张 PNG 信息卡；失败返回 None，由业务层决定平台回退方式。"""
        try:
            image_url = await self.html_render(
                template,
                data,
                options={"type": "png", "full_page": True, "animations": "disabled"},
            )
            return image_url or None
        except Exception as e:
            logger.warning(f"[Roblox] 信息卡渲染失败: {e}")
            return None

    async def _render_game_result(self, event: AstrMessageEvent, game: dict, title: str):
        """QQ 官方输出带封面的原生 Markdown，其他平台优先单张游戏卡。"""
        if self._resolve_platform(event) == "qq_official":
            result = await self._qq_markdown(event, self._build_qq_game_markdown(game))
            if result is not None:
                yield result
            return

        template_game = {
            "image_url": html.escape(game["icon_url"], quote=True),
            "name": html.escape(game["name"]),
            "creator": html.escape(game["creator"]),
            "genre": html.escape(game["genre"]),
            "universe_id": game["universe_id"],
            "place_id": game["place_id"],
            "description": html.escape(game["description"][:500]),
            "playing": f"{game['playing']:,}",
            "visits": f"{game['visits']:,}",
            "favorites": f"{game['favorites']:,}",
            "likes": f"{game['likes']:,}",
            "created": game["created"],
            "updated": game["updated"],
        }
        image_url = await self._render_html_card(GAME_CARD_TEMPLATE, {"game": template_game})
        if image_url:
            yield event.image_result(image_url)
            return

        icon_path = await _download_to_temp(game["icon_url"]) if game["icon_url"] else None
        if self._resolve_platform(event) == "qq_official":
            if icon_path:
                yield self._chain(event, [Image.fromFileSystem(icon_path)])
            result = await self._qq_markdown(event, self._build_qq_game_markdown(game))
            if result is not None:
                yield result
            return

        chain = []
        if icon_path:
            chain.append(Image.fromFileSystem(icon_path))
        chain.append(Plain(self._build_game_plain_text(game, title)))
        yield self._chain(event, chain)

    async def _build_game_data(self, detail: dict, universe_id: int, icon_url: str, votes: dict | None = None) -> dict:
        """标准化游戏字段，供官机 Markdown、OneBot 卡片和回退文本共用。"""
        creator = detail.get("creator", {}) or {}
        votes = votes or {}
        return {
            "name": self._game_value(detail.get("name")),
            "universe_id": int(detail.get("id") or universe_id),
            "place_id": int(detail.get("rootPlaceId") or 0),
            "creator": self._game_value(creator.get("name")),
            "playing": int(detail.get("playing", 0) or 0),
            "visits": int(detail.get("visits", 0) or 0),
            "favorites": int(detail.get("favorites", 0) or 0),
            "likes": int(votes.get("upVotes", detail.get("likes", 0)) or 0),
            "genre": self._game_value(detail.get("genre")),
            "created": _fmt_date(_parse_date(detail.get("created", ""))),
            "updated": _fmt_date(_parse_date(detail.get("updated", ""))),
            "description": self._game_value(detail.get("description"), "无描述"),
            "icon_url": icon_url or "",
        }

    # ============ 菜单 ============

    @filter.command("菜单", alias={"帮助", "menu"})
    async def menu(self, event: AstrMessageEvent):
        '''查看功能菜单'''
        path = await _menu_to_temp()
        if path:
            yield self._chain(event, [Image.fromFileSystem(path)])
        else:
            yield self._plain(event, "菜单生成失败，请稍后重试")

    # ============ 用户查询 ============

    @filter.command("用户名搜索")
    async def username_search(self, event: AstrMessageEvent):
        '''根据用户名查询 Roblox 用户完整资料'''
        gate = self._gate(event)
        if gate:
            yield self._plain(event, gate)
            return
        username = _parse_param(event, "用户名搜索")
        if not username:
            yield self._plain(event, "请输入用户名，例：/用户名搜索 Roblox")
            return
        user_info = await search_user(username)
        if not user_info:
            yield self._plain(event, "未找到该用户，请检查用户名是否正确！")
            return
        async for res in self._user_query(event, user_info["id"], "用户名搜索"):
            yield res

    @filter.command("用户ID搜索")
    async def user_id_search(self, event: AstrMessageEvent):
        '''按用户ID查询 Roblox 用户完整资料'''
        gate = self._gate(event)
        if gate:
            yield self._plain(event, gate)
            return
        uid_str = _parse_param(event, "用户ID搜索")
        if not uid_str or not uid_str.isdigit():
            yield self._plain(event, "请输入有效的用户ID（纯数字），例：/用户ID搜索 123456789")
            return
        uid = int(uid_str)
        async for res in self._user_query(event, uid, "用户ID搜索"):
            yield res

    async def _user_query(self, event: AstrMessageEvent, user_id: int, source: str):
        """用户详情查询（用户名搜索 / 用户ID搜索共用）。

        QQ 官方机器人用户资料走原生 Markdown，其他平台走信息卡，因此不额外发送进度消息。
        """
        total_start = time.time()
        try:
            details = await get_user_details(user_id)
            if not details or not details.get("name"):
                yield self._plain(event, "未找到该用户，请检查用户名或用户ID是否正确！")
                return

            raw_name = details.get("name", "")
            display_name = details.get("displayName", raw_name)
            created_raw = details.get("created", "")
            is_banned = details.get("isBanned", False)
            description = details.get("description", "")

            platform = self._resolve_platform(event)
            results = await asyncio.gather(
                get_user_presence(user_id),
                get_user_groups(user_id),
                get_friend_count(user_id),
                get_follower_count(user_id),
                get_following_count(user_id),
                return_exceptions=True,
            )

            def _safe(idx, default):
                r = results[idx]
                return default if isinstance(r, Exception) else r

            status = _safe(0, None)
            groups = _safe(1, []) or []
            friend_count = _safe(2, None)
            follower_count = _safe(3, None)
            following_count = _safe(4, None)
            # 官机 Markdown 与 OneBot HTML 卡都需要头像框和 3D 虚拟形象 URL。
            avatar_result, headshot_result = await asyncio.gather(
                get_avatar_url(user_id),
                get_headshot_url(user_id),
                return_exceptions=True,
            )
            avatar_url = "" if isinstance(avatar_result, Exception) else (avatar_result or "")
            headshot_url = "" if isinstance(headshot_result, Exception) else (headshot_result or "")

            online_status, location = _parse_presence(status)
            created_date, age_info = _calc_age(_parse_date(created_raw))

            def _num_or_unknown(v):
                return "未知" if v is None else f"{v:,}"

            friend_text = _num_or_unknown(friend_count)
            follower_text = _num_or_unknown(follower_count)
            following_text = _num_or_unknown(following_count)
            verified = details.get("hasVerifiedBadge") if "hasVerifiedBadge" in details else None

            def _group_text(group):
                group_name = group.get("group", {}).get("name", "未知")
                role = group.get("role", {}).get("name", "未知")
                gid = group.get("group", {}).get("id", 0)
                return f"{group_name}（职位：{role}，ID：{gid}）"

            # QQ 官方机器人必须保持原生 Markdown 输出；头像和 3D 形象放在双栏表格中。
            if platform == "qq_official":
                # QQ 手机端的表格图片总宽度不能太大，否则会被客户端改为纵向排版。
                headshot_image = self._qq_markdown_image("头像", headshot_url, 160, 160)
                avatar_image = self._qq_markdown_image("Roblox形象", avatar_url, 160, 160)
                markdown = _build_qq_user_markdown(
                    raw_name=raw_name,
                    display_name=display_name,
                    user_id=user_id,
                    created_date=created_date,
                    age_info=age_info,
                    friend_count=friend_text,
                    follower_count=follower_text,
                    following_count=following_text,
                    online_status=online_status,
                    location=location,
                    is_banned=is_banned,
                    verified=verified,
                    description=description,
                    groups=groups,
                    headshot_image=headshot_image,
                    avatar_image=avatar_image,
                )
                result = await self._qq_markdown(event, markdown)
                if result is not None:
                    yield result
                return

            name_line = raw_name if display_name == raw_name else f"{display_name}（@{raw_name}）"
            user_card = {
                "name_line": html.escape(name_line),
                "headshot_url": html.escape(headshot_url, quote=True),
                "avatar_url": html.escape(avatar_url, quote=True),
                "friends": friend_text,
                "following": following_text,
                "followers": follower_text,
                "id": user_id,
                "created": created_date or "未知",
                "age": html.escape(age_info or "未知"),
                "status": html.escape(online_status),
                "location": html.escape(location),
                "banned": "是" if is_banned else "否",
                "verified": "未知" if verified is None else ("是" if verified else "否"),
                "description": html.escape((description or "无")[:500]),
                "groups": [html.escape(_group_text(group)) for group in groups[:5]],
            }
            # OneBot 等非官机平台使用同一张 HTML 用户卡，保证头像与 3D 形象稳定处于真正的表格单元格内。
            card_url = await self._render_html_card(USER_CARD_TEMPLATE, {"user": user_card})
            if card_url:
                yield event.image_result(card_url)
                return

            output = "【Roblox 用户信息】\n"
            output += f"用户名：{raw_name}\n"
            if display_name and display_name != raw_name:
                output += f"展示名：{display_name}\n"
            output += f"用户ID：{user_id}\n"
            if created_date:
                output += f"注册日期：{created_date}\n"
                output += f"注册时长：{age_info}\n"
            output += f"好友：{friend_text} ｜ 关注：{following_text} ｜ 粉丝：{follower_text}\n"
            output += f"在线状态：{online_status}\n"
            output += f"当前位置：{location}\n"
            output += f"账号封禁：{'是' if is_banned else '否'}\n"
            if verified is not None:
                output += f"已认证：{'是' if verified else '否'}\n"

            if description:
                output += "\n【用户简介】\n"
                output += description[:500]
                if len(description) > 500:
                    output += "......"
                output += "\n"

            if groups:
                output += "\n【已加入群组（前5个）】\n"
                for idx, group in enumerate(groups[:5], 1):
                    group_name = group.get("group", {}).get("name", "未知")
                    role = group.get("role", {}).get("name", "未知")
                    gid = group.get("group", {}).get("id", 0)
                    output += f"{idx}. {group_name}（职位：{role}，ID：{gid}）\n"

            output = _truncate(output)

            # OneBot 等非官机平台保持头像框 + 形象图在前、纯文本在后。
            image_urls = [headshot_url, avatar_url]
            paths = await asyncio.gather(*(_download_to_temp(u) for u in image_urls if u))
            chain = [Image.fromFileSystem(p) for p in paths if p]
            chain.append(Plain(output))
            yield self._chain(event, chain)
        except Exception as e:
            logger.error(traceback.format_exc())
            yield self._plain(event, f"查询失败：{str(e)}")
        finally:
            logger.info(f"[Roblox] {source} 耗时: {time.time() - total_start:.2f}s")

    # ============ 群组查询 ============

    @filter.command("群组名搜索")
    async def group_name_search(self, event: AstrMessageEvent):
        '''根据群组名模糊搜索群组并展示详情'''
        gate = self._gate(event)
        if gate:
            yield self._plain(event, gate)
            return
        name = _parse_param(event, "群组名搜索")
        if not name:
            yield self._plain(event, "请输入群组名，例：/群组名搜索 Roblox")
            return
        if self._show_progress(event):
            yield self._plain(event, "正在搜索群组，请稍候...")
        try:
            search_result = await search_group(name)
            groups = search_result.get("data", []) if search_result else []
            if not groups:
                yield self._plain(event, "未找到匹配的群组，请检查群组名是否正确！")
                return

            group = groups[0]
            gid = group.get("id", 0)
            group_info, icon_url = await asyncio.gather(
                get_group_info(gid), get_group_icon(gid), return_exceptions=True)
            if isinstance(group_info, Exception):
                if isinstance(group_info, RobloxAPIError):
                    raise group_info  # 网络/服务端错误 → 外层统一提示“查询失败”
                group_info = {}
            group_info = group_info or {}
            icon_url = "" if isinstance(icon_url, Exception) else (icon_url or "")

            name_out = group_info.get("name", "未知")
            description = (group_info.get("description", "") or "").strip() or "无描述"
            member_count = group_info.get("memberCount", 0)
            owner = group_info.get("owner", {}) or {}
            # 兼容 API 两种字段命名：name/id 与 username/userId
            owner_name = owner.get("name") or owner.get("username") or "未知"
            owner_id = owner.get("id") or owner.get("userId")
            owner_text = f"{owner_name}（ID：{owner_id}）" if owner_id else owner_name
            is_public = group_info.get("publicEntryAllowed", False)
            create_dt = _parse_date(group_info.get("created", ""))

            output = "【Roblox 群组搜索】\n"
            output += f"群组名：{name_out}\n"
            output += f"群组ID：{gid}\n"
            output += f"成员数量：{member_count:,}\n"
            output += f"群主：{owner_text}\n"
            output += f"创建时间：{_fmt_date(create_dt)}\n"
            output += f"是否公开：{'是' if is_public else '否'}\n"
            output += f"\n【群组描述】\n{description[:300]}{'......' if len(description) > 300 else ''}"

            if len(groups) > 1:
                output += f"\n\n【更多匹配（共{len(groups)}个）】\n"
                for cand in groups[1:4]:
                    cid = cand.get("id", 0)
                    cname = cand.get("name", "未知")
                    output += f"· {cname}（ID：{cid}）\n"
                output += "发送 /群组ID搜索 [ID] 查看详情"

            output = _truncate(output)

            chain = []
            icon_path = await _download_to_temp(icon_url) if icon_url else None
            if icon_path:
                chain.append(Image.fromFileSystem(icon_path))
            chain.append(Plain(output))
            yield self._chain(event, chain)
        except Exception as e:
            logger.error(traceback.format_exc())
            yield self._plain(event, f"查询失败：{str(e)}")

    @filter.command("群组ID搜索")
    async def group_id_search(self, event: AstrMessageEvent):
        '''根据群组ID查询群组详情与职位列表'''
        gate = self._gate(event)
        if gate:
            yield self._plain(event, gate)
            return
        gid_str = _parse_param(event, "群组ID搜索")
        if not gid_str or not gid_str.isdigit():
            yield self._plain(event, "请输入有效的群组ID（纯数字），例：/群组ID搜索 123456")
            return
        gid = int(gid_str)
        if self._show_progress(event):
            yield self._plain(event, "正在查询群组信息，请稍候...")
        try:
            group_info, roles, icon_url = await asyncio.gather(
                get_group_info(gid), get_group_roles(gid), get_group_icon(gid),
                return_exceptions=True)
            if isinstance(group_info, Exception):
                if isinstance(group_info, RobloxAPIError):
                    raise group_info  # 网络/服务端错误 → 外层统一提示“查询失败”
                group_info = {}
            group_info = group_info or {}
            roles = [] if isinstance(roles, Exception) else (roles or [])
            icon_url = "" if isinstance(icon_url, Exception) else (icon_url or "")
            if not group_info:
                yield self._plain(event, "未找到该群组，请检查群组ID是否正确！")
                return

            name_out = group_info.get("name", "未知")
            description = (group_info.get("description", "") or "").strip() or "无描述"
            member_count = group_info.get("memberCount", 0)
            owner = group_info.get("owner", {}) or {}
            # 兼容 API 两种字段命名：name/id 与 username/userId
            owner_name = owner.get("name") or owner.get("username") or "未知"
            owner_id = owner.get("id") or owner.get("userId")
            owner_text = f"{owner_name}（ID：{owner_id}）" if owner_id else owner_name
            is_public = group_info.get("publicEntryAllowed", False)
            create_dt = _parse_date(group_info.get("created", ""))

            output = "【Roblox 群组详情】\n"
            output += f"群组名：{name_out}\n"
            output += f"群组ID：{gid}\n"
            output += f"成员数量：{member_count:,}\n"
            output += f"群主：{owner_text}\n"
            output += f"创建时间：{_fmt_date(create_dt)}\n"
            output += f"是否公开：{'是' if is_public else '否'}\n"
            output += f"\n【群组描述】\n{description[:200]}{'......' if len(description) > 200 else ''}"

            if roles:
                output += "\n\n【职位列表（前5个）】\n"
                for idx, role in enumerate(roles[:5], 1):
                    role_name = role.get("name") or role.get("displayName") or "未知"
                    role_count = role.get("memberCount") or role.get("count") or 0
                    output += f"{idx}. {role_name}（成员数：{role_count}）\n"

            output = _truncate(output)

            chain = []
            icon_path = await _download_to_temp(icon_url) if icon_url else None
            if icon_path:
                chain.append(Image.fromFileSystem(icon_path))
            chain.append(Plain(output))
            yield self._chain(event, chain)
        except Exception as e:
            logger.error(traceback.format_exc())
            yield self._plain(event, f"查询失败：{str(e)}")

    # ============ 游戏查询 ============

    @filter.command("游戏名搜索")
    async def game_name_search(self, event: AstrMessageEvent):
        '''根据游戏名搜索游戏（OMNI Search）'''
        gate = self._gate(event)
        if gate:
            yield self._plain(event, gate)
            return
        name = _parse_param(event, "游戏名搜索")
        if not name:
            yield self._plain(event, "请输入游戏名，例：/游戏名搜索 Adopt Me")
            return
        try:
            search_result = await search_game(name)
            games = search_result.get("data", []) if search_result else []
            if not games:
                yield self._plain(event, "未找到匹配的游戏，请检查游戏名是否正确后重试")
                return

            candidate = games[0]
            universe_id = int(candidate.get("id") or 0)
            if not universe_id:
                yield self._plain(event, "游戏搜索结果缺少有效的 Universe ID，请稍后重试")
                return
            game_info, icon_url, votes = await asyncio.gather(
                get_game_info_by_universe(universe_id),
                get_game_icon(universe_id),
                get_game_votes(universe_id),
                return_exceptions=True,
            )
            if isinstance(game_info, Exception):
                if isinstance(game_info, RobloxAPIError):
                    raise game_info
                game_info = {}
            game_data = game_info.get("data", []) if isinstance(game_info, dict) else []
            game_detail = game_data[0] if game_data else {}
            if not game_detail:
                yield self._plain(event, "已找到游戏候选，但无法读取游戏详情，请稍后重试")
                return
            icon_url = "" if isinstance(icon_url, Exception) else (icon_url or "")
            votes = {} if isinstance(votes, Exception) else (votes or {})
            game_data = await self._build_game_data(game_detail, universe_id, icon_url, votes)
            async for result in self._render_game_result(event, game_data, "Roblox 游戏搜索"):
                yield result
        except Exception as e:
            logger.error(traceback.format_exc())
            yield self._plain(event, f"查询失败：{str(e)}")

    @filter.command("游戏ID搜索")
    async def game_id_search(self, event: AstrMessageEvent):
        '''根据游戏ID查询游戏详情（兼容 Universe ID / 地点 ID）'''
        gate = self._gate(event)
        if gate:
            yield self._plain(event, gate)
            return
        gid_str = _parse_param(event, "游戏ID搜索")
        if not gid_str or not gid_str.isdigit():
            yield self._plain(event, "请输入有效的游戏ID（纯数字），例：/游戏ID搜索 292439477")
            return
        gid = int(gid_str)
        try:
            # 兼容输入 placeId/universeId：优先按 universeId，失败后按 placeId 兜底。
            uni_info, place_info = await asyncio.gather(
                get_game_info_by_universe(gid, retries=1),
                get_game_info(gid, retries=1),
                return_exceptions=True,
            )
            errs = [r for r in (uni_info, place_info) if isinstance(r, RobloxAPIError)]
            picked = next(
                (r for r in (uni_info, place_info) if isinstance(r, dict) and r.get("data")),
                None,
            )
            if not picked:
                if errs:
                    raise errs[0]
                yield self._plain(event, "未找到该游戏，请检查游戏ID是否正确！")
                return
            game_detail = picked["data"][0]
            universe_id = int(game_detail.get("id") or gid)
            icon_url, votes = await asyncio.gather(
                get_game_icon(universe_id),
                get_game_votes(universe_id),
                return_exceptions=True,
            )
            icon_url = "" if isinstance(icon_url, Exception) else (icon_url or "")
            votes = {} if isinstance(votes, Exception) else (votes or {})
            game_data = await self._build_game_data(game_detail, universe_id, icon_url, votes)
            async for result in self._render_game_result(event, game_data, "Roblox 游戏详情"):
                yield result
        except Exception as e:
            logger.error(traceback.format_exc())
            yield self._plain(event, f"查询失败：{str(e)}")

    # ============ 社交关系查询 ============

    async def _social_list(self, event: AstrMessageEvent, keyword: str, title: str, fetch):
        """社交列表查询（好友/粉丝/关注共用），fetch 为对应的异步查询函数。

        参数格式：[用户ID] 或 [用户ID] [页码]，每页 10 个。
        """
        gate = self._gate(event)
        if gate:
            yield self._plain(event, gate)
            return
        parts = _parse_param(event, keyword).split()
        uid_str = parts[0] if parts else ""
        page = 1
        if len(parts) >= 2 and parts[1].isdigit():
            page = max(1, min(int(parts[1]), 50))
        if not uid_str or not uid_str.isdigit():
            yield self._plain(event, f"请输入有效的用户ID（纯数字），例：/{keyword} 123456789；翻页例：/{keyword} 123456789 2")
            return
        uid = int(uid_str)
        if self._show_progress(event):
            yield self._plain(event, f"正在获取{title}（第{page}页），请稍候...")
        try:
            items = await fetch(uid, 10, page)
            items = items[:10]  # 保险起见强制截断本页 10 个
            if not items:
                if page > 1:
                    yield self._plain(event, f"第{page}页没有更多内容了")
                else:
                    yield self._plain(event, "未找到该用户的相关列表（可能用户ID不存在，或该接口暂时不可用）")
                return
            output = f"【{title}】用户ID {uid}（第{page}页，每页10个）\n"
            for idx, item in enumerate(items, 1):
                name = item.get("name") or "未知"
                display_name = item.get("displayName") or name
                iid = item.get("id", 0)
                output += f"{idx + (page - 1) * 10}. {name}（{display_name}）｜ ID：{iid}\n"
            yield self._plain(event, _truncate(output))
        except Exception as e:
            logger.error(traceback.format_exc())
            yield self._plain(event, f"获取失败：{str(e)}")

    @filter.command("获取好友列表")
    async def friends_list(self, event: AstrMessageEvent):
        '''读取用户前10位好友'''
        async for res in self._social_list(event, "获取好友列表", "好友列表", get_friends):
            yield res

    @filter.command("获取粉丝列表")
    async def followers_list(self, event: AstrMessageEvent):
        '''读取用户前10位粉丝'''
        async for res in self._social_list(event, "获取粉丝列表", "粉丝列表", get_followers):
            yield res

    @filter.command("获取关注列表")
    async def followings_list(self, event: AstrMessageEvent):
        '''读取用户前10位关注'''
        async for res in self._social_list(event, "获取关注列表", "关注列表", get_followings):
            yield res

    @filter.command("获取徽章列表")
    async def badges_list(self, event: AstrMessageEvent):
        '''读取用户获得的 Roblox 官方徽章'''
        gate = self._gate(event)
        if gate:
            yield self._plain(event, gate)
            return
        uid_str = _parse_param(event, "获取徽章列表")
        if not uid_str or not uid_str.isdigit():
            yield self._plain(event, "请输入有效的用户ID（纯数字），例：/获取徽章列表 156")
            return
        uid = int(uid_str)
        if self._show_progress(event):
            yield self._plain(event, "正在获取徽章列表，请稍候...")
        try:
            badges = await get_user_badges(uid)
            if not badges:
                yield self._plain(event, "该用户暂无徽章（或用户ID不存在、接口暂时不可用）")
                return
            output = f"【Roblox 徽章列表】用户ID {uid}（共{len(badges)}枚）\n"
            for idx, badge in enumerate(badges[:15], 1):
                output += f"{idx}. {badge.get('name') or '未知'}\n"
            if len(badges) > 15:
                output += f"……共{len(badges)}枚，仅展示前15枚\n"
            yield self._plain(event, _truncate(output))
        except Exception as e:
            logger.error(traceback.format_exc())
            yield self._plain(event, f"获取失败：{str(e)}")

    # ============ 无斜杠命令分发（可在配置中关闭） ============

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def no_slash_dispatch(self, event: AstrMessageEvent):
        '''无斜杠触发支持：如直接发送「用户名搜索 Roblox」即可触发（受 allow_no_slash_command 配置控制）'''
        if not self.allow_no_slash:
            return
        # 关键：斜杠命令、@机器人、私聊消息都已由 @filter.command 处理器接管
        # （AstrBot 唤醒阶段会剥掉 / 前缀，私聊的 is_at_or_wake_command 恒为 True），
        # 这里只兜底「群聊中无斜杠、无 @」的场景，否则同一命令会被回复两遍。
        if event.is_at_or_wake_command:
            return
        msg = event.message_str.strip()
        if not msg or msg.startswith("/"):
            return  # 斜杠命令由 @filter.command 处理，避免重复
        if msg in ("菜单", "帮助", "menu"):
            async for res in self.menu(event):
                yield res
            event.stop_event()
            return
        for kw, handler in self._no_slash_handlers.items():
            if msg.startswith(kw):
                async for res in handler(event):
                    yield res
                event.stop_event()
                return

    async def terminate(self):
        '''插件卸载/停用时关闭全局 httpx 客户端并清理临时文件'''
        await close_client()
        _cleanup_temp_files()
