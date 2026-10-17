"""SQLite persistence with strict user ownership and a restart-safe job queue."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("DATABASE_PATH", Path(__file__).resolve().parent / "data" / "database" / "jobs.sqlite3"))
STATUSES = ("QUEUED", "CREATED", "SCRIPT_GENERATED", "AUDIO_GENERATED", "ASSETS_READY", "SUBTITLES_GENERATED", "VIDEO_RENDERED", "VALIDATED", "SCHEDULED", "PUBLISHING", "UPLOADING", "PUBLISHED", "FAILED")
ACTIVE_STATUSES = ("QUEUED", "CREATED", "SCRIPT_GENERATED", "AUDIO_GENERATED", "ASSETS_READY", "SUBTITLES_GENERATED", "VIDEO_RENDERED", "UPLOADING")


class DatabaseError(Exception):
    """Raised when user or job state cannot be safely persisted."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_database() -> None:
    """Create new tables and add compatible columns to the earlier single-user jobs table."""
    try:
        with _connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                settings_json TEXT NOT NULL DEFAULT '{}', is_active INTEGER NOT NULL DEFAULT 1
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, topic TEXT NOT NULL, title TEXT, voiceover_text TEXT, caption TEXT,
                video_path TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                scheduled_at TEXT, youtube_status TEXT NOT NULL DEFAULT 'DISABLED',
                instagram_status TEXT NOT NULL DEFAULT 'DISABLED', facebook_status TEXT NOT NULL DEFAULT 'DISABLED',
                error TEXT, retry_count INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL DEFAULT '{}'
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            additions = {
                "user_id": "TEXT REFERENCES users(id) ON DELETE CASCADE",
                "progress": "INTEGER NOT NULL DEFAULT 0",
                "stage": "TEXT NOT NULL DEFAULT 'QUEUED'",
                "idempotency_key": "TEXT",
                "started_at": "TEXT",
                "completed_at": "TEXT",
                "platforms_json": "TEXT NOT NULL DEFAULT '[]'",
            }
            for name, decl in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_user_created ON jobs(user_id, created_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_user_idem ON jobs(user_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
            db.execute("""CREATE TABLE IF NOT EXISTS schedules (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0, posts_per_day INTEGER NOT NULL DEFAULT 3,
                times_json TEXT NOT NULL DEFAULT '["08:00","14:00","20:00"]',
                timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata', platforms_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS platform_connections (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                platform TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'DISCONNECTED', external_account_id TEXT,
                token_ciphertext BLOB, token_expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(user_id, platform)
            )""")
            for user in db.execute("SELECT u.id FROM users u LEFT JOIN schedules s ON s.user_id=u.id WHERE s.user_id IS NULL").fetchall():
                db.execute("INSERT INTO schedules(user_id,updated_at) VALUES(?,?)",(user["id"],_now()))
            for missing in db.execute("SELECT u.id AS user_id,p.platform FROM users u CROSS JOIN (SELECT 'youtube' AS platform UNION ALL SELECT 'instagram' UNION ALL SELECT 'facebook') p LEFT JOIN platform_connections c ON c.user_id=u.id AND c.platform=p.platform WHERE c.id IS NULL").fetchall():
                now=_now()
                db.execute("INSERT INTO platform_connections(id,user_id,platform,created_at,updated_at) VALUES(?,?,?,?,?)",(str(uuid.uuid4()),missing["user_id"],missing["platform"],now,now))
            db.execute("""CREATE TABLE IF NOT EXISTS topic_ideas (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                topic TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', created_at TEXT NOT NULL,
                scheduled_at TEXT, job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS ideas_user_status ON topic_ideas(user_id,status,created_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS oauth_states (
                state_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                platform TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS scheduler_ticks (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                local_date TEXT NOT NULL, slot TEXT NOT NULL, idea_id TEXT,
                created_at TEXT NOT NULL, PRIMARY KEY(user_id,local_date,slot)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS publishing_history (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, platform TEXT NOT NULL,
                status TEXT NOT NULL, external_post_id TEXT, permalink TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(job_id,platform)
            )""")
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not initialize database: {exc}") from exc


def health_check() -> bool:
    """Return whether SQLite is reachable without exposing database details."""
    try:
        with _connect() as db:
            return db.execute("SELECT 1").fetchone()[0] == 1
    except sqlite3.Error:
        return False


def create_user(email: str, password_hash: str) -> dict[str, Any]:
    initialize_database()
    email = email.strip().lower()
    user_id, now = str(uuid.uuid4()), _now()
    settings = {"language": "English", "voice": "en-IN-NeerjaNeural", "tone": "Friendly", "audience": "General", "style": "Auto Style", "duration": "30-60 sec"}
    try:
        with _connect() as db:
            db.execute("INSERT INTO users(id,email,password_hash,created_at,settings_json) VALUES(?,?,?,?,?)", (user_id,email,password_hash,now,json.dumps(settings)))
            db.execute("INSERT INTO schedules(user_id,updated_at) VALUES(?,?)", (user_id,now))
            for platform in ("youtube","instagram","facebook"):
                db.execute("INSERT INTO platform_connections(id,user_id,platform,created_at,updated_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()),user_id,platform,now,now))
        return get_user(user_id) or {}
    except sqlite3.IntegrityError as exc:
        raise DatabaseError("An account with this email already exists") from exc
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not create account: {exc}") from exc


def get_user(user_id: str) -> dict[str, Any] | None:
    initialize_database()
    with _connect() as db:
        row = db.execute("SELECT id,email,created_at,settings_json FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
        if not row: return None
        user = dict(row)
        user["settings"] = json.loads(user.pop("settings_json") or "{}")
        return user


def get_user_auth(email: str) -> dict[str, Any] | None:
    initialize_database()
    with _connect() as db:
        row = db.execute("SELECT id,email,password_hash,created_at FROM users WHERE email=? COLLATE NOCASE AND is_active=1", (email.strip(),)).fetchone()
        return dict(row) if row else None


def create_session(user_id: str, token_hash: str, expires_at: str) -> None:
    with _connect() as db:
        db.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)", (token_hash,user_id,_now(),expires_at))


def session_user(token_hash: str) -> dict[str, Any] | None:
    with _connect() as db:
        row = db.execute("SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?", (token_hash,_now())).fetchone()
    return get_user(row["user_id"]) if row else None


def delete_session(token_hash: str) -> None:
    with _connect() as db: db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))


def enqueue_job(user_id: str, topic: str, platforms: list[str] | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
    topic = topic.strip() if isinstance(topic,str) else ""
    if not topic: raise DatabaseError("Please enter a topic")
    initialize_database()
    jid, now = str(uuid.uuid4()), _now()
    try:
        with _connect() as db:
            # Serialize duplicate detection and insertion so two simultaneous
            # submits for the same active topic cannot enqueue two generations.
            db.execute("BEGIN IMMEDIATE")
            user=db.execute("SELECT settings_json FROM users WHERE id=? AND is_active=1",(user_id,)).fetchone()
            if not user: raise DatabaseError("User not found")
            if idempotency_key:
                row=db.execute("SELECT * FROM jobs WHERE user_id=? AND idempotency_key=?",(user_id,idempotency_key)).fetchone()
                if row:return _job(row)
            active=tuple(ACTIVE_STATUSES)
            marks=",".join("?" for _ in active)
            row=db.execute(f"SELECT * FROM jobs WHERE user_id=? AND topic=? AND status IN ({marks}) ORDER BY created_at DESC LIMIT 1",(user_id,topic,*active)).fetchone()
            if row:return _job(row)
            preferences=json.loads(user["settings_json"] or "{}")
            db.execute("INSERT INTO jobs(id,user_id,topic,status,stage,progress,created_at,updated_at,idempotency_key,platforms_json,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(jid,user_id,topic,"QUEUED","QUEUED",0,now,now,idempotency_key,json.dumps(platforms or []),json.dumps({"preferences":preferences},ensure_ascii=False)))
            return _job(db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?",(jid,user_id)).fetchone())
    except sqlite3.Error as exc: raise DatabaseError(f"Could not queue video: {exc}") from exc


def update_job(job_id: str, user_id: str, **fields: Any) -> dict[str, Any]:
    allowed={"topic","title","voiceover_text","caption","video_path","status","scheduled_at","youtube_status","instagram_status","facebook_status","error","retry_count","metadata_json","progress","stage","started_at","completed_at","platforms_json"}
    if set(fields)-allowed: raise DatabaseError(f"Unsupported job fields: {', '.join(sorted(set(fields)-allowed))}")
    if "status" in fields and fields["status"] not in STATUSES: raise DatabaseError(f"Unsupported job status: {fields['status']}")
    for key in ("metadata_json","platforms_json"):
        if isinstance(fields.get(key),(dict,list)):fields[key]=json.dumps(fields[key],ensure_ascii=False)
    fields["updated_at"]=_now()
    assignments=",".join(f"{k}=?" for k in fields)
    try:
        with _connect() as db:
            cursor=db.execute(f"UPDATE jobs SET {assignments} WHERE id=? AND user_id=?",(*fields.values(),job_id,user_id))
            if not cursor.rowcount: raise DatabaseError("Job not found")
            return _job(db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?",(job_id,user_id)).fetchone())
    except sqlite3.Error as exc: raise DatabaseError(f"Could not update job: {exc}") from exc


def get_job(job_id: str, user_id: str) -> dict[str, Any] | None:
    initialize_database()
    with _connect() as db:
        row=db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?",(job_id,user_id)).fetchone()
        return _job(row) if row else None


def list_jobs(user_id: str, limit: int=50) -> list[dict[str, Any]]:
    initialize_database()
    with _connect() as db:
        return [_job(r) for r in db.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",(user_id,max(1,min(limit,200))))]


def claim_next_job() -> dict[str, Any] | None:
    """Atomically claim one queued job so concurrent workers cannot duplicate it."""
    initialize_database()
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT * FROM jobs WHERE status='QUEUED' AND user_id IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
        if not row:return None
        now=_now()
        db.execute("UPDATE jobs SET status='CREATED',stage='Starting',started_at=?,updated_at=? WHERE id=? AND status='QUEUED'",(now,now,row["id"]))
        claimed=_job(db.execute("SELECT * FROM jobs WHERE id=?",(row["id"],)).fetchone())
        claimed["user_id"]=row["user_id"]
        return claimed


def recover_interrupted_jobs() -> int:
    """Requeue jobs left in flight by a process restart; stage artifacts allow resume."""
    initialize_database()
    active=tuple(s for s in ACTIVE_STATUSES if s!="QUEUED")
    marks=",".join("?" for _ in active)
    with _connect() as db:
        db.execute("UPDATE publishing_history SET status='UNKNOWN',error='The app restarted during upload. Check the platform account before taking further action.',updated_at=? WHERE status='UPLOADING'",(_now(),))
        db.execute("UPDATE jobs SET status='VALIDATED',stage='A publish attempt needs review after restart',error='A platform upload was in progress when SiMi restarted. Check the connected account before retrying.',updated_at=? WHERE status='PUBLISHING'",(_now(),))
        db.execute(
            "UPDATE jobs SET status='FAILED',stage='Legacy job needs a new owner',error='This job was created before user accounts were added. Create a new job to continue.',updated_at=? WHERE user_id IS NULL AND status IN ('QUEUED','CREATED','SCRIPT_GENERATED','AUDIO_GENERATED','ASSETS_READY','SUBTITLES_GENERATED','VIDEO_RENDERED','UPLOADING')",
            (_now(),),
        )
        cur=db.execute(f"UPDATE jobs SET status='QUEUED',stage='Resuming after restart',updated_at=? WHERE status IN ({marks})",(_now(),*active))
        return cur.rowcount


def save_schedule(user_id: str, *, enabled: bool, posts_per_day: int, times: list[str], timezone: str, platforms: list[str]) -> dict[str, Any]:
    if not 1<=posts_per_day<=12: raise DatabaseError("Posts per day must be between 1 and 12")
    if len(times)!=posts_per_day: raise DatabaseError("Add one publishing time for each daily post")
    if any(len(t)!=5 or t[2] != ":" or not t[:2].isdigit() or not t[3:].isdigit() or int(t[:2])>23 or int(t[3:])>59 for t in times): raise DatabaseError("Use valid 24-hour times such as 08:00")
    if len(set(times))!=len(times): raise DatabaseError("Publishing times must be unique")
    if not timezone or len(timezone)>80: raise DatabaseError("Select a valid timezone")
    permitted={"youtube","instagram","facebook"}
    if not set(platforms)<=permitted: raise DatabaseError("Unsupported publishing platform")
    initialize_database()
    with _connect() as db:
        db.execute("INSERT INTO schedules(user_id,enabled,posts_per_day,times_json,timezone,platforms_json,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,posts_per_day=excluded.posts_per_day,times_json=excluded.times_json,timezone=excluded.timezone,platforms_json=excluded.platforms_json,updated_at=excluded.updated_at",(user_id,int(enabled),posts_per_day,json.dumps(times),timezone,json.dumps(platforms),_now()))
        r=db.execute("SELECT * FROM schedules WHERE user_id=?",(user_id,)).fetchone()
        return {"enabled":bool(r["enabled"]),"posts_per_day":r["posts_per_day"],"times":json.loads(r["times_json"]),"timezone":r["timezone"],"platforms":json.loads(r["platforms_json"]),"updated_at":r["updated_at"]}


def get_schedule(user_id: str) -> dict[str, Any]:
    initialize_database()
    with _connect() as db:r=db.execute("SELECT * FROM schedules WHERE user_id=?",(user_id,)).fetchone()
    if not r:return {"enabled":False,"posts_per_day":3,"times":["08:00","14:00","20:00"],"timezone":"Asia/Kolkata","platforms":[]}
    return {"enabled":bool(r["enabled"]),"posts_per_day":r["posts_per_day"],"times":json.loads(r["times_json"]),"timezone":r["timezone"],"platforms":json.loads(r["platforms_json"]),"updated_at":r["updated_at"]}


def delete_schedule(user_id: str) -> None:
    with _connect() as db:db.execute("DELETE FROM schedules WHERE user_id=?",(user_id,))


def list_platforms(user_id: str) -> list[dict[str, Any]]:
    initialize_database()
    with _connect() as db:
        return [dict(r) for r in db.execute("SELECT platform,enabled,status,external_account_id,token_expires_at FROM platform_connections WHERE user_id=? ORDER BY platform",(user_id,))]


def create_oauth_state(state_hash: str, user_id: str, platform: str, expires_at: str) -> None:
    with _connect() as db:
        db.execute("DELETE FROM oauth_states WHERE expires_at<=? OR user_id=?", (_now(), user_id))
        db.execute("INSERT INTO oauth_states(state_hash,user_id,platform,expires_at,created_at) VALUES(?,?,?,?,?)", (state_hash,user_id,platform,expires_at,_now()))


def consume_oauth_state(state_hash: str, platform: str) -> str | None:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT user_id FROM oauth_states WHERE state_hash=? AND platform=? AND expires_at>?",(state_hash,platform,_now())).fetchone()
        if not row:return None
        db.execute("DELETE FROM oauth_states WHERE state_hash=?",(state_hash,))
        return str(row["user_id"])


def save_platform_connection(user_id: str, platform: str, account_id: str, token_ciphertext: bytes, token_expires_at: str | None) -> None:
    with _connect() as db:
        cursor=db.execute("UPDATE platform_connections SET enabled=1,status='CONNECTED',external_account_id=?,token_ciphertext=?,token_expires_at=?,updated_at=? WHERE user_id=? AND platform=?",(account_id,token_ciphertext,token_expires_at,_now(),user_id,platform))
        if not cursor.rowcount:raise DatabaseError("Platform connection record was not found")


def get_platform_connection(user_id: str, platform: str) -> dict[str, Any] | None:
    with _connect() as db:
        row=db.execute("SELECT * FROM platform_connections WHERE user_id=? AND platform=?",(user_id,platform)).fetchone()
        return dict(row) if row else None


def disconnect_platform(user_id: str, platform: str) -> None:
    with _connect() as db:
        db.execute("UPDATE platform_connections SET enabled=0,status='DISCONNECTED',external_account_id=NULL,token_ciphertext=NULL,token_expires_at=NULL,updated_at=? WHERE user_id=? AND platform=?",(_now(),user_id,platform))


def claim_publish(user_id: str, job_id: str, platform: str) -> dict[str, Any]:
    """Create a durable at-most-once publishing claim for a validated job."""
    if platform not in {"youtube", "instagram", "facebook"}:
        raise DatabaseError("Unsupported publishing platform")
    initialize_database()
    now = _now()
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id,user_id)).fetchone()
        if not job or job["status"] not in {"VALIDATED", "SCHEDULED", "PUBLISHED", "PUBLISHING"} or not job["video_path"]:
            raise DatabaseError("A validated video is required before publishing")
        platforms = json.loads(job["platforms_json"] or "[]")
        if platform not in platforms:
            raise DatabaseError("This platform was not selected for the video")
        history = db.execute("SELECT * FROM publishing_history WHERE job_id=? AND platform=?", (job_id,platform)).fetchone()
        if history:
            if history["status"] == "PUBLISHED":
                raise DatabaseError("This video has already been published to this account")
            raise DatabaseError("A publishing attempt already exists; confirm the platform result before trying again to avoid a duplicate post")
        db.execute("INSERT INTO publishing_history(id,user_id,job_id,platform,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()),user_id,job_id,platform,"UPLOADING",now,now))
        db.execute(f"UPDATE jobs SET status='PUBLISHING',stage=?,{platform}_status='UPLOADING',error=NULL,updated_at=? WHERE id=? AND user_id=?", (f"Publishing to {platform.title()}",now,job_id,user_id))
        return _job(db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?",(job_id,user_id)).fetchone())


def finish_publish(user_id: str, job_id: str, platform: str, external_post_id: str, permalink: str | None) -> dict[str, Any]:
    now = _now()
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE publishing_history SET status='PUBLISHED',external_post_id=?,permalink=?,updated_at=? WHERE user_id=? AND job_id=? AND platform=? AND status='UPLOADING'",(external_post_id,permalink,now,user_id,job_id,platform))
        db.execute(f"UPDATE jobs SET {platform}_status='PUBLISHED',updated_at=? WHERE id=? AND user_id=?",(now,job_id,user_id))
        job=db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?",(job_id,user_id)).fetchone()
        if not job:raise DatabaseError("Video not found")
        targets=json.loads(job["platforms_json"] or "[]")
        states={name:job[f"{name}_status"] for name in targets}
        if targets and all(value=="PUBLISHED" for value in states.values()):
            db.execute("UPDATE jobs SET status='PUBLISHED',stage='Published successfully',completed_at=?,updated_at=? WHERE id=? AND user_id=?",(now,now,job_id,user_id))
        else:
            db.execute("UPDATE jobs SET status='VALIDATED',stage='Published to one platform; other platforms are pending',updated_at=? WHERE id=? AND user_id=?",(now,job_id,user_id))
        return _job(db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?",(job_id,user_id)).fetchone())


def fail_publish(user_id: str, job_id: str, platform: str, error: str) -> None:
    now = _now()
    safe_error = (error or "Publishing failed")[:1000]
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE publishing_history SET status='FAILED',error=?,updated_at=? WHERE user_id=? AND job_id=? AND platform=? AND status='UPLOADING'",(safe_error,now,user_id,job_id,platform))
        db.execute(f"UPDATE jobs SET status='VALIDATED',{platform}_status='FAILED',stage=?,error=?,updated_at=? WHERE id=? AND user_id=?",(f"Video ready; {platform.title()} publishing failed",safe_error,now,job_id,user_id))


def update_settings(user_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    permitted={"language","voice","tone","audience","style","duration"}
    if set(settings)-permitted: raise DatabaseError("Unsupported preference")
    current=get_user(user_id)
    if not current: raise DatabaseError("User not found")
    current["settings"].update(settings)
    with _connect() as db: db.execute("UPDATE users SET settings_json=? WHERE id=?",(json.dumps(current["settings"],ensure_ascii=False),user_id))
    return current["settings"]


def retry_job(user_id: str, job_id: str) -> dict[str, Any]:
    with _connect() as db:
        cur=db.execute("UPDATE jobs SET status='QUEUED',stage='Queued for retry',progress=0,error=NULL,updated_at=? WHERE id=? AND user_id=? AND status='FAILED'",(_now(),job_id,user_id))
        if not cur.rowcount: raise DatabaseError("Failed job not found")
        return _job(db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?",(job_id,user_id)).fetchone())


def add_topic_ideas(user_id: str, topics: list[str]) -> list[dict[str, Any]]:
    clean=[]
    for topic in topics:
        value=topic.strip()
        if value and value not in clean:clean.append(value)
    if not clean or len(clean)>50:raise DatabaseError("Enter between 1 and 50 topic ideas")
    now=_now()
    with _connect() as db:
        if not db.execute("SELECT 1 FROM users WHERE id=?",(user_id,)).fetchone():raise DatabaseError("User not found")
        result=[]
        for topic in clean:
            idea_id=str(uuid.uuid4())
            db.execute("INSERT INTO topic_ideas(id,user_id,topic,created_at) VALUES(?,?,?,?)",(idea_id,user_id,topic,now))
            result.append({"id":idea_id,"topic":topic,"status":"PENDING","created_at":now})
        return result


def list_topic_ideas(user_id: str) -> list[dict[str, Any]]:
    with _connect() as db:return [dict(r) for r in db.execute("SELECT id,topic,status,created_at,scheduled_at,job_id FROM topic_ideas WHERE user_id=? ORDER BY created_at DESC",(user_id,))]


def complete_idea_for_job(user_id: str, job_id: str, status: str) -> None:
    with _connect() as db:db.execute("UPDATE topic_ideas SET status=? WHERE user_id=? AND job_id=?",(status,user_id,job_id))


def dispatch_due_ideas(now: datetime | None = None) -> int:
    """Prepare each user's next topic before its configured local publishing time."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:now=now.replace(tzinfo=timezone.utc)
    try:lead=max(0,min(24*60,int(os.getenv("PREPUBLISH_LEAD_MINUTES","30"))))
    except ValueError:lead=30
    count=0
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        schedules=db.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()
        for schedule in schedules:
            try:local=now.astimezone(ZoneInfo(schedule["timezone"]))
            except (ZoneInfoNotFoundError,ValueError):continue
            times=json.loads(schedule["times_json"])
            for slot in times:
                hour,minute=map(int,slot.split(":"))
                scheduled_at=local.replace(hour=hour,minute=minute,second=0,microsecond=0)
                seconds_until=(scheduled_at-local).total_seconds()
                # Generate ahead, then allow a short outage grace after the target time.
                if seconds_until>lead*60 or seconds_until < -120:continue
                key_date=scheduled_at.date().isoformat()
                if db.execute("SELECT 1 FROM scheduler_ticks WHERE user_id=? AND local_date=? AND slot=?",(schedule["user_id"],key_date,slot)).fetchone():continue
                idea=db.execute("SELECT * FROM topic_ideas WHERE user_id=? AND status='PENDING' ORDER BY created_at,id LIMIT 1",(schedule["user_id"],)).fetchone()
                idea_id=idea["id"] if idea else None
                db.execute("INSERT INTO scheduler_ticks(user_id,local_date,slot,idea_id,created_at) VALUES(?,?,?,?,?)",(schedule["user_id"],key_date,slot,idea_id,_now()))
                if not idea:continue
                jid=str(uuid.uuid4());created=_now()
                db.execute("INSERT INTO jobs(id,user_id,topic,status,stage,progress,created_at,updated_at,platforms_json,scheduled_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(jid,schedule["user_id"],idea["topic"],"QUEUED","Preparing for your scheduled post",0,created,created,schedule["platforms_json"],scheduled_at.isoformat(timespec="minutes")))
                db.execute("UPDATE topic_ideas SET status='PROCESSING',scheduled_at=?,job_id=? WHERE id=?",(scheduled_at.isoformat(timespec="minutes"),jid,idea_id))
                count+=1
    return count


def list_due_publish_jobs(now: datetime | None = None) -> list[dict[str, Any]]:
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:now=now.replace(tzinfo=timezone.utc)
    now_utc=now.astimezone(timezone.utc)
    try:late_grace=max(0,min(24*60,int(os.getenv("AUTO_PUBLISH_LATE_GRACE_MINUTES","60"))))
    except ValueError:late_grace=60
    with _connect() as db:
        rows=db.execute("SELECT j.* FROM jobs j JOIN schedules s ON s.user_id=j.user_id AND s.enabled=1 WHERE j.status='VALIDATED' AND j.scheduled_at IS NOT NULL").fetchall()
    result=[]
    for row in rows:
        job=_job(row)
        job["user_id"]=row["user_id"]
        try:
            when=datetime.fromisoformat(str(job["scheduled_at"]))
            if when.tzinfo is None:when=when.replace(tzinfo=timezone.utc)
        except (TypeError,ValueError):
            continue
        when_utc=when.astimezone(timezone.utc)
        if when_utc>now_utc or (now_utc-when_utc).total_seconds()>late_grace*60:continue
        job["due_platforms"]=[p for p in job.get("platforms",[]) if p in {"youtube","instagram","facebook"} and job.get(f"{p}_status")=="DISABLED"]
        if job["due_platforms"]:result.append(job)
    return result


def _job(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:return {}
    d=dict(row)
    d["progress"]=int(d.get("progress") or 0)
    for field in ("metadata_json","platforms_json"):
        try:d[field.removesuffix("_json")]=json.loads(d.pop(field) or ("{}" if field=="metadata_json" else "[]"))
        except json.JSONDecodeError:d[field.removesuffix("_json")]= {} if field=="metadata_json" else []
    d.pop("user_id",None)
    return d
