# app/transcriber.py
import os
import json
import subprocess
import time
from typing import Tuple, List, Optional
from app.db import update_task
from dotenv import load_dotenv
import asyncio
import logging
import tempfile
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import google.generativeai as genai

logger = logging.getLogger(__name__)
load_dotenv()

# === CONFIG ===
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("❌ GEMINI_API_KEY not set in .env")

# Grok-3 Configuration (NEW)
GROK_API_ENDPOINT = os.getenv('AI_SUMMARY_API_ENDPOINT', 'https://api.bltcy.ai/v1/chat/completions')
GROK_API_AUTHORIZATION_HEADER = os.getenv('AI_SUMMARY_API_AUTHORIZATION_HEADER')
GROK_MODEL_NAME = os.getenv('AI_SUMMARY_API_MODEL_NAME', 'grok-3')

if not GROK_API_AUTHORIZATION_HEADER:
    raise ValueError("❌ AI_SUMMARY_API_AUTHORIZATION_HEADER not set in .env")

# Configure Gemini (for transcription only)
genai.configure(api_key=GEMINI_API_KEY)
TRANSCRIPTION_MODEL = "gemini-2.0-flash"

# Audio Processing
SEGMENT_DURATION = 10 * 60  # 10 minutes per chunk

SEGMENT_DIR = os.path.join(tempfile.gettempdir(), "segments")
os.makedirs(SEGMENT_DIR, exist_ok=True)

# ============================================================================
# SESSION MANAGEMENT FOR GROK API
# ============================================================================

class SessionManager:
    """Manages HTTP sessions with retry logic for Grok API"""
    
    _session = None
    
    @classmethod
    def get_session(cls) -> requests.Session:
        """Get or create a requests session with retry strategy"""
        if cls._session is None:
            cls._session = cls._create_session()
        return cls._session
    
    @staticmethod
    def _create_session() -> requests.Session:
        """Create a session with connection pooling and retry strategy"""
        session = requests.Session()
        
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST", "GET"]
        )
        
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=10
        )
        
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        return session
    
    @classmethod
    def close(cls):
        """Close the session"""
        if cls._session:
            cls._session.close()
            cls._session = None

# ============================================================================
# AUDIO DURATION
# ============================================================================

async def get_audio_duration(file_path: str) -> int:
    """Get audio duration without loading file into memory"""
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'quiet', 
                '-print_format', 'json', 
                '-show_format', 
                file_path
            ],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=30
        )
        
        if result.returncode != 0 or not result.stdout or result.stdout.strip() == '':
            logger.warning(f"FFprobe failed or returned empty")
            return 0
        
        data = json.loads(result.stdout)
        duration = int(float(data['format']['duration']))
        logger.info(f"📊 Audio duration: {duration}s ({duration/60:.1f}min)")
        return duration
    
    except Exception as e:
        logger.error(f"Failed to get duration: {e}")
        return 0

# ============================================================================
# TRANSCRIPTION (Gemini - unchanged)
# ============================================================================

async def transcribe_audio_file_streaming(
    file_path: str, 
    filename: str, 
    task_id: int, 
    initial_progress: int = 0
) -> str:
    """
    Process audio from disk -> FFmpeg (64k Mono) -> Gemini 2.0 Flash
    """
    try:
        duration_seconds = await get_audio_duration(file_path)
        
        if duration_seconds == 0:
            duration_seconds = SEGMENT_DURATION
        
        await update_task(task_id, audio_duration=duration_seconds)
        
        num_segments = (duration_seconds // SEGMENT_DURATION) + (
            1 if duration_seconds % SEGMENT_DURATION else 0
        )
        
        full_text: List[str] = []
        logger.info(f"📁 Processing {duration_seconds/60:.1f}min → {num_segments} segments with Gemini 2.0 Flash")
        
        progress_range = 90 - initial_progress
        
        for i in range(num_segments):
            segment_path = os.path.join(SEGMENT_DIR, f"seg_{task_id}_{i}.mp3")
            
            try:
                start_sec = i * SEGMENT_DURATION
                end_sec = min((i + 1) * SEGMENT_DURATION, duration_seconds)
                
                logger.info(f"🔄 Segment {i+1}/{num_segments} ({start_sec}s-{end_sec}s)")
                
                # 1. Extract segment with optimized settings (96k Mono)
                result = subprocess.run([
                    'ffmpeg', '-y',
                    '-ss', str(start_sec),
                    '-t', str(SEGMENT_DURATION),
                    '-i', file_path,
                    '-ar', '16000',
                    '-ac', '1',
                    '-b:a', '64k',
                    '-acodec', 'libmp3lame',
                    '-vn',
                    segment_path
                ], 
                    capture_output=True, 
                    timeout=600,
                    encoding='utf-8',
                    errors='ignore'
                )
                
                if result.returncode != 0 or not os.path.exists(segment_path):
                    logger.error(f"FFmpeg failed for segment {i+1}")
                    full_text.append(f"[片段 {i+1} 錯誤: 提取失敗]")
                    continue
                
                # 2. Upload to Gemini File API
                logger.info(f"📤 Uploading segment {i+1} to Gemini...")
                
                uploaded_file = await asyncio.to_thread(
                    genai.upload_file, path=segment_path
                )

                # 3. Poll processing state
                while uploaded_file.state.name == "PROCESSING":
                    await asyncio.sleep(2)
                    uploaded_file = await asyncio.to_thread(
                        genai.get_file, uploaded_file.name
                    )
                
                if uploaded_file.state.name == "FAILED":
                    raise ValueError("Gemini File Processing Failed")

                # 4. Generate Content
                logger.info(f"🤖 Transcribing segment {i+1}...")
                
                model = genai.GenerativeModel(model_name=TRANSCRIPTION_MODEL)
                
                response = await asyncio.to_thread(
                    model.generate_content,
                    [
                        "Please provide a verbatim transcription of this audio file. Do not add titles, timestamps, or speaker labels unless necessary.", 
                        uploaded_file
                    ]
                )
                
                text = response.text if response.text else ""
                
                # 5. Cleanup Cloud File
                try:
                    await asyncio.to_thread(genai.delete_file, uploaded_file.name)
                except:
                    pass

                if text:
                    full_text.append(text)
                    logger.info(f"✅ Segment {i+1} complete")
                else:
                    full_text.append(f"[片段 {i+1}: 無內容]")

                # 6. Cleanup Local File
                try:
                    os.remove(segment_path)
                except:
                    pass
                
                # Update progress
                segment_progress = int(initial_progress + ((i + 1) / num_segments) * progress_range)
                await update_task(task_id, progress=segment_progress)
                
            except Exception as e:
                logger.error(f"Segment {i+1} error: {e}")
                full_text.append(f"[片段 {i+1} 錯誤: {str(e)[:100]}]")
                if os.path.exists(segment_path):
                    try: os.remove(segment_path) 
                    except: pass
        
        return "\n\n".join(full_text)
    
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"🧹 Deleted: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to delete: {e}")

