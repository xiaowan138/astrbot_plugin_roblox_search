"""
render_utils.py —— 菜单图片渲染

通过 PIL 将功能菜单渲染为 PNG 图片。优先使用插件自带的 Noto Sans SC 字体
（OFL 开源协议，随插件分发），避免在无中文字体的 Docker/Linux 环境回退到
PIL 默认字体而显示方格子。
"""

import io
import os
from PIL import Image, ImageDraw, ImageFont

FONT_CACHE = {}


def get_font(size=14):
    cache_key = size
    if cache_key in FONT_CACHE:
        return FONT_CACHE[cache_key]

    # 插件内置字体优先（fonts/ 目录随插件分发）
    _plugin_dir = os.path.dirname(os.path.abspath(__file__))
    _internal_font = os.path.join(_plugin_dir, "fonts", "NotoSansSC-Regular.otf")

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
    font = get_font(16)
    title_font = get_font(26)
    small_font = get_font(12)

    content_width = 600
    padding = 40
    line_height = font.getbbox("A")[3] - font.getbbox("A")[1] + 8

    menu_items = [
        ("用户查询", [
            ("/用户名搜索 [用户名]", "按用户名查询用户完整资料"),
            ("/用户ID搜索 [数字ID]", "按用户ID直查用户资料"),
        ]),
        ("群组查询", [
            ("/群组名搜索 [群组名]", "模糊搜索群组并展示详情"),
            ("/群组ID搜索 [数字ID]", "群组详情与职位列表"),
        ]),
        ("游戏查询", [
            ("/游戏名搜索 [游戏名]", "搜索游戏、在线人数、访问量"),
            ("/游戏ID搜索 [数字ID]", "游戏详情与公开服务器"),
        ]),
        ("社交查询", [
            ("/获取好友列表 [用户ID]", "读取用户前10位好友"),
            ("/获取粉丝列表 [用户ID]", "读取用户前10位粉丝"),
            ("/获取关注列表 [用户ID]", "读取用户前10位关注"),
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

    footer_text = "Roblox 全功能查询插件 v1.3.0"
    bbox = small_font.getbbox(footer_text)
    footer_width = bbox[2] - bbox[0]
    draw.text(((content_width - footer_width) // 2, image_height - 30),
              footer_text, font=small_font, fill=(60, 80, 110))

    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    return buffer.getvalue()
