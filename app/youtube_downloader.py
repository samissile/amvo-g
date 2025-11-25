# app/youtube_downloader.py
import os
import yt_dlp
import tempfile
import logging
import asyncio
import base64
from typing import Tuple

logger = logging.getLogger(__name__)

PERSISTENT_COOKIE_PATH = "/mnt/data/cookies.txt"  # <--- The magical path
YT_DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "yt_downloads")
os.makedirs(YT_DOWNLOAD_DIR, exist_ok=True)

async def extract_title_only(url: str) -> str:
    """
    Extract only the video title without downloading
    """
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'socket_timeout': 15,
            'skip_download': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            },
        }
        
        loop = asyncio.get_event_loop()
        
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info
        
        info = await loop.run_in_executor(None, extract)
        
        if info and 'title' in info:
            title = info['title'].strip()
            logger.info(f"📺 Extracted title: {title}")
            return title
        
        return None
    
    except Exception as e:
        logger.warning(f"Failed to extract title: {e}")
        return None

async def download_audio_from_url(url: str, task_id: int) -> Tuple[str, str, int]:
    """
    Download audio from YouTube/video URL using yt-dlp
    Mono 96kbps MP3 format (Optimized for Speech AI)
    Returns: (file_path, title, duration_seconds)
    """

    cookie_path = None
    temp_cookie_file = None

    # ✅ 1. PRIORITY: Check for manually uploaded file in Persistent Storage
    if os.path.exists(PERSISTENT_COOKIE_PATH):
        cookie_path = PERSISTENT_COOKIE_PATH
        logger.info(f"🍪 Found Persistent Cookies at: {cookie_path}")

    # 2. Fallback: Check Environment Variable (Base64)
    elif os.getenv("YOUTUBE_COOKIES_B64"):
        try:
            b64_cookies = os.getenv("YOUTUBE_COOKIES_B64")
            temp_cookie_file = tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.txt')
            decoded_cookies = base64.b64decode(b64_cookies).decode('utf-8')
            temp_cookie_file.write(decoded_cookies)
            temp_cookie_file.close()
            cookie_path = temp_cookie_file.name
            logger.info("🍪 Using Cookies from Environment Variable")
        except Exception as e:
            logger.error(f"❌ Failed to decode cookie variable: {e}")

    # 3. Fallback: Local file in project folder
    elif os.path.exists(os.path.join(os.getcwd(), 'cookies.txt')):
         cookie_path = os.path.join(os.getcwd(), 'cookies.txt')
         logger.info("🍪 Using Cookies from local file")
   
    output_template = os.path.join(YT_DOWNLOAD_DIR, f"yt_{task_id}_%(title)s.%(ext)s")
    
    proxy_url = os.getenv("PROXY_URL")

    ydl_opts = {
        'format': 'bestaudio/best',
        'format_sort': ['res:360'],
        'cookiefile': cookie_path,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '64',
            'nopostoverwrites': False,
        }],
        'outtmpl': output_template,
        'quiet': False,
        'no_warnings': False,
        'socket_timeout': 60,
        'postprocessor_args': {
            'ffmpeg': ['-ar', '16000', '-ac', '1']
        },
        'prefer_ffmpeg': True,
        'keepvideo': False,
        'external_downloader': 'aria2c',
        'external_downloader_args': [
            '-x', '16',      # 16 connections per server
            '-s', '16',      # Split file into 16 parts
            '-k', '1M',      # Min split size
        ],
        'source_address': '0.0.0.0',
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        },
        # ✅ SIMPLIFIED: Remove problematic extractor_args
        'retries': 10,
        'fragment_retries': 10,
        'skip_unavailable_fragments': True,
        'ignore_errors': False,
        'no_check_certificate': True,
        # ✅ REMOVED: 'allow_unplayable_formats': True,
    }

    # ✅ Add Proxy if it exists
    if proxy_url:
        ydl_opts['proxy'] = proxy_url
        logger.info(f"🛡️ Using Proxy: {proxy_url[:15]}...***")
        
    try:
        logger.info(f"📥 Downloading audio from: {url}")
        
        loop = asyncio.get_event_loop()
        
        def download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info
        
        info = await loop.run_in_executor(None, download)
        
        if info is None:
            raise ValueError("Failed to extract video information")
        
        title = info.get('title', 'Unknown')
        duration = int(info.get('duration', 0))
        
        logger.info(f"📺 Title: {title}, Duration: {duration}s")
        
        import glob
        pattern = os.path.join(YT_DOWNLOAD_DIR, f"yt_{task_id}_*.mp3")
        files = glob.glob(pattern)
        
        if not files:
            pattern = os.path.join(YT_DOWNLOAD_DIR, f"yt_{task_id}_*")
            files = [f for f in glob.glob(pattern) if f.endswith(('.mp3', '.m4a', '.wav'))]
        
        if not files:
            raise FileNotFoundError(f"Downloaded file not found")
        
        file_path = files[0]
        file_size = os.path.getsize(file_path)
        logger.info(f"✅ Downloaded: {file_path} ({file_size/1024/1024:.2f}MB)")
        
        return file_path, title, duration
    
    except Exception as e:
        logger.error(f"❌ Download failed: {e}")
        raise ValueError(f"Failed to download from URL: {str(e)}")

    finally:
        # CLEANUP: Delete the temp cookie file to keep things clean
        if temp_cookie_file and os.path.exists(temp_cookie_file.name):
            try:
                os.unlink(temp_cookie_file.name)
            except:
                pass

def validate_video_url(url: str) -> bool:
    """Check if URL is supported by yt-dlp"""
    try:
        ydl_opts = {'quiet': True, 'simulate': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=False)
            return True
    except:
        return False