# ============================================================================
# GROK-3 SUMMARY (NEW)
# ============================================================================

async def summarize_with_grok(
    transcript: str, 
    task_id: int, 
    filename: str = ""
) -> Tuple[str, str]:
    """
    Generate AI summary using Grok-3 API via bltcy.ai
    Replaces Gemini summary with Grok-3
    """
    
    headers = {
        "Authorization": GROK_API_AUTHORIZATION_HEADER,
        "Content-Type": "application/json"
    }
    
    filename_context = f"檔案名稱: {filename}\n" if filename else ""
    
    prompt = (
        "請根據以下演講稿進行分析，並以繁體中文回答：\n"
        "1. 回答以下問題並以 a, b, c 格式列點(只給答案)：\n"
        "   a. 講者是否為安利的領袖？(回答：是/否)\n"
        f"   b. 講者的名字 (若{filename}和演講稿未提及，則回答：未提及)\n"
        f"   c. 演講的主題 (若{filename}和演講稿未提及，則回答：未提及)\n"
        "2. 根據上述分析，判斷講者是否為安利領袖。若是，則在總結中使用「安利領袖」稱呼講者；若否，則僅使用「講者」或講者姓名（若已知）。"
        "請詳細歸納演講內容。\n\n"
        f"演講稿:\n{transcript}"
    )
    
    payload = {
        "model": GROK_MODEL_NAME,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.7,
        "max_tokens": 8192
    }
    
    try:
        logger.info(f"🤖 Calling Grok-3 API for summary...")
        await update_task(task_id, progress=95)
        
        session = SessionManager.get_session()
        
        response = await asyncio.to_thread(
            session.post,
            GROK_API_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=120
        )
        
        response.raise_for_status()
        response_data = response.json()
        
        # Extract content from bltcy.ai response format
        if 'choices' in response_data and len(response_data['choices']) > 0:
            choice = response_data['choices'][0]
            if 'message' in choice and 'content' in choice['message']:
                summary_text = choice['message']['content'].strip()
                logger.info(f"✅ Grok-3 summary generated successfully")
                return summary_text, ""
        
        error = f"Unexpected response structure: {json.dumps(response_data)}"
        logger.error(f"❌ Grok-3 API Error: {error}")
        return "", error
            
    except requests.exceptions.HTTPError as e:
        error = f"HTTP {response.status_code}: {response.text}"
        logger.error(f"❌ Grok-3 HTTP Error: {error}")
        return "", error
    except Exception as e:
        error = f"Unexpected error: {str(e)}"
        logger.error(f"❌ Grok-3 Error: {error}")
        return "", error

# ============================================================================
# BACKWARD COMPATIBILITY (Keep old function name)
# ============================================================================

async def summarize_with_gemini(
    transcript: str, 
    task_id: int, 
    filename: str = ""
) -> Tuple[str, str]:
    """
    Wrapper function for backward compatibility
    Now calls Grok-3 instead of Gemini
    """
    return await summarize_with_grok(transcript, task_id, filename)