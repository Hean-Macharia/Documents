"""
Gunicorn Configuration for 2000+ Concurrent Users
====================================================
Run with:
  gunicorn -c gunicorn.conf.py app:create_app()

Recommended server specs:
  • 4 CPU cores, 8GB RAM minimum
  • MongoDB with replica set
  • Redis (for sessions + rate limiting + caching)
  • Nginx reverse proxy (keepalive, buffering off for SSE)
"""

import os
import multiprocessing

# ------------------------------------------------------------------------------
# Server Socket
# ------------------------------------------------------------------------------
bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8080")
backlog = 2048  # Pending connections queue

# ------------------------------------------------------------------------------
# Worker Processes (Gevent for high I/O concurrency)
# ------------------------------------------------------------------------------
worker_class = "gevent"
workers = multiprocessing.cpu_count() * 4  # e.g., 16 workers on 4-core
worker_connections = 250  # Each gevent worker handles 250 concurrent connections
threads = 4  # Thread pool per worker for mixed workloads
max_requests = 10000
max_requests_jitter = 1000
timeout = 120
keepalive = 5
graceful_timeout = 30

# ------------------------------------------------------------------------------
# Memory & Limits
# ------------------------------------------------------------------------------
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# ------------------------------------------------------------------------------
# Process Naming & Systemd
# ------------------------------------------------------------------------------
proc_name = "supporting_docs"

# ------------------------------------------------------------------------------
# Server Mechanics
# ------------------------------------------------------------------------------
daemon = False
pidfile = "/tmp/gunicorn.pid"

# ------------------------------------------------------------------------------
# SSL (Terminate at Nginx instead if possible)
# ------------------------------------------------------------------------------
# keyfile = "/path/to/key.pem"
# certfile = "/path/to/cert.pem"

# ------------------------------------------------------------------------------
# Preload App (saves memory, but be careful with shared state)
# ------------------------------------------------------------------------------
preload_app = True

# ------------------------------------------------------------------------------
# Hooks
# ------------------------------------------------------------------------------
def on_starting(server):
    print("[Gunicorn] Starting Supporting Documents Generator...")

def worker_int(worker):
    print(f"[Gunicorn] Worker {worker.pid} interrupted")

def on_exit(server):
    print("[Gunicorn] Shutting down...")