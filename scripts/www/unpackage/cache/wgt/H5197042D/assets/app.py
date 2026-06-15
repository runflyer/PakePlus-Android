"""
视频下载工具 - 后端服务
基于 Flask + yt-dlp 实现多平台视频解析与下载
兼容 Windows / Termux / Android(Kivy)
"""

import os
import re
import json
import uuid
import threading
import subprocess
import sys
import requests
import shutil
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

# 下载任务存储（内存中）
download_tasks = {}


# ── Android / 环境检测 ──

def is_android():
    """检测是否运行在 Android 上（Kivy 打包或 Termux）"""
    if hasattr(sys, 'getandroidapilevel'):
        return True
    try:
        from android import mActivity
        return True
    except Exception:
        pass
    home = str(Path.home())
    if 'com.termux' in home or 'data/data' in home:
        return True
    return False


def get_ffmpeg_path():
    """获取 ffmpeg 路径，Android 上可能在 /data/data/.../files/ 下"""
    # 1. 检查系统 PATH
    path = shutil.which('ffmpeg')
    if path:
        return path

    # 2. Android 常见路径
    if is_android():
        android_paths = [
            os.path.join(os.path.dirname(sys.executable), 'ffmpeg'),
            '/data/data/org.kivy.android.python/files/ffmpeg',
            os.path.join(os.environ.get('HOME', ''), 'ffmpeg'),
        ]
        for p in android_paths:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p

    return 'ffmpeg'  # 回退到默认，可能不存在但不会阻止解析


def get_default_download_dir():
    """获取默认下载目录（优先相册可见的路径）"""
    if is_android():
        # Termux 软链接 → 真实路径映射:
        #   ~/storage/movies   → /storage/emulated/0/Movies   (相册可见 ✅)
        #   ~/storage/dcim     → /storage/emulated/0/DCIM     (相册可见 ✅)
        #   ~/storage/downloads → /storage/emulated/0/Download (相册不可见 ❌)
        candidates = [
            Path.home() / 'storage' / 'movies' / 'video-downloader',
            Path.home() / 'storage' / 'dcim' / 'video-downloader',
            Path.home() / 'storage' / 'downloads' / 'video-downloader',
            Path('/storage/emulated/0/Movies/video-downloader'),
            Path('/sdcard/Movies/video-downloader'),
            Path('/storage/emulated/0/DCIM/video-downloader'),
            Path('/sdcard/DCIM/video-downloader'),
            Path.home() / 'videos',
        ]
        for c in candidates:
            try:
                c.mkdir(parents=True, exist_ok=True)
                return str(c)
            except Exception:
                continue
    return str(Path.home() / 'Downloads')


def _notify_media_scanner(filepath):
    """通知 Android 系统刷新媒体库，视频立刻出现在相册"""
    try:
        subprocess.run(
            ['am', 'broadcast', '-a', 'android.intent.action.MEDIA_SCANNER_SCAN_FILE',
             '-d', f'file://{filepath}'],
            capture_output=True, timeout=5
        )
    except Exception:
        pass

# ── 全局环境变量（供 yt-dlp 使用） ──
_ANDROID = is_android()
_FFMPEG_PATH = get_ffmpeg_path()
_DEFAULT_DIR = get_default_download_dir()


