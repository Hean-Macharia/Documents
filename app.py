import os, sys, io, base64, re, uuid, copy, threading, time, json, logging, secrets, signal, atexit, hashlib, functools, zipfile, weakref
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any, Callable, Union
from functools import wraps
from enum import Enum

# ------------------------------------------------------------------------------
# Flask & Extensions
# ------------------------------------------------------------------------------
from flask import Flask, request, send_file, render_template, jsonify, session, redirect, url_for, Response, flash, g, abort
from flask_session import Session

# ------------------------------------------------------------------------------
# Database & Cache
# ------------------------------------------------------------------------------
try:
    from pymongo import MongoClient, ASCENDING, DESCENDING
    from pymongo.errors import PyMongoError, ConnectionFailure, ServerSelectionTimeoutError, DuplicateKeyError
    PYMONGO_AVAILABLE = True
except ImportError:
    PYMONGO_AVAILABLE = False

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# ------------------------------------------------------------------------------
# PDF / Image
# ------------------------------------------------------------------------------
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.utils import ImageReader
from PIL import Image, ImageDraw, ImageFont

# ------------------------------------------------------------------------------
# Cloudinary
# ------------------------------------------------------------------------------
import cloudinary
import cloudinary.uploader
import cloudinary.api

# ------------------------------------------------------------------------------
# HTTP / Environment
# ------------------------------------------------------------------------------
import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# PRODUCTION CONFIGURATION
# ============================================================================

# Detect if running on Render
IS_RENDER = os.getenv("RENDER", "").lower() == "true"

# Set TEST_MODE based on environment (False in production)
TEST_MODE = os.getenv("TEST_MODE", "False").lower() == "true"

# ============================================================================
# CLOUDINARY CONFIGURATION
# ============================================================================

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

class CloudinaryStorage:
    @staticmethod
    def upload_pdf(pdf_bytes, bundle_id, form_type, max_retries=3):
        for attempt in range(max_retries):
            try:
                pdf_file = io.BytesIO(pdf_bytes)
                result = cloudinary.uploader.upload(
                    pdf_file,
                    resource_type="raw",
                    folder=f"supporting_docs/{bundle_id}",
                    public_id=form_type,
                    overwrite=True,
                    use_filename=True,
                    unique_filename=False,
                    invalidate=True,
                    access_mode="public",
                    type="upload",
                    format="pdf",
                    upload_preset="ml_default"
                )
                url = result.get("secure_url")
                if url:
                    parts = url.split("/")
                    for i, part in enumerate(parts):
                        if part.startswith("v") and part[1:].isdigit():
                            parts.pop(i)
                            break
                    clean_url = "/".join(parts)
                    return clean_url
                else:
                    result = cloudinary.uploader.upload(
                        io.BytesIO(pdf_bytes),
                        resource_type="raw",
                        folder=f"supporting_docs/{bundle_id}",
                        public_id=form_type,
                        overwrite=True,
                        access_mode="public",
                        type="upload",
                        format="pdf"
                    )
                    url = result.get("secure_url")
                    if url:
                        parts = url.split("/")
                        for i, part in enumerate(parts):
                            if part.startswith("v") and part[1:].isdigit():
                                parts.pop(i)
                                break
                        return "/".join(parts)
            except Exception as e:
                print(f"[CLOUDINARY] Attempt {attempt + 1} failed: {e}")
                time.sleep(1)
        
        try:
            result = cloudinary.uploader.upload(
                io.BytesIO(pdf_bytes),
                resource_type="raw",
                folder=f"supporting_docs/{bundle_id}",
                public_id=form_type,
                overwrite=True,
                access_mode="public",
                type="upload"
            )
            url = result.get("secure_url")
            if url:
                return url
        except Exception as e:
            print(f"[CLOUDINARY] Final fallback failed: {e}")
        
        return None

storage = CloudinaryStorage()

# ============================================================================
# CONSTANTS & ENUMS
# ============================================================================

class PaymentStatus(Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"

class DocumentStatus(Enum):
    PAYMENT_PENDING = "payment_pending"
    PAYMENT_CONFIRMED = "payment_confirmed"
    PDF_GENERATED = "pdf_generated"
    EMAIL_SENT = "email_sent"
    COMPLETED = "completed"

PAGE_H = 792.0
PAGE_W = 612.0
TODAY = date.today().strftime("%d %B %Y")

DOCUMENT_PRICES = {
    "medical": 400,
    "sponsorship": 300,
    "single_parent": 300,
}

FORM_TYPE_DISPLAY = {
    "medical": "Medical Form",
    "sponsorship": "Sponsorship Letter",
    "single_parent": "Single Parent Certification",
}

# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass(frozen=True)
class Config:
    secret_key: str
    env: str
    debug: bool
    port: int = 8080
    host: str = "0.0.0.0"
    session_type: str = "mongodb"
    session_lifetime: int = 86400
    session_cookie_secure: bool = False
    session_cookie_httponly: bool = True
    session_cookie_samesite: str = "Lax"
    mongo_uri: str = ""
    mongo_max_pool: int = 50
    mongo_min_pool: int = 5
    mongo_server_selection_timeout_ms: int = 5000
    mongo_socket_timeout_ms: int = 30000
    mongo_db_name: str = "supporting_docs"
    redis_url: str = ""
    redis_socket_timeout: int = 5
    rate_limit_per_minute: int = 30
    rate_limit_storage: str = "memory"
    mpesa_consumer_key: str = ""
    mpesa_consumer_secret: str = ""
    mpesa_shortcode: str = "4185095"
    mpesa_passkey: str = ""
    mpesa_env: str = "production"
    mpesa_callback_url: str = ""
    mpesa_token_timeout: Tuple[int, int] = (3, 3)
    mpesa_stk_timeout: Tuple[int, int] = (10, 10)
    mpesa_query_timeout: Tuple[int, int] = (2, 2)
    payment_amount_per_doc: int = 300
    referral_discount_per_document: int = 50
    brevo_api_key: str = ""
    brevo_sender_email: str = "courseschecker@gmail.com"
    brevo_sender_name: str = "EduDocs Kenya"
    admin_username: str = ""
    admin_password: str = ""
    stamp_scale: float = 1.5
    max_background_workers: int = 4
    task_queue_max_size: int = 100
    log_level: str = "INFO"
    log_file: str = "app.log"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5
    health_check_timeout: int = 5

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def mpesa_base_url(self) -> str:
        return "https://api.safaricom.co.ke" if self.mpesa_env == "production" else "https://sandbox.safaricom.co.ke"

    @classmethod
    def load(cls) -> "Config":
        env = os.getenv("FLASK_ENV", "development").lower()
        
        if IS_RENDER:
            env = "production"
            debug = False
            print("🚀 Running on Render - Production mode")
        else:
            debug = os.getenv("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
        
        secret_key = os.getenv("SECRET_KEY", "").strip()
        if env == "production" and not secret_key:
            raise RuntimeError("CRITICAL: SECRET_KEY is required in production")
        if not secret_key:
            secret_key = secrets.token_hex(32)
        mongo_uri = os.getenv("MONGO_URI", "").strip()
        if env == "production" and not mongo_uri:
            raise RuntimeError("CRITICAL: MONGO_URI is required in production")
        admin_user = os.getenv("ADMIN_USERNAME", "").strip()
        admin_pass = os.getenv("ADMIN_PASSWORD", "").strip()
        if env == "production" and (not admin_user or not admin_pass):
            raise RuntimeError("CRITICAL: ADMIN_USERNAME and ADMIN_PASSWORD required in production")
        callback = os.getenv("MPESA_CALLBACK_URL", "").strip()
        if env == "production" and not callback:
            raise RuntimeError("CRITICAL: MPESA_CALLBACK_URL required in production")
        return cls(
            secret_key=secret_key, env=env, debug=debug,
            port=int(os.getenv("PORT", "8080")),
            session_type=os.getenv("SESSION_TYPE", "mongodb").lower(),
            session_lifetime=int(os.getenv("SESSION_LIFETIME", "86400")),
            session_cookie_secure=env == "production",
            mongo_uri=mongo_uri,
            mongo_max_pool=int(os.getenv("MONGO_MAX_POOL", "50")),
            mongo_min_pool=int(os.getenv("MONGO_MIN_POOL", "5")),
            redis_url=os.getenv("REDIS_URL", "").strip(),
            rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "30")),
            rate_limit_storage=os.getenv("RATE_LIMIT_STORAGE", "memory").lower(),
            mpesa_consumer_key=os.getenv("MPESA_CONSUMER_KEY", "").strip(),
            mpesa_consumer_secret=os.getenv("MPESA_CONSUMER_SECRET", "").strip(),
            mpesa_shortcode=os.getenv("MPESA_SHORTCODE", "4185095").strip(),
            mpesa_passkey=os.getenv("MPESA_PASSKEY", "").strip(),
            mpesa_env=os.getenv("MPESA_ENVIRONMENT", "production").strip().lower(),
            mpesa_callback_url=callback,
            payment_amount_per_doc=int(os.getenv("PAYMENT_AMOUNT_KES", "300")),
            referral_discount_per_document=int(os.getenv("REFERRAL_DISCOUNT_PER_DOCUMENT", "50")),
            brevo_api_key=os.getenv("BREVO_API_KEY", "").strip(),
            brevo_sender_email=os.getenv("BREVO_SENDER_EMAIL", "courseschecker@gmail.com").strip(),
            brevo_sender_name=os.getenv("BREVO_SENDER_NAME", "EduDocs Kenya").strip(),
            admin_username=admin_user,
            admin_password=admin_pass,
            stamp_scale=float(os.getenv("STAMP_SCALE_FACTOR", "1.5")),
            max_background_workers=int(os.getenv("MAX_BACKGROUND_WORKERS", "4")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            log_file=os.getenv("LOG_FILE", "app.log"),
        )

# ============================================================================
# PHONE FORMATTING
# ============================================================================

def format_phone(phone_number: str) -> Optional[str]:
    if not phone_number:
        return None
    cleaned = re.sub(r"\D", "", phone_number.strip())
    if not cleaned:
        return None
    if cleaned.startswith("0") and len(cleaned) == 10:
        formatted = "254" + cleaned[1:]
    elif cleaned.startswith("254") and len(cleaned) == 12:
        formatted = cleaned
    elif len(cleaned) == 9:
        formatted = "254" + cleaned
    else:
        formatted = cleaned if cleaned.startswith("254") else "254" + cleaned
    return formatted if len(formatted) == 12 and formatted.isdigit() else None

def validate_phone(phone: str) -> bool:
    return bool(phone) and len(phone) == 12 and phone.isdigit() and phone.startswith("254")

# ============================================================================
# LOGGING
# ============================================================================

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
        }
        if hasattr(record, "request_id"):
            log_obj["request_id"] = record.request_id
        if hasattr(record, "user"):
            log_obj["user"] = record.user
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj, default=str)

def setup_logging(cfg: Config):
    logger = logging.getLogger("app")
    logger.setLevel(getattr(logging, cfg.log_level, logging.INFO))
    logger.handlers = []
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(JSONFormatter())
    logger.addHandler(console)
    if cfg.log_file:
        try:
            fh = RotatingFileHandler(
                cfg.log_file, maxBytes=cfg.log_max_bytes,
                backupCount=cfg.log_backup_count, encoding="utf-8"
            )
            fh.setFormatter(JSONFormatter())
            logger.addHandler(fh)
        except Exception as e:
            logger.error(f"File logging setup failed: {e}")
    return logger

class LogContext:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def _log(self, level: int, msg: str, *args, **kwargs):
        extra = kwargs.pop("extra", {})
        try:
            from flask import has_app_context, has_request_context
            if has_request_context():
                if hasattr(g, "request_id"):
                    extra["request_id"] = g.request_id
                if hasattr(g, "user_email"):
                    extra["user"] = g.user_email
        except Exception:
            pass
        self.logger.log(level, msg, *args, extra=extra, **kwargs)

    def info(self, msg, *args, **kwargs): self._log(logging.INFO, msg, *args, **kwargs)
    def warning(self, msg, *args, **kwargs): self._log(logging.WARNING, msg, *args, **kwargs)
    def error(self, msg, *args, **kwargs): self._log(logging.ERROR, msg, *args, **kwargs)
    def critical(self, msg, *args, **kwargs): self._log(logging.CRITICAL, msg, *args, **kwargs)
    def exception(self, msg, *args, **kwargs): self._log(logging.ERROR, msg, *args, exc_info=True, **kwargs)
    def debug(self, msg, *args, **kwargs): self._log(logging.DEBUG, msg, *args, **kwargs)

# ============================================================================
# DATABASE LAYER
# ============================================================================

