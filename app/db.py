# app/db.py
import aiosqlite
import os
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime, timedelta


# Get path from env (Cloud Run) or default to local
DB_PATH = os.getenv("DB_FILE", "app/db/tasks.db")

@dataclass
class Task:
    id: int
    api_key: str
    status: str
    progress: int
    filename: str
    created_at: str
    file_size: int = 0
    audio_duration: int = 0
    result_file: Optional[str] = None
    error: Optional[str] = None
    source_type: str = "upload"
    source_url: Optional[str] = None

async def init_db():
    # Ensure the directory exists (crucial for Cloud Run mounts)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL,
                filename TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                file_size INTEGER DEFAULT 0,
                audio_duration INTEGER DEFAULT 0,
                result_file TEXT,
                error TEXT,
                source_type TEXT DEFAULT 'upload',
                source_url TEXT
            )
        """)
        await db.commit()
        
        # Check for migration
        cursor = await db.execute("PRAGMA table_info(tasks)")
        columns = await cursor.fetchall()
        column_names = [col[1] for col in columns]
        
        # Add missing columns
        if "file_size" not in column_names:
            print("🔄 Migrating database schema...")
            await db.execute("ALTER TABLE tasks ADD COLUMN file_size INTEGER DEFAULT 0")
            await db.commit()
        
        if "audio_duration" not in column_names:
            await db.execute("ALTER TABLE tasks ADD COLUMN audio_duration INTEGER DEFAULT 0")
            await db.commit()
        
        if "result_file" not in column_names:
            await db.execute("ALTER TABLE tasks ADD COLUMN result_file TEXT")
            await db.commit()
        
        if "source_type" not in column_names:
            print("🔄 Adding source_type column...")
            await db.execute("ALTER TABLE tasks ADD COLUMN source_type TEXT DEFAULT 'upload'")
            await db.commit()
        
        if "source_url" not in column_names:
            print("🔄 Adding source_url column...")
            await db.execute("ALTER TABLE tasks ADD COLUMN source_url TEXT")
            await db.commit()
            print("✅ Database ready!")

async def create_task(
    api_key: str, 
    filename: str, 
    file_size: int = 0, 
    audio_duration: int = 0,
    source_type: str = "upload",
    source_url: Optional[str] = None
) -> int:
    """Create a new task"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO tasks 
            (api_key, status, progress, filename, created_at, file_size, audio_duration, source_type, source_url) 
            VALUES (?, 'pending', 0, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)""",
            (api_key, filename, file_size, audio_duration, source_type, source_url)
        )
        await db.commit()
        return cursor.lastrowid

async def update_task(
    task_id: int, 
    status: Optional[str] = None, 
    progress: Optional[int] = None, 
    result_file: Optional[str] = None, 
    error: Optional[str] = None,
    file_size: Optional[int] = None,
    audio_duration: Optional[int] = None,
    filename: Optional[str] = None
):
    """Update task fields"""
    async with aiosqlite.connect(DB_PATH) as db:
        updates = []
        params = []
        
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if progress is not None:
            updates.append("progress = ?")
            params.append(progress)
        if result_file is not None:
            updates.append("result_file = ?")
            params.append(result_file)
        if error is not None:
            updates.append("error = ?")
            params.append(error)
        if file_size is not None:
            updates.append("file_size = ?")
            params.append(file_size)
        if audio_duration is not None:
            updates.append("audio_duration = ?")
            params.append(audio_duration)
        if filename is not None:
            updates.append("filename = ?")
            params.append(filename)
        
        if updates:
            query = f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?"
            params.append(task_id)
            await db.execute(query, params)
            await db.commit()

async def get_task(task_id: int) -> Optional[Task]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT id, api_key, status, progress, filename, created_at, 
                      file_size, audio_duration, result_file, error, source_type, source_url 
               FROM tasks WHERE id = ?""",
            (task_id,)
        )
        row = await cursor.fetchone()
        if row:
            return Task(*row)
        return None

async def get_tasks_for_key(api_key: str) -> List[Task]:
    """Get all tasks for a specific API key"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT id, api_key, status, progress, filename, created_at, 
                      file_size, audio_duration, result_file, error, source_type, source_url 
               FROM tasks WHERE api_key = ? ORDER BY created_at DESC""",
            (api_key,)
        )
        rows = await cursor.fetchall()
        return [Task(*row) for row in rows]

async def cleanup_old_tasks(days_old: int = 10):
    """Delete tasks older than N days"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM tasks WHERE created_at < datetime('now', '-' || ? || ' days')",
            (days_old,)
        )
        await db.commit()
        deleted_count = cursor.rowcount
        if deleted_count > 0:
            print(f"🧹 Cleaned up {deleted_count} old tasks")
        
        await db.execute("VACUUM")
        await db.commit()