def get_referer_headers(url):
    """根据 URL 动态生成伪装请求头"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    site = parsed.netloc.lower()
    domain = f"{parsed.scheme}://{parsed.netloc}"

    # 平台域名 → 正确的 Referer/Origin
    platform_map = {
        'bilibili': ('https://www.bilibili.com/', 'https://www.bilibili.com'),
        'b23.tv': ('https://www.bilibili.com/', 'https://www.bilibili.com'),
        'douyin': ('https://www.douyin.com/', 'https://www.douyin.com'),
        'ixigua': ('https://www.ixigua.com/', 'https://www.ixigua.com'),
        'kuaishou': ('https://www.kuaishou.com/', 'https://www.kuaishou.com'),
        'weishi': ('https://h5.weishi.qq.com/', 'https://h5.weishi.qq.com'),
        'xiaohongshu': ('https://www.xiaohongshu.com/', 'https://www.xiaohongshu.com'),
        'xhslink.com': ('https://www.xiaohongshu.com/', 'https://www.xiaohongshu.com'),
        'youku': ('https://www.youku.com/', 'https://www.youku.com'),
        'toutiao': ('https://www.toutiao.com/', 'https://www.toutiao.com'),
        '365yg.com': ('https://www.toutiao.com/', 'https://www.toutiao.com'),
        'weibo': ('https://weibo.com/', 'https://weibo.com'),
        'zhihu': ('https://www.zhihu.com/', 'https://www.zhihu.com'),
        'youtube': ('https://www.youtube.com/', 'https://www.youtube.com'),
        'youtu.be': ('https://www.youtube.com/', 'https://www.youtube.com'),
    }

    referer, origin = None, None
    for key, (ref, org) in platform_map.items():
        if key in site:
            referer, origin = ref, org
            break

    if referer is None:
        referer = domain + '/'
        origin = domain

    return [
        '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        '--add-header', f'Origin:{origin}',
        '--add-header', f'Referer:{referer}',
        '--add-header', 'Accept-Language:zh-CN,zh;q=0.9,en;q=0.8',
    ]


def detect_platform(url):
    """检测链接属于哪个平台"""
    site = urlparse(url).netloc.lower()
    # 抖音
    if 'douyin' in site:
        return 'douyin'
    # 快手
    if 'kuaishou' in site:
        return 'kuaishou'
    # 小红书
    if 'xiaohongshu' in site or 'xhslink' in site:
        return 'xiaohongshu'
    # B站
    if 'bilibili' in site or 'b23.tv' in site:
        return 'bilibili'
    # 西瓜视频
    if 'ixigua' in site:
        return 'ixigua'
    # 今日头条
    if 'toutiao' in site or '365yg.com' in site:
        return 'toutiao'
    # 微博
    if 'weibo.com' in site or 'weibo.cn' in site:
        return 'weibo'
    # 皮皮虾 / 最右
    if 'pipix.com' in site or 'ippzone' in site:
        return 'pipixia'
    if 'izuiyou' in site:
        return 'zuiyou'
    # 知乎
    if 'zhihu.com' in site:
        return 'zhihu'
    # 百度系
    if 'haokan.baidu' in site or 'baidu.com' in site:
        return 'baidu'
    # 腾讯系
    if 'v.qq.com' in site:
        return 'tencent_video'
    if 'weishi.qq.com' in site:
        return 'weishi'
    if 'channels.weixin.qq.com' in site:
        return 'weixin_channels'
    # 优酷
    if 'youku.com' in site:
        return 'youku'
    # 爱奇艺
    if 'iqiyi.com' in site:
        return 'iqiyi'
    # 芒果TV
    if 'mgtv.com' in site:
        return 'mgtv'
    # 搜狐
    if 'sohu.com' in site:
        return 'sohu'
    # 海外
    if 'tiktok.com' in site:
        return 'tiktok'
    if 'instagram.com' in site:
        return 'instagram'
    if 'youtube.com' in site or 'youtu.be' in site:
        return 'youtube'
    if 'twitter.com' in site or 'x.com' in site:
        return 'twitter'
    if 'facebook.com' in site or 'fb.watch' in site:
        return 'facebook'
    if 'reddit.com' in site:
        return 'reddit'
    if 'vimeo.com' in site:
        return 'vimeo'
    # 直播
    if 'huya.com' in site:
        return 'huya'
    if 'douyu.com' in site:
        return 'douyu'
    # 音乐
    if 'kg.qq.com' in site:
        return 'quanminkge'
    if 'music.163.com' in site:
        return 'netease_music'
    if 'kugou.com' in site:
        return 'kugou'
    if 'kuwo.cn' in site:
        return 'kuwo'
    if 'y.qq.com' in site:
        return 'qq_music'
    return 'other'


# ==================== 平台名称映射 ====================
PLATFORM_NAMES = {
    'douyin': '抖音',
    'kuaishou': '快手',
    'xiaohongshu': '小红书',
    'bilibili': 'B站',
    'ixigua': '西瓜视频',
    'toutiao': '今日头条',
    'weibo': '微博',
    'pipixia': '皮皮虾',
    'zuiyou': '最右',
    'zhihu': '知乎',
    'baidu': '百度/好看视频',
    'tencent_video': '腾讯视频',
    'weishi': '腾讯微视',
    'weixin_channels': '微信视频号',
    'youku': '优酷',
    'iqiyi': '爱奇艺',
    'mgtv': '芒果TV',
    'sohu': '搜狐视频',
    'tiktok': 'TikTok',
    'instagram': 'Instagram',
    'youtube': 'YouTube',
    'twitter': 'Twitter/X',
    'huya': '虎牙',
    'douyu': '斗鱼',
    'quanminkge': '全民K歌',
    'netease_music': '网易云音乐',
    'kugou': '酷狗音乐',
    'kuwo': '酷我音乐',
    'qq_music': 'QQ音乐',
    'facebook': 'Facebook',
    'reddit': 'Reddit',
    'vimeo': 'Vimeo',
    'other': '其他平台',
}


# ==================== 快手自定义下载 (yt-dlp 不支持) ====================
def parse_kuaishou(url):
    """解析快手视频"""
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    }

    try:
        # 步骤1: 解析短链接 → 获取真实页面URL
        resp = session.get(url, headers=headers, allow_redirects=True, timeout=30)
        final_url = resp.url

        # 步骤2: 从页面HTML中提取视频数据
        html = resp.text

        # 尝试匹配 window.__INITIAL_STATE__
        match = re.search(r'<script>\s*window\.__INITIAL_STATE__\s*=\s*({.*?})\s*</script>', html, re.DOTALL)
        if not match:
            match = re.search(r'__INITIAL_STATE__\s*=\s*({.*?});', html, re.DOTALL)

        if match:
            try:
                state = json.loads(match.group(1))
                # 获取视频信息
                photo = state.get('photo', {})
                if not photo:
                    # 尝试其他路径
                    for key in ['video', 'feed', 'shortVideo']:
                        if key in state:
                            photo = state[key]
                            break

                title = photo.get('caption', '快手视频')
                # 获取无水印视频URL
                video_url = ''
                manifest = photo.get('manifest', {})
                if manifest:
                    adaptation_set = manifest.get('adaptationSet', [{}])[0]
                    representation = adaptation_set.get('representation', [{}])[0]
                    video_url = representation.get('backupUrl', [''])[0] or representation.get('url', '')

                # 也可以用 photoUrl 降级
                if not video_url:
                    video_url = photo.get('photoUrl', '') or photo.get('videoUrl', '')

                if video_url:
                    thumb = photo.get('coverUrl', '') or photo.get('coverUrls', [{}])[0].get('url', '')
                    return {
                        'title': str(title)[:100],
                        'description': '',
                        'thumbnail': thumb,
                        'duration': int(photo.get('duration', 0)),
                        'uploader': photo.get('userName', photo.get('author', {}).get('name', '')),
                        'webpage_url': final_url,
                        'extractor': 'kuaishou',
                        'formats': [{'format_id': 'best', 'ext': 'mp4', 'height': 0, 'quality': '原画', 'filesize': '', 'is_audio': False}],
                        '_video_url': video_url,
                        '_is_custom': True,
                        '_platform': 'kuaishou',
                    }, None
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                pass

        # 尝试用正则从页面直接提取视频URL
        video_patterns = [
            r'srcNoMark\s*=\s*["\']([^"\']+)["\']',
            r'photoUrl\s*=\s*["\']([^"\']+)["\']',
            r'"photoUrl"\s*:\s*"([^"]+)"',
            r'"url"\s*:\s*"([^"]+\.mp4[^"]*)"',
        ]
        for pattern in video_patterns:
            m = re.search(pattern, html)
            if m:
                video_url = m.group(1).replace('\\u002F', '/')
                title_match = re.search(r'"caption"\s*:\s*"([^"]*)"', html)
                title = title_match.group(1) if title_match else '快手视频'
                return {
                    'title': str(title)[:100],
                    'description': '',
                    'thumbnail': '',
                    'duration': 0,
                    'uploader': '',
                    'webpage_url': final_url,
                    'extractor': 'kuaishou',
                    'formats': [{'format_id': 'best', 'ext': 'mp4', 'height': 0, 'quality': '原画', 'filesize': '', 'is_audio': False}],
                    '_video_url': video_url,
                    '_is_custom': True,
                    '_platform': 'kuaishou',
                }, None

        return None, '快手视频解析失败，可能链接已失效或需要登录'

    except requests.RequestException as e:
        return None, f'快手请求失败: {str(e)}'
    except Exception as e:
        return None, f'快手解析出错: {str(e)}'


def download_kuaishou_video(url, output_path, task):
    """下载快手视频（直接HTTP下载）"""
    try:
        # 先解析获取视频URL
        info, error = parse_kuaishou(url)
        if error or not info:
            task['status'] = 'failed'
            task['error'] = error or '解析失败'
            return

        video_url = info.get('_video_url', '')
        if not video_url:
            task['status'] = 'failed'
            task['error'] = '未能获取视频地址'
            return

        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
            'Referer': 'https://www.kuaishou.com/',
        }

        resp = requests.get(video_url, headers=headers, stream=True, timeout=120)
        resp.raise_for_status()

        total_size = int(resp.headers.get('content-length', 0))
        downloaded = 0

        safe_name = sanitize_filename(info.get('title', '快手视频'))
        output_file = Path(output_path) / f'{safe_name}.mp4'

        with open(output_file, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        task['progress'] = min(int(downloaded / total_size * 100), 99)

        task['progress'] = 100
        task['status'] = 'completed'
        task['output_file'] = str(output_file)
        task['output_filename'] = output_file.name

    except Exception as e:
        task['status'] = 'failed'
        task['error'] = f'快手下载失败: {str(e)}'


# ==================== 微视自定义下载 (yt-dlp 不支持) ====================
def parse_weishi(url):
    """解析微视视频"""
    try:
        # 从URL提取feedid
        parsed = urlparse(url)
        feedid = None
        path_parts = parsed.path.strip('/').split('/')
        for part in path_parts:
            if part and part.isalnum() and len(part) > 10:
                feedid = part
                break
        if not feedid:
            # 尝试从查询参数获取
            qs = parse_qs(parsed.query)
            feedid = qs.get('feedid', [None])[0]

        if not feedid:
            return None, '未能从链接中提取视频ID'

        api_url = f'https://h5.weishi.qq.com/webapp/json/weishi/WSH5GetPlayPage?feedid={feedid}'
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
            'Referer': 'https://h5.weishi.qq.com/',
        }
        resp = requests.get(api_url, headers=headers, timeout=30)
        data = resp.json()

        if data.get('ret') != 0:
            return None, f'微视API返回错误: {data.get("msg", "未知错误")}'

        feed = data.get('data', {}).get('feed', {})
        title = feed.get('feed_desc', '微视视频')
        video_url = feed.get('video_url', feed.get('video_spec_urls', [{}])[0].get('url', ''))
        thumb = feed.get('cover_url', '')
        poster = feed.get('poster', {}).get('nick', '')
        duration = feed.get('video_duration', 0)

        if not video_url:
            return None, '未能获取微视视频地址'

        return {
            'title': str(title)[:100],
            'description': '',
            'thumbnail': thumb,
            'duration': int(duration) if duration else 0,
            'uploader': poster,
            'webpage_url': url,
            'extractor': 'weishi',
            'formats': [{'format_id': 'best', 'ext': 'mp4', 'height': 0, 'quality': '原画', 'filesize': '', 'is_audio': False}],
            '_video_url': video_url,
            '_is_custom': True,
            '_platform': 'weishi',
        }, None

    except requests.RequestException as e:
        return None, f'微视请求失败: {str(e)}'
    except Exception as e:
        return None, f'微视解析出错: {str(e)}'


def download_weishi_video(url, output_path, task):
    """下载微视视频（直接HTTP下载）"""
    try:
        info, error = parse_weishi(url)
        if error or not info:
            task['status'] = 'failed'
            task['error'] = error or '解析失败'
            return

        video_url = info.get('_video_url', '')
        if not video_url:
            task['status'] = 'failed'
            task['error'] = '未能获取视频地址'
            return

        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
            'Referer': 'https://h5.weishi.qq.com/',
        }

        resp = requests.get(video_url, headers=headers, stream=True, timeout=120)
        resp.raise_for_status()

        total_size = int(resp.headers.get('content-length', 0))
        downloaded = 0

        safe_name = sanitize_filename(info.get('title', '微视视频'))
        output_file = Path(output_path) / f'{safe_name}.mp4'

        with open(output_file, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        task['progress'] = min(int(downloaded / total_size * 100), 99)

        task['progress'] = 100
        task['status'] = 'completed'
        task['output_file'] = str(output_file)
        task['output_filename'] = output_file.name

    except Exception as e:
        task['status'] = 'failed'
        task['error'] = f'微视下载失败: {str(e)}'


# ==================== 小红书自定义解析 (解决 cookies 问题) ====================
def parse_xiaohongshu(url):
    """解析小红书视频/图文"""
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Referer': 'https://www.xiaohongshu.com/',
    }

    try:
        resp = session.get(url, headers=headers, timeout=30)
        html = resp.text

        # 提取 window.__INITIAL_STATE__
        match = re.search(
            r'window\.__INITIAL_STATE__\s*=\s*({.*?})\s*</script>',
            html, re.DOTALL
        )
        if not match:
            match = re.search(r'__INITIAL_STATE__\s*=\s*({.*?});', html, re.DOTALL)

        if not match:
            # 可能是需要登录的页面
            if 'login' in html[:500].lower() or 'captcha' in html[:500].lower():
                return None, '小红书需要登录才能查看此内容，请在小红书APP中打开并复制新的分享链接'
            return None, '小红书页面解析失败，请确认链接有效或重新复制分享链接'

        raw_json = match.group(1).replace('undefined', 'null')
        state = json.loads(raw_json)

        # 获取视频信息
        display_id = None
        parsed = urlparse(url)
        m = re.search(r'/(?:explore|discovery/item)/([a-f0-9]+)', parsed.path)
        if m:
            display_id = m.group(1)

        if not display_id:
            return None, '未能从链接中提取小红书笔记ID'

        note_map = state.get('note', {}).get('noteDetailMap', {})
        note_info = note_map.get(display_id, {}).get('note', {})

        # 检查是否找到笔记
        if not note_info or not note_info.get('noteId'):
            # 可能 xsec_token 过期
            if display_id not in note_map:
                return None, '无法访问该小红书笔记，链接中的访问凭证可能已过期，请重新从小红书APP复制分享链接'

        title = note_info.get('title', note_info.get('desc', '小红书笔记'))
        uploader_info = note_info.get('user', {})
        uploader = uploader_info.get('nickname', uploader_info.get('nickName', ''))
        desc = (note_info.get('desc', '') or '')[:200]
        note_type = note_info.get('type', '')

        # 获取封面
        image_list = note_info.get('imageList', [])
        thumbnail = ''
        if image_list:
            thumb_info = image_list[0]
            thumbnail = thumb_info.get('urlDefault', thumb_info.get('url', ''))

        # 获取视频流
        video_streams = note_info.get('video', {}).get('media', {}).get('stream', [])
        video_url = ''

        if video_streams:
            for stream_item in video_streams:
                if isinstance(stream_item, dict):
                    for quality_key, quality_data in stream_item.items():
                        if isinstance(quality_data, dict):
                            master_url = quality_data.get('masterUrl', '')
                            backup_urls = quality_data.get('backupUrls', [])
                            urls = [master_url] + (backup_urls if isinstance(backup_urls, list) else [])
                            urls = [u for u in urls if u and isinstance(u, str)]
                            if urls and not video_url:
                                video_url = urls[0]

        # 尝试获取原始视频
        origin_key = note_info.get('video', {}).get('consumer', {}).get('originVideoKey', '')
        if origin_key:
            video_url = f'https://sns-video-bd.xhscdn.com/{origin_key}'

        if not video_url:
            # 没有视频 - 可能是图文帖
            if image_list:
                img_count = len(image_list)
                return None, f'该小红书笔记是图文帖（含{img_count}张图片），不是视频，无法下载。请确认是视频链接'
            return None, '该小红书笔记不包含视频，可能是图文内容或链接已失效'

        return {
            'title': str(title)[:100],
            'description': desc,
            'thumbnail': thumbnail,
            'duration': 0,
            'uploader': uploader,
            'webpage_url': url,
            'extractor': 'xiaohongshu',
            'formats': [{'format_id': 'best', 'ext': 'mp4', 'height': 0, 'quality': '原画', 'filesize': '', 'is_audio': False}],
            '_video_url': video_url,
            '_is_custom': True,
            '_platform': 'xiaohongshu',
        }, None

    except requests.RequestException as e:
        return None, f'小红书请求失败: {str(e)}'
    except json.JSONDecodeError:
        return None, '小红书页面数据解析异常，可能需要更新解析规则'
    except Exception as e:
        return None, f'小红书解析出错: {str(e)}'


def download_xiaohongshu_video(url, output_path, task):
    """下载小红书视频（直接HTTP下载）"""
    try:
        info, error = parse_xiaohongshu(url)
        if error or not info:
            task['status'] = 'failed'
            task['error'] = error or '解析失败'
            return

        video_url = info.get('_video_url', '')
        if not video_url:
            task['status'] = 'failed'
            task['error'] = '未能获取视频地址'
            return

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.xiaohongshu.com/',
        }

        resp = requests.get(video_url, headers=headers, stream=True, timeout=120)
        resp.raise_for_status()

        total_size = int(resp.headers.get('content-length', 0))
        downloaded = 0

        safe_name = sanitize_filename(info.get('title', '小红书视频'))
        output_file = Path(output_path) / f'{safe_name}.mp4'

        with open(output_file, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        task['progress'] = min(int(downloaded / total_size * 100), 99)

        task['progress'] = 100
        task['status'] = 'completed'
        task['output_file'] = str(output_file)
        task['output_filename'] = output_file.name

    except Exception as e:
        task['status'] = 'failed'
        task['error'] = f'小红书下载失败: {str(e)}'


# ==================== 视频号下载 (尝试通用提取器) ====================
def parse_weixin_channels(url):
    """尝试解析微信视频号（yt-dlp 官方不支持，尝试通用提取）"""
    # 微信视频号URL示例: https://channels.weixin.qq.com/...
    # yt-dlp 使用通用提取器尝试
    cmd = get_yt_dlp_path()
    args = cmd + [
        url,
        '--dump-json',
        '--no-playlist',
        '--socket-timeout', '30',
        '--no-warnings',
        '--ignore-errors',
        '--force-generic-extractor',
        '--user-agent', 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
    ]

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return None, '微信视频号解析失败，该平台限制较严格，建议使用手机APP观看或尝试其他下载工具'

        info = json.loads(result.stdout)
        return {
            'title': info.get('title', '视频号视频'),
            'description': (info.get('description', '') or '')[:200],
            'thumbnail': info.get('thumbnail', ''),
            'duration': info.get('duration', 0),
            'uploader': info.get('uploader', ''),
            'webpage_url': info.get('webpage_url', url),
            'extractor': 'generic',
            'formats': [{'format_id': 'best', 'ext': 'mp4', 'height': 0, 'quality': '原画', 'filesize': '', 'is_audio': False}],
            '_is_custom': False,
            '_platform': 'weixin_channels',
        }, None

    except subprocess.TimeoutExpired:
        return None, '解析超时，微信视频号访问可能需要特定网络环境'
    except json.JSONDecodeError:
        return None, '微信视频号解析失败，该平台内容通常需要APP内访问'
    except Exception as e:
        return None, f'视频号解析出错: {str(e)}'


def sanitize_filename(name):
    """清理文件名中的非法字符"""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip().strip('.')
    return name if name else 'video'


def get_yt_dlp_path():
    """获取 yt-dlp 可执行文件路径，优先使用 pip 安装的"""
    try:
        result = subprocess.run([sys.executable, '-m', 'yt_dlp', '--version'],
                                capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return [sys.executable, '-m', 'yt_dlp']
    except Exception:
        pass
    return ['yt-dlp']


def get_ytdlp_base_args():
    """获取 yt-dlp 基础参数（含 Android ffmpeg 路径等）"""
    args = []
    ff = _FFMPEG_PATH
    if ff and ff != 'ffmpeg' and os.path.isfile(ff):
        args += ['--ffmpeg-location', ff]
    return args


def parse_with_ytdlp(url):
    """使用 yt-dlp 解析视频信息（极速模式：少量重试、短超时）"""
    cmd = get_yt_dlp_path()
    args = cmd + get_ytdlp_base_args() + [
        url,
        '--dump-json',
        '--no-playlist',
        '--socket-timeout', '8',
        '--retries', '2',
        '--extractor-retries', '2',
        '--fragment-retries', '2',
        '--no-check-certificate',
        '--no-warnings',
        '--ignore-errors',
    ] + get_referer_headers(url)

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=25)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if 'Unsupported URL' in stderr:
                return None, '不支持的视频链接，请检查地址是否正确'
            if 'HTTP Error 403' in stderr:
                return None, '访问被拒绝，该平台可能需要登录信息'
            if 'HTTP Error 404' in stderr:
                return None, '视频不存在或已被删除'
            return None, f'解析失败: {stderr[-300:] if stderr else "未知错误"}'

        info = json.loads(result.stdout)

        # 提取关键信息
        formats = []
        seen_qualities = set()
        for fmt in info.get('formats', []):
            if fmt.get('vcodec') == 'none' and fmt.get('acodec') == 'none':
                continue
            height = fmt.get('height') or 0
            width = fmt.get('width') or 0
            ext = fmt.get('ext', 'unknown')
            filesize = fmt.get('filesize') or fmt.get('filesize_approx')
            format_id = fmt.get('format_id', '')
            format_note = fmt.get('format_note', '')
            vcodec = fmt.get('vcodec', '')
            acodec = fmt.get('acodec', '')
            tbr = fmt.get('tbr') or 0

            quality_key = f"{height}p_{ext}_{vcodec}_{format_note}"
            if quality_key in seen_qualities:
                existing = next((f for f in formats if f.get('_key') == quality_key), None)
                if existing and tbr > existing.get('_tbr', 0):
                    formats.remove(existing)
                    seen_qualities.discard(quality_key)
                else:
                    continue
            seen_qualities.add(quality_key)

            size_str = ''
            if filesize:
                if filesize > 1024 * 1024 * 1024:
                    size_str = f'{filesize / (1024**3):.1f}GB'
                elif filesize > 1024 * 1024:
                    size_str = f'{filesize / (1024**2):.1f}MB'
                else:
                    size_str = f'{filesize / 1024:.1f}KB'

            is_audio = fmt.get('vcodec') == 'none' and fmt.get('acodec') != 'none'

            if format_note:
                label = format_note
            elif height:
                label = f'{height}P'
                if width >= 3840:
                    label = '4K'
                elif width >= 2560:
                    label = f'{height}P(2K)'
            else:
                label = ext.upper()

            formats.append({
                'format_id': format_id,
                'ext': ext,
                'height': height,
                'quality': label,
                'filesize': size_str,
                'is_audio': is_audio,
                '_key': quality_key,
                '_tbr': tbr,
            })

        formats.sort(key=lambda f: (f['is_audio'], -f['height']))
        clean_formats = [{
            'format_id': f['format_id'],
            'ext': f['ext'],
            'height': f['height'],
            'quality': f['quality'],
            'filesize': f['filesize'],
            'is_audio': f['is_audio'],
        } for f in formats]

        return {
            'title': info.get('title', '未知标题'),
            'description': (info.get('description', '') or '')[:200],
            'thumbnail': info.get('thumbnail', ''),
            'duration': info.get('duration', 0),
            'uploader': info.get('uploader', ''),
            'webpage_url': info.get('webpage_url', url),
            'extractor': info.get('extractor_key', ''),
            'formats': clean_formats,
            '_is_custom': False,
            '_platform': detect_platform(url),
        }, None

    except subprocess.TimeoutExpired:
        return None, '解析超时，请检查网络连接或稍后重试'
    except json.JSONDecodeError:
        return None, '解析响应异常，该平台可能需要更新解析规则'
    except Exception as e:
        return None, f'解析出错: {str(e)}'


def resolve_shortlink(url):
    """解析短链重定向，返回真实 URL。头条/知乎/微博等短链需要先解析"""
    if '/is/' not in url and '/s/' not in url and 'b23.tv' not in url:
        return url
    try:
        session = requests.Session()
        resp = session.head(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }, allow_redirects=True, timeout=5)
        final_url = resp.url
        if final_url != url and final_url:
            return final_url
    except Exception:
        pass
    try:
        session = requests.Session()
        resp = session.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }, allow_redirects=False, timeout=5)
        if resp.status_code in (301, 302) and resp.headers.get('Location'):
            return requests.compat.urljoin(url, resp.headers['Location'])
    except Exception:
        pass
    return url


def parse_video_info(url):
    """解析视频信息 - 根据平台路由到不同解析器"""
    # 头条短链、B站短链等先解析重定向
    url = resolve_shortlink(url)
    platform = detect_platform(url)

    # ===== 快手: 使用自定义解析器 (yt-dlp 不支持) =====
    if platform == 'kuaishou':
        return parse_kuaishou(url)

    # ===== 微视: 使用自定义解析器 (yt-dlp 不支持) =====
    if platform == 'weishi':
        return parse_weishi(url)

    # ===== 视频号: 尝试通用提取 =====
    if platform == 'weixin_channels':
        return parse_weixin_channels(url)

    # ===== 小红书: 使用自定义解析器 (避免 cookie 问题) =====
    if platform == 'xiaohongshu':
        return parse_xiaohongshu(url)

    # ===== 抖音/B站/西瓜/其他: 标准 yt-dlp 解析 =====
    return parse_with_ytdlp(url)


def download_video_task(task_id, url, save_path, file_name, format_id):
    """后台下载任务 - 根据平台路由到不同下载器"""
    task = download_tasks.get(task_id)
    if not task:
        return

    task['status'] = 'downloading'
    task['progress'] = 0

    # 先解析短链重定向（头条/b站等），避免旧版 yt-dlp 跟丢
    resolved = resolve_shortlink(url)
    if resolved != url:
        task['detail'] = f'短链已解析: {url} → {resolved}'
    url = resolved

    platform = detect_platform(url)

    # ===== 快手: 使用自定义HTTP下载 =====
    if platform == 'kuaishou':
        download_kuaishou_video(url, save_path, task)
        return

    # ===== 微视: 使用自定义HTTP下载 =====
    if platform == 'weishi':
        download_weishi_video(url, save_path, task)
        return

    # ===== 小红书: 使用自定义HTTP下载 =====
    if platform == 'xiaohongshu':
        download_xiaohongshu_video(url, save_path, task)
        return

    # ===== 其他平台: 使用 yt-dlp =====
    safe_name = sanitize_filename(file_name)
    output_template = str(Path(save_path) / f'{safe_name}.%(ext)s')

    cmd = get_yt_dlp_path()
    args = cmd + get_ytdlp_base_args() + [
        url,
        '-o', output_template,
        '--no-playlist',
        '--socket-timeout', '60',
        '--newline',
        '--progress',
    ] + get_referer_headers(url)

    # Android 无 ffmpeg 时优先下载单文件格式（避免需要合并）
    if _ANDROID and not shutil.which('ffmpeg'):
        if format_id and format_id != 'best':
            args += ['-f', format_id, '--merge-output-format', 'mp4']
        else:
            # 优先选择包含视频+音频的单一格式
            args += ['-f', 'best[height<=1080]', '--merge-output-format', 'mp4']
    elif format_id and format_id != 'best':
        args += ['-f', format_id]

    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        last_line = ''
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            last_line = line

            progress_match = re.search(r'\[download\]\s+(\d+\.?\d*)%', line)
            if progress_match:
                task['progress'] = float(progress_match.group(1))
                task['status'] = 'downloading'

            if 'has already been downloaded' in line.lower():
                task['progress'] = 100
                task['status'] = 'completed'
                break

            if 'merging formats' in line.lower() or '[ffmpeg]' in line.lower():
                task['progress'] = 90
                task['status'] = 'processing'

        process.wait()

        if process.returncode == 0:
            download_dir = Path(save_path)
            downloaded_files = sorted(
                download_dir.glob(f'{safe_name}.*'),
                key=lambda f: f.stat().st_mtime,
                reverse=True
            )
            if downloaded_files:
                task['output_file'] = str(downloaded_files[0])
                task['output_filename'] = downloaded_files[0].name
                # Android 上通知系统刷新相册
                if _ANDROID:
                    _notify_media_scanner(str(downloaded_files[0]))
            task['status'] = 'completed'
            task['progress'] = 100
        else:
            task['status'] = 'failed'
            task['error'] = last_line or '下载失败，请检查视频链接是否有效'

    except Exception as e:
        task['status'] = 'failed'
        task['error'] = str(e)


# ========== API 路由 ==========

@app.route('/')
def index():
    return send_file('templates/index.html')


@app.route('/app')
def app_mobile():
    """HBuilder X 打包用的移动端页面（测试用）"""
    return send_file('hbuilder/index.html')


@app.route('/api/quick-resolve', methods=['POST'])
def quick_resolve():
    """快速解析短链（仅重定向跟随，不调用 yt-dlp，1-3 秒内返回）"""
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({'success': False, 'error': '请提供视频地址'}), 400
    url = data['url'].strip()
    if not url:
        return jsonify({'success': False, 'error': '请提供视频地址'}), 400
    resolved = resolve_shortlink(url)
    return jsonify({
        'success': True,
        'original': url,
        'resolved': resolved,
        'changed': resolved != url,
    })


@app.route('/api/parse', methods=['POST'])
def parse_video():
    """解析视频信息"""
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({'success': False, 'error': '请提供视频地址'}), 400

    url = data['url'].strip()
    if not url:
        return jsonify({'success': False, 'error': '请提供视频地址'}), 400

    info, error = parse_video_info(url)
    if error:
        return jsonify({'success': False, 'error': error}), 400

    return jsonify({'success': True, 'data': info})


@app.route('/api/download', methods=['POST'])
def start_download():
    """开始下载视频"""
    data = request.get_json()
    url = (data.get('url') or '').strip()
    save_path = (data.get('save_path') or '').strip()
    file_name = (data.get('file_name') or '').strip()
    format_id = data.get('format_id') or 'best'

    if not url:
        return jsonify({'success': False, 'error': '请提供视频地址'}), 400
    if not save_path:
        save_path = _DEFAULT_DIR
    if not file_name:
        file_name = 'video'

    # 确保保存目录存在
    try:
        Path(save_path).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({'success': False, 'error': f'无法创建下载目录: {str(e)}'}), 400

    task_id = str(uuid.uuid4())[:8]
    download_tasks[task_id] = {
        'id': task_id,
        'url': url,
        'save_path': save_path,
        'file_name': file_name,
        'status': 'starting',
        'progress': 0,
    }

    # 启动后台下载线程
    thread = threading.Thread(
        target=download_video_task,
        args=(task_id, url, save_path, file_name, format_id),
        daemon=True
    )
    thread.start()

    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/progress/<task_id>', methods=['GET'])
def get_progress(task_id):
    """查询下载进度"""
    task = download_tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'}), 404

    return jsonify({
        'success': True,
        'task_id': task['id'],
        'status': task['status'],
        'progress': task['progress'],
        'error': task.get('error', ''),
        'output_file': task.get('output_file', ''),
        'output_filename': task.get('output_filename', ''),
    })


@app.route('/api/browse', methods=['GET'])
def browse_directory():
    """浏览本地目录结构（简化版，返回常用目录）"""
    home = Path.home()

    # 检测是否在 Termux 中运行
    is_termux = (home / '../usr').exists() or 'com.termux' in str(home)

    common_dirs = [
        {'name': '视频下载目录', 'path': str(home / 'storage' / 'downloads' / 'video-downloader')},
        {'name': '手机下载文件夹', 'path': str(home / 'storage' / 'downloads')},
        {'name': '手机内部存储', 'path': str(home / 'storage' / 'shared')},
        {'name': 'Termux 主目录', 'path': str(home)},
    ]

    if not is_termux:
        common_dirs = [
            {'name': '下载文件夹', 'path': str(home / 'Downloads')},
            {'name': '桌面', 'path': str(home / 'Desktop')},
            {'name': '视频文件夹', 'path': str(home / 'Videos')},
            {'name': '用户主目录', 'path': str(home)},
        ]

    # 只返回存在的目录，不存在的也返回（会在创建时自动建）
    available = []
    for d in common_dirs:
        p = Path(d['path'])
        if p.exists():
            available.append(d)
        elif d['name'] == '视频下载目录':
            # Termux 下载目录可能还不存在，提前创建
            try:
                p.mkdir(parents=True, exist_ok=True)
                available.append(d)
            except Exception:
                pass

    if not available:
        available.append({'name': 'Termux主目录' if is_termux else '用户主目录', 'path': str(home)})

    return jsonify({'success': True, 'directories': available, 'is_termux': is_termux})


if __name__ == '__main__':
    print('=' * 50)
    print('  视频下载工具 服务端已启动')
    print(f'  访问地址: http://127.0.0.1:5000')
    print('=' * 50)
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
