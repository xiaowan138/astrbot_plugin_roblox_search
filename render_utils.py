"""
render_utils.py —— 菜单图片渲染

通过 PIL 将功能菜单渲染为 PNG 图片。优先使用插件自带的 Noto Sans SC 字体
（OFL 开源协议，随插件分发），避免在无中文字体的 Docker/Linux 环境回退到
PIL 默认字体而显示方格子。

渲染与字体探测（含 fc-list 子进程调用）为同步阻塞操作，统一放到线程池
执行（asyncio.to_thread），不阻塞事件循环。
"""

import asyncio
import io
import os
from PIL import Image, ImageDraw, ImageFont

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_CACHE = {}
_VERSION = ""

# 由 AstrBot Star.html_render 渲染的用户信息卡。动态值在调用方已做 HTML 转义。
USER_CARD_TEMPLATE = """<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"UTF-8\">
<style>
* { box-sizing: border-box; }
body { margin: 0; padding: 30px; background: radial-gradient(circle at top left, #334155, #111827 55%, #0f172a); color: #f8fafc; font-family: \"Microsoft YaHei\", \"PingFang SC\", sans-serif; }
.card { width: 960px; overflow: hidden; border: 1px solid rgba(255,255,255,.16); border-radius: 24px; background: rgba(30,41,59,.94); box-shadow: 0 18px 52px rgba(0,0,0,.4); }
.header { padding: 28px 30px 18px; background: linear-gradient(135deg, rgba(59,130,246,.26), rgba(16,185,129,.12)); }
h1 { margin: 0; font-size: 33px; }
.sub { margin-top: 8px; color: #cbd5e1; font-size: 16px; }
.portraits { width: calc(100% - 60px); margin: 0 30px 22px; table-layout: fixed; border-collapse: separate; border-spacing: 0; overflow: hidden; border: 1px solid rgba(255,255,255,.18); border-radius: 18px; background: rgba(15,23,42,.72); }
.portrait { width: 50%; height: 280px; padding: 0; vertical-align: top; background: rgba(15,23,42,.72); }
.portrait + .portrait { border-left: 1px solid rgba(255,255,255,.16); }
.portrait-title { padding: 12px 16px; background: rgba(255,255,255,.07); border-bottom: 1px solid rgba(255,255,255,.14); font-size: 18px; font-weight: 700; }
.portrait-image { width: 100%; height: 235px; object-fit: contain; display: block; padding: 12px; }
.placeholder { height: 235px; display: flex; align-items: center; justify-content: center; color: #94a3b8; font-size: 16px; }
.stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; padding: 0 30px 22px; }
.stat { padding: 15px; border: 1px solid rgba(255,255,255,.11); border-radius: 15px; background: rgba(255,255,255,.05); }
.label { color: #94a3b8; font-size: 13px; }
.value { margin-top: 6px; font-size: 20px; font-weight: 700; }
.info { padding: 0 30px 24px; }
.row { padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,.08); line-height: 1.6; }
.row:last-child { border-bottom: none; }
.key { color: #cbd5e1; font-weight: 700; display: inline-block; min-width: 112px; }
.code { padding: 2px 8px; border-radius: 7px; background: rgba(255,255,255,.12); font-family: Consolas, monospace; }
.section { margin: 0 30px 24px; padding: 18px; border: 1px solid rgba(255,255,255,.1); border-radius: 16px; background: rgba(15,23,42,.45); }
.section h2 { margin: 0 0 12px; font-size: 19px; }
.description { margin: 0; color: #dbeafe; line-height: 1.7; white-space: pre-wrap; }
ul { margin: 0; padding-left: 22px; color: #e2e8f0; line-height: 1.9; }
.footer { padding: 0 30px 28px; color: #94a3b8; font-size: 13px; }
</style>
</head>
<body>
<div class=\"card\">
  <header class=\"header\"><h1>Roblox 用户信息</h1><div class=\"sub\">{{ user.name_line }}</div></header>
  <table class=\"portraits\" aria-label=\"Roblox 头像与虚拟形象\"><tbody><tr>
    <td class=\"portrait\"><div class=\"portrait-title\">头像</div>{% if user.headshot_url %}<img class=\"portrait-image\" src=\"{{ user.headshot_url }}\" alt=\"头像\" width=\"420\" height=\"235\">{% else %}<div class=\"placeholder\">头像暂不可用</div>{% endif %}</td>
    <td class=\"portrait\"><div class=\"portrait-title\">3D 虚拟形象</div>{% if user.avatar_url %}<img class=\"portrait-image\" src=\"{{ user.avatar_url }}\" alt=\"3D 虚拟形象\" width=\"420\" height=\"235\">{% else %}<div class=\"placeholder\">虚拟形象暂不可用</div>{% endif %}</td>
  </tr></tbody></table>
  <section class=\"stats\">
    <div class=\"stat\"><div class=\"label\">好友</div><div class=\"value\">{{ user.friends }}</div></div>
    <div class=\"stat\"><div class=\"label\">关注</div><div class=\"value\">{{ user.following }}</div></div>
    <div class=\"stat\"><div class=\"label\">粉丝</div><div class=\"value\">{{ user.followers }}</div></div>
  </section>
  <section class=\"info\">
    <div class=\"row\"><span class=\"key\">用户 ID</span><span class=\"code\">{{ user.id }}</span></div>
    <div class=\"row\"><span class=\"key\">创建时间</span>{{ user.created }}</div>
    <div class=\"row\"><span class=\"key\">注册时长</span>{{ user.age }}</div>
    <div class=\"row\"><span class=\"key\">在线状态</span>{{ user.status }}</div>
    <div class=\"row\"><span class=\"key\">当前位置</span>{{ user.location }}</div>
    <div class=\"row\"><span class=\"key\">是否封禁</span>{{ user.banned }}　　<span class=\"key\">已认证</span>{{ user.verified }}</div>
  </section>
  <section class=\"section\"><h2>用户简介</h2><p class=\"description\">{{ user.description }}</p></section>
  <section class=\"section\"><h2>群组列表</h2>{% if user.groups %}<ul>{% for group in user.groups %}<li>{{ group }}</li>{% endfor %}</ul>{% else %}<p class=\"description\">暂无公开群组</p>{% endif %}</section>
  <footer class=\"footer\">Roblox 全功能查询插件</footer>
</div>
</body>
</html>"""

