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
# HTTP / Environment
# ------------------------------------------------------------------------------
import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# CONSTANTS & ENUMS
# ============================================================================

class PaymentStatus(Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

PAGE_H = 792.0
PAGE_W = 612.0
TODAY = date.today().strftime("%d %B %Y")

DOCUMENT_PRICES = {
    "medical": 1,
    "sponsorship": 1,
    "single_parent": 1,
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
    """Immutable, validated configuration."""
    secret_key: str
    env: str
    debug: bool
    port: int = 8080
    host: str = "0.0.0.0"

    # Sessions
    session_type: str = "mongodb"
    session_lifetime: int = 86400
    session_cookie_secure: bool = False
    session_cookie_httponly: bool = True
    session_cookie_samesite: str = "Lax"

    # MongoDB
    mongo_uri: str = ""
    mongo_max_pool: int = 200
    mongo_min_pool: int = 10
    mongo_server_selection_timeout_ms: int = 5000
    mongo_socket_timeout_ms: int = 30000
    mongo_db_name: str = "supporting_docs"

    # Redis
    redis_url: str = ""
    redis_socket_timeout: int = 5

    # Rate Limiting
    rate_limit_per_minute: int = 30
    rate_limit_storage: str = "memory"

    # M-Pesa
    mpesa_consumer_key: str = ""
    mpesa_consumer_secret: str = ""
    mpesa_shortcode: str = "4185095"
    mpesa_passkey: str = ""
    mpesa_env: str = "production"
    mpesa_callback_url: str = ""
    mpesa_token_timeout: Tuple[int, int] = (3, 3)
    mpesa_stk_timeout: Tuple[int, int] = (10, 10)
    mpesa_query_timeout: Tuple[int, int] = (2, 2)

    # Pricing
    payment_amount_per_doc: int = 300
    referral_discount_per_document: int = 50

    # Brevo
    brevo_api_key: str = ""
    brevo_sender_email: str = "noreply@supportingdocs.com"
    brevo_sender_name: str = "Supporting Documents"

    # Admin
    admin_username: str = ""
    admin_password: str = ""

    # Stamps
    stamp_scale: float = 1.5

    # Workers / Queue
    max_background_workers: int = 8
    task_queue_max_size: int = 1000

    # Logging
    log_level: str = "INFO"
    log_file: str = "app.log"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5

    # Health
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
        debug = os.getenv("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")

        secret_key = os.getenv("SECRET_KEY", "").strip()
        if env == "production":
            if not secret_key:
                raise RuntimeError("CRITICAL: SECRET_KEY is required in production")
            if debug:
                print("WARNING: FLASK_DEBUG enabled in production")
        else:
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
        if env == "production" and "ngrok" in callback:
            print("WARNING: MPESA_CALLBACK_URL uses ngrok")

        return cls(
            secret_key=secret_key,
            env=env,
            debug=debug,
            port=int(os.getenv("PORT", "8080")),
            session_type=os.getenv("SESSION_TYPE", "mongodb").lower(),
            session_lifetime=int(os.getenv("SESSION_LIFETIME", "86400")),
            session_cookie_secure=env == "production",
            mongo_uri=mongo_uri,
            mongo_max_pool=int(os.getenv("MONGO_MAX_POOL", "200")),
            mongo_min_pool=int(os.getenv("MONGO_MIN_POOL", "10")),
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
            brevo_sender_email=os.getenv("BREVO_SENDER_EMAIL", "noreply@supportingdocs.com").strip(),
            brevo_sender_name=os.getenv("BREVO_SENDER_NAME", "Supporting Documents").strip(),
            admin_username=admin_user,
            admin_password=admin_pass,
            stamp_scale=float(os.getenv("STAMP_SCALE_FACTOR", "1.5")),
            max_background_workers=int(os.getenv("MAX_BACKGROUND_WORKERS", "8")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            log_file=os.getenv("LOG_FILE", "app.log"),
        )

# ============================================================================
# STRUCTURED LOGGING
# ============================================================================

class JSONFormatter(logging.Formatter):
    """JSON structured logging for production observability."""
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

    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(JSONFormatter())
    logger.addHandler(console)

    # File
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
    """Inject request_id and user into log records. Safe outside Flask context."""
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
            self.log.warning("PyMongo not installed. Using in-memory storage (NOT for production).")
            return

        if not cfg.mongo_uri:
            self.log.warning("MONGO_URI not set. Using in-memory storage.")
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
            self._db.documents.create_index("checkout_request_id", background=True)
            self._db.documents.create_index("student_details.email", background=True)
            self._db.documents.create_index("created_at", background=True)
            self._db.documents.create_index("payment_status", background=True)
            self._db.documents.create_index("bundle_id", unique=True, background=True)
            self._db.referral_codes.create_index("code", unique=True, background=True)
            self._db.sessions.create_index("expires", expireAfterSeconds=0, background=True)
        except Exception as e:
            self.log.error(f"Index creation error: {e}")

    @property
    def db(self):
        if self._connected and self._db is not None:
            return self._db
        return None

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
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = threading.RLock()

    def call(self, func: Callable, *args, **kwargs):
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.time() - (self._last_failure_time or 0) > self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._failure_count = 0
                    if self.log:
                        self.log.info("Circuit breaker half-open")
                else:
                    raise RuntimeError("Circuit breaker is OPEN")

        try:
            result = func(*args, **kwargs)
            with self._lock:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
            return result
        except Exception as e:
            with self._lock:
                self._failure_count += 1
                self._last_failure_time = time.time()
                if self._failure_count >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    if self.log:
                        self.log.error(f"Circuit breaker OPEN after {self.failure_threshold} failures")
            raise e


# ============================================================================
# M-PESA CLIENT (with pending handling)
# ============================================================================

class MpesaClient:
    def __init__(self, cfg: Config, cache: CacheManager, logger: LogContext):
        self.cfg = cfg
        self.cache = cache
        self.log = logger
        self._cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60, logger=logger)

        self._session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[502, 503, 504], allowed_methods=["GET", "POST"])
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

    def query_transaction(self, checkout_request_id: str) -> Tuple[bool, Dict]:
        try:
            return self._cb.call(self._query, checkout_request_id)
        except Exception as e:
            return False, {"error": str(e)}

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
        # Treat pending codes (1, 1037) as pending, not failed
        if result_code in (1, 1037) or "pending" in data.get("ResultDesc", "").lower():
            return False, {"status": "pending", "error": data.get("ResultDesc", "Still processing")}
        # Any other non-zero code means a real failure
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
# BACKGROUND TASK EXECUTOR
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
# EMAIL SERVICE (with CC support)
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
        """
        Send an email via Brevo.
        :param cc: List of email addresses to CC.
        """
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

        # Add CC if provided
        if cc:
            payload["cc"] = [{"email": email} for email in cc]

        if attachments:
            payload["attachment"] = [
                {"content": base64.b64encode(data).decode("utf-8"), "name": name}
                for name, data in attachments
            ]

        try:
            resp = self._session.post(url, json=payload, headers=headers, timeout=10)
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
STAMPED_BASE_DIR = os.path.join(BASE_DIR, "stamped_templates")
STAMPS_DIR = os.path.join(BASE_DIR, "stamps")
os.makedirs(STAMPED_BASE_DIR, exist_ok=True)
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

# Stamp coordinates (unchanged)
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

STAMP_POSITIONS = {
    "medical": {
        "hospital_stamp": {"x": 475.1, "y": 792 - 440.2, "width": 76.8, "height": 28.0},
        "commissioner_stamp": {"x": 490.5, "y": 792 - 566.6, "width": 76.8, "height": 24.0}
    },
    "sponsorship": {
        "sponsor_stamp": {"x": 457.1, "y": 792 - 452.0, "width": 76.8, "height": 24.0},
        "commissioner_stamp": {"x": 460.0, "y": 792 - 607.7, "width": 76.8, "height": 24.0}
    },
    "single_parent": {
        "commissioner_stamp": {"x": 475.2, "y": 792 - 698.1, "width": 76.8, "height": 24.0}
    }
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
# FORM BUILDERS – Commissioner name removed, date variables defined
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
    # Date variable for commissioner date – defined here
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
    # Commissioner name field removed – only date and signature remain
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
    # Date variable for commissioner date – defined here
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
    # Commissioner name field removed – only date and signature remain
    fields.append({"font_size": date_fs, "x": 430.0, "y": SP_COMM_DATE_Y, "text": d.get("comm_date", TODAY), "font_name": STD_FONT})

    sigs = []
    sigs.append((d.get("parent_sig", ""), 240.8, SP_PAR_SIG_Y, 130, 28))
    if include_admin_sigs:
        sigs.append((adm.get("commissioner_sig", ""), 355.9, SP_COMM_SIG_Y, 80, 28))
    return fields, sigs, underlines


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
# EMAIL TEMPLATE
# ============================================================================

def build_payment_confirmation_email_multi(student_name, bundle_id, transaction_code, form_types, total_amount):
    doc_list = "".join([f"<li>{FORM_TYPE_DISPLAY.get(ft, ft)}</li>" for ft in form_types])
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: #10B981; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }}
        .content {{ padding: 30px; background: #f9f9f9; }}
        .footer {{ padding: 20px; text-align: center; color: #666; font-size: 12px; }}
        .details {{ background: white; padding: 15px; border-radius: 5px; margin: 15px 0; }}
        .doc-list {{ list-style: none; padding: 0; }}
        .doc-list li {{ padding: 8px 0; border-bottom: 1px solid #eee; }}
        .button {{ display: inline-block; padding: 12px 30px; background: #10B981; color: white; text-decoration: none; border-radius: 5px; font-weight: bold; }}
    </style>
    </head>
    <body>
    <div class="container">
        <div class="header"><h2>Payment Confirmed!</h2><p>Supporting Documents Generation</p></div>
        <div class="content">
            <h3>Dear {student_name},</h3>
            <p>We are pleased to confirm that your payment has been successfully processed.</p>
            <div class="details">
                <h4>Transaction Details</h4>
                <p><strong>Documents Generated:</strong></p>
                <ul class="doc-list">{doc_list}</ul>
                <p><strong>Bundle ID:</strong> {bundle_id}</p>
                <p><strong>Transaction Code:</strong> {transaction_code}</p>
                <p><strong>Date:</strong> {datetime.now().strftime('%d %B %Y at %H:%M')}</p>
                <p><strong>Total Paid:</strong> KES {total_amount}</p>
            </div>
            <p><strong>All your documents are attached to this email.</strong></p>
            <p>You can also download them anytime using your email address on our portal.</p>
            <p style="margin-top: 20px;">Thank you for using our service.<br><strong>Supporting Documents Team</strong></p>
        </div>
        <div class="footer"><p>This is an automated message. Please do not reply to this email.</p><p>&copy; 2026 Supporting Documents. All rights reserved.</p></div>
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
    # SESSION PERSISTENCE HELPERS
    # ============================================================

    def save_session_state(selected_types=None, form_data_map=None, referral_code=None):
        """Store user's current selection and form data in Flask session."""
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
        """Retrieve stored session data."""
        return {
            'selected_types': session.get('selected_types', []),
            'form_data_map': session.get('form_data_map', {}),
            'referral_code': session.get('referral_code', '')
        }

    # ============================================================
    # HELPERS
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

    def save_user_document(record):
        if use_mongo:
            return mongo_db.documents.insert_one(record)
        memory_storage[record["bundle_id"]] = record
        return record

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
                {"student_details.email": {"$regex": f"^{re.escape(email_lower)}$", "$options": "i"}}
            ).sort("created_at", -1).limit(10))
            for doc in docs:
                if doc.get("payment_status") == "success":
                    return doc
            return docs[0] if docs else None
        matching = [v for v in memory_storage.values() if isinstance(v, dict) and v.get("student_details", {}).get("email", "").lower() == email_lower]
        matching.sort(key=lambda x: x.get("created_at", datetime.min), reverse=True)
        for doc in matching:
            if doc.get("payment_status") == "success":
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
        code = code.upper().strip()
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

    def _generate_multiple_pdfs_and_send_email(bundle_id, form_types, form_data_map, student_email, student_name, tx_code):
        try:
            attachments = []
            pdf_map = {}
            total_amount = sum(DOCUMENT_PRICES.get(ft, cfg.payment_amount_per_doc) for ft in form_types)

            for ft in form_types:
                pdf_bytes = _make_pdf(ft, form_data_map.get(ft, {}), stamped=True)
                pdf_map[ft] = pdf_bytes
                _, _, filename = BUILDERS.get(ft, (None, None, "document.pdf"))
                attachments.append((filename, pdf_bytes))
                log.info(f"[PDF] Stamped PDF generated for {ft}")

            encoded_pdfs = {ft: base64.b64encode(pb).decode() for ft, pb in pdf_map.items()}

            if use_mongo:
                mongo_db.documents.update_one({"bundle_id": bundle_id}, {"$set": {"pdfs": encoded_pdfs}})
            elif bundle_id in memory_storage:
                memory_storage[bundle_id]["pdfs"] = encoded_pdfs

            if student_email and cfg.brevo_api_key:
                doc_names = [FORM_TYPE_DISPLAY.get(ft, ft) for ft in form_types]
                subject = f"Your Documents ({', '.join(doc_names)}) - {bundle_id}"
                html = build_payment_confirmation_email_multi(student_name, bundle_id, tx_code, form_types, total_amount)
                # ✅ Added CC: admin email
                success, message = email_service.send(
                    to_email=student_email,
                    to_name=student_name,
                    subject=subject,
                    html=html,
                    attachments=attachments,
                    cc=["kuccpscourses@gmail.com"]  # <-- CC added here
                )
                if success:
                    log.info(f"[EMAIL] Sent to {student_email} (CC: kuccpscourses@gmail.com)")
                elif message == "IP_WHITELIST_ERROR":
                    log.warning("[EMAIL] IP whitelist error")
                else:
                    log.warning(f"[EMAIL] Failed: {message}")
            else:
                log.warning(f"[EMAIL] Skipped for {bundle_id}")
        except Exception as e:
            log.exception(f"[BACKGROUND] Task failed for {bundle_id}: {e}")

    # ============================================================
    # ACTIVE POLLING (with pending handling)
    # ============================================================

    def _active_poll_payment(checkout_request_id, bundle_id, form_types, form_data_map,
                             student_email, student_name, max_attempts=8, interval=1.0):
        time.sleep(2.0)
        for attempt in range(max_attempts):
            record = get_user_document_by_bundle_id(bundle_id)
            if record and record.get("payment_status") == PaymentStatus.SUCCESS.value:
                log.info(f"[POLL] {bundle_id} already confirmed by callback")
                return

            try:
                success, result = mpesa.query_transaction(checkout_request_id)
                if success:
                    tx_code = result.get("mpesa_receipt_number", checkout_request_id)
                    if use_mongo:
                        mongo_db.documents.update_one(
                            {"bundle_id": bundle_id, "payment_status": {"$ne": PaymentStatus.SUCCESS.value}},
                            {"$set": {
                                "payment_status": PaymentStatus.SUCCESS.value,
                                "transaction_code": tx_code,
                                "paid_at": datetime.now()
                            }}
                        )
                    elif bundle_id in memory_storage:
                        if memory_storage[bundle_id].get("payment_status") != PaymentStatus.SUCCESS.value:
                            memory_storage[bundle_id].update({
                                "payment_status": PaymentStatus.SUCCESS.value,
                                "transaction_code": tx_code,
                                "paid_at": datetime.now()
                            })
                    executor.submit(
                        _generate_multiple_pdfs_and_send_email,
                        bundle_id, form_types, form_data_map,
                        student_email, student_name, tx_code
                    )
                    log.info(f"[POLL] Payment confirmed via query for {bundle_id} after {attempt+1} attempts")
                    return

                if result.get("status") == "pending":
                    log.debug(f"[POLL] {bundle_id} still pending (attempt {attempt+1}/{max_attempts})")
                    time.sleep(interval)
                    continue

                if result.get("status") == "failed":
                    reason = result.get("error", "Transaction failed")
                    if use_mongo:
                        mongo_db.documents.update_one(
                            {"bundle_id": bundle_id},
                            {"$set": {
                                "payment_status": PaymentStatus.FAILED.value,
                                "payment_failure_reason": reason
                            }}
                        )
                    elif bundle_id in memory_storage:
                        memory_storage[bundle_id]["payment_status"] = PaymentStatus.FAILED.value
                        memory_storage[bundle_id]["payment_failure_reason"] = reason
                    log.warning(f"[POLL] Payment failed for {bundle_id}: {reason}")
                    return

            except Exception as e:
                log.error(f"[POLL] Query exception for {bundle_id}: {e}")

            time.sleep(interval)

        log.info(f"[POLL] Timeout for {bundle_id}. Falling back to callback.")

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
    # ERROR HANDLERS
    # ============================================================

    @app.errorhandler(404)
    def not_found(e):
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"error": "Endpoint not found"}), 404
        flash("Page not found.", "danger")
        return render_template("error.html", message="Page not found"), 404

    @app.errorhandler(500)
    def internal_error(e):
        log.exception("Internal Server Error")
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"error": "An internal server error occurred. Please try again later."}), 500
        flash("An internal server error occurred. Please try again later.", "danger")
        return render_template("error.html", message="Server error"), 500

    @app.errorhandler(Exception)
    def handle_exception(e):
        log.exception("Unhandled Exception")
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"error": "An unexpected error occurred. Our team has been notified."}), 500
        flash("An unexpected error occurred. Our team has been notified.", "danger")
        raise e

    # ============================================================
    # ROUTES
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

        # ✅ Use KCSE index as account reference (or fallback to bundle_id)
        kcse_index = student_details.get("kcse_index", "").strip()
        # Clean to alphanumeric and limit to 12 characters
        if kcse_index:
            clean_kcse = re.sub(r"[^a-zA-Z0-9]", "", kcse_index)
            account_ref = clean_kcse[:12]
        else:
            account_ref = bundle_id[:12]  # fallback

        try:
            save_user_document({
                "bundle_id": bundle_id, "form_types": form_types, "student_details": student_details,
                "form_data_map": form_data_map, "payment_status": PaymentStatus.PENDING.value,
                "created_at": datetime.now(), "transaction_code": "", "checkout_request_id": None,
                "phone_number": formatted_phone, "total_amount": total_amount,
                "referral_code": referral_code if valid_code else "",
                "discount_applied": discount_per_doc if valid_code else 0,
                "marketer_name": marketer if valid_code else "",
                "account_reference": account_ref  # store for reference
            })
        except Exception as e:
            log.error(f"Failed to save document: {e}")
            return jsonify({"error": "Database error"}), 500

        success, result = mpesa.init_stk_push(formatted_phone, account_ref, f"{len(form_types)} Docs", total_amount)
        if success:
            checkout_request_id = result["checkout_request_id"]
            if use_mongo:
                mongo_db.documents.update_one({"bundle_id": bundle_id}, {"$set": {
                    "checkout_request_id": checkout_request_id, "account_reference": account_ref
                }})
            elif bundle_id in memory_storage:
                memory_storage[bundle_id]["checkout_request_id"] = checkout_request_id
                memory_storage[bundle_id]["account_reference"] = account_ref

            executor.submit(
                _active_poll_payment,
                checkout_request_id,
                bundle_id,
                form_types,
                form_data_map,
                student_email,
                student_details.get("student_name", "Student"),
                max_attempts=8,
                interval=1.0
            )

            return jsonify({
                "success": True, "checkout_request_id": checkout_request_id,
                "merchant_request_id": result["merchant_request_id"],
                "customer_message": result["customer_message"], "bundle_id": bundle_id,
                "elapsed_seconds": result.get("elapsed_seconds"),
                "redirect_url": "/payment_status?bundle_id=" + bundle_id
            })
        else:
            return jsonify({"error": result.get("error", "Payment initiation failed")}), 400

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
        if result_code == 0:
            tx_code = metadata.get("MpesaReceiptNumber", checkout_request_id)
            student_email = record.get("student_details", {}).get("email", "")
            student_name = record.get("student_details", {}).get("student_name", "Student")

            if use_mongo:
                mongo_db.documents.update_one(
                    {"bundle_id": bundle_id, "payment_status": {"$ne": PaymentStatus.SUCCESS.value}},
                    {"$set": {"payment_status": PaymentStatus.SUCCESS.value, "transaction_code": tx_code, "paid_at": datetime.now()}}
                )
            elif bundle_id in memory_storage and memory_storage[bundle_id].get("payment_status") != PaymentStatus.SUCCESS.value:
                memory_storage[bundle_id].update({"payment_status": PaymentStatus.SUCCESS.value, "transaction_code": tx_code, "paid_at": datetime.now()})

            executor.submit(_generate_multiple_pdfs_and_send_email, bundle_id, record.get("form_types", []),
                            record.get("form_data_map", {}), student_email, student_name, tx_code)
            log.info(f"[M-PESA] Payment confirmed for {bundle_id}")
            return jsonify({"status": "success", "bundle_id": bundle_id}), 200
        else:
            log.warning(f"[M-PESA] Payment failed for {bundle_id}: {result_desc}")
            if use_mongo:
                mongo_db.documents.update_one({"bundle_id": bundle_id}, {"$set": {
                    "payment_status": PaymentStatus.FAILED.value, "payment_failure_reason": result_desc
                }})
            elif bundle_id in memory_storage:
                memory_storage[bundle_id]["payment_status"] = PaymentStatus.FAILED.value
                memory_storage[bundle_id]["payment_failure_reason"] = result_desc
            return jsonify({"status": "failed", "bundle_id": bundle_id}), 200

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
                             student_name=record.get("student_details", {}).get("student_name", ""),
                             payment_status=record.get("payment_status", PaymentStatus.PENDING.value),
                             doc_names=doc_names, total_amount=record.get("total_amount", 0))

    @app.route("/download_pdf/<bundle_id>/<form_type>", methods=["GET"])
    def download_single_pdf(bundle_id, form_type):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Document not found"}), 404
        if record.get("payment_status") != PaymentStatus.SUCCESS.value:
            return jsonify({"error": "Payment not completed"}), 402
        pdfs = record.get("pdfs", {})
        if not pdfs or form_type not in pdfs:
            return jsonify({"error": "PDF not found. Please wait a moment."}), 404
        pdf_bytes = base64.b64decode(pdfs[form_type])
        _, _, dl_name = BUILDERS.get(form_type, (None, None, "document.pdf"))
        return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True, download_name=dl_name)

    @app.route("/download_all/<bundle_id>", methods=["GET"])
    def download_all_pdfs(bundle_id):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Document not found"}), 404
        if record.get("payment_status") != PaymentStatus.SUCCESS.value:
            return jsonify({"error": "Payment not completed"}), 402
        pdfs = record.get("pdfs", {})
        if not pdfs:
            return jsonify({"error": "PDFs not found. Please wait a moment."}), 404
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for ft, encoded in pdfs.items():
                pdf_bytes = base64.b64decode(encoded)
                _, _, dl_name = BUILDERS.get(ft, (None, None, f"{ft}.pdf"))
                zf.writestr(dl_name, pdf_bytes)
        zip_buffer.seek(0)
        return send_file(zip_buffer, mimetype="application/zip", as_attachment=True, download_name=f"documents_{bundle_id}.zip")

    @app.route("/api/payment_status/<bundle_id>", methods=["GET"])
    def api_payment_status(bundle_id):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Document not found"}), 404
        status = record.get("payment_status", PaymentStatus.PENDING.value)
        response = {
            "bundle_id": bundle_id, "status": status,
            "transaction_code": record.get("transaction_code", ""),
            "student_name": record.get("student_details", {}).get("student_name", "")
        }
        if status == PaymentStatus.SUCCESS.value:
            pdfs = record.get("pdfs", {})
            response["ready"] = bool(pdfs)
            if pdfs:
                response["download_all_url"] = f"/download_all/{bundle_id}"
        return jsonify(response)

    @app.route("/check_payment_status", methods=["POST"])
    def check_payment_status():
        data = request.json or {}
        bundle_id = data.get("bundle_id", "").strip()
        checkout_request_id = data.get("checkout_request_id", "").strip()
        record = None

        if bundle_id:
            record = get_user_document_by_bundle_id(bundle_id)
        elif checkout_request_id:
            if use_mongo:
                record = mongo_db.documents.find_one({"checkout_request_id": checkout_request_id})
            else:
                for rec in memory_storage.values():
                    if isinstance(rec, dict) and rec.get("checkout_request_id") == checkout_request_id:
                        record = rec
                        break

        if not record:
            return jsonify({"error": "Document not found"}), 404
        status = record.get("payment_status", PaymentStatus.PENDING.value)

        if status == PaymentStatus.SUCCESS.value:
            return jsonify({"status": "success", "paid": True, "transaction_code": record.get("transaction_code", ""), "bundle_id": record.get("bundle_id")})
        if status == PaymentStatus.FAILED.value:
            return jsonify({"status": "failed", "paid": False, "reason": record.get("payment_failure_reason", "Payment failed")})

        cr_id = record.get("checkout_request_id", "")
        if cr_id:
            success, result = mpesa.query_transaction(cr_id)
            if success:
                tx_code = result.get("mpesa_receipt_number", cr_id)
                if use_mongo:
                    mongo_db.documents.update_one({"bundle_id": record["bundle_id"]}, {"$set": {
                        "payment_status": PaymentStatus.SUCCESS.value, "transaction_code": tx_code, "paid_at": datetime.now()
                    }})
                elif record["bundle_id"] in memory_storage:
                    memory_storage[record["bundle_id"]].update({"payment_status": PaymentStatus.SUCCESS.value, "transaction_code": tx_code, "paid_at": datetime.now()})

                executor.submit(_generate_multiple_pdfs_and_send_email, record["bundle_id"], record.get("form_types", []),
                                record.get("form_data_map", {}), record.get("student_details", {}).get("email", ""),
                                record.get("student_details", {}).get("student_name", "Student"), tx_code)
                return jsonify({"status": "success", "paid": True, "transaction_code": tx_code, "bundle_id": record["bundle_id"]})
            else:
                if result.get("status") == "failed":
                    return jsonify({"status": "failed", "paid": False, "reason": result.get("error", "Transaction failed")})
                # pending – still waiting
                return jsonify({"status": "pending", "paid": False})
        return jsonify({"status": "pending", "paid": False})

    @app.route("/wait_for_payment/<bundle_id>", methods=["GET"])
    @rate_limit(limiter)
    def wait_for_payment(bundle_id):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Document not found"}), 404

        status = record.get("payment_status", PaymentStatus.PENDING.value)
        if status == PaymentStatus.SUCCESS.value:
            return jsonify({
                "status": "success",
                "paid": True,
                "transaction_code": record.get("transaction_code", ""),
                "bundle_id": bundle_id
            })
        if status == PaymentStatus.FAILED.value:
            return jsonify({
                "status": "failed",
                "paid": False,
                "reason": record.get("payment_failure_reason", "Payment failed")
            })

        timeout, interval = 10.0, 0.5
        for _ in range(int(timeout / interval)):
            time.sleep(interval)
            record = get_user_document_by_bundle_id(bundle_id)
            if not record:
                continue
            status = record.get("payment_status", PaymentStatus.PENDING.value)
            if status == PaymentStatus.SUCCESS.value:
                return jsonify({
                    "status": "success",
                    "paid": True,
                    "transaction_code": record.get("transaction_code", ""),
                    "bundle_id": bundle_id
                })
            if status == PaymentStatus.FAILED.value:
                return jsonify({
                    "status": "failed",
                    "paid": False,
                    "reason": record.get("payment_failure_reason", "Payment failed")
                })

        return jsonify({"status": "pending", "paid": False, "bundle_id": bundle_id})

    @app.route("/test_callback/<bundle_id>", methods=["POST"])
    def test_callback(bundle_id):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Document not found"}), 404
        if record.get("payment_status") == PaymentStatus.SUCCESS.value:
            return jsonify({"status": "already_success", "bundle_id": bundle_id}), 200

        tx_code = f"TEST{datetime.now().strftime('%Y%m%d%H%M%S')}"
        if use_mongo:
            mongo_db.documents.update_one({"bundle_id": bundle_id}, {"$set": {
                "payment_status": PaymentStatus.SUCCESS.value, "transaction_code": tx_code, "paid_at": datetime.now()
            }})
        elif bundle_id in memory_storage:
            memory_storage[bundle_id].update({"payment_status": PaymentStatus.SUCCESS.value, "transaction_code": tx_code, "paid_at": datetime.now()})

        executor.submit(_generate_multiple_pdfs_and_send_email, bundle_id, record.get("form_types", []),
                        record.get("form_data_map", {}), record.get("student_details", {}).get("email", ""),
                        record.get("student_details", {}).get("student_name", "Student"), tx_code)
        return jsonify({"status": "success", "bundle_id": bundle_id, "transaction_code": tx_code, "message": "Callback triggered. PDF generation started."})

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

        pdfs = record.get("pdfs", {})
        form_types = record.get("form_types", [])
        response_data = {
            "bundle_id": record.get("bundle_id", ""),
            "student_name": record.get("student_details", {}).get("student_name", ""),
            "transaction_code": record.get("transaction_code", ""),
            "paid_at": record.get("paid_at", "").isoformat() if hasattr(record.get("paid_at"), "isoformat") else str(record.get("paid_at", "")),
            "documents": []
        }
        for ft in form_types:
            doc = {"type": ft, "name": FORM_TYPE_DISPLAY.get(ft, ft), "download_url": f"/download_pdf/{record['bundle_id']}/{ft}"}
            if ft in pdfs:
                doc["pdf"] = pdfs[ft]
            response_data["documents"].append(doc)
        response_data["download_all_url"] = f"/download_all/{record['bundle_id']}"
        return jsonify(response_data)

    @app.route("/retrieve_direct/<bundle_id>", methods=["GET"])
    def retrieve_direct(bundle_id):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Document not found"}), 404
        if record.get("payment_status") != PaymentStatus.SUCCESS.value:
            return jsonify({"error": "Payment not completed"}), 402
        pdfs = record.get("pdfs", {})
        if not pdfs:
            return jsonify({"error": "PDFs not ready yet. Please wait."}), 404
        return jsonify({"bundle_id": bundle_id, "documents": [{"type": ft, "pdf": enc} for ft, enc in pdfs.items()]})

    @app.route("/verify_payment/<bundle_id>", methods=["GET"])
    def verify_payment_status(bundle_id):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Document not found"}), 404
        return jsonify({
            "bundle_id": bundle_id, "payment_status": record.get("payment_status", PaymentStatus.PENDING.value),
            "transaction_code": record.get("transaction_code", ""), "checkout_request_id": record.get("checkout_request_id", ""),
            "student_name": record.get("student_details", {}).get("student_name", "")
        })

    # ============================================================
    # ADMIN ROUTES
    # ============================================================

    @app.route("/admin")
    def admin_redirect():
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            if request.form.get("username") == cfg.admin_username and request.form.get("password") == cfg.admin_password:
                session["admin_logged_in"] = True
                session.permanent = True
                flash("Logged in successfully.", "success")
                return redirect(url_for("admin_dashboard"))
            flash("Invalid credentials.", "danger")
            return render_template("admin_login.html", error="Invalid credentials")
        return render_template("admin_login.html")

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("admin_logged_in", None)
        flash("Logged out.", "info")
        return redirect(url_for("admin_login"))

    @app.route("/admin/dashboard")
    @admin_required
    def admin_dashboard():
        return render_template("admin_dashboard.html", referral_discount=cfg.referral_discount_per_document)

    @app.route("/admin/settings", methods=["GET", "POST"])
    @admin_required
    def admin_settings_route():
        if request.method == "POST":
            try:
                data = request.json or {}
                save_admin_settings({
                    "medical_officer": {
                        "officer_name": data.get("med_officer_name", ""), "hospital_name": data.get("med_hospital_name", ""),
                        "designation": data.get("med_designation", ""), "reg_number": data.get("med_reg_number", ""),
                        "signature": data.get("med_signature", "")
                    },
                    "sponsor": {
                        "sponsor_name": data.get("spo_sponsor_name", ""), "sponsor_email": data.get("spo_sponsor_email", ""),
                        "sponsor_telephone": data.get("spo_sponsor_phone", ""), "signature": data.get("spo_signature", "")
                    },
                    "commissioner": {
                        "name": data.get("comm_name", ""), "signature": data.get("comm_signature", "")
                    }
                })
                return jsonify({"success": True})
            except Exception as e:
                log.error(f"Settings save error: {e}")
                return jsonify({"success": False, "error": str(e)}), 500
        return render_template("admin_settings.html", settings=get_admin_settings())

    @app.route("/admin/stamps", methods=["GET", "POST"])
    @admin_required
    def admin_stamps():
        if request.method == "POST":
            stamp_type = request.form.get("stamp_type")
            if not stamp_type:
                return jsonify({"error": "Stamp type required"}), 400
            if "stamp_image" not in request.files:
                return jsonify({"error": "No image file provided"}), 400
            file = request.files["stamp_image"]
            if file.filename == "":
                return jsonify({"error": "No image selected"}), 400
            allowed = {"png", "jpg", "jpeg", "gif", "webp"}
            if not any(file.filename.lower().endswith(ext) for ext in allowed):
                return jsonify({"error": "Invalid file type"}), 400
            file_path = os.path.join(STAMPS_DIR, f"{stamp_type}.png")
            file.save(file_path)
            _stamp_image_cache.clear()
            return jsonify({"success": True, "message": f"Stamp {stamp_type} uploaded successfully!"})

        stamps = []
        if os.path.exists(STAMPS_DIR):
            for f in os.listdir(STAMPS_DIR):
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                    stamps.append({"type": os.path.splitext(f)[0], "filename": f, "path": f"/static/stamps/{f}", "exists": True})

        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.args.get("format") == "json":
            return jsonify(stamps)
        return render_template("admin_stamps.html", stamps=stamps, stamp_positions=STAMP_POSITIONS)

    @app.route("/admin/stamps/delete/<stamp_type>", methods=["DELETE"])
    @admin_required
    def admin_delete_stamp(stamp_type):
        try:
            for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]:
                fp = os.path.join(STAMPS_DIR, f"{stamp_type}{ext}")
                if os.path.exists(fp):
                    os.remove(fp)
                    _stamp_image_cache.clear()
                    return jsonify({"success": True})
            return jsonify({"error": "Stamp not found"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/admin/api/stamps")
    @admin_required
    def admin_api_stamps():
        stamps = []
        if os.path.exists(STAMPS_DIR):
            for f in os.listdir(STAMPS_DIR):
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                    stamps.append({"type": os.path.splitext(f)[0], "filename": f, "path": f"/static/stamps/{f}", "exists": True})
        return jsonify(stamps)

    @app.route("/admin/get_stats")
    @admin_required
    def admin_get_stats():
        records = get_all_user_documents()
        total = len(records)
        paid = sum(1 for r in records if r.get("payment_status") == PaymentStatus.SUCCESS.value)
        total_revenue = sum(r.get("total_amount", 0) for r in records if r.get("payment_status") == PaymentStatus.SUCCESS.value)
        return jsonify({"total_bundles": total, "paid_bundles": paid, "pending_bundles": total - paid, "total_revenue": total_revenue})

    @app.route("/admin/get_forms")
    @admin_required
    def admin_get_forms():
        records = get_all_user_documents()
        forms = []
        for r in records:
            created = r.get("created_at", "")
            doc_names = ", ".join([FORM_TYPE_DISPLAY.get(ft, ft) for ft in r.get("form_types", [])])
            forms.append({
                "_id": str(r.get("_id", "")), "bundle_id": r.get("bundle_id", ""),
                "created_at": created.strftime("%Y-%m-%d %H:%M:%S") if hasattr(created, "strftime") else str(created),
                "student_details": r.get("student_details", {}), "form_types": r.get("form_types", []),
                "documents": doc_names, "payment_status": r.get("payment_status", PaymentStatus.PENDING.value),
                "transaction_code": r.get("transaction_code", ""), "checkout_request_id": r.get("checkout_request_id", ""),
                "total_amount": r.get("total_amount", 0)
            })
        return jsonify(sorted(forms, key=lambda x: x["created_at"], reverse=True))

    @app.route("/admin/get_document_pdf/<bundle_id>")
    @admin_required
    def admin_get_document_pdf(bundle_id):
        record = get_user_document_by_bundle_id(bundle_id)
        if not record:
            return jsonify({"error": "Document not found"}), 404
        pdfs = record.get("pdfs", {})
        if not pdfs:
            return jsonify({"error": "PDFs not found"}), 404
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for ft, enc in pdfs.items():
                pdf_bytes = base64.b64decode(enc)
                _, _, dl_name = BUILDERS.get(ft, (None, None, f"{ft}.pdf"))
                zf.writestr(dl_name, pdf_bytes)
        zip_buffer.seek(0)
        return send_file(zip_buffer, mimetype="application/zip", as_attachment=True, download_name=f"documents_{bundle_id}.zip")

    @app.route("/admin/referral_codes", methods=["GET", "POST"])
    @admin_required
    def admin_referral_codes():
        if request.method == "POST":
            data = request.json or {}
            code = data.get("code", "").strip().upper()
            marketer = data.get("marketer_name", "").strip()
            discount = int(data.get("discount_per_doc", cfg.referral_discount_per_document))
            if not code or not marketer:
                return jsonify({"error": "Code and marketer name required"}), 400
            if create_referral_code(code, marketer, discount):
                return jsonify({"success": True, "message": f"Code {code} created."})
            return jsonify({"error": "Code already exists or creation failed."}), 400
        return jsonify(get_all_referral_codes())

    # ============================================================
    # HEALTH CHECK
    # ============================================================

    @app.route("/health")
    def health():
        db_ok, db_msg = db_manager.health_check()
        cache_ok, cache_msg = cache.health_check()
        status = {
            "status": "healthy" if (db_ok and cache_ok) else "degraded",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "database": {"connected": db_ok, "message": db_msg},
            "cache": {"connected": cache_ok, "message": cache_msg},
            "mpesa": "configured" if (cfg.mpesa_consumer_key and cfg.mpesa_passkey) else "missing",
            "brevo": "configured" if cfg.brevo_api_key else "missing",
            "environment": "production" if cfg.is_production else "development",
            "version": "2.0.0"
        }
        code = 200 if status["status"] == "healthy" else 503
        return jsonify(status), code

    # ============================================================
    # STATIC STAMPS
    # ============================================================

    @app.route("/static/stamps/<path:filename>")
    def serve_stamp(filename):
        return send_file(os.path.join(STAMPS_DIR, filename))

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
    log.info("SUPPORTING DOCUMENTS GENERATOR v2.1 — SESSION PERSISTENCE")
    log.info("=" * 60)
    log.info(f"Environment: {'PRODUCTION' if cfg.is_production else 'DEVELOPMENT'}")
    log.info(f"Database: {'MongoDB' if use_mongo else 'In-Memory (NOT FOR PRODUCTION)'}")
    log.info(f"Cache: {'Redis' if cache._redis else 'In-Memory'}")
    log.info(f"Session Store: {cfg.session_type}")
    log.info(f"Rate Limit: {cfg.rate_limit_per_minute}/min")
    log.info(f"Workers: {cfg.max_background_workers}")
    log.info(f"M-Pesa: {'CONFIGURED' if (cfg.mpesa_consumer_key and cfg.mpesa_passkey) else 'NOT CONFIGURED'}")
    log.info("=" * 60)

    return app


# ============================================================================
# ENTRY POINT
# ============================================================================

# Create the Flask application instance (available for Gunicorn)
app = create_app()

if __name__ == "__main__":
    cfg = Config.load()
    if cfg.is_production:
        print("WARNING: Do not use app.run() in production. Use Gunicorn with gevent workers.")
    app.run(debug=cfg.debug, host=cfg.host, port=cfg.port)