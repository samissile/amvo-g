# app/main.py
from fastapi import FastAPI, File, UploadFile, HTTPException, Form, Request, Depends, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import secrets
import os
import logging
import aiofiles
import asyncio
import glob
import atexit
import tempfile
from typing import List
from datetime import datetime, timedelta
from dotenv import load_dotenv
from app.transcriber import transcribe_audio_file_streaming, summarize_with_gemini, get_audio_duration
from app.db import create_task, update_task, get_task, get_tasks_for_key, init_db, cleanup_old_tasks
from app.youtube_downloader import download_audio_from_url

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

app = FastAPI(title="Amway Speech AI")

# === CONFIG ===
VALID_API_KEY = os.getenv("VALID_API_KEY")
if not VALID_API_KEY:
    raise ValueError("❌ VALID_API_KEY not set in .env")

STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')


# ✅ CORRECT: Allows Environment Variable override for Persistent Storage
RESULTS_DIR = os.getenv("RESULTS_DIR", os.path.join(os.path.dirname(__file__), 'results'))

# Temp files should stay in /tmp (non-persistent) for speed
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "audio_uploads")
SEGMENT_DIR = os.path.join(tempfile.gettempdir(), "segments")
YT_DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "yt_downloads")

# Create all directories
for directory in [RESULTS_DIR, UPLOAD_DIR, SEGMENT_DIR, YT_DOWNLOAD_DIR]:
    os.makedirs(directory, exist_ok=True)

logger.info(f"📁 Results: {RESULTS_DIR}")
logger.info(f"📁 Upload: {UPLOAD_DIR}")
logger.info(f"📁 YouTube: {YT_DOWNLOAD_DIR}")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=STATIC_DIR)

# === CLEANUP ===
def cleanup_temp_files():
    logger.info("🧹 Cleaning up temporary files...")
    for directory in [UPLOAD_DIR, SEGMENT_DIR, YT_DOWNLOAD_DIR]:
        pattern = os.path.join(directory, "*")
        for file in glob.glob(pattern):
            try:
                os.remove(file)
                logger.debug(f"Deleted: {file}")
            except Exception as e:
                logger.warning(f"Failed to delete {file}: {e}")

async def cleanup_old_temp_files():
    """Delete temp files older than 2 hours"""
    now = datetime.now()
    cutoff = now - timedelta(hours=2)
    
    for directory in [UPLOAD_DIR, SEGMENT_DIR, YT_DOWNLOAD_DIR]:
        pattern = os.path.join(directory, "*")
        for file in glob.glob(pattern):
            try:
                file_time = datetime.fromtimestamp(os.path.getmtime(file))
                if file_time < cutoff:
                    os.remove(file)
                    logger.info(f"🧹 Deleted old temp: {file}")
            except Exception as e:
                logger.warning(f"Cleanup failed: {e}")

async def cleanup_old_result_files(days_old: int = 10):
    """Delete result files older than 10 days"""
    now = datetime.now()
    cutoff = now - timedelta(days=days_old)
    
    pattern = os.path.join(RESULTS_DIR, "result_*.txt")
    for file in glob.glob(pattern):
        try:
            file_time = datetime.fromtimestamp(os.path.getmtime(file))
            if file_time < cutoff:
                os.remove(file)
                logger.info(f"🧹 Deleted old result: {file}")
        except Exception as e:
            logger.warning(f"Cleanup failed: {e}")

async def periodic_cleanup():
    """Run cleanup every 30 minutes"""
    while True:
        await asyncio.sleep(1800)
        await cleanup_old_temp_files()
        await cleanup_old_result_files(days_old=10)
        
        try:
            await cleanup_old_tasks(days_old=10)
        except Exception as e:
            logger.error(f"DB cleanup failed: {e}")

atexit.register(cleanup_temp_files)

# === STARTUP/SHUTDOWN ===
@app.on_event("startup")
async def startup_event():
    await init_db()
    await cleanup_old_tasks(days_old=10)
    await cleanup_old_result_files(days_old=10)
    asyncio.create_task(periodic_cleanup())
    logger.info("✅ App started with YouTube + batch processing (10-day retention)")

@app.on_event("shutdown")
async def shutdown_event():
    cleanup_temp_files()
    import gc
    gc.collect()
    logger.info("👋 Shutdown complete")