# 由 AstrBot Star.html_render 渲染的游戏信息卡。动态值在调用方已做 HTML 转义。
GAME_CARD_TEMPLATE = """<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"UTF-8\">
<style>
* { box-sizing: border-box; }
body { margin: 0; padding: 32px; background: radial-gradient(circle at top right, #1d4ed8 0%, transparent 32%), linear-gradient(135deg, #0f172a, #111827); color: #f8fafc; font-family: \"Microsoft YaHei\", \"PingFang SC\", sans-serif; }
.card { width: 980px; overflow: hidden; border: 1px solid rgba(255,255,255,.18); border-radius: 24px; background: rgba(15,23,42,.9); box-shadow: 0 20px 55px rgba(0,0,0,.35); }
.hero { display: flex; gap: 26px; padding: 28px; background: linear-gradient(135deg, rgba(59,130,246,.24), rgba(16,185,129,.12)); }
.icon { width: 220px; height: 220px; flex: 0 0 220px; overflow: hidden; border-radius: 20px; background: #1e293b; border: 1px solid rgba(255,255,255,.16); }
.icon img { width: 100%; height: 100%; object-fit: cover; }
.placeholder { height: 100%; display: flex; align-items: center; justify-content: center; color: #94a3b8; text-align: center; padding: 16px; }
h1 { margin: 0 0 14px; font-size: 34px; line-height: 1.25; }
.meta { display: flex; flex-wrap: wrap; gap: 9px; margin-bottom: 16px; }
.tag { padding: 6px 10px; border-radius: 999px; background: rgba(255,255,255,.11); font-size: 14px; }
.desc { margin: 0; line-height: 1.65; color: #dbeafe; white-space: pre-wrap; }
.stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; padding: 0 28px 24px; }
.stat { padding: 16px; border: 1px solid rgba(255,255,255,.11); border-radius: 16px; background: rgba(255,255,255,.05); }
.label { color: #94a3b8; font-size: 13px; }
.value { margin-top: 7px; font-size: 21px; font-weight: 700; }
.footer { padding: 0 28px 28px; color: #94a3b8; font-size: 13px; line-height: 1.6; }
</style>
</head>
<body>
<div class=\"card\">
  <section class=\"hero\">
    <div class=\"icon\">{% if game.image_url %}<img src=\"{{ game.image_url }}\" alt=\"游戏封面\">{% else %}<div class=\"placeholder\">游戏封面暂不可用</div>{% endif %}</div>
    <div>
      <h1>{{ game.name }}</h1>
      <div class=\"meta\">
        <span class=\"tag\">开发者：{{ game.creator }}</span>
        <span class=\"tag\">类型：{{ game.genre }}</span>
        <span class=\"tag\">Universe ID：{{ game.universe_id }}</span>
        <span class=\"tag\">地点 ID：{{ game.place_id }}</span>
      </div>
      <p class=\"desc\">{{ game.description }}</p>
    </div>
  </section>
  <section class=\"stats\">
    <div class=\"stat\"><div class=\"label\">当前游玩</div><div class=\"value\">{{ game.playing }}</div></div>
    <div class=\"stat\"><div class=\"label\">总访问量</div><div class=\"value\">{{ game.visits }}</div></div>
    <div class=\"stat\"><div class=\"label\">收藏数</div><div class=\"value\">{{ game.favorites }}</div></div>
    <div class=\"stat\"><div class=\"label\">点赞数</div><div class=\"value\">{{ game.likes }}</div></div>
    <div class=\"stat\"><div class=\"label\">创建时间</div><div class=\"value\">{{ game.created }}</div></div>
    <div class=\"stat\"><div class=\"label\">更新时间</div><div class=\"value\">{{ game.updated }}</div></div>
  </section>
  <div class=\"footer\">Roblox 链接：https://www.roblox.com/games/{{ game.place_id }}</div>
</div>
</body>
</html>"""


