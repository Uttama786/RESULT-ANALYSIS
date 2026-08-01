import os
import re
import uuid
import json
import secrets
import hashlib
import logging
import requests
import threading
from datetime import datetime
from typing import Dict, List, Any, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from config import DATA_DIR, EXPORTS_DIR, TEMPLATES_DIR
from database import Base, engine, SessionLocal, upgrade_db_schema
from models import User, History
from scraper import VTUScraper, solve_captcha_ocr_base64
from analyzer import ResultAnalyzer

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Initialize database tables and auto-migrate missing columns
Base.metadata.create_all(bind=engine)
upgrade_db_schema()

app = FastAPI(
    title="VTU Result Scraper & Analysis Tool",
    description="Automated VTU student results crawler and deep analytical dashboard generator.",
    version="1.0.0"
)

# Enable CORS for React frontend integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for development ease
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global dictionaries and thread-safety locks for session files & active user tokens
session_registry: Dict[str, Dict[str, Any]] = {}
active_tokens: Dict[str, Dict[str, Any]] = {}

tokens_lock = threading.Lock()
registry_lock = threading.Lock()

USERS_FILE = os.path.join(DATA_DIR, "users.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")

def hash_password(password: str, salt: Optional[str] = None) -> tuple:
    if not salt:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    )
    return key.hex(), salt

def verify_password(password: str, salt: str, hashed: str) -> bool:
    key_hex, _ = hash_password(password, salt)
    return secrets.compare_digest(key_hex, hashed)

def migrate_json_to_db():
    """Seamlessly imports existing users.json and history.json into the persistent database on startup."""
    db = SessionLocal()
    try:
        if os.path.exists(USERS_FILE):
            try:
                with open(USERS_FILE, "r", encoding="utf-8") as f:
                    json_users = json.load(f)
                migrated_count = 0
                for u in json_users:
                    username = u.get("username", "").strip()
                    if username and not db.query(User).filter(User.username.ilike(username)).first():
                        db_user = User(
                            id=u.get("id", f"usr_{uuid.uuid4().hex[:8]}"),
                            username=username,
                            hashed_password=u.get("hashed_password", ""),
                            salt=u.get("salt", ""),
                            full_name=u.get("full_name", username),
                            email=u.get("email", ""),
                            role=u.get("role", "student"),
                            department=u.get("department", ""),
                            created_at=u.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                        )
                        db.add(db_user)
                        migrated_count += 1
                db.commit()
                if migrated_count > 0:
                    logger.info(f"📦 Migrated {migrated_count} users from users.json to database.")
            except Exception as e:
                logger.error(f"Error migrating users.json to database: {e}")
                db.rollback()

        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    json_history = json.load(f)
                migrated_hist = 0
                for h in json_history:
                    hist_id = h.get("id")
                    if hist_id and not db.query(History).filter(History.id == hist_id).first():
                        db_hist = History(
                            id=hist_id,
                            user_id=h.get("user_id", ""),
                            username=h.get("username", ""),
                            session_id=h.get("session_id", ""),
                            timestamp=h.get("timestamp", ""),
                            usn_count=h.get("usn_count", 0),
                            usn_range_summary=h.get("usn_range_summary", ""),
                            excel_file=h.get("excel_file", ""),
                            excel_path=h.get("excel_path", ""),
                            file_size_kb=h.get("file_size_kb", 0.0)
                        )
                        db.add(db_hist)
                        migrated_hist += 1
                db.commit()
                if migrated_hist > 0:
                    logger.info(f"📦 Migrated {migrated_hist} history records from history.json to database.")
            except Exception as e:
                logger.error(f"Error migrating history.json to database: {e}")
                db.rollback()
    finally:
        db.close()

migrate_json_to_db()

