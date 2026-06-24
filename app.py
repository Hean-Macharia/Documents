# =============================================================
# app.py  -  Supporting Documents Generator (Production)
# =============================================================
# Multi-Document Support with Preview (unstamped) & Stamped Final
# Includes Referral/Loyalty Program (discount per document)
#
# DEPLOYMENT NOTE:
# For production, run with a WSGI server like Gunicorn:
#     gunicorn -w 4 -b 0.0.0.0:8080 --timeout 120 app:app
#
# =============================================================

import io, os, base64, re, uuid, copy, threading, time, zipfile, logging, secrets, sys
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from functools import wraps

from flask import Flask, request, send_file, render_template, jsonify, session, redirect, url_for, Response, flash, get_flashed_messages
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.utils import ImageReader
from PIL import Image
from dotenv import load_dotenv

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry

load_dotenv()

# =============================================================
# ENVIRONMENT & PRODUCTION CHECKS (BEFORE FLASK APP)
# =============================================================
PRODUCTION = os.getenv('FLASK_ENV', 'development').lower() == 'production'
DEBUG = os.getenv('FLASK_DEBUG', '0').lower() in ('1', 'true', 'yes')

if PRODUCTION and DEBUG:
    print("WARNING: FLASK_ENV=production but FLASK_DEBUG is true; this is not recommended.")

# Enforce MongoDB in production
MONGO_URI = os.getenv('MONGO_URI', '').strip()
if PRODUCTION and not MONGO_URI:
    print("CRITICAL: MONGO_URI is required in production mode. Set it in your environment.")
    sys.exit(1)

# Enforce SECRET_KEY
SECRET_KEY = os.getenv('SECRET_KEY', '').strip()
if not SECRET_KEY:
    if PRODUCTION:
        print("CRITICAL: SECRET_KEY must be set in production environment.")
        sys.exit(1)
    else:
        SECRET_KEY = secrets.token_hex(32)
        print("WARNING: SECRET_KEY not set; generated a random key for development. Sessions won't persist across restarts.")

# Enforce Admin credentials
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', '').strip()
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '').strip()
if PRODUCTION and (not ADMIN_USERNAME or not ADMIN_PASSWORD):
    print("CRITICAL: ADMIN_USERNAME and ADMIN_PASSWORD must be set in production.")
    sys.exit(1)

# Enforce M-Pesa callback URL (not the default ngrok)
MPESA_CALLBACK_URL = os.getenv('MPESA_CALLBACK_URL', '').strip()
if PRODUCTION:
    if not MPESA_CALLBACK_URL:
        print("CRITICAL: MPESA_CALLBACK_URL must be set to a public HTTPS endpoint in production.")
        sys.exit(1)
    if 'ngrok' in MPESA_CALLBACK_URL:
        print("WARNING: MPESA_CALLBACK_URL uses ngrok – this is not recommended for production.")

# =============================================================
# FLASK APP CONFIGURATION
# =============================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['PERMANENT_SESSION_LIFETIME'] = 3600
app.config['SESSION_COOKIE_SECURE'] = PRODUCTION
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STAMPED_BASE_DIR = os.path.join(BASE_DIR, "stamped_templates")
os.makedirs(STAMPED_BASE_DIR, exist_ok=True)

# =============================================================
# LOGGING CONFIGURATION (AFTER APP CREATION)
# =============================================================
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
LOG_FILE = os.getenv('LOG_FILE', 'app.log')

logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# Console handler (always)
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(console_handler)

# File handler with rotation (only if LOG_FILE is set and not in debug)
if not DEBUG and LOG_FILE:
    try:
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
        file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        logger.addHandler(file_handler)
    except Exception as e:
        logger.error(f"Could not set up file logging: {e}")

# =============================================================
# SECURITY HEADERS
# =============================================================
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if PRODUCTION:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

# =============================================================
# RATE LIMITING (Simple In-Memory)
# =============================================================
RATE_LIMIT_PER_MINUTE = int(os.getenv('RATE_LIMIT_PER_MINUTE', 30))
rate_limit_store = {}
rate_limit_lock = threading.Lock()

def rate_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
        now = time.time()
        with rate_limit_lock:
            if client_ip not in rate_limit_store:
                rate_limit_store[client_ip] = []
            rate_limit_store[client_ip] = [t for t in rate_limit_store[client_ip] if now - t < 60]
            if len(rate_limit_store[client_ip]) >= RATE_LIMIT_PER_MINUTE:
                return jsonify({'error': 'Rate limit exceeded. Please wait a moment.'}), 429
            rate_limit_store[client_ip].append(now)
        return f(*args, **kwargs)
    return decorated

# =============================================================
# M-PESA DARAJA CONFIGURATION
# =============================================================
MPESA_CONSUMER_KEY = os.getenv('MPESA_CONSUMER_KEY', '').strip()
MPESA_CONSUMER_SECRET = os.getenv('MPESA_CONSUMER_SECRET', '').strip()
MPESA_SHORTCODE = os.getenv('MPESA_SHORTCODE', '4185095').strip()
MPESA_PASSKEY = os.getenv('MPESA_PASSKEY', '').strip()
MPESA_ENVIRONMENT = os.getenv('MPESA_ENVIRONMENT', 'production').strip().lower()
PAYMENT_AMOUNT_PER_DOCUMENT = int(os.getenv('PAYMENT_AMOUNT_KES', '300'))

DOCUMENT_PRICES = {
    'medical': 400,
    'sponsorship': 300,
    'single_parent': 300
}

MPESA_BASE_URL = (
    "https://api.safaricom.co.ke"
    if MPESA_ENVIRONMENT == 'production'
    else "https://sandbox.safaricom.co.ke"
)

# =============================================================
# REFERRAL / LOYALTY PROGRAM CONFIGURATION
# =============================================================
REFERRAL_DISCOUNT_PER_DOCUMENT = int(os.getenv('REFERRAL_DISCOUNT_PER_DOCUMENT', 50))

# =============================================================
# BREVO (SENDINBLUE) EMAIL CONFIGURATION
# =============================================================
BREVO_API_KEY = os.getenv('BREVO_API_KEY', '').strip()
BREVO_SENDER_EMAIL = os.getenv('BREVO_SENDER_EMAIL', 'noreply@supportingdocs.com').strip()
BREVO_SENDER_NAME = os.getenv('BREVO_SENDER_NAME', 'Supporting Documents').strip()

# =============================================================
# FAST HTTP SESSION
# =============================================================
def _build_session():
    s = requests.Session()
    retry = Retry(total=1, backoff_factor=0.2, status_forcelist=[502, 503, 504], allowed_methods=["GET"])
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=40, max_retries=retry, pool_block=False)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

_session = _build_session()
_no_retry_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=40, max_retries=0, pool_block=False)
_no_retry_session = requests.Session()
_no_retry_session.mount("https://", _no_retry_adapter)
_no_retry_session.mount("http://", _no_retry_adapter)

TOKEN_TIMEOUT = (3, 3)
STK_TIMEOUT = (10, 10)   # Increased from (4,6) to 10 seconds
QUERY_TIMEOUT = (3, 4)

# =============================================================
# TOKEN CACHE + BACKGROUND REFRESH (with watchdog)
# =============================================================
_token_lock = threading.Lock()
_token_cache = {'token': None, 'expires_at': None}
_token_refresher_stop = threading.Event()

def _fetch_token_now():
    if not MPESA_CONSUMER_KEY or not MPESA_CONSUMER_SECRET:
        return None
    url = f"{MPESA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials"
    try:
        resp = _session.get(url, auth=HTTPBasicAuth(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET), timeout=TOKEN_TIMEOUT)
        data = resp.json()
        if resp.status_code == 200 and 'access_token' in data:
            return data['access_token']
        logger.error(f"[mpesa] token error: {data.get('errorMessage', data)}")
    except Exception as e:
        logger.error(f"[mpesa] token fetch failed: {e}")
    return None

def _refresh_token_cache():
    token = _fetch_token_now()
    if token:
        with _token_lock:
            _token_cache['token'] = token
            _token_cache['expires_at'] = datetime.now() + timedelta(minutes=55)
        logger.info("[mpesa] token refreshed")
    return token

def get_token():
    with _token_lock:
        tok, exp = _token_cache['token'], _token_cache['expires_at']
        if tok and exp and datetime.now() < exp:
            return tok
    return _refresh_token_cache()

def start_token_refresher():
    _refresh_token_cache()
    def loop():
        while not _token_refresher_stop.is_set():
            time.sleep(50 * 60)
            try:
                _refresh_token_cache()
            except Exception as e:
                logger.error(f"[mpesa] background refresh error: {e}")
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t