class DatabaseManager:
    def __init__(self, cfg: Config, logger: LogContext):
        self.cfg = cfg
        self.log = logger
        self._client: Optional[MongoClient] = None
        self._db = None
        self._lock = threading.RLock()
        self._connected = False
        if not PYMONGO_AVAILABLE:
            self.log.warning("PyMongo not installed.")
            return
        if not cfg.mongo_uri:
            self.log.warning("MONGO_URI not set.")
            return
        self._connect()

    def _connect(self):
        try:
            self._client = MongoClient(
                self.cfg.mongo_uri,
                maxPoolSize=self.cfg.mongo_max_pool,
                minPoolSize=self.cfg.mongo_min_pool,
                serverSelectionTimeoutMS=self.cfg.mongo_server_selection_timeout_ms,
                socketTimeoutMS=self.cfg.mongo_socket_timeout_ms,
                retryWrites=True,
                w="majority",
            )
            self._client.admin.command("ping")
            self._db = self._client[self.cfg.mongo_db_name]
            self._connected = True
            self.log.info("MongoDB connected successfully")
            self._ensure_indexes()
        except Exception as e:
            self.log.critical(f"MongoDB connection failed: {e}")
            if self.cfg.is_production:
                raise RuntimeError(f"MongoDB required in production: {e}")

    def _ensure_indexes(self):
        try:
            self._db.documents.create_index("bundle_id", unique=True, background=True)
            self._db.documents.create_index("student_email", background=True)
            self._db.documents.create_index("payment_status", background=True)
            self._db.documents.create_index("transaction_code", background=True)
            self._db.documents.create_index("checkout_request_id", background=True)
            self._db.documents.create_index("created_at", background=True)
            self._db.documents.create_index("document_status", background=True)
        except DuplicateKeyError as e:
            self.log.warning(f"Index creation warning: {e}")
        except Exception as e:
            self.log.error(f"Index creation error: {e}")

    @property
    def db(self):
        return self._db if self._connected and self._db is not None else None

    @property
    def is_connected(self) -> bool:
        if not self._connected or not self._client:
            return False
        try:
            self._client.admin.command("ping")
            return True
        except Exception:
            return False

    def health_check(self) -> Tuple[bool, str]:
        if not self._connected:
            return False, "Not connected"
        try:
            self._client.admin.command("ping", serverSelectionTimeoutMS=2000)
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def close(self):
        if self._client:
            self._client.close()
            self._connected = False
            self.log.info("MongoDB connection closed")

# ============================================================================
# REDIS / CACHE
# ============================================================================

class CacheManager:
    def __init__(self, cfg: Config, logger: LogContext):
        self.cfg = cfg
        self.log = logger
        self._redis: Optional[redis.Redis] = None
        self._memory_cache: Dict[str, Tuple[Any, float]] = {}
        self._memory_lock = threading.RLock()
        if cfg.redis_url and REDIS_AVAILABLE:
            try:
                self._redis = redis.from_url(
                    cfg.redis_url, socket_timeout=cfg.redis_socket_timeout,
                    socket_connect_timeout=3, health_check_interval=30
                )
                self._redis.ping()
                self.log.info("Redis connected")
            except Exception as e:
                self.log.warning(f"Redis connection failed: {e}. Using memory fallback.")
                self._redis = None
        else:
            self.log.info("Redis not configured. Using in-memory cache.")

    def get(self, key: str) -> Optional[Any]:
        try:
            if self._redis:
                val = self._redis.get(key)
                return json.loads(val) if val else None
        except Exception as e:
            self.log.debug(f"Redis get error: {e}")
        with self._memory_lock:
            entry = self._memory_cache.get(key)
            if entry and time.time() < entry[1]:
                return entry[0]
            self._memory_cache.pop(key, None)
            return None

    def set(self, key: str, value: Any, ttl: int = 300):
        try:
            if self._redis:
                self._redis.setex(key, ttl, json.dumps(value, default=str))
                return
        except Exception as e:
            self.log.debug(f"Redis set error: {e}")
        with self._memory_lock:
            self._memory_cache[key] = (value, time.time() + ttl)

    def delete(self, key: str):
        try:
            if self._redis:
                self._redis.delete(key)
        except Exception:
            pass
        with self._memory_lock:
            self._memory_cache.pop(key, None)

    def health_check(self) -> Tuple[bool, str]:
        if self._redis is None:
            return True, "Memory fallback"
        try:
            self._redis.ping()
            return True, "OK"
        except Exception as e:
            return False, str(e)

# ============================================================================
# CIRCUIT BREAKER
# ============================================================================

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60, logger: Optional[LogContext] = None):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.log = logger
        self._state = "CLOSED"
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = threading.RLock()

    def call(self, func: Callable, *args, **kwargs):
        with self._lock:
            if self._state == "OPEN":
                if time.time() - (self._last_failure_time or 0) > self.recovery_timeout:
                    self._state = "HALF_OPEN"
                    self._failure_count = 0
                    if self.log:
                        self.log.info("Circuit breaker half-open")
                else:
                    raise RuntimeError("Circuit breaker is OPEN")
        try:
            result = func(*args, **kwargs)
            with self._lock:
                self._state = "CLOSED"
                self._failure_count = 0
            return result
        except Exception as e:
            with self._lock:
                self._failure_count += 1
                self._last_failure_time = time.time()
                if self._failure_count >= self.failure_threshold:
                    self._state = "OPEN"
                    if self.log:
                        self.log.error(f"Circuit breaker OPEN after {self.failure_threshold} failures")
            raise e

# ============================================================================
# M-PESA CLIENT
# ============================================================================

class MpesaClient:
    def __init__(self, cfg: Config, cache: CacheManager, logger: LogContext):
        self.cfg = cfg
        self.cache = cache
        self.log = logger
        self._cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60, logger=logger)
        self._session = requests.Session()
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[502, 503, 504], allowed_methods=["GET", "POST"])
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _get_token(self) -> Optional[str]:
        cached = self.cache.get("mpesa_token")
        if cached:
            return cached
        if not self.cfg.mpesa_consumer_key or not self.cfg.mpesa_consumer_secret:
            self.log.error("M-Pesa credentials not configured")
            return None
        url = f"{self.cfg.mpesa_base_url}/oauth/v1/generate?grant_type=client_credentials"
        try:
            resp = self._session.get(
                url, auth=HTTPBasicAuth(self.cfg.mpesa_consumer_key, self.cfg.mpesa_consumer_secret),
                timeout=self.cfg.mpesa_token_timeout
            )
            data = resp.json()
            if resp.status_code == 200 and "access_token" in data:
                token = data["access_token"]
                self.cache.set("mpesa_token", token, ttl=3300)
                return token
            self.log.error(f"Token error: {data}")
        except Exception as e:
            self.log.error(f"Token fetch failed: {e}")
        return None

    def _password_and_timestamp(self):
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        data = self.cfg.mpesa_shortcode + self.cfg.mpesa_passkey + timestamp
        password = base64.b64encode(data.encode()).decode("utf-8")
        return password, timestamp

    def init_stk_push(self, phone: str, account_ref: str, desc: str, amount: int) -> Tuple[bool, Dict]:
        try:
            return self._cb.call(self._stk_push, phone, account_ref, desc, amount)
        except Exception as e:
            return False, {"error": str(e)}

    def _stk_push(self, phone: str, account_ref: str, desc: str, amount: int) -> Tuple[bool, Dict]:
        t0 = time.time()
        token = self._get_token()
        if not token:
            return False, {"error": "Could not obtain M-Pesa access token"}
        password, timestamp = self._password_and_timestamp()
        payload = {
            "BusinessShortCode": self.cfg.mpesa_shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": amount,
            "PartyA": phone,
            "PartyB": self.cfg.mpesa_shortcode,
            "PhoneNumber": phone,
            "CallBackURL": self.cfg.mpesa_callback_url,
            "AccountReference": account_ref[:12],
            "TransactionDesc": desc[:13],
        }
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        resp = self._session.post(
            f"{self.cfg.mpesa_base_url}/mpesa/stkpush/v1/processrequest",
            json=payload, headers=headers, timeout=self.cfg.mpesa_stk_timeout
        )
        data = resp.json()
        elapsed = time.time() - t0
        if resp.status_code == 200 and data.get("ResponseCode") == "0":
            return True, {
                "checkout_request_id": data.get("CheckoutRequestID"),
                "merchant_request_id": data.get("MerchantRequestID"),
                "customer_message": data.get("CustomerMessage", "Check your phone"),
                "elapsed_seconds": round(elapsed, 2),
            }
        return False, {"error": data.get("errorMessage", data.get("ResponseDescription", "STK push failed"))}

    def query_transaction(self, checkout_request_id: str, max_retries: int = 3) -> Tuple[bool, Dict]:
        for attempt in range(max_retries):
            try:
                return self._cb.call(self._query, checkout_request_id)
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return False, {"error": str(e)}
        return False, {"error": "Max retries exceeded"}

    def _query(self, checkout_request_id: str) -> Tuple[bool, Dict]:
        token = self._get_token()
        if not token:
            return False, {"error": "Could not obtain M-Pesa access token"}
        password, timestamp = self._password_and_timestamp()
        payload = {
            "BusinessShortCode": self.cfg.mpesa_shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        resp = self._session.post(
            f"{self.cfg.mpesa_base_url}/mpesa/stkpushquery/v1/query",
            json=payload, headers=headers, timeout=self.cfg.mpesa_query_timeout
        )
        data = resp.json()
        if resp.status_code != 200:
            return False, {"error": data.get("errorMessage", "Query failed")}
        result_code = data.get("ResultCode")
        if result_code == 0:
            items = {i.get("Name"): i.get("Value") for i in data.get("CallbackMetadata", {}).get("Item", [])}
            return True, {
                "status": "success",
                "mpesa_receipt_number": items.get("MpesaReceiptNumber", ""),
                "amount": items.get("Amount", 0),
                "phone": items.get("PhoneNumber", ""),
                "transaction_date": items.get("TransactionDate", ""),
            }
        if result_code in (1, 1037) or "pending" in data.get("ResultDesc", "").lower():
            return False, {"status": "pending", "error": data.get("ResultDesc", "Still processing")}
        return False, {"status": "failed", "error": data.get("ResultDesc", "Transaction failed")}

    def parse_callback(self, callback_data: Dict) -> Tuple[Optional[str], Optional[int], str, Dict]:
        try:
            stk = callback_data.get("Body", {}).get("stkCallback", {})
            checkout = stk.get("CheckoutRequestID", "")
            result_code = stk.get("ResultCode")
            result_desc = stk.get("ResultDesc", "")
            items = {i.get("Name"): i.get("Value") for i in stk.get("CallbackMetadata", {}).get("Item", [])}
            return checkout, result_code, result_desc, items
        except Exception as e:
            return None, None, str(e), {}

# ============================================================================
# RATE LIMITER
# ============================================================================

class RateLimiter:
    def __init__(self, cfg: Config, cache: CacheManager, logger: LogContext):
        self.cfg = cfg
        self.cache = cache
        self.log = logger
        self._memory_store: Dict[str, List[float]] = {}
        self._lock = threading.RLock()

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window = 60
        if self.cache._redis:
            try:
                pipe = self.cache._redis.pipeline()
                pipe.zremrangebyscore(key, 0, now - window)
                pipe.zcard(key)
                pipe.zadd(key, {str(now): now})
                pipe.expire(key, window)
                _, count, _, _ = pipe.execute()
                return count < self.cfg.rate_limit_per_minute
            except Exception as e:
                self.log.debug(f"Redis rate limit error: {e}")
        with self._lock:
            if key not in self._memory_store:
                self._memory_store[key] = []
            self._memory_store[key] = [t for t in self._memory_store[key] if now - t < window]
            if len(self._memory_store[key]) >= self.cfg.rate_limit_per_minute:
                return False
            self._memory_store[key].append(now)
            return True

def rate_limit(limiter: RateLimiter):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            client_ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
            if not limiter.is_allowed(client_ip):
                return jsonify({"error": "Rate limit exceeded. Please wait a moment."}), 429
            return f(*args, **kwargs)
        return decorated
    return decorator

# ============================================================================
# TASK EXECUTOR
# ============================================================================

class TaskExecutor:
    def __init__(self, max_workers: int, queue_size: int, logger: LogContext):
        self.log = logger
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._semaphore = threading.Semaphore(queue_size)
        self._futures = weakref.WeakSet()
        self._shutdown = False

    def submit(self, func: Callable, *args, **kwargs):
        if self._shutdown:
            self.log.warning("Task submitted after shutdown")
            return None
        acquired = self._semaphore.acquire(timeout=5)
        if not acquired:
            self.log.error("Task queue full — backpressure triggered")
            raise RuntimeError("Server busy. Please try again later.")
        def wrapper():
            try:
                return func(*args, **kwargs)
            except Exception as e:
                self.log.exception(f"Background task failed: {e}")
                raise
            finally:
                self._semaphore.release()
        future = self._executor.submit(wrapper)
        self._futures.add(future)
        return future

    def shutdown(self, wait: bool = True):
        self._shutdown = True
        self._executor.shutdown(wait=wait)
        self.log.info("Task executor shut down")

# ============================================================================
# EMAIL SERVICE
# ============================================================================

class EmailService:
    def __init__(self, cfg: Config, logger: LogContext):
        self.cfg = cfg
        self.log = logger
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=Retry(total=2, backoff_factor=0.5))
        self._session.mount("https://", adapter)

    def send(self, to_email: str, to_name: str, subject: str, html: str,
             attachments: Optional[List[Tuple[str, bytes]]] = None,
             cc: Optional[List[str]] = None) -> Tuple[bool, str]:
        if not self.cfg.brevo_api_key:
            self.log.error("BREVO_API_KEY not configured")
            return False, "Brevo API key not configured"
        
        url = "https://api.brevo.com/v3/smtp/email"
        headers = {"accept": "application/json", "api-key": self.cfg.brevo_api_key, "content-type": "application/json"}
        
        payload = {
            "sender": {"name": self.cfg.brevo_sender_name, "email": self.cfg.brevo_sender_email},
            "to": [{"email": to_email, "name": to_name or "Valued Customer"}],
            "subject": subject,
            "htmlContent": html,
        }
        
        if cc:
            payload["cc"] = [{"email": email} for email in cc]
            self.log.info(f"CC emails: {cc}")
        
        if attachments:
            payload["attachment"] = [
                {"content": base64.b64encode(data).decode("utf-8"), "name": name}
                for name, data in attachments
            ]

        try:
            resp = self._session.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code in (200, 201):
                self.log.info(f"Email sent to {to_email} (CC: {cc})")
                return True, "OK"
            text = resp.text.lower()
            if "unauthorized" in text or "authorised_ips" in text:
                return False, "IP_WHITELIST_ERROR"
            return False, f"Brevo API error {resp.status_code}: {resp.text}"
        except Exception as e:
            self.log.error(f"Email send failed: {e}")
            return False, str(e)