def _load_version() -> str:
    """从 metadata.yaml 读取插件版本号（仅首次读取，失败返回空串）"""
    global _VERSION
    if _VERSION:
        return _VERSION
    try:
        import yaml
        with open(os.path.join(_PLUGIN_DIR, "metadata.yaml"), encoding="utf-8") as f:
            _VERSION = str((yaml.safe_load(f) or {}).get("version", "")).strip()
    except Exception:
        pass
    return _VERSION


def get_font(size=14):
    cache_key = size
    if cache_key in FONT_CACHE:
        return FONT_CACHE[cache_key]

    # 插件内置字体优先（fonts/ 目录随插件分发）
    _internal_font = os.path.join(_PLUGIN_DIR, "fonts", "NotoSansSC-Regular.otf")

    font_paths = [
        _internal_font,
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto/NotoSansCJK-SC-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-SC-Regular.otf",
        "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/STSong.ttf",
        "C:/Windows/Fonts/STHeiti.ttf",
    ]

    for path in font_paths:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                FONT_CACHE[cache_key] = font
                return font
            except Exception:
                continue

    import subprocess
    try:
        result = subprocess.run(
            ["fc-list", ":lang=zh", "file"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line:
                    font_path = line.split(':')[0]
                    try:
                        font = ImageFont.truetype(font_path, size)
                        FONT_CACHE[cache_key] = font
                        return font
                    except Exception:
                        continue
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["fc-list", "file"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line:
                    font_path = line.split(':')[0]
                    try:
                        font = ImageFont.truetype(font_path, size)
                        FONT_CACHE[cache_key] = font
                        return font
                    except Exception:
                        continue
    except Exception:
        pass

    FONT_CACHE[cache_key] = ImageFont.load_default()
    return ImageFont.load_default()


async def menu_to_image() -> bytes:
    """生成菜单图片（渲染在线程池执行，避免阻塞事件循环）"""
    return await asyncio.to_thread(_render_menu_sync)


def _render_menu_sync() -> bytes:
    font = get_font(16)
    title_font = get_font(26)
    small_font = get_font(12)

    content_width = 600
    padding = 40
    line_height = font.getbbox("A")[3] - font.getbbox("A")[1] + 8

    menu_items = [
        ("用户查询", [
            ("/用户名搜索 [用户名]", "官机图片MD / OneBot用户卡"),
            ("/用户ID搜索 [数字ID]", "按用户ID直查用户资料"),
        ]),
        ("群组查询", [
            ("/群组名搜索 [群组名]", "模糊搜索群组并展示详情"),
            ("/群组ID搜索 [数字ID]", "群组详情与职位列表"),
        ]),
        ("游戏查询", [
            ("/游戏名搜索 [游戏名]", "官机封面MD / OneBot游戏卡"),
            ("/游戏ID搜索 [数字ID]", "游戏详情（官机MD / OneBot卡）"),
        ]),
        ("社交查询", [
            ("/获取好友列表 [ID] [页码]", "读取用户好友（每页10个）"),
            ("/获取粉丝列表 [ID] [页码]", "读取用户粉丝（每页10个）"),
            ("/获取关注列表 [ID] [页码]", "读取用户关注（每页10个）"),
            ("/获取徽章列表 [用户ID]", "读取用户获得的官方徽章"),
        ]),
    ]

    # 按实际行高动态计算总高度，避免字体度量变化导致内容溢出/压到页脚
    category_heights = [
        40 + len(items) * line_height + 18
        for _, items in menu_items
    ]
    total_items_height = sum(category_heights)
    image_height = padding * 2 + 100 + 25 + total_items_height + 30

    img = Image.new('RGB', (content_width, image_height), color=(15, 18, 25))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([(5, 5), (content_width - 5, image_height - 5)],
                           radius=15, outline=(50, 120, 255), width=2)

    gradient = Image.new('RGBA', (content_width, 80), color=(0, 0, 0, 0))
    grad_draw = ImageDraw.Draw(gradient)
    for i in range(80):
        alpha = int(255 * (1 - i / 80))
        grad_draw.line([(0, i), (content_width, i)], fill=(50, 120, 255, alpha // 3))
    img.paste(gradient, (0, 0), gradient)

    title_text = "Roblox 全功能查询"
    bbox = title_font.getbbox(title_text)
    title_width = bbox[2] - bbox[0]
    draw.text(((content_width - title_width) // 2, padding), title_text,
              font=title_font, fill=(100, 200, 255))

    subtitle_text = "功能菜单"
    bbox = font.getbbox(subtitle_text)
    sub_width = bbox[2] - bbox[0]
    draw.text(((content_width - sub_width) // 2, padding + 50), subtitle_text,
              font=font, fill=(150, 180, 220))

    y = padding + 100
    draw.line([(padding, y), (content_width - padding, y)], fill=(40, 80, 150), width=1)
    y += 25

    for category, items in menu_items:
        cat_bbox = font.getbbox(category)
        cat_width = cat_bbox[2] - cat_bbox[0]
        draw.text((padding, y), category, font=font, fill=(80, 180, 255))

        draw.rounded_rectangle([
            (padding - 5, y - 5),
            (padding + cat_width + 10, y + cat_bbox[3] - cat_bbox[1] + 5)
        ], radius=8, outline=(80, 180, 255), width=1)

        y += 40

        for cmd, desc in items:
            draw.text((padding + 15, y), cmd, font=font, fill=(200, 215, 240))
            draw.text((padding + 220, y), desc, font=small_font, fill=(120, 140, 170))
            y += line_height

        y += 18

    version = _load_version()
    footer_text = f"Roblox 全功能查询插件 {version}".rstrip()
    bbox = small_font.getbbox(footer_text)
    footer_width = bbox[2] - bbox[0]
    draw.text(((content_width - footer_width) // 2, image_height - 30),
              footer_text, font=small_font, fill=(60, 80, 110))

    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    return buffer.getvalue()