def ensure_default_admin():
    db = SessionLocal()
    try:
        admin_user = db.query(User).filter(User.username.ilike("UttamBhise")).first()
        if not admin_user:
            admin_hash, admin_salt = hash_password("#Uttama207")
            default_admin = User(
                id="usr_admin_uttam",
                username="UttamBhise",
                hashed_password=admin_hash,
                salt=admin_salt,
                full_name="Uttam Bhise",
                email="uttamabhise@gmail.com",
                role="admin",
                department="Computer Science & Engineering",
                created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            db.add(default_admin)
            db.commit()
            logger.info("🔑 Auto-seeded default Administrator account 'UttamBhise' into persistent database.")
    except Exception as e:
        logger.error(f"Error seeding default admin user in DB: {e}")
        db.rollback()
    finally:
        db.close()

ensure_default_admin()

def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    db = SessionLocal()
    try:
        clean_username = username.strip().lower()
        user = db.query(User).filter(User.username.ilike(clean_username)).first()
        if user:
            return user.to_dict(include_sensitive=True)
        return None
    finally:
        db.close()

def load_history() -> List[Dict[str, Any]]:
    db = SessionLocal()
    try:
        entries = db.query(History).order_by(History.timestamp.desc()).all()
        return [e.to_dict() for e in entries]
    finally:
        db.close()

def upload_to_persistent_storage(excel_path: str, session_id: str) -> tuple:
    """
    Uploads generated Excel report to Cloud storage (Cloudinary/S3 if configured)
    and loads binary bytes for persistent database BLOB storage.
    Returns: (excel_url, excel_bytes, storage_provider)
    """
    excel_bytes = None
    excel_url = ""
    storage_provider = "db"

    if os.path.exists(excel_path):
        try:
            with open(excel_path, "rb") as f:
                excel_bytes = f.read()
        except Exception as e:
            logger.error(f"Error reading Excel file bytes for session '{session_id}': {e}")

    # Check Cloudinary configuration in environment variables
    cloudinary_url = os.getenv("CLOUDINARY_URL", "").strip()
    cloudinary_name = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
    cloudinary_preset = os.getenv("CLOUDINARY_UPLOAD_PRESET", "").strip()

    if (cloudinary_url or (cloudinary_name and cloudinary_preset)) and excel_bytes:
        try:
            logger.info(f"☁️ Uploading Excel report to Cloudinary for session: {session_id}")
            try:
                import cloudinary
                import cloudinary.uploader
                if cloudinary_url:
                    cloudinary.config(cloudinary_url=cloudinary_url)
                res = cloudinary.uploader.upload(
                    excel_path,
                    resource_type="raw",
                    public_id=f"vtu_results/VTU_Results_{session_id}"
                )
                excel_url = res.get("secure_url", "")
                if excel_url:
                    storage_provider = "cloudinary"
                    logger.info(f"✅ Successfully uploaded Excel report to Cloudinary: {excel_url}")
            except Exception as sdk_err:
                logger.warning(f"Cloudinary SDK upload fallback to HTTP API: {sdk_err}")
                if cloudinary_name and cloudinary_preset:
                    upload_endpoint = f"https://api.cloudinary.com/v1_1/{cloudinary_name}/raw/upload"
                    files = {"file": (f"VTU_Results_{session_id}.xlsx", excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
                    data = {"upload_preset": cloudinary_preset}
                    resp = requests.post(upload_endpoint, files=files, data=data, timeout=15)
                    if resp.status_code == 200:
                        res_json = resp.json()
                        excel_url = res_json.get("secure_url", "")
                        storage_provider = "cloudinary"
                        logger.info(f"✅ Successfully uploaded Excel report to Cloudinary via REST: {excel_url}")
        except Exception as err:
            logger.error(f"⚠️ Cloud storage upload error: {err}. Defaulting to Database BLOB storage.")

    return excel_url, excel_bytes, storage_provider


def migrate_excel_files_to_db():
    """Reads existing local Excel report files from disk into persistent DB storage for legacy history records."""
    db = SessionLocal()
    try:
        entries = db.query(History).filter(History.excel_data == None).all()
        migrated_count = 0
        for entry in entries:
            path = entry.excel_path
            if not path or not os.path.exists(path):
                path = os.path.join(EXPORTS_DIR, entry.excel_file or "")
            if path and os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        entry.excel_data = f.read()
                    migrated_count += 1
                except Exception as e:
                    logger.warning(f"Could not load legacy Excel file '{path}' into DB: {e}")
        if migrated_count > 0:
            db.commit()
            logger.info(f"💾 Backed up {migrated_count} existing Excel reports into persistent database storage.")
    except Exception as e:
        logger.error(f"Error backing up legacy Excel files to DB: {e}")
        db.rollback()
    finally:
        db.close()

migrate_excel_files_to_db()


def add_history_entry(user_id: str, username: str, session_id: str, excel_path: str, usn_count: int, usn_range_summary: str) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        file_name = os.path.basename(excel_path)
        file_size = os.path.getsize(excel_path) if os.path.exists(excel_path) else 0
        file_size_kb = round(file_size / 1024, 1)

        # Upload to persistent cloud storage & load binary bytes for DB BLOB backup
        excel_url, excel_bytes, storage_provider = upload_to_persistent_storage(excel_path, session_id)

        entry = History(
            id=f"hist_{uuid.uuid4().hex[:8]}",
            user_id=user_id,
            username=username,
            session_id=session_id,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            usn_count=usn_count,
            usn_range_summary=usn_range_summary,
            excel_file=file_name,
            excel_path=excel_path,
            excel_url=excel_url,
            storage_provider=storage_provider,
            excel_data=excel_bytes,
            file_size_kb=file_size_kb
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        logger.info(f"📁 Archived Excel analysis history entry for '{username}': {file_name} (Provider: {storage_provider}, Size: {file_size_kb} KB)")
        return entry.to_dict()
    except Exception as e:
        logger.error(f"Error saving history entry to database: {e}")
        db.rollback()
        return {}
    finally:
        db.close()


class UserRegisterRequest(BaseModel):
    username: str
    password: str
    full_name: str = ""
    email: str = ""
    role: str = "student"
    department: str = ""

class UserUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    department: Optional[str] = None
    new_password: Optional[str] = None

class UserLoginRequest(BaseModel):
    username: str
    password: str


def clean_session(session_id: str):
    """Destroys temporary workspace and cached processing data for a specific session."""
    if not session_id:
        return
    with registry_lock:
        session_data = session_registry.pop(session_id, None)

    # Clean up session workspace directory
    session_dir = os.path.join(EXPORTS_DIR, f"session_{session_id}")
    if os.path.exists(session_dir):
        try:
            import shutil
            shutil.rmtree(session_dir, ignore_errors=True)
            logger.info(f"🧹 Purged temporary workspace for session: {session_id}")
        except Exception as e:
            logger.warning(f"Could not purge session workspace '{session_dir}': {e}")


def cleanup_old_sessions(max_sessions: int = 50):
    """Clean up oldest session files and memory entries to prevent disk/memory bloat."""
    with registry_lock:
        if len(session_registry) > max_sessions:
            keys_to_remove = list(session_registry.keys())[:-max_sessions]
            for key in keys_to_remove:
                session_data = session_registry.pop(key, None)
                if session_data:
                    session_dir = session_data.get("session_dir") or os.path.join(EXPORTS_DIR, f"session_{key}")
                    file_path = session_data.get("file_path")
                    if session_dir and os.path.exists(session_dir):
                        try:
                            import shutil
                            shutil.rmtree(session_dir, ignore_errors=True)
                        except Exception:
                            pass
                    elif file_path and os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except Exception:
                            pass

def parse_usn_range(start_usn: str, end_usn: str) -> List[str]:
    """Generates a list of USNs from start to end range (inclusive). Handles same prefix or shortened right end (e.g. 120 or 4DM21CS120)."""
    start = start_usn.strip().upper()
    end = end_usn.strip().upper()
    
    match_start = re.match(r"^([A-Z0-9]+?)(\d+)$", start)
    if not match_start:
        return [start]
        
    prefix_start, num_start = match_start.groups()
    width = len(num_start)
    
    # Check if end is just numeric (e.g. "120" or "430")
    if re.match(r"^\d+$", end):
        end = f"{prefix_start}{int(end):0{width}d}"
        
    match_end = re.match(r"^([A-Z0-9]+?)(\d+)$", end)
    if not match_end:
        return [start, end]
        
    prefix_end, num_end = match_end.groups()
    if prefix_start != prefix_end:
        return [start, end]
        
    s_idx = int(num_start)
    e_idx = int(num_end)
    step = 1 if s_idx <= e_idx else -1
    
    usns = []
    for i in range(s_idx, e_idx + step, step):
        usns.append(f"{prefix_start}{i:0{width}d}")
    return usns

def parse_multiple_usns(usn_input_str: str) -> List[str]:
    """
    Parses comma-, semicolon-, or newline-separated list of individual USNs and range expressions.
    Examples supported:
      - "4DM21CS001, 4DM21CS002"
      - "4DM21CS001-4DM21CS120, 4DM22CS400-4DM22CS430" (Regular + Lateral Entry)
      - "4DM21CS001-060, 4DM22CS400-430" (Shortened range notation)
    """
    raw_list = re.split(r'[,;\n]+', usn_input_str)
    result_usns = []
    seen = set()
    
    for item in raw_list:
        token = item.strip().upper()
        if not token:
            continue
            
        if "-" in token:
            parts = token.split("-")
            if len(parts) == 2:
                range_list = parse_usn_range(parts[0], parts[1])
                for u in range_list:
                    if u not in seen:
                        seen.add(u)
                        result_usns.append(u)
                continue
                
        if token not in seen:
            seen.add(token)
            result_usns.append(token)
            
    return result_usns

@app.get("/", response_class=HTMLResponse)
def read_root():
    html_path = os.path.join(TEMPLATES_DIR, "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h3>VTU Result Analyzer templates/index.html is missing.</h3>", status_code=404)

# ============================================
# AUTHENTICATION & ADMIN USER MANAGEMENT APIS
# ============================================

def get_admin_user(token: str = Query(default=""), authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Helper to enforce Admin-only access on administrative endpoints (restricted to UttamBhise)."""
    req_token = token.strip()
    if not req_token and authorization and authorization.startswith("Bearer "):
        req_token = authorization.replace("Bearer ", "").strip()
    
    if not req_token or req_token not in active_tokens:
        raise HTTPException(status_code=401, detail="Authentication required. Please log in.")
    
    user_info = active_tokens[req_token]
    clean_username = user_info.get("username", "").strip().lower()
    if user_info.get("role") != "admin" or clean_username != "uttambhise":
        raise HTTPException(status_code=403, detail="🔒 Access Denied: CRUD operations are strictly restricted to primary administrator UttamBhise.")
        
    return user_info

@app.post("/api/auth/register")
def register_user(req: UserRegisterRequest, token: str = Query(default=""), authorization: Optional[str] = Header(None)):
    """Restricted Registration Endpoint: Only accessible by System Administrators."""
    get_admin_user(token, authorization)
    return admin_create_user(req, token, authorization)

@app.get("/api/admin/users")
def get_all_users(token: str = Query(default=""), authorization: Optional[str] = Header(None)):
    """Admin endpoint to fetch list of all registered user accounts."""
    get_admin_user(token, authorization)
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.created_at.desc()).all()
        return [u.to_dict() for u in users]
    finally:
        db.close()

@app.post("/api/admin/users")
def admin_create_user(req: UserRegisterRequest, token: str = Query(default=""), authorization: Optional[str] = Header(None)):
    """Admin endpoint to provision a new user account."""
    admin_info = get_admin_user(token, authorization)
    
    username = req.username.strip()
    if not username or len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters.")
    if not req.password or len(req.password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters.")
    
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username.ilike(username)).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Username '{username}' already exists.")
        
        hashed_pwd, salt = hash_password(req.password)
        user_id = f"usr_{uuid.uuid4().hex[:8]}"
        role = req.role.strip() if req.role and req.role.strip() else "student"
        department = req.department.strip() if req.department else ""
        full_name = req.full_name.strip() or username
        
        new_user = User(
            id=user_id,
            username=username,
            hashed_password=hashed_pwd,
            salt=salt,
            full_name=full_name,
            email=req.email.strip(),
            role=role,
            department=department,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        
        logger.info(f"👑 Admin '{admin_info['username']}' created user account: '{username}' ({role})")
        
        return {
            "status": "success",
            "message": f"User '{username}' created successfully!",
            "user": new_user.to_dict()
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating user in DB: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred while creating user.")
    finally:
        db.close()

@app.put("/api/admin/users/{user_id}")
def admin_update_user(user_id: str, req: UserUpdateRequest, token: str = Query(default=""), authorization: Optional[str] = Header(None)):
    """Admin endpoint to edit/update a user account's profile, role, or reset password."""
    admin_info = get_admin_user(token, authorization)
    db = SessionLocal()
    try:
        target_user = db.query(User).filter(User.id == user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="User account not found.")
            
        if req.full_name is not None:
            target_user.full_name = req.full_name.strip()
        if req.email is not None:
            target_user.email = req.email.strip()
        if req.department is not None:
            target_user.department = req.department.strip()
        if req.role is not None:
            if target_user.username.strip().lower() == "uttambhise":
                target_user.role = "admin"
            else:
                target_user.role = req.role.strip() or "student"
                
        if req.new_password and len(req.new_password.strip()) >= 4:
            hashed_pwd, salt = hash_password(req.new_password.strip())
            target_user.hashed_password = hashed_pwd
            target_user.salt = salt
            
        db.commit()
        db.refresh(target_user)
        updated_dict = target_user.to_dict()
        
        # Update active token data if user is logged in
        for tok, info in list(active_tokens.items()):
            if info.get("user_id") == user_id:
                info["full_name"] = updated_dict["full_name"]
                info["email"] = updated_dict["email"]
                info["role"] = updated_dict["role"]
                info["department"] = updated_dict.get("department", "")
                
        logger.info(f"👑 Admin '{admin_info['username']}' updated user account: '{target_user.username}'")
        return {"status": "success", "message": f"User account '{target_user.username}' updated successfully.", "user": updated_dict}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating user in DB: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred while updating user.")
    finally:
        db.close()

@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: str, token: str = Query(default=""), authorization: Optional[str] = Header(None)):
    """Admin endpoint to delete/revoke a user account."""
    admin_info = get_admin_user(token, authorization)
    db = SessionLocal()
    try:
        target_user = db.query(User).filter(User.id == user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="User account not found.")
            
        if target_user.username.strip().lower() == "uttambhise":
            raise HTTPException(status_code=400, detail="Cannot delete primary administrator account UttamBhise.")
            
        deleted_username = target_user.username
        db.delete(target_user)
        db.commit()
        
        # Invalidate active session tokens for deleted user
        for tok, info in list(active_tokens.items()):
            if info.get("user_id") == user_id:
                del active_tokens[tok]
                
        logger.info(f"👑 Admin '{admin_info['username']}' deleted user account: '{deleted_username}'")
        return {"status": "success", "message": f"User account '{deleted_username}' deleted successfully."}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting user from DB: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred while deleting user.")
    finally:
        db.close()


@app.post("/api/auth/login")
def login_user(req: UserLoginRequest):
    username = req.username.strip()
    user = get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
        
    if not verify_password(req.password, user.get("salt", ""), user.get("hashed_password", "")):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
        
    token = secrets.token_hex(24)
    role = user.get("role", "student")
    full_name = user.get("full_name", user["username"])
    
    with tokens_lock:
        active_tokens[token] = {
            "token": token,
            "user_id": user["id"],
            "username": user["username"],
            "role": role,
            "department": user.get("department", ""),
            "full_name": full_name,
            "email": user.get("email", ""),
            "created_at": datetime.now().isoformat()
        }
    
    logger.info(f"🔑 User logged in: {user['username']} ({role}) [Token: {token[:8]}...]")
    
    return {
        "status": "success",
        "message": f"Welcome back, {full_name}!",
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": role,
            "full_name": full_name,
            "email": user.get("email", "")
        }
    }

@app.get("/api/auth/me")
def get_current_user(token: str = Query(default=""), authorization: Optional[str] = Header(None)):
    req_token = token.strip()
    if not req_token and authorization and authorization.startswith("Bearer "):
        req_token = authorization.replace("Bearer ", "").strip()
        
    if req_token:
        with tokens_lock:
            user_info = active_tokens.get(req_token)
        if user_info:
            return {
                "authenticated": True,
                "user": user_info
            }
    return {
        "authenticated": False,
        "user": None
    }

@app.post("/api/auth/logout")
def logout_user(token: str = Query(default=""), session_id: str = Query(default=""), authorization: Optional[str] = Header(None)):
    """
    Safely logs out the current user session:
    1. Invalidates ONLY the user's specific session token.
    2. Destroys and cleans up that session's temporary workspace directory.
    3. Preserves permanent database history records intact.
    """
    req_token = token.strip()
    if not req_token and authorization and authorization.startswith("Bearer "):
        req_token = authorization.replace("Bearer ", "").strip()
        
    if req_token:
        with tokens_lock:
            active_tokens.pop(req_token, None)

    if session_id:
        clean_session(session_id.strip())

    return {"status": "success", "message": "Logged out successfully."}
        
# ============================================
# EXCEL RESULT ANALYSIS HISTORY API ENDPOINTS
# ============================================

@app.get("/api/history")
def get_user_history(token: str = Query(default=""), authorization: Optional[str] = Header(None)):
    """Fetches past Excel result analysis history."""
    req_token = token.strip() if isinstance(token, str) else ""
    if not req_token and isinstance(authorization, str) and authorization.startswith("Bearer "):
        req_token = authorization.replace("Bearer ", "").strip()
        
    history = load_history()
    
    if req_token:
        with tokens_lock:
            user_info = active_tokens.get(req_token)
        if user_info:
            clean_uname = user_info.get("username", "").strip().lower()
            if user_info.get("role") == "admin" or clean_uname == "uttambhise":
                return history
            else:
                return [h for h in history if h.get("user_id") == user_info.get("user_id") or h.get("username", "").strip().lower() == clean_uname]
            
    # Always return history entries if logged out or token refreshed so no file is lost
    return history

@app.get("/api/history/download/{history_id}")
def download_history_excel(history_id: str, token: str = Query(default=""), authorization: Optional[str] = Header(None)):
    """Downloads a specific historical Excel report (.xlsx) permanently from persistent storage."""
    req_token = token.strip()
    if not req_token and authorization and authorization.startswith("Bearer "):
        req_token = authorization.replace("Bearer ", "").strip()
        
    if not req_token:
        raise HTTPException(status_code=401, detail="Authentication required.")
        
    with tokens_lock:
        user_info = active_tokens.get(req_token)
        
    if not user_info:
        raise HTTPException(status_code=401, detail="Authentication required.")
    db = SessionLocal()
    try:
        entry = db.query(History).filter(History.id == history_id).first()
        if not entry:
            raise HTTPException(status_code=404, detail="History record not found in database.")
            
        if user_info.get("role") != "admin" and entry.user_id != user_info.get("user_id"):
            raise HTTPException(status_code=403, detail="Access denied.")
            
        filename = f"VTU_Analysis_Report_{entry.session_id[:8]}.xlsx" if entry.session_id else (entry.excel_file or "VTU_Analysis_Report.xlsx")

        # 1. Redirect if Cloud URL is available
        if entry.excel_url and entry.excel_url.startswith("http"):
            logger.info(f"☁️ Redirecting history download to cloud URL: {entry.excel_url}")
            return Response(status_code=307, headers={"Location": entry.excel_url})

        # 2. Serve from local disk if file is present
        excel_path = entry.excel_path
        if not excel_path or not os.path.exists(excel_path):
            excel_path = os.path.join(EXPORTS_DIR, entry.excel_file or "")

        if excel_path and os.path.exists(excel_path):
            return FileResponse(
                path=excel_path,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename=filename
            )

        # 3. Serve binary BLOB stored persistently in database (Survives Render restarts)
        if entry.excel_data:
            logger.info(f"📥 Serving persistent database Excel BLOB for history record '{history_id}'")
            return Response(
                content=entry.excel_data,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'}
            )

        raise HTTPException(status_code=404, detail="Excel report file does not exist on server or persistent storage.")
    finally:
        db.close()


def get_admin_user(token: str = "", authorization: Optional[str] = None) -> Dict[str, Any]:
    """Helper to verify if request caller is an authenticated administrator."""
    req_token = token.strip()
    if not req_token and authorization and authorization.startswith("Bearer "):
        req_token = authorization.replace("Bearer ", "").strip()
        
    if not req_token:
        raise HTTPException(status_code=401, detail="Authentication token required.")
        
    with tokens_lock:
        user_info = active_tokens.get(req_token)
        
    if not user_info:
        raise HTTPException(status_code=401, detail="Session expired or invalid token.")
        
    clean_username = user_info.get("username", "").strip().lower()
    if user_info.get("role") != "admin" and clean_username != "uttambhise":
        raise HTTPException(status_code=403, detail="🔒 Access Denied: Administrator privileges required.")
        
    return user_info

@app.delete("/api/history/{history_id}")
def delete_history_entry(history_id: str, token: str = Query(default=""), authorization: Optional[str] = Header(None)):
    """Deletes an Excel analysis history record and purges its stored Excel sheet."""
    # Verify Admin Access
    get_admin_user(token, authorization)
        
    db = SessionLocal()
    try:
        entry = db.query(History).filter(History.id == history_id).first()
        if not entry:
            raise HTTPException(status_code=404, detail="History record not found.")
            
        # Delete excel file from disk if present
        excel_path = entry.excel_path
        if excel_path and os.path.exists(excel_path):
            try:
                os.remove(excel_path)
            except Exception as e:
                logger.warning(f"Could not remove Excel file from disk: {e}")
                
        db.delete(entry)
        db.commit()
        return {"status": "success", "message": "Excel analysis history entry deleted."}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting history entry from DB: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred while deleting history entry.")
    finally:
        db.close()



@app.get("/api/download/{session_id}")
def download_excel(session_id: str):
    """Serves the generated Excel sheet for download, with persistent DB fallback if local disk was cleared by server restart."""
    filename = f"VTU_Result_Analysis_{session_id[:8]}.xlsx"

    # 1. Check in-memory registry for local file
    if session_id in session_registry:
        file_path = session_registry[session_id].get("file_path")
        if file_path and os.path.exists(file_path):
            return FileResponse(
                path=file_path,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename=filename
            )

    # 2. Check local disk EXPORTS_DIR
    local_file = os.path.join(EXPORTS_DIR, f"VTU_Results_{session_id}.xlsx")
    if os.path.exists(local_file):
        return FileResponse(
            path=local_file,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=filename
        )

    # 3. Persistent Database Storage Fallback (Survives Render restarts / sleeps)
    db = SessionLocal()
    try:
        entry = db.query(History).filter(History.session_id == session_id).first()
        if entry:
            if entry.excel_url and entry.excel_url.startswith("http"):
                logger.info(f"☁️ Redirecting session download to cloud URL: {entry.excel_url}")
                return Response(status_code=307, headers={"Location": entry.excel_url})

            if entry.excel_data:
                logger.info(f"📥 Serving persistent database Excel BLOB for session '{session_id}'")
                return Response(
                    content=entry.excel_data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'}
                )
    finally:
        db.close()

    raise HTTPException(status_code=404, detail="Excel report file not found on server or persistent storage.")

@app.websocket("/ws/scrape/{session_id}")
async def websocket_scrape(websocket: WebSocket, session_id: str):
    """
    WebSocket scraper loop.
    1. Connect and parse scraping settings.
    2. Loop through targeted USNs.
    3. Stream Captchas to the UI and await user entries.
    4. Submit & Crawl pages.
    5. Aggregate and run Pandas analysis.
    6. Generate Excel download bundle.
    """
    await websocket.accept()
    logger.info(f"WebSocket connection established for session: {session_id}")
    
    # Helper: silently swallow send errors on already-closed connections
    async def safe_send(payload: dict):
        try:
            await websocket.send_json(payload)
        except Exception:
            pass  # Connection was already closed by client
    
    scraper = None
    try:
        # Step 1: Wait for client config input
        config = await websocket.receive_json()
        logger.info(f"Received scraping configurations: {config}")
        
        # Check API Key if SERVER_API_KEY environment variable is configured
        server_api_key = os.getenv("SERVER_API_KEY", "").strip()
        client_api_key = config.get("api_key", "").strip()
        if server_api_key and client_api_key != server_api_key:
            await safe_send({
                "type": "error",
                "message": "🔒 Access Denied: Invalid or missing Server API Key authorization."
            })
            await websocket.close()
            return

        start_usn = config.get("start_usn", "").strip().upper()
        end_usn = config.get("end_usn", "").strip()
        usn_list_str = config.get("usn_list", "").strip()
        portal_url = config.get("portal_url", "").strip()
        existing_results = config.get("existing_results", [])
        completed_usns_input = config.get("completed_usns", [])
        start_from_usn = config.get("start_from_usn", "").strip().upper()
        
        # Build USN scraping list
        target_usns = []
        if usn_list_str:
            target_usns = parse_multiple_usns(usn_list_str)
        elif start_usn and end_usn:
            target_usns = parse_usn_range(start_usn, end_usn)
            
        if not target_usns:
            await safe_send({
                "type": "error",
                "message": "No valid USNs specified. Please input correct ranges or lists."
            })
            await websocket.close()
            return
            
        # Determine completed USNs for session resume
        completed_usns = set(u.upper() for u in completed_usns_input)
        if isinstance(existing_results, list):
            for item in existing_results:
                if isinstance(item, dict) and "usn" in item:
                    completed_usns.add(item["usn"].upper())

        if start_from_usn and start_from_usn in target_usns:
            start_index = target_usns.index(start_from_usn)
            for u in target_usns[:start_index]:
                completed_usns.add(u)

        already_done_count = len([u for u in target_usns if u in completed_usns])
        remaining_count = len(target_usns) - already_done_count

        # Log parsed count
        if already_done_count > 0:
            await safe_send({
                "type": "log",
                "message": f"🔄 Resuming session: {already_done_count} USNs already completed. {remaining_count} remaining out of {len(target_usns)} target USNs."
            })
        else:
            await safe_send({
                "type": "log",
                "message": f"Successfully loaded {len(target_usns)} target USNs into the scraping queue."
            })
        
        # Step 2: Initialize crawler
        use_fast_http = config.get("use_fast_http", True)
        auto_solve_captcha = config.get("auto_solve_captcha", True)
        
        scraper = VTUScraper(session_id=session_id, custom_url=portal_url, use_simulation=False, use_fast_http=use_fast_http)
        
        if use_fast_http:
            await safe_send({"type": "log", "message": "⚡ Ultra-Fast Direct HTTP Scraper Engine active (Sub-second per USN)."})
        else:
            await safe_send({"type": "log", "message": "Launching automated Selenium Chrome driver in background..."})
            success = scraper.initialize_browser()
            if not success:
                await safe_send({
                    "type": "error",
                    "message": "❌ Could not initialize Google Chrome WebDriver. Please ensure Chrome is installed on the system."
                })
                await websocket.close()
                return
            
        scraped_results = list(existing_results) if isinstance(existing_results, list) else []
        
        # Step 3: Run Scraping loop for each USN
        for idx, usn in enumerate(target_usns):
            # Skip if already processed in resumed session
            if usn in completed_usns:
                continue

            # Stream status
            await safe_send({
                "type": "status_update",
                "usn": usn,
                "current": idx + 1,
                "total": len(target_usns),
                "message": f"Opening result portal for USN: {usn}"
            })
            
            resolved = False
            attempts = 0
            max_attempts = 4
            
            while not resolved and attempts < max_attempts:
                attempts += 1
                try:
                    # Get captcha screenshot / fast HTTP image
                    captcha_img_base64 = scraper.get_captcha(usn)
                    
                    captcha_code = None
                    result = None

                    # Auto-OCR solve attempt
                    if auto_solve_captcha and attempts == 1:
                        ocr_code = solve_captcha_ocr_base64(captcha_img_base64)
                        if ocr_code:
                            await safe_send({
                                "type": "log",
                                "message": f"🤖 Auto-OCR predicted CAPTCHA '{ocr_code}' for {usn}. Submitting..."
                            })
                            ocr_result = scraper.submit_and_scrape(usn, ocr_code)
                            if ocr_result["status"] in ["success", "not_found"]:
                                result = ocr_result
                                captcha_code = ocr_code
                            else:
                                await safe_send({
                                    "type": "log",
                                    "message": f"⚠️ Auto-OCR attempt for {usn} returned invalid captcha. Prompting for manual input..."
                                })

                    if not captcha_code:
                        # Send captcha image to frontend and wait for user input
                        await safe_send({
                            "type": "captcha_required",
                            "usn": usn,
                            "captcha_img": captcha_img_base64,
                            "attempt": attempts,
                            "message": "Invalid CAPTCHA code! Please enter a valid captcha code." if attempts > 1 else "Enter CAPTCHA code to fetch result."
                        })
                        
                        # Wait for client solution input — detect disconnect
                        logger.info(f"Waiting for CAPTCHA solution for {usn} (Attempt {attempts}/{max_attempts})")
                        try:
                            client_response = await websocket.receive_json()
                        except WebSocketDisconnect:
                            logger.info(f"Client disconnected while waiting for captcha for {usn}")
                            return  # Exit handler cleanly
                        
                        # Handle CAPTCHA refresh request from frontend without penalizing attempts counter
                        if client_response.get("action") == "refresh_captcha" or client_response.get("refresh"):
                            logger.info(f"User requested fresh CAPTCHA image for {usn}")
                            await safe_send({
                                "type": "log",
                                "message": f"🔄 Refreshing CAPTCHA image for {usn}..."
                            })
                            attempts -= 1  # Revert attempt count increment
                            continue  # Fetch a new captcha image and send to client

                        captcha_code = client_response.get("captcha_code", "").strip()
                        result = scraper.submit_and_scrape(usn, captcha_code)
                    
                    if result["status"] == "invalid_captcha":
                        await safe_send({
                            "type": "log",
                            "message": f"❌ Invalid CAPTCHA code for USN {usn}. Remaining on USN {usn} — please enter a valid CAPTCHA."
                        })
                        continue  # Re-loops to fetch new captcha for same USN
                        
                    elif result["status"] == "not_found":
                        reason = result.get("message", "University Seat Number is not available or Invalid.")
                        await safe_send({
                            "type": "progress",
                            "usn": usn,
                            "status": "NOT_FOUND",
                            "message": f"⏩ Skipping {usn}: {reason}"
                        })
                        resolved = True
                        
                    elif result["status"] == "success":
                        student_data = result["data"]
                        scraped_results.append(student_data)
                        await safe_send({
                            "type": "progress",
                            "usn": usn,
                            "status": "SUCCESS",
                            "data": student_data,
                            "message": f"✅ {usn}: Scraped successfully. {student_data['name']} - {student_data['status']} ({student_data['percentage']}%)"
                        })
                        resolved = True
                        
                    else:  # Error
                        await safe_send({
                            "type": "progress",
                            "usn": usn,
                            "status": "ERROR",
                            "message": f"⚠️ {usn}: Scraping error: {result.get('error', 'Unknown exception')}"
                        })
                        resolved = True
                        
                except Exception as e:
                    logger.error(f"Exception during USN {usn} crawl cycle: {str(e)}")
                    await safe_send({
                        "type": "log",
                        "message": f"⚠️ Exception occurred while fetching {usn}: {str(e)}"
                    })
                    resolved = True  # Move on if crashed
                    
            if not resolved:
                await safe_send({
                    "type": "progress",
                    "usn": usn,
                    "status": "TIMEOUT",
                    "message": f"⏳ {usn}: Skipping USN. Failed to resolve captcha after {max_attempts} attempts."
                })
                
        # Step 4: Finish scraping and run analysis
        await safe_send({"type": "log", "message": "📊 Compiling scraped student data and running analytics..."})
        
        if scraped_results:
            analyzer = ResultAnalyzer(scraped_results)
            analysis_data = analyzer.analyze()
            
            # Export to Excel inside session-isolated workspace directory
            session_dir = os.path.join(EXPORTS_DIR, f"session_{session_id}")
            os.makedirs(session_dir, exist_ok=True)

            excel_filename = f"VTU_Results_{session_id}.xlsx"
            excel_path = os.path.join(session_dir, excel_filename)
            analyzer.export_to_excel(excel_path)
            
            # Register session in thread-safe in-memory registry
            with registry_lock:
                session_registry[session_id] = {
                    "file_path": excel_path,
                    "session_dir": session_dir,
                    "data": scraped_results,
                    "analysis": analysis_data
                }
            cleanup_old_sessions()

            # Archive analysis history for user
            user_token = config.get("auth_token", "").strip() or config.get("token", "").strip()
            user_info = None
            if user_token:
                with tokens_lock:
                    user_info = active_tokens.get(user_token)
            
            user_id = user_info.get("user_id", "usr_admin_uttam") if user_info else "usr_admin_uttam"
            username = user_info.get("username", "UttamBhise") if user_info else "UttamBhise"

            usn_summary = f"{len(scraped_results)} Students Scraped"
            if target_usns:
                usn_summary = f"{target_usns[0]} - {target_usns[-1]} ({len(scraped_results)} Scraped)"
                
            add_history_entry(
                user_id=user_id,
                username=username,
                session_id=session_id,
                excel_path=excel_path,
                usn_count=len(scraped_results),
                usn_range_summary=usn_summary
            )
            
            await safe_send({
                "type": "completed",
                "message": f"🎉 Successfully completed! Scraped {len(scraped_results)}/{len(target_usns)} students successfully.",
                "download_url": f"/api/download/{session_id}",
                "analysis": analysis_data
            })
        else:
            await safe_send({
                "type": "completed",
                "error": "Failed to scrape any student result data. Please verify your settings or USN range.",
                "message": "Finished with 0 results."
            })
            
    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected for session {session_id}")
    except Exception as e:
        logger.error(f"WebSocket exception: {str(e)}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": f"Fatal server error occurred: {str(e)}"
            })
        except Exception:
            pass
    finally:
        # Safely shut down browser instance to prevent leaks
        if scraper:
            scraper.close_browser()
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"WebSocket scrape session finished: {session_id}")

FEEDBACKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedbacks.json")
TARGET_FEEDBACK_EMAIL = "uttamabhise@gmail.com"

class FeedbackRequest(BaseModel):
    name: str = ""
    email: str = ""
    category: str = "General Feedback"
    rating: int = 5
    message: str

def send_feedback_email_async(entry: dict):
    """
    Sends email notification of user feedback directly to uttamabhise@gmail.com.
    Dispatches via HTTPS API (Resend / Web3Forms) on Port 443 to bypass cloud host SMTP socket blocks,
    falling back to Gmail SMTP socket connection.
    """
    import smtplib
    import json
    import urllib.request
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    
    resend_api_key = os.getenv("RESEND_API_KEY", "").strip()
    web3forms_key = os.getenv("WEB3FORMS_ACCESS_KEY", "8708b761-cd16-4b79-ba2c-554eb547f017").strip()
    
    smtp_user = os.getenv("SMTP_USER", "").strip() or os.getenv("GMAIL_USER", "uttamabhise@gmail.com").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip() or os.getenv("GMAIL_APP_PASSWORD", "bkizjhjlvazlrxho").strip()
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    
    subject = f"[VTU Result Platform Feedback] {entry['category']} - {'⭐' * entry['rating']}"
    
    body = f"""New User Feedback Received for VTU Result Analysis Platform:
======================================================================
Timestamp: {entry['timestamp']}
Category:  {entry['category']}
Rating:    {'⭐' * entry['rating']} ({entry['rating']}/5)
From Name: {entry['name']}
From Email:{entry['email'] or 'Not provided'}

Feedback Comment:
----------------------------------------------------------------------
{entry['message']}
======================================================================
Feedback Entry ID: {entry['id']}
Destination Address: {TARGET_FEEDBACK_EMAIL}
"""

    logger.info(f"📧 [FEEDBACK ROUTER] Processing feedback notification for: {TARGET_FEEDBACK_EMAIL}")
    
    # 1. Dispatch via Web3Forms API (HTTPS Port 443 - Bypasses Render socket restrictions 100%)
    if web3forms_key:
        try:
            req_data = json.dumps({
                "access_key": web3forms_key,
                "subject": subject,
                "name": entry['name'],
                "email": entry['email'] or TARGET_FEEDBACK_EMAIL,
                "message": body
            }).encode("utf-8")
            
            req = urllib.request.Request(
                "https://api.web3forms.com/submit",
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                if resp_data.get("success") is True or resp.status in [200, 201]:
                    logger.info(f"✅ Feedback email successfully delivered via Web3Forms API to {TARGET_FEEDBACK_EMAIL}")
                    return
                else:
                    logger.warning(f"⚠️ Web3Forms API response: {resp_data}")
        except Exception as e:
            logger.error(f"⚠️ Web3Forms API dispatch error: {e}")

    # 3. Dispatch via Gmail SMTP (Tries SSL Port 465 first for local/VPS environments)
    if smtp_user and smtp_password:
        msg = MIMEMultipart()
        msg['From'] = smtp_user
        msg['To'] = TARGET_FEEDBACK_EMAIL
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        
        try:
            with smtplib.SMTP_SSL(smtp_server, 465, timeout=10) as server:
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
            logger.info(f"✅ Feedback email successfully dispatched via SMTP_SSL (Port 465) to {TARGET_FEEDBACK_EMAIL}")
            return
        except Exception as ssl_err:
            logger.warning(f"SMTP_SSL (Port 465) attempt: {ssl_err}. Trying TLS Port 587...")
            try:
                with smtplib.SMTP(smtp_server, 587, timeout=10) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)
                logger.info(f"✅ Feedback email successfully dispatched via SMTP (Port 587) to {TARGET_FEEDBACK_EMAIL}")
                return
            except Exception as e:
                logger.error(f"❌ Failed to dispatch SMTP feedback email to {TARGET_FEEDBACK_EMAIL}: {e}")

    logger.info(f"ℹ️ Feedback saved in feedbacks.json. To enable live email delivery to {TARGET_FEEDBACK_EMAIL}, set GMAIL_APP_PASSWORD or RESEND_API_KEY.")

@app.post("/api/feedback")
def submit_feedback(feedback: FeedbackRequest):
    import json
    import threading
    from datetime import datetime
    
    if not feedback.message.strip():
        raise HTTPException(status_code=400, detail="Feedback message cannot be empty.")
        
    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "name": feedback.name.strip() or "Anonymous User",
        "email": feedback.email.strip(),
        "category": feedback.category,
        "rating": feedback.rating,
        "message": feedback.message.strip(),
        "target_email": TARGET_FEEDBACK_EMAIL
    }
    
    feedbacks = []
    if os.path.exists(FEEDBACKS_FILE):
        try:
            with open(FEEDBACKS_FILE, "r", encoding="utf-8") as f:
                feedbacks = json.load(f)
        except Exception:
            feedbacks = []
            
    feedbacks.insert(0, entry)
    
    try:
        with open(FEEDBACKS_FILE, "w", encoding="utf-8") as f:
            json.dump(feedbacks, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to write feedback: {e}")
        raise HTTPException(status_code=500, detail="Could not save feedback.")
        
    # Dispatch email notification in background thread
    threading.Thread(target=send_feedback_email_async, args=(entry,), daemon=True).start()
    
    return {"status": "success", "message": f"Thank you for your feedback! Your message is sent to {TARGET_FEEDBACK_EMAIL}."}

@app.get("/api/feedbacks")
def get_feedbacks():
    import json
    if os.path.exists(FEEDBACKS_FILE):
        try:
            with open(FEEDBACKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