# === SECURITY ===
async def verify_api_key(api_key: str = Query(...)):
    if not secrets.compare_digest(api_key, VALID_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key

async def verify_api_key_form(api_key: str = Form(...)):
    if not secrets.compare_digest(api_key, VALID_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key

# === ROUTES ===
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/auth")
async def auth(api_key: str = Depends(verify_api_key_form)):
    return RedirectResponse(url=f"/?api_key={api_key}", status_code=303)

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, api_key: str = Query(None)):
    if not api_key:
        return RedirectResponse(url="/login")
    
    try:
        await verify_api_key(api_key)
    except HTTPException:
        return RedirectResponse(url="/login")
    
    tasks = await get_tasks_for_key(api_key)
    return templates.TemplateResponse("status.html", {
        "request": request, 
        "tasks": tasks, 
        "api_key": api_key
    })

@app.post("/transcribe")
async def transcribe_files(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    summarize: str = Form("true"),
    api_key: str = Depends(verify_api_key_form)
):
    """Handle multiple file uploads"""
    if not files or all(f.filename == '' for f in files):
        raise HTTPException(400, "No files provided")
    
    summarize_bool = summarize.lower() in ("true", "on", "1")
    task_ids = []
    
    for file in files:
        if file.filename == '':
            continue
            
        if not file.filename.lower().endswith(('.mp3', '.m4a', '.wav')):
            logger.warning(f"Skipping unsupported: {file.filename}")
            continue
        
        temp_filename = f"upload_{secrets.token_hex(8)}_{file.filename}"
        temp_path = os.path.join(UPLOAD_DIR, temp_filename)
        file_size = 0
        
        try:
            logger.info(f"📥 Streaming upload: {file.filename}")
            async with aiofiles.open(temp_path, 'wb') as f:
                while chunk := await file.read(8192):
                    await f.write(chunk)
                    file_size += len(chunk)
            
            logger.info(f"✅ Upload complete: {file_size / 1024 / 1024:.2f}MB")
            
            if file_size > 500 * 1024 * 1024:
                os.remove(temp_path)
                continue
            
            audio_duration = await get_audio_duration(temp_path)
            task_id = await create_task(
                api_key, 
                file.filename, 
                file_size, 
                audio_duration,
                source_type="upload"
            )
            
            background_tasks.add_task(
                process_audio_from_file, 
                temp_path, 
                file.filename, 
                summarize_bool, 
                task_id
            )
            
            task_ids.append(task_id)
            
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            logger.error(f"Upload failed: {e}")
    
    if not task_ids:
        raise HTTPException(400, "No valid files")
    
    return {"task_ids": task_ids, "message": f"{len(task_ids)} tasks started"}

async def extract_video_title(url: str) -> str:
    """
    Extract video title from URL without downloading
    Returns title or a fallback name
    """
    try:
        from app.youtube_downloader import extract_title_only
        title = await extract_title_only(url)
        return title if title else "YouTube Video"
    except Exception as e:
        logger.warning(f"Failed to extract title: {e}")
        # Return a shortened URL as fallback
        if 'youtube.com' in url or 'youtu.be' in url:
            return "YouTube Video"
        elif 'vimeo.com' in url:
            return "Vimeo Video"
        else:
            return "Video"

@app.post("/transcribe-youtube")
async def transcribe_youtube(
    background_tasks: BackgroundTasks,
    urls: str = Form(...),
    summarize: str = Form("true"),
    api_key: str = Depends(verify_api_key_form)
):
    """Handle multiple YouTube URLs"""
    logger.info(f"📥 Received YouTube request with URLs: {urls[:100]}")
    
    # Parse URLs
    url_list = []
    for line in urls.split('\n'):
        line = line.strip()
        if line and (line.startswith('http://') or line.startswith('https://')):
            url_list.append(line)
    
    logger.info(f"📊 Parsed {len(url_list)} URLs from input")
    
    if not url_list:
        logger.error(f"❌ No valid URLs found in: {urls}")
        raise HTTPException(400, "No valid URLs provided")
    
    summarize_bool = summarize.lower() in ("true", "on", "1")
    task_ids = []
    
    for idx, url in enumerate(url_list):
        try:
            logger.info(f"🔗 Creating task for URL {idx+1}: {url}")
            
            # Extract title before creating task
            title = await extract_video_title(url)
            
            task_id = await create_task(
                api_key=api_key,
                filename=title,  # Use actual title instead of URL
                file_size=0,
                audio_duration=0,
                source_type="youtube",
                source_url=url
            )
            
            logger.info(f"✅ Task {task_id} created for: {title}")
            
            background_tasks.add_task(
                process_youtube_url,
                url,
                summarize_bool,
                task_id
            )
            
            task_ids.append(task_id)
            
        except Exception as e:
            logger.error(f"❌ Failed to create task for URL {idx+1} ({url}): {e}", exc_info=True)
    
    if not task_ids:
        logger.error("❌ No tasks were created")
        raise HTTPException(400, "Failed to create tasks for any URLs")
    
    logger.info(f"✅ Created {len(task_ids)} tasks")
    return {"task_ids": task_ids, "message": f"{len(task_ids)} YouTube tasks started"}

async def process_youtube_url(url: str, summarize: bool, task_id: int):
    """Download from YouTube and process"""
    temp_path = None
    
    try:
        await update_task(task_id, status='downloading', progress=5)
        
        try:
            temp_path, title, duration = await download_audio_from_url(url, task_id)
        except ValueError as e:
            # ✅ NEW: More informative error message
            error_msg = str(e)
            if "not available" in error_msg.lower():
                error_msg = "YouTube 限制了此影片的下載。請稍後重試或嘗試其他影片。"
            elif "403" in error_msg:
                error_msg = "存取被拒絕。YouTube 可能已封鎖此請求。"
            
            logger.error(f"❌ YouTube task {task_id} failed: {error_msg}")
            await update_task(task_id, status='error', progress=0, error=error_msg)
            return
        
        file_size = os.path.getsize(temp_path)
        await update_task(
            task_id,
            status='processing',
            progress=10,
            file_size=file_size,
            audio_duration=duration,
            filename=title
        )
        
        await process_audio_from_file(temp_path, title, summarize, task_id, initial_progress=10)
        
    except Exception as e:
        logger.error(f"❌ YouTube task {task_id} failed: {e}")
        await update_task(task_id, status='error', progress=0, error=str(e))
    
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
                logger.info(f"🧹 Deleted YouTube download")
            except Exception as e:
                logger.warning(f"Failed to delete: {e}")

async def process_audio_from_file(
    file_path: str, 
    filename: str, 
    summarize: bool, 
    task_id: int,
    initial_progress: int = 0
):
    """Process audio file (from upload or YouTube)"""
    result_file_path = None
    
    try:
        transcript = await transcribe_audio_file_streaming(
            file_path, 
            filename, 
            task_id,
            initial_progress=initial_progress
        )
        
        if not summarize:
            result = transcript
        else:
            summary, info = await summarize_with_gemini(transcript, task_id, filename)
            result = (
                "================================================\n"
                "AI 演講內容總結:\n"
                "================================================\n"
                f"{summary}\n\n"
                "================================================\n"
                "完整轉錄文字稿:\n"
                "================================================\n"
                f"{transcript}"
            )
        
        result_file_path = os.path.join(RESULTS_DIR, f"result_{task_id}.txt")
        async with aiofiles.open(result_file_path, 'w', encoding='utf-8') as f:
            await f.write(result)
        
        logger.info(f"💾 Result saved: {result_file_path}")
        
        await update_task(task_id, status='done', progress=100, result_file=result_file_path)
        logger.info(f"✅ Task {task_id} completed")
    
    except Exception as e:
        logger.error(f"❌ Task {task_id} failed: {e}")
        await update_task(task_id, status='error', progress=100, error=str(e))
    
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.warning(f"Failed to delete: {e}")

@app.get("/status/{task_id}")
async def get_status(task_id: int, api_key: str = Depends(verify_api_key)):
    task = await get_task(task_id)
    if not task or task.api_key != api_key:
        raise HTTPException(404, "Task not found")
    
    # ✅ CHANGED: Check if result file exists
    has_result = task.result_file and os.path.exists(task.result_file)
    
    return {
        "status": task.status,
        "progress": task.progress,
        "error": task.error,
        "has_result": has_result,
        "file_size": task.file_size,
        "audio_duration": task.audio_duration,
        "filename": task.filename,  # ✅ NEW: Return filename
        "source_type": getattr(task, 'source_type', 'upload')  # ✅ NEW: Return source type
    }

@app.get("/download/{task_id}")
async def download(task_id: int, api_key: str = Depends(verify_api_key)):
    task = await get_task(task_id)
    if not task or task.api_key != api_key:
        raise HTTPException(404, "Task not found")
    
    if task.status != 'done' or not task.result_file:
        raise HTTPException(400, "Task not ready")
    
    if not os.path.exists(task.result_file):
        raise HTTPException(404, "Result file not found")
    
    original_name = task.filename.rsplit('.', 1)[0]
    download_filename = f"{original_name}_result.txt"
    
    from urllib.parse import quote
    filename_utf8 = quote(download_filename.encode('utf-8'), safe='')
    
    return FileResponse(
        path=task.result_file,
        media_type="text/plain; charset=utf-8",
        filename=download_filename,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename_utf8}"
        }
    )

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "features": ["file_upload", "youtube_download", "batch_processing"],
        "cleanup_days": 10,
        "max_file_size_mb": 500
    }