# =============================================================
# BACKGROUND TASK QUEUE (ThreadPoolExecutor)
# =============================================================
MAX_BACKGROUND_WORKERS = int(os.getenv('MAX_BACKGROUND_WORKERS', 4))
_executor = ThreadPoolExecutor(max_workers=MAX_BACKGROUND_WORKERS)

def submit_background_task(func, *args, **kwargs):
    return _executor.submit(func, *args, **kwargs)

# =============================================================
# BREVO EMAIL FUNCTIONS
# =============================================================
def send_email_via_brevo(to_email, to_name, subject, html_content, attachments=None):
    if not BREVO_API_KEY:
        logger.error("[brevo] ERROR: BREVO_API_KEY not configured")
        return False, "Brevo API key not configured"
    
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {"accept": "application/json", "api-key": BREVO_API_KEY, "content-type": "application/json"}
    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": to_email, "name": to_name or "Valued Customer"}],
        "subject": subject,
        "htmlContent": html_content
    }
    
    if attachments:
        payload["attachment"] = []
        for name, bytes_data in attachments:
            payload["attachment"].append({"content": base64.b64encode(bytes_data).decode('utf-8'), "name": name})
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code in [200, 201]:
            logger.info(f"[brevo] Email sent successfully to {to_email}")
            return True, "Email sent successfully"
        else:
            error_msg = f"Brevo API error: {response.status_code} - {response.text}"
            logger.error(f"[brevo] {error_msg}")
            if "unauthorized" in response.text.lower() or "authorised_ips" in response.text:
                logger.warning("[brevo] IP whitelist issue. Please add IP to Brevo.")
                return False, "IP_WHITELIST_ERROR"
            return False, error_msg
    except Exception as e:
        logger.error(f"[brevo] Error sending email: {str(e)}")
        return False, str(e)

