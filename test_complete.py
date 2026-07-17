# fix_all_users.py
import os
import sys
import io
import base64
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient
import cloudinary
import cloudinary.uploader
import requests
import re

load_dotenv()

# Configuration
MONGO_URI = os.getenv("MONGO_URI")

# Configure Cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

# ============================================================================
# CONFIGURATION
# ============================================================================

class FixConfig:
    SEND_EMAILS = True  # Set to True to actually send emails
    DRY_RUN = False
    # Set to True to preview without making changes
    BATCH_SIZE = 5      # Process this many at a time
    SLEEP_BETWEEN = 2   # Seconds between batches

# ============================================================================
# COPY NEEDED FUNCTIONS FROM APP (since we can't import them)
# ============================================================================

FORM_TYPE_DISPLAY = {
    "medical": "Medical Form",
    "sponsorship": "Sponsorship Letter",
    "single_parent": "Single Parent Certification",
}

def build_payment_confirmation_email_with_buttons(student_name, bundle_id, transaction_code, form_types, total_amount, pdf_urls):
    """Build HTML email with large, visible download buttons"""
    
    doc_buttons = ""
    for ft in form_types:
        display_name = FORM_TYPE_DISPLAY.get(ft, ft)
        download_url = f"/download_pdf/{bundle_id}/{ft}"
        doc_buttons += f'''
        <div style="margin: 15px 0; padding: 15px; background: #f8f9fa; border-radius: 10px; border-left: 4px solid #10B981;">
            <div style="font-weight: bold; font-size: 16px; color: #333; margin-bottom: 10px;">📄 {display_name}</div>
            <a href="{download_url}" 
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
            .button-primary {{ display: block; text-align: center; margin: 10px 0; }}
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
# PDF GENERATION - Using the app's preview endpoint
# ============================================================================

def generate_pdf_via_endpoint(form_type, form_data):
    """Generate PDF using the app's preview_stamped endpoint"""
    try:
        response = requests.post(
            f"http://127.0.0.1:8080/preview_stamped/{form_type}",
            json=form_data,
            timeout=30
        )
        if response.status_code == 200:
            return response.content
        else:
            print(f"   ❌ Preview failed: {response.status_code}")
            return None
    except Exception as e:
        print(f"   ❌ Error calling preview: {e}")
        return None

def generate_pdf_direct(form_type, form_data):
    """Try to import and use _make_pdf directly"""
    try:
        # Try to import from app
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app import app
        
        with app.app_context():
            # Import _make_pdf inside app context
            from app import _make_pdf
            return _make_pdf(form_type, form_data, stamped=True)
    except Exception as e:
        print(f"   ⚠️ Direct import failed: {e}")
        return None

def get_pdf_bytes(form_type, form_data):
    """Get PDF bytes using available method"""
    # Try direct import first
    pdf_bytes = generate_pdf_direct(form_type, form_data)
    if pdf_bytes:
        return pdf_bytes
    
    # Fallback to endpoint
    return generate_pdf_via_endpoint(form_type, form_data)

# ============================================================================
# EMAIL SERVICE - Using Brevo API directly
# ============================================================================

def send_email_via_brevo(to_email, to_name, subject, html, cc=None):
    """Send email using Brevo API directly"""
    api_key = os.getenv("BREVO_API_KEY")
    if not api_key:
        print("   ❌ BREVO_API_KEY not configured")
        return False, "API key missing"
    
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json"
    }
    
    sender_email = os.getenv("BREVO_SENDER_EMAIL", "courseschecker@gmail.com")
    sender_name = os.getenv("BREVO_SENDER_NAME", "EduDocs Kenya")
    
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email, "name": to_name or "Valued Customer"}],
        "subject": subject,
        "htmlContent": html,
    }
    
    if cc:
        payload["cc"] = [{"email": email} for email in cc]
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.status_code in (200, 201):
            return True, "OK"
        return False, f"Brevo API error {response.status_code}: {response.text}"
    except Exception as e:
        return False, str(e)

def send_email_to_user(record):
    """Resend email with PDF links"""
    if not FixConfig.SEND_EMAILS:
        print("   📧 Email sending disabled (SEND_EMAILS=False)")
        return True
    
    bundle_id = record.get("bundle_id")
    student_name = record.get("student_name", "Student")
    student_email = record.get("student_email", "")
    tx_code = record.get("transaction_code", "N/A")
    form_types = record.get("form_types", [])
    total_amount = record.get("total_amount", 0)
    pdf_urls = record.get("pdf_urls", {})
    
    print(f"\n📧 Sending email to {student_email}")
    
    if not student_email:
        print("   ⚠️ No email address found")
        return False
    
    if not pdf_urls:
        print("   ⚠️ No PDF URLs found")
        return False
    
    if not FixConfig.DRY_RUN:
        try:
            doc_names = [FORM_TYPE_DISPLAY.get(ft, ft) for ft in form_types]
            subject = f"Your Documents ({', '.join(doc_names)}) - {bundle_id}"
            html = build_payment_confirmation_email_with_buttons(
                student_name, bundle_id, tx_code, form_types, total_amount, pdf_urls
            )
            
            success, message = send_email_via_brevo(
                to_email=student_email,
                to_name=student_name,
                subject=subject,
                html=html,
                cc=["kuccpscourses@gmail.com"]
            )
            
            if success:
                print(f"   ✅ Email sent to {student_email}")
                # Update email_sent status
                if not FixConfig.DRY_RUN:
                    client = MongoClient(MONGO_URI)
                    db = client["supporting_docs"]
                    collection = db["documents"]
                    collection.update_one(
                        {"bundle_id": bundle_id},
                        {"$set": {
                            "email_sent": True,
                            "email_sent_at": datetime.now(),
                            "email_resent": True,
                            "email_resent_at": datetime.now()
                        }}
                    )
                return True
            else:
                print(f"   ❌ Email failed: {message}")
                return False
        except Exception as e:
            print(f"   ❌ Email error: {e}")
            return False
    else:
        print(f"   🔄 Would send email to {student_email} (DRY RUN)")
        return True

# ============================================================================
# FIX FUNCTIONS
# ============================================================================

def get_all_users():
    """Get all users that need fixing"""
    client = MongoClient(MONGO_URI)
    db = client["supporting_docs"]
    collection = db["documents"]
    
    # Users with payment success but marked as failed (wrong status)
    wrong_status = list(collection.find({
        "payment_status": "failed",
        "transaction_code": {"$regex": "^UGH9Z", "$ne": ""}
    }))
    
    # Users with old base64 PDFs (need migration)
    old_style = list(collection.find({
        "payment_status": "success",
        "pdfs": {"$exists": True, "$ne": {}},
        "$or": [
            {"pdf_urls": {"$exists": False}},
            {"pdf_urls": {}}
        ]
    }))
    
    # Users with missing Cloudinary PDFs (404)
    missing_cloudinary = []
    users_with_urls = list(collection.find({
        "payment_status": "success",
        "pdf_urls": {"$exists": True, "$ne": {}}
    }))
    
    for user in users_with_urls:
        pdf_urls = user.get("pdf_urls", {})
        for ft, url in pdf_urls.items():
            try:
                response = requests.head(url, timeout=5)
                if response.status_code != 200:
                    missing_cloudinary.append(user)
                    break
            except:
                missing_cloudinary.append(user)
                break
    
    return {
        "wrong_status": wrong_status,
        "old_style": old_style,
        "missing_cloudinary": missing_cloudinary
    }

def fix_payment_status(record):
    """Fix users where payment succeeded but status shows failed"""
    bundle_id = record.get("bundle_id")
    tx_code = record.get("transaction_code", "")
    
    print(f"\n📌 Fixing status for {bundle_id}")
    print(f"   Transaction: {tx_code}")
    print(f"   Current Status: {record.get('payment_status')}")
    
    if not FixConfig.DRY_RUN:
        client = MongoClient(MONGO_URI)
        db = client["supporting_docs"]
        collection = db["documents"]
        
        collection.update_one(
            {"bundle_id": bundle_id},
            {"$set": {
                "payment_status": "success",
                "document_status": "payment_confirmed"
            }}
        )
        print(f"   ✅ Status updated to success")
        return True
    else:
        print(f"   🔄 Would update status to success (DRY RUN)")
        return True

def upload_to_cloudinary(pdf_bytes, bundle_id, form_type):
    """Upload PDF to Cloudinary"""
    try:
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
            # Remove version from URL
            parts = url.split("/")
            for i, part in enumerate(parts):
                if part.startswith("v") and part[1:].isdigit():
                    parts.pop(i)
                    break
            return "/".join(parts)
        return None
    except Exception as e:
        print(f"   ❌ Upload failed: {e}")
        return None

def migrate_old_pdfs(record):
    """Migrate users with base64 PDFs to Cloudinary"""
    bundle_id = record.get("bundle_id")
    pdfs = record.get("pdfs", {})
    form_data_map = record.get("form_data_map", {})
    form_types = record.get("form_types", [])
    
    print(f"\n📄 Migrating old PDFs for {bundle_id}")
    print(f"   Found {len(pdfs)} base64 PDFs")
    
    pdf_urls = {}
    
    for ft in form_types:
        pdf_bytes = None
        
        # Try to get from base64 first
        if ft in pdfs:
            try:
                pdf_bytes = base64.b64decode(pdfs[ft])
                print(f"   📄 Decoded {ft}: {len(pdf_bytes)} bytes from base64")
            except Exception as e:
                print(f"   ⚠️ Could not decode {ft}: {e}")
        
        # If base64 failed, try to regenerate
        if not pdf_bytes and ft in form_data_map:
            print(f"   🔄 Regenerating {ft} from form data...")
            pdf_bytes = get_pdf_bytes(ft, form_data_map[ft])
        
        if not pdf_bytes:
            print(f"   ❌ Could not get PDF for {ft}")
            continue
        
        # Upload to Cloudinary
        if not FixConfig.DRY_RUN:
            url = upload_to_cloudinary(pdf_bytes, bundle_id, ft)
            if url:
                pdf_urls[ft] = url
                print(f"   ✅ Uploaded {ft}: {url}")
        else:
            print(f"   🔄 Would upload {ft} to Cloudinary (DRY RUN)")
            pdf_urls[ft] = f"https://res.cloudinary.com/.../{bundle_id}/{ft}.pdf"
    
    # Update database
    if pdf_urls and not FixConfig.DRY_RUN:
        client = MongoClient(MONGO_URI)
        db = client["supporting_docs"]
        collection = db["documents"]
        
        collection.update_one(
            {"bundle_id": bundle_id},
            {"$set": {
                "pdf_urls": pdf_urls,
                "document_status": "pdf_generated"
            }}
        )
        print(f"   ✅ Updated database with {len(pdf_urls)} Cloudinary URLs")
        
        # Remove old base64 PDFs to save space
        collection.update_one(
            {"bundle_id": bundle_id},
            {"$unset": {"pdfs": ""}}
        )
        print(f"   🗑️  Removed old base64 PDFs")
        return True
    elif pdf_urls and FixConfig.DRY_RUN:
        print(f"   🔄 Would update {len(pdf_urls)} URLs (DRY RUN)")
        return True
    
    return False

def fix_missing_cloudinary(record):
    """Regenerate PDFs for users with 404 Cloudinary URLs"""
    bundle_id = record.get("bundle_id")
    form_data_map = record.get("form_data_map", {})
    form_types = record.get("form_types", [])
    
    print(f"\n📄 Fixing missing Cloudinary PDFs for {bundle_id}")
    print(f"   Form types: {form_types}")
    
    pdf_urls = {}
    
    for ft in form_types:
        if ft in form_data_map:
            print(f"   🔄 Regenerating {ft}...")
            pdf_bytes = get_pdf_bytes(ft, form_data_map[ft])
            
            if pdf_bytes and not FixConfig.DRY_RUN:
                url = upload_to_cloudinary(pdf_bytes, bundle_id, ft)
                if url:
                    pdf_urls[ft] = url
                    print(f"   ✅ Uploaded {ft}: {url}")
            elif pdf_bytes and FixConfig.DRY_RUN:
                print(f"   🔄 Would upload {ft} (DRY RUN)")
                pdf_urls[ft] = f"https://res.cloudinary.com/.../{bundle_id}/{ft}.pdf"
            else:
                print(f"   ❌ Failed to generate {ft}")
    
    if pdf_urls and not FixConfig.DRY_RUN:
        client = MongoClient(MONGO_URI)
        db = client["supporting_docs"]
        collection = db["documents"]
        
        collection.update_one(
            {"bundle_id": bundle_id},
            {"$set": {"pdf_urls": pdf_urls, "document_status": "pdf_generated"}}
        )
        print(f"   ✅ Updated {len(pdf_urls)} Cloudinary URLs")
        return True
    elif pdf_urls and FixConfig.DRY_RUN:
        print(f"   🔄 Would update {len(pdf_urls)} URLs (DRY RUN)")
        return True
    
    return False

# ============================================================================
# MAIN FUNCTION
# ============================================================================

def main():
    print("\n" + "="*70)
    print("🔧 COMPREHENSIVE USER FIX SCRIPT")
    print("="*70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"DRY RUN: {FixConfig.DRY_RUN}")
    print(f"SEND EMAILS: {FixConfig.SEND_EMAILS}")
    print("="*70)
    
    # Get all users that need fixing
    users = get_all_users()
    
    total_fixed = 0
    total_emails = 0
    total_errors = 0
    
    # ================================================================
    # 1. Fix wrong payment status
    # ================================================================
    if users["wrong_status"]:
        print(f"\n📌 FIXING WRONG PAYMENT STATUS ({len(users['wrong_status'])} users)")
        print("-"*50)
        
        for record in users["wrong_status"]:
            try:
                success = fix_payment_status(record)
                if success:
                    total_fixed += 1
                    # Generate PDFs if they were missing
                    if not record.get("pdf_urls"):
                        migrate_old_pdfs(record)
            except Exception as e:
                print(f"   ❌ Error: {e}")
                total_errors += 1
            
            time.sleep(0.5)
    
    # ================================================================
    # 2. Migrate old base64 PDFs
    # ================================================================
    if users["old_style"]:
        print(f"\n📌 MIGRATING OLD BASE64 PDFS ({len(users['old_style'])} users)")
        print("-"*50)
        
        for record in users["old_style"]:
            try:
                success = migrate_old_pdfs(record)
                if success:
                    total_fixed += 1
                    # Send email with new Cloudinary links
                    if record.get("student_email"):
                        send_email_to_user(record)
                        total_emails += 1
            except Exception as e:
                print(f"   ❌ Error: {e}")
                total_errors += 1
            
            time.sleep(0.5)
    
    # ================================================================
    # 3. Fix missing Cloudinary PDFs
    # ================================================================
    if users["missing_cloudinary"]:
        print(f"\n📌 FIXING MISSING CLOUDINARY PDFS ({len(users['missing_cloudinary'])} users)")
        print("-"*50)
        
        for record in users["missing_cloudinary"]:
            try:
                success = fix_missing_cloudinary(record)
                if success:
                    total_fixed += 1
                    # Resend email with fixed PDFs
                    if record.get("student_email"):
                        send_email_to_user(record)
                        total_emails += 1
            except Exception as e:
                print(f"   ❌ Error: {e}")
                total_errors += 1
            
            time.sleep(0.5)
    
    # ================================================================
    # Summary
    # ================================================================
    print("\n" + "="*70)
    print("📊 SUMMARY")
    print("="*70)
    print(f"✅ Fixed: {total_fixed} users")
    print(f"📧 Emails sent: {total_emails}")
    print(f"❌ Errors: {total_errors}")
    
    if FixConfig.DRY_RUN:
        print("\n⚠️ This was a DRY RUN. No actual changes were made.")
        print("   Set FixConfig.DRY_RUN = False to make changes.")
    
    print("\n" + "="*70)

if __name__ == "__main__":
    print("\n⚠️ This script will:")
    print("   1. Fix users where payment status shows 'failed' but transaction code exists")
    print("   2. Migrate old base64 PDFs to Cloudinary")
    print("   3. Regenerate missing Cloudinary PDFs (404 errors)")
    print("   4. Resend emails with new Cloudinary links")
    print(f"   DRY RUN: {FixConfig.DRY_RUN}")
    print(f"   SEND EMAILS: {FixConfig.SEND_EMAILS}")
    print("\n" + "="*70)
    
    confirm = input("\nContinue? (yes/no): ")
    
    if confirm.lower() == 'yes':
        main()
    else:
        print("❌ Cancelled")