# ============================================================================
# IN-MEMORY LRU CACHES
# ============================================================================

class LRUCache:
    def __init__(self, capacity: int = 100):
        self._cache = OrderedDict()
        self._lock = threading.RLock()
        self.capacity = capacity

    def get(self, key: str) -> Any:
        with self._lock:
            if key not in self._cache:
                return None
            self._cache.move_to_end(key)
            return self._cache[key]

    def put(self, key: str, value: Any):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            if len(self._cache) > self.capacity:
                self._cache.popitem(last=False)

    def clear(self):
        with self._lock:
            self._cache.clear()

# ============================================================================
# FONT & STAMP SETUP
# ============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STAMPS_DIR = os.path.join(BASE_DIR, "stamps")
os.makedirs(STAMPS_DIR, exist_ok=True)

_font_path = os.path.join(BASE_DIR, "PatrickHand.ttf")
FONT = "Helvetica"
STD_FONT = "Helvetica"
STD_FONT_BOLD = "Helvetica-Bold"

try:
    if os.path.exists(_font_path):
        pdfmetrics.registerFont(TTFont("Hand", _font_path))
        FONT = "Hand"
except Exception:
    pass

# ============================================================================
# STAMP COORDINATES
# ============================================================================

MEDICAL_STAMPS = {
    "officer_stamp": {"box": (475.1, 412.2, 551.9, 440.2)},
    "commissioner_stamp": {"box": (490.5, 542.6, 567.3, 566.6)},
}
SPONSORSHIP_STAMPS = {
    "sponsor_stamp": {"box": (457.1, 428.0, 533.9, 452.0)},
    "commissioner_stamp": {"box": (460.0, 583.7, 536.8, 607.7)},
}
SINGLE_PARENT_STAMPS = {
    "commissioner_stamp": {"box": (475.2, 674.1, 552.0, 698.1)},
}

FORM_STAMP_MAP = {
    "medical": {
        "defs": MEDICAL_STAMPS,
        "type_map": {"officer_stamp": "hospital_stamp", "commissioner_stamp": "commissioner_stamp"}
    },
    "sponsorship": {
        "defs": SPONSORSHIP_STAMPS,
        "type_map": {"sponsor_stamp": "sponsor_stamp", "commissioner_stamp": "commissioner_stamp"}
    },
    "single_parent": {
        "defs": SINGLE_PARENT_STAMPS,
        "type_map": {"commissioner_stamp": "commissioner_stamp"}
    },
}

_stamp_image_cache = LRUCache(capacity=50)

def to_reportlab_box(box, page_h=PAGE_H):
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    rl_y = page_h - y1
    return x0, rl_y, w, h

def get_stamp_image(stamp_type: str, stamps_dir: str, logger: LogContext) -> Optional[ImageReader]:
    cached = _stamp_image_cache.get(stamp_type)
    if cached is not None:
        return cached
    possible_paths = [
        os.path.join(stamps_dir, f"{stamp_type}.{ext}")
        for ext in ["png", "PNG", "jpg", "jpeg", "gif", "webp"]
    ]
    for path in possible_paths:
        if not os.path.exists(path):
            continue
        try:
            file_size = os.path.getsize(path)
            if file_size < 100:
                continue
            img = Image.open(path).convert("RGBA")
            datas = img.getdata()
            new_data = [
                (255, 255, 255, 0) if (item[0] > 200 and item[1] > 200 and item[2] > 200) else item
                for item in datas
            ]
            img.putdata(new_data)
            max_size = 300
            if img.width > max_size or img.height > max_size:
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            reader = ImageReader(buf)
            _stamp_image_cache.put(stamp_type, reader)
            logger.info(f"[STAMP] Loaded {stamp_type}")
            return reader
        except Exception as e:
            logger.error(f"[STAMP] Failed to load {path}: {e}")
    logger.warning(f"[STAMP] No image found for {stamp_type}")
    return None

def draw_stamp(cv, stamp_img, x, y, width, height, rotation=0):
    if not stamp_img:
        return False
    try:
        cv.saveState()
        if rotation != 0:
            cx, cy = x + width / 2, y + height / 2
            cv.translate(cx, cy)
            cv.rotate(rotation)
            cv.drawImage(stamp_img, -width / 2, -height / 2, width=width, height=height, preserveAspectRatio=True, mask="auto")
        else:
            cv.drawImage(stamp_img, x, y, width=width, height=height, preserveAspectRatio=True, mask="auto")
        cv.restoreState()
        return True
    except Exception:
        return False

def get_stamps_for_form_type(form_type: str, stamp_scale: float, logger: LogContext) -> Optional[List[Tuple]]:
    if form_type not in FORM_STAMP_MAP:
        return None
    form_info = FORM_STAMP_MAP[form_type]
    stamps = []
    for internal_name, stamp_def in form_info["defs"].items():
        stamp_type = form_info["type_map"][internal_name]
        x, y, w, h = to_reportlab_box(stamp_def["box"])
        if stamp_scale != 1.0:
            new_w, new_h = w * stamp_scale, h * stamp_scale
            x -= (new_w - w) / 2
            y -= (new_h - h) / 2
            w, h = new_w, new_h
        stamps.append((stamp_type, x, y, w, h, 0))
    return stamps if stamps else None

def draw_text_stamp(cv, stamp_type, x, y, width, height):
    try:
        cv.saveState()
        cx, cy = x + width / 2, y + height / 2
        radius = min(width, height) / 2
        cv.setStrokeColorRGB(0.8, 0.2, 0.2)
        cv.setLineWidth(2)
        cv.circle(cx, cy, radius)
        cv.stroke()
        cv.circle(cx, cy, radius - 5)
        cv.stroke()
        cv.setFillColorRGB(0.8, 0.2, 0.2)
        cv.setFont(STD_FONT_BOLD, 8)
        labels = {
            "hospital_stamp": "HOSPITAL\nSTAMP",
            "commissioner_stamp": "COMMISSIONER\nOF OATHS",
            "sponsor_stamp": "SPONSOR\nSTAMP"
        }
        label = labels.get(stamp_type, stamp_type.upper().replace("_", " "))
        lines = label.split("\n")
        lh = 10
        start_y = cy - (len(lines) * lh) / 2 + lh / 2
        for i, line in enumerate(lines):
            cv.drawCentredString(cx, start_y + i * lh, line)
        cv.restoreState()
    except Exception:
        pass