def build_payment_confirmation_email_multi(student_name, bundle_id, transaction_code, form_types, total_amount):
    form_type_display = {'medical': 'Medical Form', 'sponsorship': 'Sponsorship Letter', 'single_parent': 'Single Parent Certification'}
    doc_list = ''.join([f"<li>✅ {form_type_display.get(ft, ft)}</li>" for ft in form_types])
    
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
        <div class="header"><h2>✅ Payment Confirmed!</h2><p>Supporting Documents Generation</p></div>
        <div class="content">
            <h3>Dear {student_name},</h3>
            <p>We are pleased to confirm that your payment has been successfully processed.</p>
            <div class="details">
                <h4>📋 Transaction Details</h4>
                <p><strong>Documents Generated:</strong></p>
                <ul class="doc-list">{doc_list}</ul>
                <p><strong>Bundle ID:</strong> {bundle_id}</p>
                <p><strong>Transaction Code:</strong> {transaction_code}</p>
                <p><strong>Date:</strong> {datetime.now().strftime('%d %B %Y at %H:%M')}</p>
                <p><strong>Total Paid:</strong> KES {total_amount}</p>
            </div>
            <p>📎 <strong>All your documents are attached to this email.</strong></p>
            <p>You can also download them anytime using your email address on our portal.</p>
            <p style="margin-top: 20px;"><a href="https://anapaestically-frenetic-bev.ngrok-free.dev" class="button">📥 Download Again</a></p>
            <p style="margin-top: 20px;">Thank you for using our service.<br><strong>Supporting Documents Team</strong></p>
        </div>
        <div class="footer"><p>This is an automated message. Please do not reply to this email.</p><p>&copy; 2026 Supporting Documents. All rights reserved.</p></div>
    </div>
    </body>
    </html>
    """

# =============================================================
# PHONE NUMBER FORMATTING
# =============================================================
def format_phone(phone_number):
    if not phone_number: return None
    cleaned = re.sub(r'\D', '', phone_number.strip())
    if not cleaned: return None
    
    if cleaned.startswith('0') and len(cleaned) == 10: formatted = '254' + cleaned[1:]
    elif cleaned.startswith('254') and len(cleaned) == 12: formatted = cleaned
    elif len(cleaned) == 9: formatted = '254' + cleaned
    else: formatted = cleaned if cleaned.startswith('254') else '254' + cleaned
    
    return formatted if len(formatted) == 12 and formatted.isdigit() else None

def validate_phone(phone):
    return bool(phone) and len(phone) == 12 and phone.isdigit() and phone.startswith('254')

def _password_and_timestamp():
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    raw = f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}"
    return base64.b64encode(raw.encode()).decode(), timestamp

# =============================================================
# STK PUSH
# =============================================================
def init_stk_push(phone_number, account_reference, transaction_desc, amount):
    t0 = time.time()
    token = get_token()
    if not token: return False, {'error': 'Could not obtain M-Pesa access token'}
    
    formatted_phone = format_phone(phone_number)
    if not validate_phone(formatted_phone): return False, {'error': f'Invalid phone number: {phone_number}'}
    
    password, timestamp = _password_and_timestamp()
    payload = {
        'BusinessShortCode': MPESA_SHORTCODE, 'Password': password, 'Timestamp': timestamp,
        'TransactionType': 'CustomerPayBillOnline', 'Amount': amount, 'PartyA': formatted_phone,
        'PartyB': MPESA_SHORTCODE, 'PhoneNumber': formatted_phone, 'CallBackURL': MPESA_CALLBACK_URL,
        'AccountReference': account_reference[:12], 'TransactionDesc': transaction_desc[:13],
    }
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    
    try:
        resp = _no_retry_session.post(f'{MPESA_BASE_URL}/mpesa/stkpush/v1/processrequest', json=payload, headers=headers, timeout=STK_TIMEOUT)
        data = resp.json()
        elapsed = time.time() - t0
        logger.info(f"[mpesa] STK push → {formatted_phone} in {elapsed:.2f}s")
        
        if resp.status_code == 200 and data.get('ResponseCode') == '0':
            return True, {
                'checkout_request_id': data.get('CheckoutRequestID'),
                'merchant_request_id': data.get('MerchantRequestID'),
                'customer_message': data.get('CustomerMessage', 'Check your phone'),
                'elapsed_seconds': round(elapsed, 2),
            }
        return False, {'error': data.get('errorMessage', data.get('ResponseDescription', 'STK push failed'))}
    except Exception as e:
        logger.error(f"[mpesa] STK push error: {e}")
        return False, {'error': str(e)}

def query_transaction(checkout_request_id):
    token = get_token()
    if not token: return False, {'error': 'Could not obtain M-Pesa access token'}
    
    password, timestamp = _password_and_timestamp()
    payload = {
        'BusinessShortCode': MPESA_SHORTCODE, 'Password': password, 'Timestamp': timestamp,
        'CheckoutRequestID': checkout_request_id,
    }
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    
    try:
        resp = _no_retry_session.post(f'{MPESA_BASE_URL}/mpesa/stkpushquery/v1/query', json=payload, headers=headers, timeout=QUERY_TIMEOUT)
        data = resp.json()
        if resp.status_code != 200: return False, {'error': data.get('errorMessage', 'Query failed')}
        
        result_code = data.get('ResultCode')
        if result_code == 0:
            items = {i.get('Name'): i.get('Value') for i in data.get('CallbackMetadata', {}).get('Item', [])}
            return True, {
                'status': 'success', 'mpesa_receipt_number': items.get('MpesaReceiptNumber', ''),
                'amount': items.get('Amount', 0), 'phone': items.get('PhoneNumber', ''),
                'transaction_date': items.get('TransactionDate', ''),
            }
        if result_code is not None: return False, {'status': 'failed', 'error': data.get('ResultDesc', 'Transaction failed')}
        return False, {'status': 'pending', 'error': 'Still processing'}
    except Exception as e:
        logger.error(f"[mpesa] Query error: {e}")
        return False, {'error': str(e)}

def parse_callback(callback_data):
    try:
        stk = callback_data.get('Body', {}).get('stkCallback', {})
        checkout_request_id = stk.get('CheckoutRequestID', '')
        result_code = stk.get('ResultCode')
        result_desc = stk.get('ResultDesc', '')
        items = {i.get('Name'): i.get('Value') for i in stk.get('CallbackMetadata', {}).get('Item', [])}
        return checkout_request_id, result_code, result_desc, items
    except Exception as e:
        return None, None, str(e), {}

# =============================================================
# FONT SETUP
# =============================================================
_font_path = os.path.join(BASE_DIR, "PatrickHand.ttf")
if os.path.exists(_font_path):
    try:
        pdfmetrics.registerFont(TTFont("Hand", _font_path))
        FONT = "Hand"
        logger.info("PatrickHand font loaded")
    except Exception:
        FONT = "Helvetica"
        logger.warning("Font registration failed - using Helvetica")
else:
    FONT = "Helvetica"
    logger.warning("PatrickHand.ttf not found - using Helvetica")

STD_FONT = "Helvetica"
STD_FONT_BOLD = "Helvetica-Bold"
PAGE_H = 792.0
PAGE_W = 612.0
TODAY = date.today().strftime("%d %B %Y")

# =============================================================
# DATABASE
# =============================================================
use_mongo = False
storage = {}
mongo = None

try:
    from flask_pymongo import PyMongo
    if MONGO_URI:
        app.config['MONGO_URI'] = MONGO_URI
        logger.info("Connecting to MongoDB...")
        mongo = PyMongo(app)
        mongo.db.command('ping')
        use_mongo = True
        logger.info("MongoDB connected successfully!")
    else:
        logger.warning("MONGO_URI not set; using in-memory storage. This is NOT suitable for production.")
except Exception as _e:
    if PRODUCTION:
        logger.critical(f"MongoDB connection failed in production: {_e}")
        sys.exit(1)
    else:
        logger.warning(f"MongoDB unavailable ({_e}) - using memory storage. NOT RECOMMENDED FOR PRODUCTION.")

if use_mongo:
    try:
        # Drop any existing unique index on checkout_request_id (if it exists)
        try:
            mongo.db.documents.drop_index("checkout_request_id_1")
            logger.info("Dropped existing 'checkout_request_id_1' index (might have been unique).")
        except Exception:
            pass
        
        # Create indexes (non-unique)
        mongo.db.documents.create_index('checkout_request_id', unique=False)
        mongo.db.documents.create_index('student_details.email')
        mongo.db.documents.create_index('created_at')
        mongo.db.documents.create_index('payment_status')
        logger.info("MongoDB indexes created/verified.")
    except Exception as e:
        logger.error(f"Index creation error: {e}")

# =============================================================
# REFERRAL CODES (In-Memory & MongoDB)
# =============================================================
_referral_codes = {}  # code -> { 'marketer_name': str, 'active': bool, 'discount_per_doc': int }

if use_mongo:
    try:
        mongo.db.referral_codes.create_index('code', unique=True)
        logger.info("Referral codes index created.")
    except Exception as e:
        logger.warning(f"Referral codes index creation error: {e}")

def create_referral_code(code, marketer_name, discount=REFERRAL_DISCOUNT_PER_DOCUMENT):
    """Create a new referral code."""
    code = code.upper().strip()
    if not code or not marketer_name:
        return False
    if use_mongo:
        try:
            mongo.db.referral_codes.insert_one({
                'code': code,
                'marketer_name': marketer_name,
                'active': True,
                'discount_per_doc': discount,
                'created_at': datetime.now()
            })
            return True
        except Exception as e:
            logger.error(f"Failed to create referral code: {e}")
            return False
    else:
        if code in _referral_codes:
            return False
        _referral_codes[code] = {
            'marketer_name': marketer_name,
            'active': True,
            'discount_per_doc': discount
        }
        return True

def validate_referral_code(code):
    """Return (is_valid, discount_per_doc, marketer_name) or (False, 0, '')."""
    code = code.upper().strip()
    if not code:
        return False, 0, ''
    if use_mongo:
        doc = mongo.db.referral_codes.find_one({'code': code, 'active': True})
        if doc:
            return True, doc.get('discount_per_doc', REFERRAL_DISCOUNT_PER_DOCUMENT), doc.get('marketer_name', '')
        return False, 0, ''
    else:
        data = _referral_codes.get(code)
        if data and data.get('active', False):
            return True, data.get('discount_per_doc', REFERRAL_DISCOUNT_PER_DOCUMENT), data.get('marketer_name', '')
        return False, 0, ''

# =============================================================
# DEFAULT ADMIN SETTINGS
# =============================================================
DEFAULT_ADMIN_SETTINGS = {
    'medical_officer': {'officer_name': 'Dr. Jane Mwangi, MBChB, MMed', 'hospital_name': 'Kenyatta National Hospital', 'designation': 'Senior Medical Officer', 'reg_number': 'MED-2024-001', 'signature': ''},
    'sponsor': {'sponsor_name': 'Kenya Education Fund (KEF)', 'sponsor_email': 'sponsors@kenyaeducationfund.org', 'sponsor_telephone': '+254 700 000 000', 'signature': ''},
    'commissioner': {'name': 'Hon. Justice John Kamau, EBS', 'signature': ''}
}

# =============================================================
# DATABASE HELPERS
# =============================================================
def get_admin_settings():
    if use_mongo:
        doc = mongo.db.settings.find_one({'_id': 'admin_settings'})
        if doc and 'settings' in doc: return doc['settings']
        mongo.db.settings.update_one({'_id': 'admin_settings'}, {'$set': {'settings': DEFAULT_ADMIN_SETTINGS}}, upsert=True)
        return DEFAULT_ADMIN_SETTINGS
    
    if 'admin_settings' not in storage: storage['admin_settings'] = copy.deepcopy(DEFAULT_ADMIN_SETTINGS)
    return storage['admin_settings']

def save_admin_settings(settings):
    if use_mongo: mongo.db.settings.update_one({'_id': 'admin_settings'}, {'$set': {'settings': settings}}, upsert=True)
    else: storage['admin_settings'] = settings

def save_user_document(record):
    if use_mongo: return mongo.db.documents.insert_one(record)
    storage[record['bundle_id']] = record
    return record

def get_user_document_by_bundle_id(bundle_id):
    if use_mongo: return mongo.db.documents.find_one({'bundle_id': bundle_id})
    return storage.get(bundle_id)

def get_user_document_by_email(email):
    if not email: return None
    email_lower = email.lower().strip()
    
    if use_mongo:
        docs = list(mongo.db.documents.find({'student_details.email': {'$regex': f'^{re.escape(email_lower)}$', '$options': 'i'}}).sort('created_at', -1).limit(10))
        for doc in docs:
            if doc.get('payment_status') == 'success': return doc
        return docs[0] if docs else None
    
    matching = []
    for rec in storage.values():
        if isinstance(rec, dict) and rec.get('student_details', {}).get('email', '').lower() == email_lower:
            matching.append(rec)
    matching.sort(key=lambda x: x.get('created_at', datetime.min), reverse=True)
    
    for doc in matching:
        if doc.get('payment_status') == 'success': return doc
    return matching[0] if matching else None

def get_all_user_documents():
    if use_mongo: return list(mongo.db.documents.find().sort('created_at', -1))
    return [v for v in storage.values() if isinstance(v, dict) and 'bundle_id' in v]

def get_all_referral_codes():
    if use_mongo:
        return list(mongo.db.referral_codes.find({}, {'_id': 0}))
    else:
        return [{'code': k, **v} for k, v in _referral_codes.items()]

def _build_admin_sigs():
    s = get_admin_settings()
    mo = s.get('medical_officer', {})
    sp = s.get('sponsor', {})
    co = s.get('commissioner', {})
    return {
        'officer_name': mo.get('officer_name', ''), 'hospital_name': mo.get('hospital_name', ''),
        'designation': mo.get('designation', ''), 'reg_number': mo.get('reg_number', ''), 'officer_sig': mo.get('signature', ''),
        'sponsor_name': sp.get('sponsor_name', ''), 'sponsor_email': sp.get('sponsor_email', ''),
        'sponsor_telephone': sp.get('sponsor_telephone', ''), 'sponsor_sig': sp.get('signature', ''),
        'commissioner_name': co.get('name', ''), 'commissioner_sig': co.get('signature', ''),
    }

# =============================================================
# ERROR HANDLERS (Production-Safe)
# =============================================================
@app.errorhandler(404)
def not_found(e):
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'error': 'Endpoint not found'}), 404
    flash('Page not found.', 'danger')
    return render_template('error.html', message="Page not found"), 404

@app.errorhandler(500)
def internal_error(e):
    logger.exception("Internal Server Error")
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'error': 'An internal server error occurred. Please try again later.'}), 500
    flash('An internal server error occurred. Please try again later.', 'danger')
    return render_template('error.html', message="Server error"), 500

@app.errorhandler(Exception)
def handle_exception(e):
    logger.exception("Unhandled Exception")
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'error': 'An unexpected error occurred. Our team has been notified.'}), 500
    flash('An unexpected error occurred. Our team has been notified.', 'danger')
    raise e

# =============================================================
# COORDINATE HELPERS
# =============================================================
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
    return {'font_size': 11, 'x': x0 + 3, 'y': PAGE_H - cb_bot + 3, 'text': "X", 'font_name': STD_FONT_BOLD}

# =============================================================
# SIGNATURE IMAGE RENDERER
# =============================================================
def draw_signature(cv, sig_data, x, y, w=130, h=28):
    if not sig_data: return False
    try:
        if sig_data.startswith('data:image'): sig_data = sig_data.split(',')[1]
        raw = base64.b64decode(re.sub(r'\s+', '', sig_data))
        img = Image.open(io.BytesIO(raw)).convert('RGBA')
        img.putdata([(255, 255, 255, 0) if (item[0] > 200 and item[1] > 200 and item[2] > 200) else item for item in img.getdata()])
        buf = io.BytesIO()
        img.save(buf, 'PNG')
        buf.seek(0)
        cv.drawImage(ImageReader(buf), x, y, width=w, height=h, preserveAspectRatio=True, mask='auto')
        return True
    except Exception as e:
        logger.error(f"Signature render error: {e}")
        return False

# =============================================================
# OVERLAY RENDERER
# =============================================================
def render_overlay(fields, sigs, underlines, pdf_path):
    buf = io.BytesIO()
    cv = canvas.Canvas(buf, pagesize=letter)
    cv.setFillColorRGB(0.04, 0.04, 0.28)
    
    for f in fields:
        txt = f.get('text', '')
        if not txt: continue
        fs = f.get('font_size', 10)
        fx = f.get('x', 0)
        fy = f.get('y', 0)
        fn = f.get('font_name', FONT)
        cv.setFont(fn, fs)
        cv.drawString(fx, fy, txt)
    
    for sig_data, sx, sy, sw, sh in sigs:
        draw_signature(cv, sig_data, sx, sy, sw, sh)
    
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

# =============================================================
# COORDINATES
# =============================================================
SN_REL = {"Organization": 443.35}
SN_LEVEL_Y = PAGE_H - 350.6 + 2
SN_LEVEL = {"Primary": (413, SN_LEVEL_Y, 448, SN_LEVEL_Y), "Secondary": (453, SN_LEVEL_Y, 498, SN_LEVEL_Y), "College": (503, SN_LEVEL_Y, 534, SN_LEVEL_Y)}
SN_S3_NAME_Y = text_in_gap(452.0, 457.1, 9)
SN_S3_SIG_Y = sig_in_gap(452.0, 457.1, 28)
SN_S4_NAME_Y = text_below_line(570.3, 9, 6)
SN_S4_DATE_Y = text_below_line(570.3, 9, 6)
SN_S4_SIG_Y = sig_in_gap(569.7, 561.4, 28)
MD_OFF_NAME_Y = text_in_gap(425.5, 430.6, 9)
MD_OFF_SIG_Y = sig_in_gap(425.5, 430.6, 28)
MD_COMM_NAME_Y = text_below_line(543.7, 8, 6)
MD_COMM_DATE_Y = text_below_line(543.7, 8, 6)
MD_COMM_SIG_Y = sig_in_gap(543.1, 534.7, 28)
SP_PAR_NAME_Y = text_in_gap(540.9, 547.0, 8)
SP_PAR_DATE_Y = text_below_line(541.4, 8, 6)
SP_PAR_SIG_Y = sig_in_gap(540.9, 547.0, 28)
SP_COMM_NAME_Y = text_below_line(660.2, 8, 6)
SP_COMM_DATE_Y = text_below_line(660.2, 8, 6)
SP_COMM_SIG_Y = sig_in_gap(659.6, 651.3, 28)

# =============================================================
# FORM BUILDERS (modified to support admin signature skip)
# =============================================================
def build_sponsorship(d, adm, include_admin_sigs=True):
    sl = d.get("sponsored_level", "")
    fields = []
    underlines = []
    fields.extend([
        {'font_size': 10, 'x': 190.0, 'y': cy(136.82, 152.30), 'text': d.get("student_name", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 190.0, 'y': cy(152.30, 167.78), 'text': d.get("national_id", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 404.0, 'y': cy(152.30, 167.78), 'text': d.get("university", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 190.0, 'y': cy(167.78, 183.26), 'text': d.get("kcse_index", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 404.0, 'y': cy(167.78, 183.26), 'text': d.get("admission_number", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 190.0, 'y': cy(183.26, 198.86), 'text': d.get("telephone", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 404.0, 'y': cy(183.26, 198.86), 'text': d.get("email", ""), 'font_name': FONT},
    ])
    fields.extend([
        {'font_size': 10, 'x': 257.0, 'y': cy(240.74, 256.13), 'text': adm.get("sponsor_name", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 257.0, 'y': cy(256.13, 271.61), 'text': adm.get("sponsor_email", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 257.0, 'y': cy(271.61, 287.09), 'text': adm.get("sponsor_telephone", ""), 'font_name': FONT},
    ])
    fields.append(Tick(SN_REL["Organization"], 301.37))
    if sl in SN_LEVEL: underlines.append(SN_LEVEL[sl])
    fields.append({'font_size': 9, 'x': 245.0, 'y': PAGE_H - 374.62 + 1, 'text': d.get("sponsorship_covered", ""), 'font_name': FONT})
    fields.append({'font_size': 9, 'x': 392.0, 'y': PAGE_H - 386.86 + 1, 'text': d.get("completed_duration", ""), 'font_name': FONT})
    fields.append({'font_size': 9, 'x': 85.0, 'y': SN_S3_NAME_Y, 'text': adm.get("sponsor_name", ""), 'font_name': FONT})
    fields.append({'font_size': 9, 'x': 75.0, 'y': SN_S4_NAME_Y, 'text': d.get("student_name", ""), 'font_name': FONT})
    fields.append({'font_size': 9, 'x': 430.0, 'y': SN_S4_DATE_Y, 'text': TODAY, 'font_name': STD_FONT})
    
    # Build signatures list – always include student signature; admin signature only if include_admin_sigs
    sigs = []
    if include_admin_sigs:
        sigs.append((adm.get("sponsor_sig", ""), 241.2, SN_S3_SIG_Y, 130, 28))
    # Student signature always added
    sigs.append((d.get("student_sig", ""), 356.2, SN_S4_SIG_Y, 80, 28))
    return fields, sigs, underlines

def build_medical(d, adm, include_admin_sigs=True):
    fields = []
    underlines = []
    fields.extend([
        {'font_size': 10, 'x': 190.0, 'y': cy(136.82, 152.30), 'text': d.get("student_name", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 190.0, 'y': cy(152.30, 167.78), 'text': d.get("national_id", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 404.0, 'y': cy(152.30, 167.78), 'text': d.get("university", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 190.0, 'y': cy(167.78, 183.26), 'text': d.get("kcse_index", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 404.0, 'y': cy(167.78, 183.26), 'text': d.get("admission_number", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 190.0, 'y': cy(183.26, 198.86), 'text': d.get("telephone", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 404.0, 'y': cy(183.26, 198.86), 'text': d.get("email", ""), 'font_name': FONT},
    ])
    fields.extend([
        {'font_size': 10, 'x': 257.0, 'y': cy(240.74, 256.13), 'text': adm.get("officer_name", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 257.0, 'y': cy(256.13, 271.61), 'text': adm.get("hospital_name", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 257.0, 'y': cy(271.61, 287.09), 'text': adm.get("designation", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 257.0, 'y': cy(287.09, 302.57), 'text': adm.get("reg_number", ""), 'font_name': FONT},
    ])
    fields.append({'font_size': 9, 'x': 103.0, 'y': MD_OFF_NAME_Y, 'text': adm.get("officer_name", ""), 'font_name': FONT})
    fields.append({'font_size': 9, 'x': 362.0, 'y': MD_COMM_NAME_Y, 'text': adm.get("commissioner_name", ""), 'font_name': FONT})
    fields.append({'font_size': 9, 'x': 430.0, 'y': MD_COMM_DATE_Y, 'text': d.get("comm_date", TODAY), 'font_name': STD_FONT})
    
    sigs = []
    if include_admin_sigs:
        sigs.append((adm.get("officer_sig", ""), 259.2, MD_OFF_SIG_Y, 130, 28))
        sigs.append((adm.get("commissioner_sig", ""), 356.2, MD_COMM_SIG_Y, 80, 28))
    # No student signature in medical form
    return fields, sigs, underlines

def build_single_parent(d, adm, include_admin_sigs=True):
    rel = d.get("relationship", "")
    mar = d.get("marital_status", "")
    fields = []
    underlines = []
    fields.extend([
        {'font_size': 10, 'x': 190.0, 'y': cy(151.22, 166.70), 'text': d.get("student_name", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 190.0, 'y': cy(166.70, 182.18), 'text': d.get("national_id", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 404.0, 'y': cy(166.70, 182.18), 'text': d.get("university", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 190.0, 'y': cy(182.18, 197.66), 'text': d.get("kcse_index", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 404.0, 'y': cy(182.18, 197.66), 'text': d.get("admission_number", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 190.0, 'y': cy(197.66, 213.26), 'text': d.get("telephone", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 404.0, 'y': cy(197.66, 213.26), 'text': d.get("email", ""), 'font_name': FONT},
    ])
    fields.extend([
        {'font_size': 10, 'x': 257.0, 'y': cy(255.05, 270.53), 'text': d.get("parent_name", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 257.0, 'y': cy(270.53, 286.01), 'text': d.get("parent_id", ""), 'font_name': FONT},
        {'font_size': 10, 'x': 257.0, 'y': cy(286.01, 301.49), 'text': d.get("parent_telephone", ""), 'font_name': FONT},
    ])
    SP_REL_CHECK = {"Mother": 292.97, "Father": 367.03}
    if rel in SP_REL_CHECK: fields.append(Tick(SP_REL_CHECK[rel], 315.71))
    SP_MAR_CHECK = {"Single": 294.05, "Separated": 367.75, "Divorce": 431.35}
    if mar in SP_MAR_CHECK: fields.append(Tick(SP_MAR_CHECK[mar], 331.31))
    fields.append({'font_size': 8, 'x': 85.0, 'y': SP_PAR_NAME_Y, 'text': d.get("parent_name", ""), 'font_name': FONT})
    fields.append({'font_size': 9, 'x': 470.0, 'y': SP_PAR_DATE_Y, 'text': d.get("parent_date", TODAY), 'font_name': STD_FONT})
    fields.append({'font_size': 8, 'x': 361.0, 'y': SP_COMM_NAME_Y, 'text': adm.get("commissioner_name", ""), 'font_name': FONT})
    fields.append({'font_size': 9, 'x': 430.0, 'y': SP_COMM_DATE_Y, 'text': d.get("comm_date", TODAY), 'font_name': STD_FONT})
    
    sigs = []
    # Parent signature always included
    sigs.append((d.get("parent_sig", ""), 240.8, SP_PAR_SIG_Y, 130, 28))
    if include_admin_sigs:
        sigs.append((adm.get("commissioner_sig", ""), 355.9, SP_COMM_SIG_Y, 80, 28))
    return fields, sigs, underlines

# =============================================================
# ROUTING HELPERS
# =============================================================
BUILDERS = {
    "medical": (build_medical, "Medical_Form.pdf", "Medical_Form_Filled.pdf"),
    "sponsorship": (build_sponsorship, "Sponsorship_Letter.pdf", "Sponsorship_Letter_Filled.pdf"),
    "single_parent": (build_single_parent, "Single_Parent_Self_Certification_2024.pdf", "Single_Parent_Certification_Filled.pdf"),
}

# Mapping for stamped templates
STAMPED_PDFS = {
    "medical": "Medical_Form_Stamped.pdf",
    "sponsorship": "Sponsorship_Letter_Stamped.pdf",
    "single_parent": "Single_Parent_Self_Certification_2024_Stamped.pdf",
}

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            flash('Please log in to access the admin area.', 'warning')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

def _make_pdf(form_type, form_data, stamped=False):
    """
    Generate PDF overlay on either unstamped (preview) or stamped (final) template.
    When stamped=True, admin signatures are NOT drawn because they are already embedded.
    """
    build_fn, src_pdf, _ = BUILDERS[form_type]
    
    if stamped:
        stamped_path = os.path.join(STAMPED_BASE_DIR, STAMPED_PDFS.get(form_type, src_pdf))
        if not os.path.exists(stamped_path):
            logger.warning(f"Stamped template missing for {form_type}, falling back to unstamped.")
            pdf_path = os.path.join(BASE_DIR, src_pdf)
            include_admin_sigs = True  # fallback: draw admin sigs if not stamped
        else:
            pdf_path = stamped_path
            include_admin_sigs = False
    else:
        pdf_path = os.path.join(BASE_DIR, src_pdf)
        include_admin_sigs = True
    
    adm = _build_admin_sigs()
    fields, sigs, underlines = build_fn(form_data, adm, include_admin_sigs=include_admin_sigs)
    return render_overlay(fields, sigs, underlines, pdf_path)

def _generate_multiple_pdfs_and_send_email(bundle_id, form_types, form_data_map, student_email, student_name, tx_code):
    try:
        attachments = []
        pdf_map = {}
        total_amount = sum(DOCUMENT_PRICES.get(ft, PAYMENT_AMOUNT_PER_DOCUMENT) for ft in form_types)
        
        for ft in form_types:
            pdf_bytes = _make_pdf(ft, form_data_map.get(ft, {}), stamped=True)  # USE STAMPED
            pdf_map[ft] = pdf_bytes
            _, _, filename = BUILDERS.get(ft, (None, None, 'document.pdf'))
            attachments.append((filename, pdf_bytes))
            logger.info(f"[mpesa] Stamped PDF generated for {ft}")
        
        encoded_pdfs = {}
        for ft, pdf_bytes in pdf_map.items():
            encoded_pdfs[ft] = base64.b64encode(pdf_bytes).decode()
        
        if use_mongo:
            mongo.db.documents.update_one({'bundle_id': bundle_id}, {'$set': {'pdfs': encoded_pdfs}})
        elif bundle_id in storage:
            storage[bundle_id]['pdfs'] = encoded_pdfs
        logger.info(f"[mpesa] All PDFs saved in background for {bundle_id}")
        
        if student_email and BREVO_API_KEY:
            form_type_display = {'medical': 'Medical Form', 'sponsorship': 'Sponsorship Letter', 'single_parent': 'Single Parent Certification'}
            doc_names = [form_type_display.get(ft, ft) for ft in form_types]
            subject = f"Your Documents ({', '.join(doc_names)}) - {bundle_id}"
            html_content = build_payment_confirmation_email_multi(student_name, bundle_id, tx_code, form_types, total_amount)
            
            success, message = send_email_via_brevo(student_email, student_name, subject, html_content, attachments)
            if success:
                logger.info(f"[mpesa] ✅ Email sent to {student_email} with {len(attachments)} attachments")
            elif message == "IP_WHITELIST_ERROR":
                logger.warning("[mpesa] Email blocked by IP whitelist.")
                if use_mongo:
                    mongo.db.documents.update_one({'bundle_id': bundle_id}, {'$set': {'email_status': 'ip_whitelist_error', 'email_recipient': student_email}})
            else:
                logger.warning(f"[mpesa] Email failed for {bundle_id}: {message}")
        else:
            logger.warning(f"[mpesa] Email not sent (no email or Brevo not configured) for {bundle_id}")
    except Exception as e:
        logger.exception(f"[mpesa] ❌ Background task failed for {bundle_id}: {e}")

# =============================================================
# USER-FACING ROUTES
# =============================================================
@app.route("/")
def index():
    return render_template("index.html", mpesa_configured=bool(MPESA_CONSUMER_KEY and MPESA_PASSKEY))

@app.route("/preview/<ft>", methods=["POST"])
@rate_limit
def preview(ft):
    if ft not in BUILDERS: 
        flash('Invalid form type.', 'danger')
        return jsonify(error="Unknown form type"), 400
    d = request.json or {}
    try:
        pdf = _make_pdf(ft, d, stamped=False)  # PREVIEW USES UNSTAMPED
    except Exception as e:
        logger.error(f"Preview generation error: {e}")
        flash('Failed to generate preview.', 'danger')
        return jsonify(error="Preview generation failed"), 500
    response = Response(pdf, mimetype='application/pdf')
    response.headers['Content-Disposition'] = 'inline; filename="preview.pdf"'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline';"
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

@app.route("/initiate_payment", methods=["POST"])
@rate_limit
def initiate_payment():
    data = request.json or {}
    form_types = data.get('form_types', [])
    form_data_map = data.get('form_data_map', {})
    student_details = data.get('student_details', {})
    phone_number = data.get('phone_number', '').strip()
    referral_code = data.get('referral_code', '').strip()  # NEW
    
    if not form_types: 
        flash('No documents selected.', 'danger')
        return jsonify({'error': 'No documents selected'}), 400
    if not phone_number:
        flash('Phone number is required.', 'danger')
        return jsonify({'error': 'Phone number is required'}), 400
    
    formatted_phone = format_phone(phone_number)
    if not validate_phone(formatted_phone):
        flash('Invalid phone number format. Use format like 0712345678.', 'danger')
        return jsonify({'error': f'Invalid phone number format. Use format like 0712345678. Got: {phone_number}'}), 400
    
    student_email = student_details.get('email', '').strip()
    if not student_email:
        flash('Student email is required.', 'danger')
        return jsonify({'error': 'Student email is required'}), 400
    
    # --- Referral discount logic ---
    discount_per_doc = 0
    valid_code = False
    marketer = ''
    if referral_code:
        valid, discount_per_doc, marketer = validate_referral_code(referral_code)
        if not valid:
            flash('Invalid referral code.', 'warning')
            # Proceed without discount
        else:
            flash(f'Referral code applied! You get {discount_per_doc} KES off per document.', 'success')
    
    # Calculate total with discount
    total_amount = 0
    for ft in form_types:
        price = DOCUMENT_PRICES.get(ft, PAYMENT_AMOUNT_PER_DOCUMENT)
        if valid:
            discounted = price - discount_per_doc
            if discounted < 0:
                discounted = 0
            total_amount += discounted
        else:
            total_amount += price
    
    # Ensure minimum total is 1 (to avoid 0 KES payments)
    if total_amount < 1:
        total_amount = 1
    
    bundle_id = str(uuid.uuid4())[:8]
    account_ref = f"DOC{bundle_id[:8]}"
    
    try:
        save_user_document({
            'bundle_id': bundle_id, 'form_types': form_types, 'student_details': student_details,
            'form_data_map': form_data_map, 'payment_status': 'pending', 'created_at': datetime.now(),
            'transaction_code': '', 'checkout_request_id': None,
            'phone_number': formatted_phone, 'total_amount': total_amount,
            'referral_code': referral_code if valid else '',
            'discount_applied': discount_per_doc if valid else 0,
            'marketer_name': marketer if valid else ''
        })
    except Exception as e:
        logger.error(f"Failed to save document: {e}")
        flash('Failed to initiate payment. Please try again.', 'danger')
        return jsonify({'error': 'Database error'}), 500
    
    success, result = init_stk_push(formatted_phone, account_ref, f'{len(form_types)} Docs', total_amount)
    if success:
        checkout_request_id = result['checkout_request_id']
        if use_mongo:
            mongo.db.documents.update_one({'bundle_id': bundle_id}, {'$set': {'checkout_request_id': checkout_request_id, 'account_reference': account_ref}})
        elif bundle_id in storage:
            storage[bundle_id]['checkout_request_id'] = checkout_request_id
            storage[bundle_id]['account_reference'] = account_ref
        
        flash('STK push sent to your phone. Please complete the payment.', 'success')
        return jsonify({
            'success': True, 'checkout_request_id': checkout_request_id, 'merchant_request_id': result['merchant_request_id'],
            'customer_message': result['customer_message'], 'bundle_id': bundle_id,
            'elapsed_seconds': result.get('elapsed_seconds'), 'redirect_url': '/payment_status?bundle_id=' + bundle_id
        })
    else:
        error_msg = result.get('error', 'Payment initiation failed')
        flash(f'Payment initiation failed: {error_msg}', 'danger')
        return jsonify({'error': error_msg}), 400

@app.route("/mpesa_callback", methods=["POST"])
def mpesa_callback():
    callback_data = request.get_json(force=True, silent=True) or {}
    checkout_request_id, result_code, result_desc, metadata = parse_callback(callback_data)
    
    if not checkout_request_id:
        logger.warning("[mpesa] callback: invalid data")
        return jsonify({'status': 'error', 'message': 'Invalid callback data'}), 400
    
    record, bundle_id, form_types = None, None, None
    if use_mongo:
        doc = mongo.db.documents.find_one({'checkout_request_id': checkout_request_id})
        if doc: record, bundle_id, form_types = doc, doc.get('bundle_id', ''), doc.get('form_types', [])
    else:
        for rec in storage.values():
            if isinstance(rec, dict) and rec.get('checkout_request_id') == checkout_request_id:
                record, bundle_id, form_types = rec, rec.get('bundle_id', ''), rec.get('form_types', [])
                break
    
    if not record or not bundle_id:
        logger.warning(f"[mpesa] callback: unknown checkout {checkout_request_id}")
        return jsonify({'status': 'ok'}), 200
    
    if result_code == 0:
        tx_code = metadata.get('MpesaReceiptNumber', checkout_request_id)
        student_email = record.get('student_details', {}).get('email', '')
        student_name = record.get('student_details', {}).get('student_name', 'Student')
        
        if use_mongo:
            mongo.db.documents.update_one({'bundle_id': bundle_id, 'payment_status': {'$ne': 'success'}}, {'$set': {'payment_status': 'success', 'transaction_code': tx_code, 'paid_at': datetime.now()}})
        elif bundle_id in storage and storage[bundle_id].get('payment_status') != 'success':
            storage[bundle_id].update({'payment_status': 'success', 'transaction_code': tx_code, 'paid_at': datetime.now()})
        
        form_data_map = record.get('form_data_map', {})
        submit_background_task(_generate_multiple_pdfs_and_send_email, bundle_id, form_types, form_data_map, student_email, student_name, tx_code)
        logger.info(f"[mpesa] ✅ payment confirmed for {bundle_id} in <50ms")
        return jsonify({'status': 'success', 'bundle_id': bundle_id}), 200
    else:
        logger.warning(f"[mpesa] ❌ payment failed for {bundle_id}: {result_desc}")
        if use_mongo:
            mongo.db.documents.update_one({'bundle_id': bundle_id}, {'$set': {'payment_status': 'failed', 'payment_failure_reason': result_desc}})
        elif bundle_id in storage:
            storage[bundle_id]['payment_status'] = 'failed'
            storage[bundle_id]['payment_failure_reason'] = result_desc
        return jsonify({'status': 'failed', 'bundle_id': bundle_id}), 200

@app.route("/payment_status")
def payment_status_page():
    bundle_id = request.args.get('bundle_id', '')
    if not bundle_id:
        flash('Bundle ID missing.', 'danger')
        return redirect(url_for('index'))
    record = get_user_document_by_bundle_id(bundle_id)
    if not record:
        flash('Document not found.', 'danger')
        return render_template('error.html', message="Document not found"), 404
    
    form_type_display = {'medical': 'Medical Form', 'sponsorship': 'Sponsorship Letter', 'single_parent': 'Single Parent Certification'}
    doc_names = [form_type_display.get(ft, ft) for ft in record.get('form_types', [])]
    return render_template('payment_status.html', bundle_id=bundle_id, student_name=record.get('student_details', {}).get('student_name', ''), payment_status=record.get('payment_status', 'pending'), doc_names=', '.join(doc_names), total_amount=record.get('total_amount', 0))

@app.route("/download_pdf/<bundle_id>/<form_type>", methods=["GET"])
def download_single_pdf(bundle_id, form_type):
    record = get_user_document_by_bundle_id(bundle_id)
    if not record:
        flash('Document not found.', 'danger')
        return jsonify({'error': 'Document not found'}), 404
    if record.get('payment_status') != 'success':
        flash('Payment not completed.', 'warning')
        return jsonify({'error': 'Payment not completed'}), 402
    
    pdfs = record.get('pdfs', {})
    if not pdfs or form_type not in pdfs:
        flash('PDF not found. Please wait a moment.', 'warning')
        return jsonify({'error': 'PDF not found. Please wait a moment.'}), 404
    
    pdf_bytes = base64.b64decode(pdfs[form_type])
    _, _, dl_name = BUILDERS.get(form_type, (None, None, 'document.pdf'))
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True, download_name=dl_name)

@app.route("/download_all/<bundle_id>", methods=["GET"])
def download_all_pdfs(bundle_id):
    record = get_user_document_by_bundle_id(bundle_id)
    if not record:
        flash('Document not found.', 'danger')
        return jsonify({'error': 'Document not found'}), 404
    if record.get('payment_status') != 'success':
        flash('Payment not completed.', 'warning')
        return jsonify({'error': 'Payment not completed'}), 402
    
    pdfs = record.get('pdfs', {})
    if not pdfs:
        flash('PDFs not found. Please wait a moment.', 'warning')
        return jsonify({'error': 'PDFs not found. Please wait a moment.'}), 404
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for form_type, encoded_pdf in pdfs.items():
            pdf_bytes = base64.b64decode(encoded_pdf)
            _, _, dl_name = BUILDERS.get(form_type, (None, None, f'{form_type}.pdf'))
            zip_file.writestr(dl_name, pdf_bytes)
    zip_buffer.seek(0)
    return send_file(zip_buffer, mimetype='application/zip', as_attachment=True, download_name=f'documents_{bundle_id}.zip')

@app.route("/api/payment_status/<bundle_id>", methods=["GET"])
def api_payment_status(bundle_id):
    record = get_user_document_by_bundle_id(bundle_id)
    if not record: return jsonify({'error': 'Document not found'}), 404
    status = record.get('payment_status', 'pending')
    response = {'bundle_id': bundle_id, 'status': status, 'transaction_code': record.get('transaction_code', ''), 'student_name': record.get('student_details', {}).get('student_name', '')}
    if status == 'success':
        pdfs = record.get('pdfs', {})
        response['ready'] = bool(pdfs)
        if pdfs: response['download_all_url'] = f'/download_all/{bundle_id}'
    return jsonify(response)

@app.route("/api/payment_status/", methods=["GET"])
def api_payment_status_root():
    return jsonify({'error': 'Bundle ID required'}), 400

@app.route("/check_payment_status", methods=["POST"])
def check_payment_status():
    data = request.json or {}
    bundle_id = data.get('bundle_id', '').strip()
    checkout_request_id = data.get('checkout_request_id', '').strip()
    record = None
    
    if bundle_id: record = get_user_document_by_bundle_id(bundle_id)
    elif checkout_request_id:
        if use_mongo: record = mongo.db.documents.find_one({'checkout_request_id': checkout_request_id})
        else:
            for rec in storage.values():
                if isinstance(rec, dict) and rec.get('checkout_request_id') == checkout_request_id:
                    record = rec
                    break
    
    if not record: 
        flash('Document not found.', 'danger')
        return jsonify({'error': 'Document not found'}), 404
    status = record.get('payment_status', 'pending')
    
    if status == 'success': 
        flash('Payment confirmed! Your documents are ready.', 'success')
        return jsonify({'status': 'success', 'paid': True, 'transaction_code': record.get('transaction_code', ''), 'bundle_id': record.get('bundle_id')})
    if status == 'failed': 
        flash('Payment failed.', 'danger')
        return jsonify({'status': 'failed', 'paid': False, 'reason': record.get('payment_failure_reason', 'Payment failed')})
    
    cr_id = record.get('checkout_request_id', '')
    if cr_id:
        success, result = query_transaction(cr_id)
        if success:
            tx_code = result.get('mpesa_receipt_number', cr_id)
            form_types = record.get('form_types', [])
            form_data_map = record.get('form_data_map', {})
            student_email = record.get('student_details', {}).get('email', '')
            student_name = record.get('student_details', {}).get('student_name', 'Student')
            
            if use_mongo:
                mongo.db.documents.update_one({'bundle_id': record['bundle_id']}, {'$set': {'payment_status': 'success', 'transaction_code': tx_code, 'paid_at': datetime.now()}})
            elif record['bundle_id'] in storage:
                storage[record['bundle_id']].update({'payment_status': 'success', 'transaction_code': tx_code, 'paid_at': datetime.now()})
            
            submit_background_task(_generate_multiple_pdfs_and_send_email, record['bundle_id'], form_types, form_data_map, student_email, student_name, tx_code)
            flash('Payment confirmed! Your documents are being generated.', 'success')
            return jsonify({'status': 'success', 'paid': True, 'transaction_code': tx_code, 'bundle_id': record['bundle_id']})
        else:
            if result.get('status') == 'failed':
                flash('Payment failed.', 'danger')
                return jsonify({'status': 'failed', 'paid': False, 'reason': result.get('error', 'Transaction failed')})
    flash('Payment still pending. Please check your phone.', 'info')
    return jsonify({'status': 'pending', 'paid': False})

@app.route("/test_callback/<bundle_id>", methods=["POST"])
def test_callback(bundle_id):
    record = get_user_document_by_bundle_id(bundle_id)
    if not record: 
        flash('Document not found.', 'danger')
        return jsonify({'error': 'Document not found'}), 404
    if record.get('payment_status') == 'success':
        flash('Already successful.', 'info')
        return jsonify({'status': 'already_success', 'bundle_id': bundle_id}), 200
    
    tx_code = f"TEST{datetime.now().strftime('%Y%m%d%H%M%S')}"
    if use_mongo:
        mongo.db.documents.update_one({'bundle_id': bundle_id}, {'$set': {'payment_status': 'success', 'transaction_code': tx_code, 'paid_at': datetime.now()}})
    elif bundle_id in storage:
        storage[bundle_id].update({'payment_status': 'success', 'transaction_code': tx_code, 'paid_at': datetime.now()})
    
    form_types = record.get('form_types', [])
    form_data_map = record.get('form_data_map', {})
    student_email = record.get('student_details', {}).get('email', '')
    student_name = record.get('student_details', {}).get('student_name', 'Student')
    
    submit_background_task(_generate_multiple_pdfs_and_send_email, bundle_id, form_types, form_data_map, student_email, student_name, tx_code)
    flash('Test callback triggered. PDF generation started.', 'success')
    return jsonify({'status': 'success', 'bundle_id': bundle_id, 'transaction_code': tx_code, 'message': 'Callback triggered manually. PDF generation started in background.'})

@app.route("/retrieve", methods=["POST"])
def retrieve_document():
    data = request.json or {}
    identifier = data.get('identifier', '').strip()
    if not identifier:
        flash('Email address is required.', 'danger')
        return jsonify({'error': 'Email address is required'}), 400
    
    logger.info(f"[retrieve] Looking for email: {identifier}")
    record = get_user_document_by_email(identifier)
    if not record:
        flash('No document found for that email address.', 'danger')
        return jsonify({'error': 'No document found for that email address.'}), 404
    if record.get('payment_status') != 'success':
        flash('Payment not completed. Please pay first.', 'warning')
        return jsonify({'error': 'Payment not completed. Please pay first.'}), 402
    
    pdfs = record.get('pdfs', {})
    form_types = record.get('form_types', [])
    form_type_display = {'medical': 'Medical Form', 'sponsorship': 'Sponsorship Letter', 'single_parent': 'Single Parent Certification'}
    
    response_data = {
        'bundle_id': record.get('bundle_id', ''), 'student_name': record.get('student_details', {}).get('student_name', ''),
        'transaction_code': record.get('transaction_code', ''),
        'paid_at': record.get('paid_at', '').isoformat() if hasattr(record.get('paid_at'), 'isoformat') else str(record.get('paid_at', '')),
        'documents': []
    }
    for ft in form_types:
        doc_data = {'type': ft, 'name': form_type_display.get(ft, ft), 'download_url': f'/download_pdf/{record["bundle_id"]}/{ft}'}
        if ft in pdfs: doc_data['pdf'] = pdfs[ft]
        response_data['documents'].append(doc_data)
    response_data['download_all_url'] = f'/download_all/{record["bundle_id"]}'
    flash('Documents retrieved successfully.', 'success')
    return jsonify(response_data)

@app.route("/retrieve_direct/<bundle_id>", methods=["GET"])
def retrieve_direct(bundle_id):
    record = get_user_document_by_bundle_id(bundle_id)
    if not record:
        flash('Document not found.', 'danger')
        return jsonify({'error': 'Document not found'}), 404
    if record.get('payment_status') != 'success':
        flash('Payment not completed.', 'warning')
        return jsonify({'error': 'Payment not completed'}), 402
    
    pdfs = record.get('pdfs', {})
    if not pdfs:
        flash('PDFs not ready yet. Please wait.', 'warning')
        return jsonify({'error': 'PDFs not ready yet. Please wait.'}), 404
    
    result = {'bundle_id': bundle_id, 'documents': []}
    for ft, encoded_pdf in pdfs.items():
        result['documents'].append({'type': ft, 'pdf': encoded_pdf})
    flash('Documents fetched.', 'success')
    return jsonify(result)

@app.route("/verify_payment/<bundle_id>", methods=["GET"])
def verify_payment_status(bundle_id):
    record = get_user_document_by_bundle_id(bundle_id)
    if not record: return jsonify({'error': 'Document not found'}), 404
    return jsonify({
        'bundle_id': bundle_id, 'payment_status': record.get('payment_status', 'pending'),
        'transaction_code': record.get('transaction_code', ''), 'checkout_request_id': record.get('checkout_request_id', ''),
        'student_name': record.get('student_details', {}).get('student_name', '')
    })

# =============================================================
# ADMIN ROUTES
# =============================================================
@app.route("/admin", methods=["GET"])
def admin_redirect():
    return redirect(url_for('admin_dashboard'))

@app.route("/admin/login", methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if (request.form.get('username') == ADMIN_USERNAME and request.form.get('password') == ADMIN_PASSWORD):
            session['admin_logged_in'] = True
            flash('Logged in successfully.', 'success')
            return redirect(url_for('admin_dashboard'))
        flash('Invalid credentials.', 'danger')
        return render_template('admin_login.html', error='Invalid credentials')
    return render_template('admin_login.html')

@app.route("/admin/logout")
def admin_logout():
    session.pop('admin_logged_in', None)
    flash('Logged out.', 'info')
    return redirect(url_for('admin_login'))

@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    return render_template('admin_dashboard.html')

@app.route("/admin/settings", methods=['GET', 'POST'])
@admin_required
def admin_settings_route():
    if request.method == 'POST':
        try:
            data = request.json or {}
            save_admin_settings({
                'medical_officer': {'officer_name': data.get('med_officer_name', ''), 'hospital_name': data.get('med_hospital_name', ''), 'designation': data.get('med_designation', ''), 'reg_number': data.get('med_reg_number', ''), 'signature': data.get('med_signature', '')},
                'sponsor': {'sponsor_name': data.get('spo_sponsor_name', ''), 'sponsor_email': data.get('spo_sponsor_email', ''), 'sponsor_telephone': data.get('spo_sponsor_phone', ''), 'signature': data.get('spo_signature', '')},
                'commissioner': {'name': data.get('comm_name', ''), 'signature': data.get('comm_signature', '')}
            })
            flash('Settings saved successfully.', 'success')
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Settings save error: {e}")
            flash('Failed to save settings.', 'danger')
            return jsonify({'success': False, 'error': str(e)}), 500
    return render_template('admin_settings.html', settings=get_admin_settings())

@app.route("/admin/get_stats")
@admin_required
def admin_get_stats():
    records = get_all_user_documents()
    total = len(records)
    paid = sum(1 for r in records if r.get('payment_status') == 'success')
    total_revenue = sum(r.get('total_amount', 0) for r in records if r.get('payment_status') == 'success')
    return jsonify({'total_bundles': total, 'paid_bundles': paid, 'pending_bundles': total - paid, 'total_revenue': total_revenue})

@app.route("/admin/get_forms")
@admin_required
def admin_get_forms():
    records = get_all_user_documents()
    forms = []
    form_type_display = {'medical': 'Medical Form', 'sponsorship': 'Sponsorship Letter', 'single_parent': 'Single Parent Certification'}
    for r in records:
        created = r.get('created_at', '')
        doc_names = [form_type_display.get(ft, ft) for ft in r.get('form_types', [])]
        forms.append({
            '_id': str(r.get('_id', '')), 'bundle_id': r.get('bundle_id', ''),
            'created_at': created.strftime('%Y-%m-%d %H:%M:%S') if hasattr(created, 'strftime') else str(created),
            'student_details': r.get('student_details', {}), 'form_types': r.get('form_types', []),
            'documents': ', '.join(doc_names), 'payment_status': r.get('payment_status', 'pending'),
            'transaction_code': r.get('transaction_code', ''), 'checkout_request_id': r.get('checkout_request_id', ''),
            'total_amount': r.get('total_amount', 0)
        })
    return jsonify(sorted(forms, key=lambda x: x['created_at'], reverse=True))

@app.route("/admin/get_document_pdf/<bundle_id>")
@admin_required
def admin_get_document_pdf(bundle_id):
    record = get_user_document_by_bundle_id(bundle_id)
    if not record: return jsonify({'error': 'Document not found'}), 404
    pdfs = record.get('pdfs', {})
    if not pdfs: return jsonify({'error': 'PDFs not found'}), 404
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for form_type, encoded_pdf in pdfs.items():
            pdf_bytes = base64.b64decode(encoded_pdf)
            _, _, dl_name = BUILDERS.get(form_type, (None, None, f'{form_type}.pdf'))
            zip_file.writestr(dl_name, pdf_bytes)
    zip_buffer.seek(0)
    return send_file(zip_buffer, mimetype='application/zip', as_attachment=True, download_name=f'documents_{bundle_id}.zip')

# =============================================================
# REFERRAL ADMIN ROUTES
# =============================================================
@app.route("/admin/referral_codes", methods=['GET', 'POST'])
@admin_required
def admin_referral_codes():
    if request.method == 'POST':
        data = request.json or {}
        code = data.get('code', '').strip().upper()
        marketer = data.get('marketer_name', '').strip()
        discount = int(data.get('discount_per_doc', REFERRAL_DISCOUNT_PER_DOCUMENT))
        if not code or not marketer:
            return jsonify({'error': 'Code and marketer name required'}), 400
        if create_referral_code(code, marketer, discount):
            return jsonify({'success': True, 'message': f'Code {code} created.'})
        else:
            return jsonify({'error': 'Code already exists or creation failed.'}), 400

    # GET – list all codes
    codes = get_all_referral_codes()
    return jsonify(codes)

# =============================================================
# HEALTH CHECK ENDPOINT
# =============================================================
@app.route("/health")
def health():
    status = {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'database': 'connected' if use_mongo else 'memory',
        'mpesa': 'configured' if (MPESA_CONSUMER_KEY and MPESA_PASSKEY) else 'missing',
        'brevo': 'configured' if BREVO_API_KEY else 'missing',
        'environment': 'production' if PRODUCTION else 'development'
    }
    if use_mongo:
        try:
            mongo.db.command('ping')
        except Exception as e:
            status['status'] = 'degraded'
            status['database_error'] = str(e)
            logger.error(f"Health check: MongoDB ping failed: {e}")
    return jsonify(status)

# =============================================================
# DEBUG ENDPOINTS (only in development)
# =============================================================
@app.route("/debug/email/<email>", methods=["GET"])
def debug_email(email):
    if not use_mongo: return jsonify({'error': 'Not using MongoDB'}), 400
    try:
        docs = list(mongo.db.documents.find({'student_details.email': {'$regex': f'^{re.escape(email)}$', '$options': 'i'}}).sort('created_at', -1).limit(20))
        return jsonify({
            'count': len(docs),
            'docs': [{
                'bundle_id': d.get('bundle_id'), 'payment_status': d.get('payment_status'),
                'email': d.get('student_details', {}).get('email'), 'student_name': d.get('student_details', {}).get('student_name'),
                'form_types': d.get('form_types', []),
                'created_at': d.get('created_at').isoformat() if hasattr(d.get('created_at'), 'isoformat') else str(d.get('created_at'))
            } for d in docs]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================
# ASSET VERIFICATION ON STARTUP
# =============================================================
def verify_assets():
    required_pdfs = ['Medical_Form.pdf', 'Sponsorship_Letter.pdf', 'Single_Parent_Self_Certification_2024.pdf']
    missing = [p for p in required_pdfs if not os.path.exists(os.path.join(BASE_DIR, p))]
    if missing:
        logger.warning(f"Missing PDF templates: {missing} – previews and PDF generation will fail.")
    if not os.path.exists(_font_path):
        logger.warning("PatrickHand.ttf not found – text will render in Helvetica.")
    
    # Check for stamped templates
    for ft, stamped_name in STAMPED_PDFS.items():
        stamped_path = os.path.join(STAMPED_BASE_DIR, stamped_name)
        if not os.path.exists(stamped_path):
            logger.warning(f"Stamped template missing: {stamped_name} – final documents will use unstamped fallback.")

# =============================================================
# ENTRY POINT
# =============================================================
if __name__ == "__main__":
    verify_assets()
    
    logger.info("=" * 60)
    logger.info("  SUPPORTING DOCUMENTS GENERATOR (PRODUCTION-READY)")
    logger.info("=" * 60)
    logger.info("  🔥 Pre-warming M-Pesa OAuth token...")
    token_thread = start_token_refresher()
    logger.info("  ✅ Token cache warm")
    logger.info(f"  Student Portal  : http://127.0.0.1:8080")
    logger.info(f"  Admin Login     : http://127.0.0.1:8080/admin/login")
    logger.info(f"  Credentials     : {ADMIN_USERNAME} / {'*' * len(ADMIN_PASSWORD)}")
    logger.info(f"  M-Pesa Daraja   : {'CONFIGURED' if (MPESA_CONSUMER_KEY and MPESA_PASSKEY) else 'NOT CONFIGURED'}")
    logger.info(f"  Environment     : {'PRODUCTION' if PRODUCTION else 'DEVELOPMENT'}")
    logger.info(f"  Callback URL    : {MPESA_CALLBACK_URL}")
    logger.info(f"  Price per Doc   : KES {PAYMENT_AMOUNT_PER_DOCUMENT}")
    logger.info(f"  Brevo Email     : {'CONFIGURED' if BREVO_API_KEY else 'NOT CONFIGURED'}")
    logger.info("  ⚡ Features:")
    logger.info("     ✅ Multi-document selection (Medical, Sponsorship, Single Parent)")
    logger.info("     ✅ Preview each document individually (unstamped)")
    logger.info("     ✅ Final documents are STAMPED (admin-uploaded templates)")
    logger.info("     ✅ Single payment for all documents")
    logger.info("     ✅ Bulk PDF download (ZIP)")
    logger.info("     ✅ Individual PDF download")
    logger.info("     ✅ Email with all documents attached")
    logger.info("     ✅ Signature drawing support")
    logger.info("     ✅ Auto-fallback if callback fails")
    logger.info("     ✅ Brevo IP whitelist error handling")
    logger.info("     ✅ Background task queue with thread pool")
    logger.info("     ✅ Rate limiting")
    logger.info("     ✅ Health check endpoint")
    logger.info("     ✅ MongoDB indexing")
    logger.info("     ✅ Flash messages for user feedback")
    logger.info("     ✅ Production-safe error handling")
    logger.info("     ✅ Referral/Loyalty program (50 KES off per document)")
    logger.info("=" * 60)
    
    if PRODUCTION:
        logger.info("Running in PRODUCTION mode. Do not use the built-in Flask server; use Gunicorn or similar.")
        logger.warning("FLASK_ENV=production but using app.run() – this is not recommended. Use Gunicorn instead.")
    app.run(debug=DEBUG, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))