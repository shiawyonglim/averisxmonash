"""
Modal.com Serverless Deployment for Averis Shipping AI & Laya Classifier
========================================================================
Deploys:
  1. Full-Stack Web Application (React + Vite static frontend)
  2. FastAPI Operational Shipping Backend (7-Field Auditor, Discrepancy Matrix)
  3. Laya Neural Decision Model (Convai Innovations ModernBERT in-memory)
  4. Real-time Personal Gmail IMAP & SMTP live integration

Quick Start:
  1. Authenticate with Modal:
     python -m modal setup
  2. Deploy to a live public HTTPS URL:
     python -m modal deploy modal_app.py
  3. Or test live with hot reload:
     python -m modal serve modal_app.py
"""

import os
from pathlib import Path
import modal

LOCAL_ROOT = Path(__file__).parent

# 1. Define Modal App
app = modal.App("averis-shipping-ai")

# 2. Define Container Image with all dependencies, pre-baked Laya ModernBERT weights & project files
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("poppler-utils")
    .pip_install(
        "fastapi[standard]>=0.115.0",
        "uvicorn>=0.30.0",
        "pydantic>=2.7.0",
        "python-dotenv>=1.0.0",
        "python-multipart>=0.0.9",
        "pdfplumber>=0.11.0",
        "openpyxl>=3.1.2",
        "python-docx>=1.1.0",
        "tqdm>=4.66.0",
        "pillow>=10.3.0",
        "safetensors>=0.4.3",
        "transformers>=4.40.0",
        "torch>=2.2.0",
        "huggingface_hub>=0.23.0",
        "laya==0.3.4",
        "openai>=1.30.0",
    )
    # Pre-cache Laya ModernBERT model weights in container image so cold start is <1 second
    .run_commands(
        "python -c \"from huggingface_hub import snapshot_download; print('Baking Laya weights into Modal image...'); snapshot_download('convaiinnovations/laya'); print('Laya weights baked!')\""
    )
    # Add project files and shipping datasets to the container
    .add_local_file(LOCAL_ROOT / "server.py", remote_path="/root/server.py")
    .add_local_dir(LOCAL_ROOT / "sdoc-hackathon-bundle", remote_path="/root/sdoc-hackathon-bundle")
    .add_local_dir(LOCAL_ROOT / "frontend" / "dist", remote_path="/root/frontend/dist")
)

# 3. Serverless Web ASGI Endpoint
@app.function(
    image=image,
    secrets=[
        # Ingests local .env (SMTP credentials, API keys) into Modal runtime environment
        modal.Secret.from_dotenv(),
    ],
    timeout=600,
    cpu=2.0,
    memory=4096,  # 4GB RAM for fast ModernBERT CPU forward passes
    scaledown_window=300,  # Keep container warm for 5 minutes after traffic
)
@modal.asgi_app()
def web():
    import sys
    sys.path.insert(0, "/root")
    
    from server import app as fastapi_app
    from fastapi.staticfiles import StaticFiles
    
    # Mount compiled React/Vite UI at root so the entire full-stack app is served on your Modal URL
    dist_dir = "/root/frontend/dist"
    if os.path.exists(dist_dir):
        fastapi_app.mount("/", StaticFiles(directory=dist_dir, html=True), name="frontend_static")
        
    return fastapi_app