def create_default_stamps(stamps_dir: str, logger: LogContext):
    configs = {
        "hospital_stamp": {
            "text": "MBAGATHI HOSPITAL\nNAIROBI CITY\nTEL: +2540 202 728 530",
            "color": "#1a237e", "bg_color": "#e8eaf6"
        },
        "commissioner_stamp": {
            "text": "JOSEPH ADVOCATE\nCOMMISSIONER OF OATHS\n& NOTARY PUBLIC",
            "color": "#b71c1c", "bg_color": "#ffebee"
        },
        "sponsor_stamp": {
            "text": "JOMO KENYATTA FOUNDATION\n--- JKF ---",
            "color": "#1b5e20", "bg_color": "#e8f5e9"
        }
    }
    for stamp_type, cfg in configs.items():
        path = os.path.join(stamps_dir, f"{stamp_type}.png")
        if os.path.exists(path):
            continue
        try:
            size = 300
            img = Image.new("RGBA", (size, size), (255, 255, 255, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([5, 5, size - 5, size - 5], fill=cfg["bg_color"], outline=cfg["color"], width=5)
            draw.ellipse([20, 20, size - 20, size - 20], outline=cfg["color"], width=2)
            try:
                font = ImageFont.truetype("arial.ttf", 20)
            except Exception:
                font = ImageFont.load_default()
            lines = cfg["text"].split("\n")
            for i, line in enumerate(lines):
                draw.text((size // 2, size // 2 - 20 + i * 25), line, fill=cfg["color"], font=font, anchor="mm")
            img.save(path, "PNG")
            logger.info(f"Created default stamp: {stamp_type}")
        except Exception as e:
            logger.error(f"Failed to create default stamp {stamp_type}: {e}")

# ============================================================================
# COORDINATE HELPERS
# ============================================================================

def pt_to_rl_y(pdfplumber_top): return PAGE_H - pdfplumber_top
def cy(cell_top, cell_bot, fs=10): return PAGE_H - (cell_top + cell_bot) / 2.0 - fs * 0.35
def text_in_gap(line_top_pdf, label_top_pdf, font_size=9):
    gap_center_pdf = (line_top_pdf + label_top_pdf) / 2.0
    baseline_pdf = gap_center_pdf - font_size * 0.3
    return PAGE_H - baseline_pdf
def text_below_line(line_bottom_pdf, font_size=9, gap=6):
    baseline_pdf = line_bottom_pdf + gap + font_size * 0.25
    return PAGE_H - baseline_pdf
def sig_in_gap(line_top_pdf, label_top_pdf, sig_h=28):
    bottom_pdf = label_top_pdf + 1
    return PAGE_H - bottom_pdf
def Tick(x0, cb_bot):
    return {"font_size": 11, "x": x0 + 3, "y": PAGE_H - cb_bot + 3, "text": "X", "font_name": STD_FONT_BOLD}

# ============================================================================
# SIGNATURE & OVERLAY RENDERERS
# ============================================================================

def draw_signature(cv, sig_data, x, y, w=130, h=28):
    if not sig_data:
        return False
    try:
        if sig_data.startswith("data:image"):
            sig_data = sig_data.split(",")[1]
        raw = base64.b64decode(re.sub(r"\s+", "", sig_data))
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        datas = img.getdata()
        new_data = [
            (255, 255, 255, 0) if (item[0] > 200 and item[1] > 200 and item[2] > 200) else item
            for item in datas
        ]
        img.putdata(new_data)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        cv.drawImage(ImageReader(buf), x, y, width=w, height=h, preserveAspectRatio=True, mask="auto")
        return True
    except Exception:
        return False

def render_overlay(fields, sigs, underlines, pdf_path, stamps=None, stamps_dir: str = STAMPS_DIR, logger: Optional[LogContext] = None):
    buf = io.BytesIO()
    cv = canvas.Canvas(buf, pagesize=letter)
    cv.setFillColorRGB(0.04, 0.04, 0.28)
    for f in fields:
        txt = f.get("text", "")
        if not txt:
            continue
        cv.setFont(f.get("font_name", FONT), f.get("font_size", 10))
        cv.drawString(f.get("x", 0), f.get("y", 0), txt)
    for sig_data, sx, sy, sw, sh in sigs:
        draw_signature(cv, sig_data, sx, sy, sw, sh)
    if stamps and logger:
        for stamp_info in stamps:
            if len(stamp_info) == 5:
                stamp_type, sx, sy, sw, sh = stamp_info
                rotation = 0
            elif len(stamp_info) == 6:
                stamp_type, sx, sy, sw, sh, rotation = stamp_info
            else:
                continue
            stamp_img = get_stamp_image(stamp_type, stamps_dir, logger)
            if stamp_img:
                draw_stamp(cv, stamp_img, sx, sy, sw, sh, rotation)
            else:
                draw_text_stamp(cv, stamp_type, sx, sy, sw, sh)
    cv.setStrokeColorRGB(0.04, 0.04, 0.28)
    cv.setLineWidth(1.5)
    for x1, y1, x2, y2 in underlines:
        cv.line(x1, y1, x2, y2)
    cv.save()
    buf.seek(0)
    orig = PdfReader(pdf_path)
    ovl = PdfReader(buf)
    writer = PdfWriter()
    pg = orig.pages[0]
    pg.merge_page(ovl.pages[0])
    writer.add_page(pg)
    for p in orig.pages[1:]:
        writer.add_page(p)
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()

# ============================================================================
# FORM BUILDERS
# ============================================================================

def build_sponsorship(d, adm, include_admin_sigs=True):
    sl = d.get("sponsored_level", "")
    fields = []
    underlines = []

    SN_REL = {"Organization": 443.35}
    SN_LEVEL_Y = PAGE_H - 350.6 + 2
    SN_LEVEL = {
        "Primary": (413, SN_LEVEL_Y, 448, SN_LEVEL_Y),
        "Secondary": (453, SN_LEVEL_Y, 498, SN_LEVEL_Y),
        "College": (503, SN_LEVEL_Y, 534, SN_LEVEL_Y),
    }
    name_fs = 11
    date_fs = 11
    SN_S3_NAME_Y = text_in_gap(452.0, 457.1, name_fs)
    SN_S3_SIG_Y = sig_in_gap(452.0, 457.1, 28)
    SN_S4_NAME_Y = text_below_line(570.3, name_fs, 6)
    SN_S4_DATE_Y = text_below_line(570.3, date_fs, 6)
    SN_S4_SIG_Y = sig_in_gap(569.7, 561.4, 28)

    fields.extend([
        {"font_size": 10, "x": 190.0, "y": cy(136.82, 152.30), "text": d.get("student_name", ""), "font_name": FONT},
        {"font_size": 10, "x": 190.0, "y": cy(152.30, 167.78), "text": d.get("national_id", ""), "font_name": FONT},
        {"font_size": 10, "x": 404.0, "y": cy(152.30, 167.78), "text": d.get("university", ""), "font_name": FONT},
        {"font_size": 10, "x": 190.0, "y": cy(167.78, 183.26), "text": d.get("kcse_index", ""), "font_name": FONT},
        {"font_size": 10, "x": 404.0, "y": cy(167.78, 183.26), "text": d.get("admission_number", ""), "font_name": FONT},
        {"font_size": 10, "x": 190.0, "y": cy(183.26, 198.86), "text": d.get("telephone", ""), "font_name": FONT},
        {"font_size": 10, "x": 404.0, "y": cy(183.26, 198.86), "text": d.get("email", ""), "font_name": FONT},
    ])
    fields.extend([
        {"font_size": 10, "x": 257.0, "y": cy(240.74, 256.13), "text": adm.get("sponsor_name", ""), "font_name": FONT},
        {"font_size": 10, "x": 257.0, "y": cy(256.13, 271.61), "text": adm.get("sponsor_email", ""), "font_name": FONT},
        {"font_size": 10, "x": 257.0, "y": cy(271.61, 287.09), "text": adm.get("sponsor_telephone", ""), "font_name": FONT},
    ])
    fields.append(Tick(SN_REL["Organization"], 301.37))
    if sl in SN_LEVEL:
        underlines.append(SN_LEVEL[sl])
    fields.append({"font_size": 9, "x": 245.0, "y": PAGE_H - 374.62 + 1, "text": d.get("sponsorship_covered", ""), "font_name": FONT})
    fields.append({"font_size": 9, "x": 392.0, "y": PAGE_H - 386.86 + 1, "text": d.get("completed_duration", ""), "font_name": FONT})
    fields.append({"font_size": name_fs, "x": 85.0, "y": SN_S3_NAME_Y, "text": adm.get("sponsor_name", ""), "font_name": FONT})
    fields.append({"font_size": name_fs, "x": 75.0, "y": SN_S4_NAME_Y, "text": d.get("student_name", ""), "font_name": FONT})
    fields.append({"font_size": date_fs, "x": 430.0, "y": SN_S4_DATE_Y, "text": TODAY, "font_name": STD_FONT})

    sigs = []
    if include_admin_sigs:
        sigs.append((adm.get("sponsor_sig", ""), 241.2, SN_S3_SIG_Y, 130, 28))
    sigs.append((d.get("student_sig", ""), 356.2, SN_S4_SIG_Y, 80, 28))
    return fields, sigs, underlines

def build_medical(d, adm, include_admin_sigs=True):
    fields = []
    underlines = []
    name_fs = 11
    date_fs = 11
    MD_OFF_NAME_Y = text_in_gap(425.5, 430.6, name_fs)
    MD_OFF_SIG_Y = sig_in_gap(425.5, 430.6, 28)
    MD_COMM_DATE_Y = text_below_line(543.7, date_fs, 6)
    MD_COMM_SIG_Y = sig_in_gap(543.1, 534.7, 28)

    fields.extend([
        {"font_size": 10, "x": 190.0, "y": cy(136.82, 152.30), "text": d.get("student_name", ""), "font_name": FONT},
        {"font_size": 10, "x": 190.0, "y": cy(152.30, 167.78), "text": d.get("national_id", ""), "font_name": FONT},
        {"font_size": 10, "x": 404.0, "y": cy(152.30, 167.78), "text": d.get("university", ""), "font_name": FONT},
        {"font_size": 10, "x": 190.0, "y": cy(167.78, 183.26), "text": d.get("kcse_index", ""), "font_name": FONT},
        {"font_size": 10, "x": 404.0, "y": cy(167.78, 183.26), "text": d.get("admission_number", ""), "font_name": FONT},
        {"font_size": 10, "x": 190.0, "y": cy(183.26, 198.86), "text": d.get("telephone", ""), "font_name": FONT},
        {"font_size": 10, "x": 404.0, "y": cy(183.26, 198.86), "text": d.get("email", ""), "font_name": FONT},
    ])
    fields.extend([
        {"font_size": 10, "x": 257.0, "y": cy(240.74, 256.13), "text": adm.get("officer_name", ""), "font_name": FONT},
        {"font_size": 10, "x": 257.0, "y": cy(256.13, 271.61), "text": adm.get("hospital_name", ""), "font_name": FONT},
        {"font_size": 10, "x": 257.0, "y": cy(271.61, 287.09), "text": adm.get("designation", ""), "font_name": FONT},
        {"font_size": 10, "x": 257.0, "y": cy(287.09, 302.57), "text": adm.get("reg_number", ""), "font_name": FONT},
    ])
    fields.append({"font_size": name_fs, "x": 103.0, "y": MD_OFF_NAME_Y, "text": adm.get("officer_name", ""), "font_name": FONT})
    fields.append({"font_size": date_fs, "x": 430.0, "y": MD_COMM_DATE_Y, "text": d.get("comm_date", TODAY), "font_name": STD_FONT})

    sigs = []
    if include_admin_sigs:
        sigs.append((adm.get("officer_sig", ""), 259.2, MD_OFF_SIG_Y, 130, 28))
        sigs.append((adm.get("commissioner_sig", ""), 356.2, MD_COMM_SIG_Y, 80, 28))
    return fields, sigs, underlines

def build_single_parent(d, adm, include_admin_sigs=True):
    rel = d.get("relationship", "")
    mar = d.get("marital_status", "")
    fields = []
    underlines = []
    name_fs = 11
    date_fs = 11
    SP_PAR_NAME_Y = text_in_gap(540.9, 547.0, name_fs)
    SP_PAR_DATE_Y = SP_PAR_NAME_Y
    SP_PAR_SIG_Y = sig_in_gap(540.9, 547.0, 28)
    SP_COMM_DATE_Y = text_below_line(660.2, date_fs, 6)
    SP_COMM_SIG_Y = sig_in_gap(659.6, 651.3, 28)

    fields.extend([
        {"font_size": 10, "x": 190.0, "y": cy(151.22, 166.70), "text": d.get("student_name", ""), "font_name": FONT},
        {"font_size": 10, "x": 190.0, "y": cy(166.70, 182.18), "text": d.get("national_id", ""), "font_name": FONT},
        {"font_size": 10, "x": 404.0, "y": cy(166.70, 182.18), "text": d.get("university", ""), "font_name": FONT},
        {"font_size": 10, "x": 190.0, "y": cy(182.18, 197.66), "text": d.get("kcse_index", ""), "font_name": FONT},
        {"font_size": 10, "x": 404.0, "y": cy(182.18, 197.66), "text": d.get("admission_number", ""), "font_name": FONT},
        {"font_size": 10, "x": 190.0, "y": cy(197.66, 213.26), "text": d.get("telephone", ""), "font_name": FONT},
        {"font_size": 10, "x": 404.0, "y": cy(197.66, 213.26), "text": d.get("email", ""), "font_name": FONT},
    ])
    fields.extend([
        {"font_size": 10, "x": 257.0, "y": cy(255.05, 270.53), "text": d.get("parent_name", ""), "font_name": FONT},
        {"font_size": 10, "x": 257.0, "y": cy(270.53, 286.01), "text": d.get("parent_id", ""), "font_name": FONT},
        {"font_size": 10, "x": 257.0, "y": cy(286.01, 301.49), "text": d.get("parent_telephone", ""), "font_name": FONT},
    ])
    SP_REL_CHECK = {"Mother": 292.97, "Father": 367.03}
    if rel in SP_REL_CHECK:
        fields.append(Tick(SP_REL_CHECK[rel], 315.71))
    SP_MAR_CHECK = {"Single": 294.05, "Separated": 367.75, "Divorce": 431.35}
    if mar in SP_MAR_CHECK:
        fields.append(Tick(SP_MAR_CHECK[mar], 331.31))
    fields.append({"font_size": name_fs, "x": 85.0, "y": SP_PAR_NAME_Y, "text": d.get("parent_name", ""), "font_name": FONT})
    fields.append({"font_size": date_fs, "x": 470.0, "y": SP_PAR_DATE_Y, "text": d.get("parent_date", TODAY), "font_name": STD_FONT})
    fields.append({"font_size": date_fs, "x": 430.0, "y": SP_COMM_DATE_Y, "text": d.get("comm_date", TODAY), "font_name": STD_FONT})

    sigs = []
    sigs.append((d.get("parent_sig", ""), 240.8, SP_PAR_SIG_Y, 130, 28))
    if include_admin_sigs:
        sigs.append((adm.get("commissioner_sig", ""), 355.9, SP_COMM_SIG_Y, 80, 28))
    return fields, sigs, underlines

# ============================================================================
# BUILDERS DICTIONARY
# ============================================================================

BUILDERS = {
    "medical": (build_medical, "Medical_Form.pdf", "Medical_Form_Filled.pdf"),
    "sponsorship": (build_sponsorship, "Sponsorship_Letter.pdf", "Sponsorship_Letter_Filled.pdf"),
    "single_parent": (build_single_parent, "Single_Parent_Self_Certification_2024.pdf", "Single_Parent_Certification_Filled.pdf"),
}

# ============================================================================
# DEFAULT ADMIN SETTINGS
# ============================================================================

DEFAULT_ADMIN_SETTINGS = {
    "medical_officer": {
        "officer_name": "Dr. Jane Mwangi, MBChB, MMed",
        "hospital_name": "Kenyatta National Hospital",
        "designation": "Senior Medical Officer",
        "reg_number": "MED-2024-001",
        "signature": ""
    },
    "sponsor": {
        "sponsor_name": "Kenya Education Fund (KEF)",
        "sponsor_email": "sponsors@kenyaeducationfund.org",
        "sponsor_telephone": "+254 700 000 000",
        "signature": ""
    },
    "commissioner": {
        "name": "Hon. Justice John Kamau, EBS",
        "signature": ""
    }
}

# ============================================================================
# EMAIL TEMPLATE WITH DIRECT CLOUDINARY LINKS
# ============================================================================

def build_payment_confirmation_email_with_buttons(student_name, bundle_id, transaction_code, form_types, total_amount, pdf_urls):
    """Build HTML email with direct Cloudinary download links"""
    
    doc_buttons = ""
    for ft in form_types:
        display_name = FORM_TYPE_DISPLAY.get(ft, ft)
        cloudinary_url = pdf_urls.get(ft, "#")
        
        doc_buttons += f'''
        <div style="margin: 15px 0; padding: 15px; background: #f8f9fa; border-radius: 10px; border-left: 4px solid #10B981;">
            <div style="font-weight: bold; font-size: 16px; color: #333; margin-bottom: 10px;">📄 {display_name}</div>
            <a href="{cloudinary_url}" 
               style="display: inline-block; padding: 14px 35px; background: #10B981; color: white; 
                      text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 16px;
                      border: none; cursor: pointer; box-shadow: 0 2px 5px rgba(0,0,0,0.2);">
                ⬇️ Download {display_name}
            </a>
            <span style="display: inline-block; margin-left: 15px; color: #666; font-size: 14px;">
                (PDF, click to save to your device)
            </span>
        </div>
        '''

    if len(form_types) > 1:
        all_download_url = f"/download_all/{bundle_id}"
        download_all = f'''
        <div style="margin: 20px 0; padding: 20px; background: #ecfdf5; border-radius: 10px; text-align: center; border: 2px dashed #10B981;">
            <a href="{all_download_url}" 
               style="display: inline-block; padding: 16px 45px; background: #059669; color: white; 
                      text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 18px;
                      border: none; cursor: pointer; box-shadow: 0 3px 8px rgba(0,0,0,0.2);">
                📦 Download All Documents (ZIP)
            </a>
            <br>
            <span style="display: block; margin-top: 10px; color: #666; font-size: 14px;">
                Click to download all your documents in one zip file
            </span>
        </div>
        '''
    else:
        download_all = ""

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; background: #f4f6f9; margin: 0; padding: 0; }}
        .container {{ max-width: 650px; margin: 20px auto; background: white; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #10B981, #059669); color: white; padding: 30px; text-align: center; }}
        .header h1 {{ margin: 0; font-size: 28px; }}
        .header p {{ margin: 10px 0 0 0; font-size: 16px; opacity: 0.9; }}
        .content {{ padding: 35px; }}
        .content h3 {{ color: #10B981; font-size: 22px; margin-top: 0; }}
        .details {{ background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0; }}
        .details p {{ margin: 8px 0; font-size: 15px; }}
        .divider {{ border-top: 2px solid #e5e7eb; margin: 25px 0; }}
        .doc-section {{ margin: 20px 0; }}
        .doc-section h4 {{ font-size: 18px; color: #333; margin-bottom: 15px; }}
        .button-primary {{ display: inline-block; padding: 14px 35px; background: #10B981; color: white; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 16px; border: none; cursor: pointer; box-shadow: 0 2px 5px rgba(0,0,0,0.2); }}
        .button-primary:hover {{ transform: scale(1.02); background: #059669; }}
        .footer {{ padding: 20px; text-align: center; color: #6B7280; font-size: 13px; border-top: 1px solid #e5e7eb; }}
        @media only screen and (max-width: 480px) {{
            .content {{ padding: 20px; }}
            .header {{ padding: 20px; }}
            .header h1 {{ font-size: 22px; }}
            .button-primary {{ display: block; text-align: center; margin: 10px 0; width: 100%; }}
        }}
    </style>
    </head>
    <body>
    <div class="container">
        <div class="header">
            <h1>✅ Payment Confirmed!</h1>
            <p>Your Supporting Documents Are Ready</p>
        </div>
        
        <div class="content">
            <h3>Dear {student_name},</h3>
            <p>We are pleased to confirm that your payment has been successfully processed. Your documents are ready for download.</p>
            
            <div class="details">
                <p><strong>🔑 Bundle ID:</strong> <span style="background: #e5e7eb; padding: 2px 10px; border-radius: 4px; font-family: monospace;">{bundle_id}</span></p>
                <p><strong>📝 Transaction Code:</strong> <span style="background: #e5e7eb; padding: 2px 10px; border-radius: 4px; font-family: monospace;">{transaction_code}</span></p>
                <p><strong>📅 Date:</strong> {datetime.now().strftime('%d %B %Y at %H:%M')}</p>
                <p><strong>💰 Total Paid:</strong> <span style="color: #10B981; font-weight: bold; font-size: 18px;">KES {total_amount}</span></p>
            </div>
            
            <div class="divider"></div>
            
            <div class="doc-section">
                <h4>📄 Your Documents</h4>
                <p style="color: #6B7280; font-size: 14px; margin-bottom: 20px;">
                    Click the buttons below to download each document. All files are in PDF format.
                </p>
                {doc_buttons}
            </div>
            
            {download_all}
            
            <div style="margin-top: 25px; padding: 15px; background: #fef3c7; border-radius: 8px; border-left: 4px solid #F59E0B;">
                <p style="margin: 0; font-size: 14px; color: #92400E;">
                    💡 <strong>Tip:</strong> If the document opens in your browser, look for the save/download icon 
                    (usually a floppy disk or downward arrow) to save it to your device.
                </p>
            </div>
            
            <p style="margin-top: 30px; font-size: 15px;">
                Thank you for using our service.<br>
                <strong style="color: #10B981;">Supporting Documents Team</strong>
            </p>
        </div>
        
        <div class="footer">
            <p>This is an automated message. Please do not reply to this email.</p>
            <p>&copy; 2026 Supporting Documents. All rights reserved.</p>
        </div>
    </div>
    </body>
    </html>
    """

# ============================================================================
# APPLICATION FACTORY
# ============================================================================

def create_app() -> Flask:
    cfg = Config.load()
    raw_logger = setup_logging(cfg)
    log = LogContext(raw_logger)

    db_manager = DatabaseManager(cfg, log)
    mongo_db = db_manager.db
    use_mongo = db_manager.is_connected

    cache = CacheManager(cfg, log)
    mpesa = MpesaClient(cfg, cache, log)
    limiter = RateLimiter(cfg, cache, log)
    executor = TaskExecutor(cfg.max_background_workers, cfg.task_queue_max_size, log)
    email_service = EmailService(cfg, log)

    memory_storage = {}
    memory_referral_codes = {}

    app = Flask(__name__)
    app.config["SECRET_KEY"] = cfg.secret_key
    app.config["SESSION_TYPE"] = cfg.session_type
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(seconds=cfg.session_lifetime)
    app.config["SESSION_COOKIE_SECURE"] = cfg.session_cookie_secure
    app.config["SESSION_COOKIE_HTTPONLY"] = cfg.session_cookie_httponly
    app.config["SESSION_COOKIE_SAMESITE"] = cfg.session_cookie_samesite
    app.config["SESSION_USE_SIGNER"] = True
    app.config["SESSION_KEY_PREFIX"] = "session:"

    if cfg.session_type == "mongodb" and use_mongo:
        app.config["SESSION_MONGODB"] = db_manager._client
        app.config["SESSION_MONGODB_DB"] = cfg.mongo_db_name
    elif cfg.session_type == "redis" and cache._redis:
        app.config["SESSION_REDIS"] = cache._redis
    else:
        app.config["SESSION_TYPE"] = "filesystem"
        app.config["SESSION_FILE_DIR"] = os.path.join(BASE_DIR, "flask_session")
        os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)

    Session(app)

    # ============================================================
    # MIDDLEWARE
    # ============================================================

    @app.before_request
    def before_request():
        g.request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
        g.start_time = time.time()

    @app.after_request
    def after_request(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Request-ID"] = getattr(g, "request_id", "unknown")
        if cfg.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if hasattr(g, "start_time"):
            elapsed = (time.time() - g.start_time) * 1000
            log.debug(f"{request.method} {request.path} {response.status_code} {elapsed:.1f}ms")
        return response

    # ============================================================
    # SESSION HELPERS
    # ============================================================

    def save_session_state(selected_types=None, form_data_map=None, referral_code=None):
        if selected_types is not None:
            session['selected_types'] = selected_types
        if form_data_map is not None:
            existing = session.get('form_data_map', {})
            existing.update(form_data_map)
            session['form_data_map'] = existing
        if referral_code is not None:
            session['referral_code'] = referral_code
        session.modified = True

    def load_session_state():
        return {
            'selected_types': session.get('selected_types', []),
            'form_data_map': session.get('form_data_map', {}),
            'referral_code': session.get('referral_code', '')
        }

    # ============================================================
    # ADMIN DECORATOR
    # ============================================================

    def admin_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("admin_logged_in"):
                flash("Please log in to access the admin area.", "warning")
                return redirect(url_for("admin_login"))
            g.user_email = "admin"
            return f(*args, **kwargs)
        return decorated

    # ============================================================
    # DATABASE HELPERS
    # ============================================================

    def get_admin_settings():
        if use_mongo:
            doc = mongo_db.settings.find_one({"_id": "admin_settings"})
            if doc and "settings" in doc:
                return doc["settings"]
            mongo_db.settings.update_one({"_id": "admin_settings"}, {"$set": {"settings": DEFAULT_ADMIN_SETTINGS}}, upsert=True)
            return DEFAULT_ADMIN_SETTINGS
        if "admin_settings" not in memory_storage:
            memory_storage["admin_settings"] = copy.deepcopy(DEFAULT_ADMIN_SETTINGS)
        return memory_storage["admin_settings"]

    def save_admin_settings(settings):
        if use_mongo:
            mongo_db.settings.update_one({"_id": "admin_settings"}, {"$set": {"settings": settings}}, upsert=True)
        else:
            memory_storage["admin_settings"] = settings

    def get_user_document_by_bundle_id(bundle_id):
        if use_mongo:
            return mongo_db.documents.find_one({"bundle_id": bundle_id})
        return memory_storage.get(bundle_id)

    def get_user_document_by_email(email):
        if not email:
            return None
        email_lower = email.lower().strip()
        if use_mongo:
            docs = list(mongo_db.documents.find(
                {"student_email": {"$regex": f"^{re.escape(email_lower)}$", "$options": "i"}}
            ).sort("created_at", -1).limit(10))
            for doc in docs:
                if doc.get("payment_status") == PaymentStatus.SUCCESS.value:
                    return doc
            return docs[0] if docs else None
        matching = [v for v in memory_storage.values() if isinstance(v, dict) and v.get("student_details", {}).get("email", "").lower() == email_lower]
        matching.sort(key=lambda x: x.get("created_at", datetime.min), reverse=True)
        for doc in matching:
            if doc.get("payment_status") == PaymentStatus.SUCCESS.value:
                return doc
        return matching[0] if matching else None

    def get_all_user_documents():
        if use_mongo:
            return list(mongo_db.documents.find().sort("created_at", -1))
        return [v for v in memory_storage.values() if isinstance(v, dict) and "bundle_id" in v]

    def get_all_referral_codes():
        if use_mongo:
            return list(mongo_db.referral_codes.find({}, {"_id": 0}))
        return [{"code": k, **v} for k, v in memory_referral_codes.items()]

    def create_referral_code(code, marketer_name, discount=50):
        code = code.upper().strip()
        if not code or not marketer_name:
            return False
        if use_mongo:
            try:
                mongo_db.referral_codes.insert_one({
                    "code": code, "marketer_name": marketer_name, "active": True,
                    "discount_per_doc": discount, "created_at": datetime.now()
                })
                return True
            except DuplicateKeyError:
                return False
            except Exception as e:
                log.error(f"Failed to create referral code: {e}")
                return False
        else:
            if code in memory_referral_codes:
                return False
            memory_referral_codes[code] = {"marketer_name": marketer_name, "active": True, "discount_per_doc": discount}
            return True

    def validate_referral_code(code):
        code = code.upper().strip() if code else ""
        if not code:
            return False, 0, ""
        if use_mongo:
            doc = mongo_db.referral_codes.find_one({"code": code, "active": True})
            if doc:
                return True, doc.get("discount_per_doc", 50), doc.get("marketer_name", "")
            return False, 0, ""
        else:
            data = memory_referral_codes.get(code)
            if data and data.get("active", False):
                return True, data.get("discount_per_doc", 50), data.get("marketer_name", "")
            return False, 0, ""

    def _build_admin_sigs():
        s = get_admin_settings()
        mo = s.get("medical_officer", {})
        sp = s.get("sponsor", {})
        co = s.get("commissioner", {})
        return {
            "officer_name": mo.get("officer_name", ""), "hospital_name": mo.get("hospital_name", ""),
            "designation": mo.get("designation", ""), "reg_number": mo.get("reg_number", ""), "officer_sig": mo.get("signature", ""),
            "sponsor_name": sp.get("sponsor_name", ""), "sponsor_email": sp.get("sponsor_email", ""),
            "sponsor_telephone": sp.get("sponsor_telephone", ""), "sponsor_sig": sp.get("signature", ""),
            "commissioner_name": co.get("name", ""), "commissioner_sig": co.get("signature", ""),
        }

    def _make_pdf(form_type, form_data, stamped=False):
        build_fn, src_pdf, _ = BUILDERS[form_type]
        pdf_path = os.path.join(BASE_DIR, src_pdf)
        adm = _build_admin_sigs()
        fields, sigs, underlines = build_fn(form_data, adm, include_admin_sigs=True)
        stamps = get_stamps_for_form_type(form_type, cfg.stamp_scale, log) if stamped else None
        return render_overlay(fields, sigs, underlines, pdf_path, stamps=stamps, stamps_dir=STAMPS_DIR, logger=log)

    # ============================================================
    # CORE FUNCTIONS
    # ============================================================

    def _simulate_payment_confirmation(bundle_id):
        try:
            tx_code = f"TEST{datetime.now().strftime('%Y%m%d%H%M%S')}"
            if use_mongo:
                result = mongo_db.documents.update_one(
                    {"bundle_id": bundle_id, "payment_status": PaymentStatus.PENDING.value},
                    {"$set": {
                        "payment_status": PaymentStatus.SUCCESS.value,
                        "transaction_code": tx_code,
                        "paid_at": datetime.now(),
                        "document_status": DocumentStatus.PAYMENT_CONFIRMED.value
                    }}
                )
                if result.modified_count > 0:
                    log.info(f"[TEST] Simulated payment success for {bundle_id}")
                    record = mongo_db.documents.find_one({"bundle_id": bundle_id})
                    if record:
                        executor.submit(
                            _generate_multiple_pdfs_and_send_email,
                            bundle_id,
                            record.get("form_types", []),
                            record.get("form_data_map", {}),
                            record.get("student_email", ""),
                            record.get("student_name", "Student"),
                            tx_code
                        )
                        return True
            return False
        except Exception as e:
            log.error(f"[TEST] Simulation error: {e}")
            return False

    def _active_poll_payment(checkout_request_id, bundle_id, form_types, form_data_map,
                             student_email, student_name, max_attempts=10, interval=1.0):
        time.sleep(3.0)
        for attempt in range(max_attempts):
            record = get_user_document_by_bundle_id(bundle_id)
            if not record:
                log.warning(f"[POLL] Document {bundle_id} not found")
                return
            current_status = record.get("payment_status")
            if current_status == PaymentStatus.SUCCESS.value:
                log.info(f"[POLL] {bundle_id} already confirmed by callback")
                return
            if current_status == PaymentStatus.FAILED.value:
                log.info(f"[POLL] {bundle_id} already marked as failed by callback")
                return
            try:
                success, result = mpesa.query_transaction(checkout_request_id)
                if success:
                    tx_code = result.get("mpesa_receipt_number", checkout_request_id)
                    record_check = get_user_document_by_bundle_id(bundle_id)
                    if record_check and record_check.get("payment_status") == PaymentStatus.SUCCESS.value:
                        log.info(f"[POLL] {bundle_id} was updated by callback while querying")
                        return
                    if use_mongo:
                        update_result = mongo_db.documents.update_one(
                            {"bundle_id": bundle_id, "payment_status": PaymentStatus.PENDING.value},
                            {"$set": {
                                "payment_status": PaymentStatus.SUCCESS.value,
                                "transaction_code": tx_code,
                                "paid_at": datetime.now(),
                                "document_status": DocumentStatus.PAYMENT_CONFIRMED.value
                            }}
                        )
                        if update_result.modified_count == 0:
                            log.info(f"[POLL] {bundle_id} was already updated to success")
                            continue
                    elif bundle_id in memory_storage:
                        if memory_storage[bundle_id].get("payment_status") == PaymentStatus.PENDING.value:
                            memory_storage[bundle_id]["payment_status"] = PaymentStatus.SUCCESS.value
                            memory_storage[bundle_id]["transaction_code"] = tx_code
                            memory_storage[bundle_id]["paid_at"] = datetime.now()
                    record_check = get_user_document_by_bundle_id(bundle_id)
                    if record_check and record_check.get("payment_status") == PaymentStatus.SUCCESS.value:
                        log.info(f"[POLL] Payment confirmed via query for {bundle_id}")
                        executor.submit(
                            _generate_multiple_pdfs_and_send_email,
                            bundle_id, form_types, form_data_map,
                            student_email, student_name, tx_code
                        )
                        return
                elif result.get("status") == "pending":
                    time.sleep(interval)
                    continue
                elif result.get("status") == "failed":
                    record_check = get_user_document_by_bundle_id(bundle_id)
                    if record_check and record_check.get("payment_status") == PaymentStatus.SUCCESS.value:
                        log.info(f"[POLL] {bundle_id} was updated by callback, ignoring query failure")
                        return
                    reason = result.get("error", "Transaction failed")
                    if use_mongo:
                        mongo_db.documents.update_one(
                            {"bundle_id": bundle_id, "payment_status": PaymentStatus.PENDING.value},
                            {"$set": {
                                "payment_status": PaymentStatus.FAILED.value,
                                "payment_failure_reason": reason
                            }}
                        )
                    log.warning(f"[POLL] Payment failed for {bundle_id}: {reason}")
                    return
            except Exception as e:
                log.error(f"[POLL] Query exception for {bundle_id}: {e}")
                time.sleep(interval)
                continue
            time.sleep(interval)
        log.info(f"[POLL] Timeout for {bundle_id}. Check callback status.")
        final_record = get_user_document_by_bundle_id(bundle_id)
        if final_record and final_record.get("payment_status") == PaymentStatus.PENDING.value:
            log.info(f"[POLL] {bundle_id} still pending after max attempts. Waiting for callback.")

    def _generate_multiple_pdfs_and_send_email(bundle_id, form_types, form_data_map, student_email, student_name, tx_code):
        """Generate PDFs, upload to Cloudinary, send email with direct Cloudinary links"""
        try:
            pdf_urls = {}
            total_amount = sum(DOCUMENT_PRICES.get(ft, cfg.payment_amount_per_doc) for ft in form_types)
            
            if use_mongo:
                mongo_db.documents.update_one(
                    {"bundle_id": bundle_id},
                    {"$set": {"document_status": DocumentStatus.PDF_GENERATED.value}}
                )
            
            for ft in form_types:
                pdf_bytes = _make_pdf(ft, form_data_map.get(ft, {}), stamped=True)
                url = storage.upload_pdf(pdf_bytes, bundle_id, ft)
                if url:
                    pdf_urls[ft] = url
                    log.info(f"[CLOUDINARY] Uploaded {ft} for {bundle_id}: {url}")
                else:
                    log.warning(f"[CLOUDINARY] Failed to upload {ft} for {bundle_id}")
                    try:
                        result = cloudinary.uploader.upload(
                            io.BytesIO(pdf_bytes),
                            resource_type="raw",
                            folder=f"supporting_docs/{bundle_id}",
                            public_id=ft,
                            overwrite=True,
                            access_mode="public",
                            type="upload",
                            format="pdf"
                        )
                        url = result.get("secure_url")
                        if url:
                            parts = url.split("/")
                            for i, part in enumerate(parts):
                                if part.startswith("v") and part[1:].isdigit():
                                    parts.pop(i)
                                    break
                            pdf_urls[ft] = "/".join(parts)
                            log.info(f"[CLOUDINARY] Uploaded {ft} on retry")
                    except Exception as e2:
                        log.error(f"[CLOUDINARY] Retry failed: {e2}")
            
            if use_mongo and pdf_urls:
                result = mongo_db.documents.update_one(
                    {"bundle_id": bundle_id},
                    {"$set": {"pdf_urls": pdf_urls, "document_status": DocumentStatus.EMAIL_SENT.value}}
                )
                if result.modified_count > 0:
                    log.info(f"[DB] Updated pdf_urls for {bundle_id}: {list(pdf_urls.keys())}")
                else:
                    log.warning(f"[DB] Failed to update pdf_urls for {bundle_id}")
            elif bundle_id in memory_storage:
                memory_storage[bundle_id]["pdf_urls"] = pdf_urls
            
            if student_email and cfg.brevo_api_key and pdf_urls:
                doc_names = [FORM_TYPE_DISPLAY.get(ft, ft) for ft in form_types]
                subject = f"Your Documents ({', '.join(doc_names)}) - {bundle_id}"
                html = build_payment_confirmation_email_with_buttons(
                    student_name, bundle_id, tx_code, form_types, total_amount, pdf_urls
                )
                
                success, message = email_service.send(
                    to_email=student_email,
                    to_name=student_name,
                    subject=subject,
                    html=html,
                    attachments=[],
                    cc=["kuccpscourses@gmail.com"]
                )
                
                if success:
                    log.info(f"[EMAIL] Sent to {student_email} (CC: kuccpscourses@gmail.com)")
                    if use_mongo:
                        mongo_db.documents.update_one(
                            {"bundle_id": bundle_id},
                            {"$set": {
                                "email_sent": True,
                                "email_sent_at": datetime.now(),
                                "document_status": DocumentStatus.COMPLETED.value
                            }}
                        )
                else:
                    log.warning(f"[EMAIL] Failed: {message}")
            else:
                log.warning(f"[EMAIL] Skipped for {bundle_id}")
                
        except Exception as e:
            log.exception(f"[BACKGROUND] Task failed for {bundle_id}: {e}")

    # ============================================================
    # ROUTES - PUBLIC ROUTES
    # ============================================================

    @app.route("/")
    def index():
        state = load_session_state()
        return render_template("index.html",
                               selected_types=state['selected_types'],
                               form_data_map=state['form_data_map'],
                               referral_code=state['referral_code'],
                               mpesa_configured=bool(cfg.mpesa_consumer_key and cfg.mpesa_passkey))

    @app.route("/api/session/save", methods=["POST"])
    def save_session():
        data = request.json or {}
        selected_types = data.get("selected_types")
        form_data = data.get("form_data_map")
        referral = data.get("referral_code")
        save_session_state(selected_types, form_data, referral)
        return jsonify({"success": True})

    @app.route("/api/session/load", methods=["GET"])
    def load_session():
        return jsonify(load_session_state())

    @app.route("/preview/<ft>", methods=["POST"])
    @rate_limit(limiter)
    def preview(ft):
        if ft not in BUILDERS:
            return jsonify(error="Unknown form type"), 400
        d = request.json or {}
        try:
            pdf = _make_pdf(ft, d, stamped=False)
        except Exception as e:
            log.error(f"Preview generation error: {e}")
            return jsonify(error="Preview generation failed"), 500
        response = Response(pdf, mimetype="application/pdf")
        response.headers["Content-Disposition"] = "inline; filename=\"preview.pdf\""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.route("/preview_stamped/<ft>", methods=["POST"])
    @rate_limit(limiter)
    def preview_stamped(ft):
        if ft not in BUILDERS:
            return jsonify(error="Unknown form type"), 400
        d = request.json or {}
        try:
            pdf = _make_pdf(ft, d, stamped=True)
        except Exception as e:
            log.error(f"Stamped preview error: {e}")
            return jsonify(error="Stamped preview generation failed"), 500
        response = Response(pdf, mimetype="application/pdf")
        response.headers["Content-Disposition"] = "inline; filename=\"stamped_preview.pdf\""
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.route("/initiate_payment", methods=["POST"])
    @rate_limit(limiter)
    def initiate_payment():
        data = request.json or {}
        form_types = data.get("form_types", [])
        form_data_map = data.get("form_data_map", {})
        student_details = data.get("student_details", {})
        phone_number = data.get("phone_number", "").strip()
        referral_code = data.get("referral_code", "").strip()

        if not form_types:
            return jsonify({"error": "No documents selected"}), 400
        if not phone_number:
            return jsonify({"error": "Phone number is required"}), 400

        formatted_phone = format_phone(phone_number)
        if not validate_phone(formatted_phone):
            return jsonify({"error": f"Invalid phone number: {phone_number}"}), 400

        student_email = student_details.get("email", "").strip()
        if not student_email:
            return jsonify({"error": "Student email is required"}), 400

        valid_code = False
        discount_per_doc = 0
        marketer = ""
        if referral_code:
            valid_code, discount_per_doc, marketer = validate_referral_code(referral_code)

        total_amount = 0
        for ft in form_types:
            price = DOCUMENT_PRICES.get(ft, cfg.payment_amount_per_doc)
            discounted = max(0, price - discount_per_doc) if valid_code else price
            total_amount += discounted
        total_amount = max(1, total_amount)

        bundle_id = str(uuid.uuid4())[:8]

        kcse_index = student_details.get("kcse_index", "").strip()
        if kcse_index:
            clean_kcse = re.sub(r"[^a-zA-Z0-9]", "", kcse_index)
            account_ref = clean_kcse[:12]
        else:
            account_ref = bundle_id[:12]

        try:
            record = {
                "bundle_id": bundle_id,
                "form_types": form_types,
                "student_details": student_details,
                "student_name": student_details.get("student_name", ""),
                "student_email": student_email,
                "form_data_map": form_data_map,
                "payment_status": PaymentStatus.PENDING.value,
                "document_status": DocumentStatus.PAYMENT_PENDING.value,
                "created_at": datetime.now(),
                "transaction_code": None,
                "checkout_request_id": None,
                "phone_number": formatted_phone,
                "total_amount": total_amount,
                "referral_code": referral_code if valid_code else "",
                "discount_applied": discount_per_doc if valid_code else 0,
                "marketer_name": marketer if valid_code else "",
                "account_reference": account_ref,
                "pdf_urls": {},
                "email_sent": False,
                "paid_at": None
            }
            if use_mongo:
                existing = mongo_db.documents.find_one({"bundle_id": bundle_id})
                if existing:
                    return jsonify({"error": "Duplicate request. Please try again."}), 409
                mongo_db.documents.insert_one(record)
            else:
                memory_storage[bundle_id] = record
        except Exception as e:
            log.error(f"Failed to create document record: {e}")
            return jsonify({"error": "Database error"}), 500

        if TEST_MODE:
            log.info(f"[TEST] Auto-confirming payment for {bundle_id}")
            if use_mongo:
                mongo_db.documents.update_one(
                    {"bundle_id": bundle_id},
                    {"$set": {
                        "payment_status": PaymentStatus.SUCCESS.value,
                        "transaction_code": f"TEST{datetime.now().strftime('%Y%m%d%H%M%S')}",
                        "paid_at": datetime.now(),
                        "document_status": DocumentStatus.PAYMENT_CONFIRMED.value
                    }}
                )
            else:
                memory_storage[bundle_id]["payment_status"] = PaymentStatus.SUCCESS.value
                memory_storage[bundle_id]["transaction_code"] = f"TEST{datetime.now().strftime('%Y%m%d%H%M%S')}"
                memory_storage[bundle_id]["paid_at"] = datetime.now()
            
            executor.submit(
                _generate_multiple_pdfs_and_send_email,
                bundle_id,
                form_types,
                form_data_map,
                student_email,
                student_details.get("student_name", "Student"),
                f"TEST{datetime.now().strftime('%Y%m%d%H%M%S')}"
            )
            
            return jsonify({
                "success": True,
                "checkout_request_id": f"TEST_{bundle_id}",
                "merchant_request_id": f"MERCHANT_{bundle_id}",
                "customer_message": "✅ TEST MODE: Payment auto-confirmed!",
                "bundle_id": bundle_id,
                "test_mode": True,
                "redirect_url": "/payment_status?bundle_id=" + bundle_id
            })

        success, result = mpesa.init_stk_push(formatted_phone, account_ref, f"{len(form_types)} Docs", total_amount)
        if success:
            checkout_request_id = result["checkout_request_id"]
            if use_mongo:
                mongo_db.documents.update_one(
                    {"bundle_id": bundle_id},
                    {"$set": {"checkout_request_id": checkout_request_id}}
                )
            elif bundle_id in memory_storage:
                memory_storage[bundle_id]["checkout_request_id"] = checkout_request_id
            executor.submit(
                _active_poll_payment,
                checkout_request_id,
                bundle_id,
                form_types,
                form_data_map,
                student_email,
                student_details.get("student_name", "Student"),
                max_attempts=10,
                interval=1.0
            )
            return jsonify({
                "success": True,
                "checkout_request_id": checkout_request_id,
                "merchant_request_id": result["merchant_request_id"],
                "customer_message": result["customer_message"],
                "bundle_id": bundle_id,
                "elapsed_seconds": result.get("elapsed_seconds"),
                "redirect_url": "/payment_status?bundle_id=" + bundle_id
            })
        else:
            if use_mongo:
                mongo_db.documents.delete_one({"bundle_id": bundle_id})
            elif bundle_id in memory_storage:
                del memory_storage[bundle_id]
            return jsonify({"error": result.get("error", "Payment initiation failed")}), 400

    @app.route("/test_pay/<bundle_id>", methods=["GET"])
    def test_pay(bundle_id):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Not found"}), 404
        if record.get("payment_status") == PaymentStatus.SUCCESS.value:
            return jsonify({"status": "already_done", "bundle_id": bundle_id})
        success = _simulate_payment_confirmation(bundle_id)
        if success:
            return jsonify({"status": "success", "bundle_id": bundle_id})
        return jsonify({"status": "failed", "bundle_id": bundle_id}), 500

    @app.route("/mpesa_callback", methods=["POST"])
    def mpesa_callback():
        callback_data = request.get_json(force=True, silent=True) or {}
        checkout_request_id, result_code, result_desc, metadata = mpesa.parse_callback(callback_data)
        if not checkout_request_id:
            log.warning("[M-PESA] Invalid callback data")
            return jsonify({"status": "error", "message": "Invalid callback data"}), 400
        record = None
        if use_mongo:
            record = mongo_db.documents.find_one({"checkout_request_id": checkout_request_id})
        else:
            for rec in memory_storage.values():
                if isinstance(rec, dict) and rec.get("checkout_request_id") == checkout_request_id:
                    record = rec
                    break
        if not record:
            log.warning(f"[M-PESA] Unknown checkout: {checkout_request_id}")
            return jsonify({"status": "ok"}), 200
        bundle_id = record.get("bundle_id", "")
        current_record = get_user_document_by_bundle_id(bundle_id)
        if current_record:
            current_status = current_record.get("payment_status")
            if current_status == PaymentStatus.SUCCESS.value:
                log.info(f"[M-PESA] {bundle_id} already marked as success, ignoring duplicate callback")
                return jsonify({"status": "already_success", "bundle_id": bundle_id}), 200
            if current_status == PaymentStatus.FAILED.value and result_code == 0:
                log.info(f"[M-PESA] Overriding failed status for {bundle_id} with success from callback")
        if result_code == 0:
            tx_code = metadata.get("MpesaReceiptNumber", checkout_request_id)
            student_email = record.get("student_email", "")
            student_name = record.get("student_name", "Student")
            if use_mongo:
                result = mongo_db.documents.update_one(
                    {"bundle_id": bundle_id, "payment_status": {"$ne": PaymentStatus.SUCCESS.value}},
                    {"$set": {
                        "payment_status": PaymentStatus.SUCCESS.value,
                        "transaction_code": tx_code,
                        "paid_at": datetime.now(),
                        "document_status": DocumentStatus.PAYMENT_CONFIRMED.value
                    }}
                )
                if result.modified_count == 0:
                    log.info(f"[M-PESA] {bundle_id} was already updated to success")
            elif bundle_id in memory_storage:
                if memory_storage[bundle_id].get("payment_status") != PaymentStatus.SUCCESS.value:
                    memory_storage[bundle_id]["payment_status"] = PaymentStatus.SUCCESS.value
                    memory_storage[bundle_id]["transaction_code"] = tx_code
                    memory_storage[bundle_id]["paid_at"] = datetime.now()
            executor.submit(
                _generate_multiple_pdfs_and_send_email,
                bundle_id,
                record.get("form_types", []),
                record.get("form_data_map", {}),
                student_email,
                student_name,
                tx_code
            )
            log.info(f"[M-PESA] Payment confirmed for {bundle_id}")
            return jsonify({"status": "success", "bundle_id": bundle_id}), 200
        else:
            log.warning(f"[M-PESA] Payment failed for {bundle_id}: {result_desc}")
            if use_mongo:
                mongo_db.documents.update_one(
                    {"bundle_id": bundle_id, "payment_status": PaymentStatus.PENDING.value},
                    {"$set": {
                        "payment_status": PaymentStatus.FAILED.value,
                        "payment_failure_reason": result_desc
                    }}
                )
            elif bundle_id in memory_storage:
                if memory_storage[bundle_id].get("payment_status") == PaymentStatus.PENDING.value:
                    memory_storage[bundle_id]["payment_status"] = PaymentStatus.FAILED.value
                    memory_storage[bundle_id]["payment_failure_reason"] = result_desc
            return jsonify({"status": "failed", "bundle_id": bundle_id}), 200

    # ============================================================
    # DOWNLOAD ROUTES
    # ============================================================

    @app.route("/download_pdf/<bundle_id>/<form_type>", methods=["GET"])
    def download_single_pdf(bundle_id, form_type):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Document not found"}), 404
        if record.get("payment_status") != PaymentStatus.SUCCESS.value:
            return jsonify({"error": "Payment not completed"}), 402
        
        pdf_urls = record.get("pdf_urls", {})
        if form_type in pdf_urls:
            try:
                response = requests.get(pdf_urls[form_type], timeout=30)
                if response.status_code == 200:
                    _, _, dl_name = BUILDERS.get(form_type, (None, None, "document.pdf"))
                    return send_file(
                        io.BytesIO(response.content),
                        mimetype="application/pdf",
                        as_attachment=True,
                        download_name=dl_name
                    )
            except Exception as e:
                log.error(f"Cloudinary error: {e}")
        
        form_data_map = record.get("form_data_map", {})
        if form_type in form_data_map:
            try:
                log.info(f"Regenerating PDF on the fly for {bundle_id}/{form_type}")
                pdf_bytes = _make_pdf(form_type, form_data_map[form_type], stamped=True)
                _, _, dl_name = BUILDERS.get(form_type, (None, None, "document.pdf"))
                url = storage.upload_pdf(pdf_bytes, bundle_id, form_type)
                if url and use_mongo:
                    mongo_db.documents.update_one(
                        {"bundle_id": bundle_id},
                        {"$set": {f"pdf_urls.{form_type}": url}}
                    )
                return send_file(
                    io.BytesIO(pdf_bytes),
                    mimetype="application/pdf",
                    as_attachment=True,
                    download_name=dl_name
                )
            except Exception as e:
                log.error(f"Error regenerating PDF: {e}")
        
        return jsonify({"error": "PDF not found"}), 404

    @app.route("/download_all/<bundle_id>", methods=["GET"])
    def download_all_pdfs(bundle_id):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Document not found"}), 404
        if record.get("payment_status") != PaymentStatus.SUCCESS.value:
            return jsonify({"error": "Payment not completed"}), 402
        
        pdf_urls = record.get("pdf_urls", {})
        if pdf_urls:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for ft, url in pdf_urls.items():
                    try:
                        response = requests.get(url, timeout=30)
                        if response.status_code == 200:
                            _, _, dl_name = BUILDERS.get(ft, (None, None, f"{ft}.pdf"))
                            zf.writestr(dl_name, response.content)
                    except Exception as e:
                        log.error(f"Error downloading {ft}: {e}")
            zip_buffer.seek(0)
            return send_file(
                zip_buffer,
                mimetype="application/zip",
                as_attachment=True,
                download_name=f"documents_{bundle_id}.zip"
            )
        
        return jsonify({"error": "PDFs not found"}), 404

    @app.route("/api/payment_status/<bundle_id>", methods=["GET"])
    def api_payment_status(bundle_id):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Document not found"}), 404
        status = record.get("payment_status", PaymentStatus.PENDING.value)
        pdf_urls = record.get("pdf_urls", {})
        return jsonify({
            "bundle_id": bundle_id,
            "status": status,
            "transaction_code": record.get("transaction_code", ""),
            "student_name": record.get("student_name", ""),
            "ready": bool(pdf_urls)
        })

    @app.route("/payment_status")
    def payment_status_page():
        bundle_id = request.args.get("bundle_id", "")
        if not bundle_id:
            flash("Bundle ID missing.", "danger")
            return redirect(url_for("index"))
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            flash("Document not found.", "danger")
            return render_template("error.html", message="Document not found"), 404
        doc_names = ", ".join([FORM_TYPE_DISPLAY.get(ft, ft) for ft in record.get("form_types", [])])
        return render_template("payment_status.html", bundle_id=bundle_id,
                             student_name=record.get("student_name", ""),
                             payment_status=record.get("payment_status", PaymentStatus.PENDING.value),
                             doc_names=doc_names, total_amount=record.get("total_amount", 0))

    @app.route("/retrieve", methods=["POST"])
    def retrieve_document():
        data = request.json or {}
        identifier = data.get("identifier", "").strip()
        if not identifier:
            return jsonify({"error": "Email address is required"}), 400

        record = get_user_document_by_email(identifier)
        if not record:
            return jsonify({"error": "No document found for that email address."}), 404
        if record.get("payment_status") != PaymentStatus.SUCCESS.value:
            return jsonify({"error": "Payment not completed. Please pay first."}), 402

        pdf_urls = record.get("pdf_urls", {})
        form_types = record.get("form_types", [])
        response_data = {
            "bundle_id": record.get("bundle_id", ""),
            "student_name": record.get("student_name", ""),
            "transaction_code": record.get("transaction_code", ""),
            "paid_at": record.get("paid_at", "").isoformat() if hasattr(record.get("paid_at"), "isoformat") else str(record.get("paid_at", "")),
            "documents": []
        }
        for ft in form_types:
            doc = {"type": ft, "name": FORM_TYPE_DISPLAY.get(ft, ft)}
            if ft in pdf_urls:
                doc["download_url"] = pdf_urls[ft]
                doc["cloudinary"] = True
            else:
                doc["download_url"] = f"/download_pdf/{record['bundle_id']}/{ft}"
            response_data["documents"].append(doc)
        
        response_data["download_all_url"] = f"/download_all/{record['bundle_id']}"
        return jsonify(response_data)

    @app.route("/check_pdfs/<bundle_id>", methods=["GET"])
    def check_pdfs(bundle_id):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Document not found"}), 404
        pdf_urls = record.get("pdf_urls", {})
        return jsonify({
            "bundle_id": bundle_id,
            "has_pdfs": bool(pdf_urls),
            "pdf_urls": pdf_urls,
            "form_types": record.get("form_types", [])
        })

    @app.route("/health")
    def health():
        db_ok, db_msg = db_manager.health_check()
        cache_ok, cache_msg = cache.health_check()
        return jsonify({
            "status": "healthy" if (db_ok and cache_ok) else "degraded",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "database": {"connected": db_ok, "message": db_msg},
            "cache": {"connected": cache_ok, "message": cache_msg},
            "mpesa": "configured" if (cfg.mpesa_consumer_key and cfg.mpesa_passkey) else "missing",
            "brevo": "configured" if cfg.brevo_api_key else "missing",
            "cloudinary": "configured" if (os.getenv("CLOUDINARY_CLOUD_NAME") and os.getenv("CLOUDINARY_API_KEY")) else "missing",
            "environment": "production" if cfg.is_production else "development",
            "version": "3.0.0"
        }), 200 if db_ok and cache_ok else 503

    # ============================================================
    # ADMIN ROUTES
    # ============================================================

    @app.route("/admin")
    def admin_index():
        """Redirect to admin login"""
        if session.get("admin_logged_in"):
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("admin_login"))

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            if request.form.get("username") == cfg.admin_username and request.form.get("password") == cfg.admin_password:
                session["admin_logged_in"] = True
                flash("Logged in successfully.", "success")
                return redirect(url_for("admin_dashboard"))
            flash("Invalid credentials.", "danger")
        return render_template("admin_login.html")

    @app.route("/admin/dashboard")
    @admin_required
    def admin_dashboard():
        return render_template("admin_dashboard.html", referral_discount=cfg.referral_discount_per_document)

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("admin_logged_in", None)
        flash("Logged out.", "info")
        return redirect(url_for("admin_login"))

    # ============================================================
    # ADMIN API ROUTES
    # =======================c=====================================

    @app.route("/admin/get_forms", methods=["GET"])
    @admin_required
    def admin_get_forms():
        """Get all documents for admin dashboard"""
        try:
            docs = get_all_user_documents()
            for doc in docs:
                if '_id' in doc:
                    doc['_id'] = str(doc['_id'])
                if 'created_at' in doc and isinstance(doc['created_at'], datetime):
                    doc['created_at'] = doc['created_at'].isoformat()
                if 'paid_at' in doc and isinstance(doc['paid_at'], datetime):
                    doc['paid_at'] = doc['paid_at'].isoformat()
            return jsonify(docs)
        except Exception as e:
            log.error(f"Error getting forms: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/admin/get_stats", methods=["GET"])
    @admin_required
    def admin_get_stats():
        """Get statistics for admin dashboard - Accurate test document detection"""
        try:
            docs = get_all_user_documents()
            total = len(docs)
            paid = 0
            pending = 0
            revenue = 0
            test_docs = 0
            test_revenue = 0
            
            # Current prices for real documents
            current_prices = {
                "medical": 400,
                "sponsorship": 300,
                "single_parent": 300,
            }
            discount_per_doc = 50
            
            for doc in docs:
                status = doc.get('payment_status', '')
                form_types = doc.get('form_types', [])
                stored_amount = doc.get('total_amount', 0)
                referral_code = doc.get('referral_code', '')
                transaction_code = doc.get('transaction_code', '')
                
                # IMPORTANT: A document is ONLY a test document if:
                # 1. It has NO transaction_code (never paid)
                # 2. AND total_amount < 100 (test amount)
                # 
                # If it has a transaction_code, it's a REAL payment regardless of amount
                is_test = False
                
                # If there's a transaction code, it's a real payment
                if transaction_code and transaction_code != '':
                    is_test = False
                else:
                    # Only classify as test if no transaction and amount is small
                    if stored_amount < 100 and stored_amount > 0:
                        is_test = True
                
                if is_test:
                    test_docs += 1
                    if status == 'success':
                        test_revenue += stored_amount
                        # Don't add to main revenue
                    continue
                
                # Calculate real document revenue
                base_price = 0
                for ft in form_types:
                    base_price += current_prices.get(ft, 300)
                
                # Apply discount if referral code exists
                discount = len(form_types) * discount_per_doc if referral_code else 0
                final_price = base_price - discount
                
                # Check if paid (either status success or has transaction code)
                is_paid = (status == 'success') or (transaction_code and transaction_code != '')
                
                if is_paid:
                    paid += 1
                    # Use the stored amount if it's correct, otherwise use calculated
                    # For documents with transaction code, use stored amount if it's reasonable
                    if stored_amount >= 100:
                        revenue += stored_amount
                    else:
                        # Use calculated amount for documents with transaction code but low stored amount
                        revenue += final_price
                elif status == 'pending':
                    pending += 1
            
            return jsonify({
                "total_bundles": total,
                "paid_bundles": paid,
                "pending_bundles": pending,
                "total_revenue": revenue,
                "test_documents": test_docs,
                "test_revenue": test_revenue
            })
        except Exception as e:
            log.error(f"Error getting stats: {e}")
            return jsonify({"error": str(e)}), 500
    # ============================================================
    # ADMIN REFERRAL CODES
    # ============================================================

    @app.route("/admin/referral_codes", methods=["GET"])
    @admin_required
    def admin_referral_codes_page():
        """Render referral codes management page"""
        return render_template("admin_referral_codes.html", referral_discount=cfg.referral_discount_per_document)

    @app.route("/admin/api/referral_codes", methods=["GET", "POST"])
    @admin_required
    def admin_referral_codes_api():
        """API endpoint for referral codes"""
        if request.method == "GET":
            try:
                codes = get_all_referral_codes()
                return jsonify(codes)
            except Exception as e:
                log.error(f"Error getting referral codes: {e}")
                return jsonify({"error": str(e)}), 500
        
        if request.method == "POST":
            try:
                data = request.json or {}
                code = data.get('code', '').upper().strip()
                marketer = data.get('marketer_name', '').strip()
                if not code or not marketer:
                    return jsonify({"error": "Code and marketer name are required"}), 400
                
                success = create_referral_code(code, marketer)
                if success:
                    return jsonify({"success": True})
                else:
                    return jsonify({"error": "Failed to create code (may already exist)"}), 400
            except Exception as e:
                log.error(f"Error creating referral code: {e}")
                return jsonify({"error": str(e)}), 500

    # ============================================================
    # ADMIN SETTINGS
    # ============================================================

    @app.route("/admin/settings", methods=["GET"])
    @admin_required
    def admin_settings_page():
        """Render admin settings page"""
        try:
            settings = get_admin_settings()
            return render_template("admin_settings.html", settings=settings)
        except Exception as e:
            log.error(f"Error getting settings: {e}")
            flash("Error loading settings", "error")
            return redirect(url_for("admin_dashboard"))

    @app.route("/admin/api/settings", methods=["GET", "POST"])
    @admin_required
    def admin_settings_api():
        """API endpoint for admin settings"""
        if request.method == "GET":
            try:
                settings = get_admin_settings()
                return jsonify(settings)
            except Exception as e:
                log.error(f"Error getting settings: {e}")
                return jsonify({"error": str(e)}), 500
        
        if request.method == "POST":
            try:
                data = request.json or {}
                # Convert flat data to nested structure
                settings = {
                    "medical_officer": {
                        "officer_name": data.get('med_officer_name', ''),
                        "hospital_name": data.get('med_hospital_name', ''),
                        "designation": data.get('med_designation', ''),
                        "reg_number": data.get('med_reg_number', ''),
                        "signature": data.get('med_signature', '')
                    },
                    "sponsor": {
                        "sponsor_name": data.get('spo_sponsor_name', ''),
                        "sponsor_email": data.get('spo_sponsor_email', ''),
                        "sponsor_telephone": data.get('spo_sponsor_phone', ''),
                        "signature": data.get('spo_signature', '')
                    },
                    "commissioner": {
                        "name": data.get('comm_name', ''),
                        "signature": data.get('comm_signature', '')
                    }
                }
                save_admin_settings(settings)
                return jsonify({"success": True})
            except Exception as e:
                log.error(f"Error saving settings: {e}")
                return jsonify({"error": str(e)}), 500

    @app.route("/admin/settings_route", methods=["GET"])
    @admin_required
    def admin_settings_route():
        """Legacy route for settings - redirect to admin_settings"""
        return redirect(url_for('admin_settings_page'))

    # ============================================================
    # ADMIN STAMPS
    # ============================================================

    @app.route("/admin/stamps", methods=["GET"])
    @admin_required
    def admin_stamps():
        """Manage stamps page"""
        return render_template("admin_stamps.html")

    @app.route("/admin/api/stamps", methods=["GET"])
    @admin_required
    def admin_api_stamps():
        """Get list of available stamps"""
        try:
            stamps = []
            stamp_types = ['hospital_stamp', 'commissioner_stamp', 'sponsor_stamp']
            for stamp_type in stamp_types:
                stamp_path = os.path.join(STAMPS_DIR, f"{stamp_type}.png")
                if os.path.exists(stamp_path):
                    stamps.append({
                        'type': stamp_type,
                        'path': f"/static/stamps/{stamp_type}.png",
                        'exists': True
                    })
            return jsonify(stamps)
        except Exception as e:
            log.error(f"Error getting stamps: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/admin/stamps/delete/<stamp_type>", methods=["DELETE"])
    @admin_required
    def admin_stamps_delete(stamp_type):
        """Delete a stamp image"""
        try:
            if not stamp_type:
                return jsonify({"error": "Stamp type is required"}), 400
            
            filepath = os.path.join(STAMPS_DIR, f"{stamp_type}.png")
            if os.path.exists(filepath):
                os.remove(filepath)
                _stamp_image_cache.put(stamp_type, None)
                log.info(f"Deleted stamp: {stamp_type}")
                return jsonify({"success": True, "message": f"Stamp {stamp_type} deleted"})
            else:
                return jsonify({"error": "Stamp file not found"}), 404
        except Exception as e:
            log.error(f"Error deleting stamp: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/admin/update_stamp", methods=["POST"])
    @admin_required
    def admin_update_stamp():
        """Upload or update a stamp image"""
        try:
            if 'stamp_file' not in request.files:
                return jsonify({"error": "No file uploaded"}), 400
            
            file = request.files['stamp_file']
            stamp_type = request.form.get('stamp_type', '')
            
            if not stamp_type:
                return jsonify({"error": "Stamp type is required"}), 400
            
            if file.filename == '':
                return jsonify({"error": "No file selected"}), 400
            
            if file:
                filename = f"{stamp_type}.png"
                filepath = os.path.join(STAMPS_DIR, filename)
                file.save(filepath)
                _stamp_image_cache.put(stamp_type, None)
                log.info(f"Updated stamp: {stamp_type}")
                return jsonify({"success": True, "message": f"Stamp {stamp_type} updated successfully"})
            
            return jsonify({"error": "Invalid file"}), 400
        except Exception as e:
            log.error(f"Error updating stamp: {e}")
            return jsonify({"error": str(e)}), 500

    # ============================================================
    # CONTEXT PROCESSOR - Inject stamp positions into templates
    # ============================================================

    @app.context_processor
    def inject_stamp_positions():
        """Inject stamp positions into all templates"""
        positions = {
            'medical': {
                'hospital_stamp': {'x': 475.1, 'y': 412.2, 'width': 76.8, 'height': 28.0},
                'commissioner_stamp': {'x': 490.5, 'y': 542.6, 'width': 76.8, 'height': 24.0}
            },
            'sponsorship': {
                'sponsor_stamp': {'x': 457.1, 'y': 428.0, 'width': 76.8, 'height': 24.0},
                'commissioner_stamp': {'x': 460.0, 'y': 583.7, 'width': 76.8, 'height': 24.0}
            },
            'single_parent': {
                'commissioner_stamp': {'x': 475.2, 'y': 674.1, 'width': 76.8, 'height': 24.0}
            }
        }
        return {'stamp_positions': positions}

    # ============================================================
    # STARTUP / SHUTDOWN
    # ============================================================

    def verify_assets():
        required = ["Medical_Form.pdf", "Sponsorship_Letter.pdf", "Single_Parent_Self_Certification_2024.pdf"]
        missing = [p for p in required if not os.path.exists(os.path.join(BASE_DIR, p))]
        if missing:
            log.warning(f"Missing PDF templates: {missing}")
        create_default_stamps(STAMPS_DIR, log)

    def shutdown_handler(signum, frame):
        log.info(f"Received signal {signum}, initiating graceful shutdown...")
        executor.shutdown(wait=True)
        db_manager.close()
        log.info("Shutdown complete.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    verify_assets()

    log.info("=" * 60)
    log.info("SUPPORTING DOCUMENTS GENERATOR v3.0 — PRODUCTION READY")
    log.info("=" * 60)
    log.info(f"Environment: {'PRODUCTION' if cfg.is_production else 'DEVELOPMENT'}")
    log.info(f"Database: {'MongoDB' if use_mongo else 'In-Memory'}")
    log.info(f"Test Mode: {'ON' if TEST_MODE else 'OFF'}")
    log.info(f"Cloudinary: {'CONFIGURED' if (os.getenv('CLOUDINARY_CLOUD_NAME') and os.getenv('CLOUDINARY_API_KEY')) else 'NOT CONFIGURED'}")
    log.info(f"Email CC: kuccpscourses@gmail.com")
    log.info(f"Base64 Storage: DISABLED")
    log.info("=" * 60)

    return app

# ============================================================================
# ENTRY POINT
# ============================================================================

app = create_app()

if __name__ == "__main__":
    cfg = Config.load()
    if cfg.is_production:
        from gunicorn.app.base import BaseApplication
        
        class FlaskApplication(BaseApplication):
            def __init__(self, app, options=None):
                self.options = options or {}
                self.application = app
                super().__init__()
            
            def load_config(self):
                for key, value in self.options.items():
                    if key in self.cfg.settings and value is not None:
                        self.cfg.set(key, value)
            
            def load(self):
                return self.application
        
        options = {
            'bind': f"{cfg.host}:{cfg.port}",
            'workers': 2,
            'threads': 4,
            'worker_class': 'gthread',
            'max_requests': 100,
            'timeout': 120,
            'preload_app': True,
        }
        
        FlaskApplication(app, options).run()
    else:
        app.run(debug=cfg.debug, host=cfg.host, port=cfg.port)