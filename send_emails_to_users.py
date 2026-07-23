# fix_and_resend_all.py
import os
import sys
import time
import base64
import requests
from datetime import datetime
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")

# ============================================================================
# GET ALL USERS WITH PDFS
# ============================================================================

def get_all_users_with_pdfs():
    """Get all users with Cloudinary URLs regardless of email status"""
    client = MongoClient(MONGO_URI)
    db = client["supporting_docs"]
    collection = db["documents"]
    
    users = list(collection.find({
        "pdf_urls": {"$exists": True, "$ne": {}}
    }).sort("created_at", -1))
    
    return users

def fix_missing_emails():
    """Recover emails from student_details for users with missing student_email"""
    client = MongoClient(MONGO_URI)
    db = client["supporting_docs"]
    collection = db["documents"]
    
    # Find users with PDFs but missing student_email
    users = list(collection.find({
        "pdf_urls": {"$exists": True, "$ne": {}},
        "$or": [
            {"student_email": {"$exists": False}},
            {"student_email": None},
            {"student_email": ""}
        ]
    }))
    
    print(f"\n🔧 Found {len(users)} users with missing emails")
    
    fixed = 0
    for user in users:
        bundle_id = user.get("bundle_id")
        
        # Try to get email from student_details
        student_details = user.get("student_details", {})
        email = student_details.get("email") or student_details.get("student_email")
        
        # Also try form_data_map
        if not email:
            form_data_map = user.get("form_data_map", {})
            for ft, data in form_data_map.items():
                if data.get("email"):
                    email = data.get("email")
                    break
        
        if email:
            collection.update_one(
                {"bundle_id": bundle_id},
                {"$set": {"student_email": email}}
            )
            print(f"   ✅ Fixed {bundle_id}: {email}")
            fixed += 1
        else:
            print(f"   ⚠️ No email found for {bundle_id}")
    
    print(f"\n✅ Fixed {fixed} users")
    return fixed

def send_email_direct(to_email, to_name, bundle_id, transaction_code, form_types, total_amount, pdf_urls):
    """Send email using Brevo API directly"""
    
    api_key = os.getenv("BREVO_API_KEY")
    if not api_key:
        return False, "BREVO_API_KEY not configured"
    
    # Build email HTML with direct Cloudinary links
    doc_buttons = ""
    form_type_display = {
        "medical": "Medical Form",
        "sponsorship": "Sponsorship Letter",
        "single_parent": "Single Parent Certification"
    }
    
    for ft in form_types:
        display_name = form_type_display.get(ft, ft)
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
    
    html = f"""
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
    </style>
    </head>
    <body>
    <div class="container">
        <div class="header">
            <h1>✅ Payment Confirmed!</h1>
            <p>Your Supporting Documents Are Ready</p>
        </div>
        <div class="content">
            <h3>Dear {to_name},</h3>
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
    
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json"
    }
    
    payload = {
        "sender": {
            "name": os.getenv("BREVO_SENDER_NAME", "EduDocs Kenya"),
            "email": os.getenv("BREVO_SENDER_EMAIL", "courseschecker@gmail.com")
        },
        "to": [{"email": to_email, "name": to_name}],
        "cc": [{"email": "kuccpscourses@gmail.com"}],
        "subject": f"Your Documents ({', '.join(form_types)}) - {bundle_id}",
        "htmlContent": html
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.status_code in (200, 201):
            return True, "OK"
        else:
            return False, f"API Error {response.status_code}"
    except Exception as e:
        return False, str(e)

def show_all_users():
    """Show all users with PDFs"""
    users = get_all_users_with_pdfs()
    
    print(f"\n📋 ALL USERS WITH PDFS: {len(users)}")
    print("-"*80)
    print(f"{'Bundle ID':<12} {'Email':<35} {'Name':<20} {'PDFs':<5} {'Sent':<5}")
    print("-"*80)
    
    for user in users:
        bundle_id = user.get("bundle_id", "N/A")[:10]
        email = user.get("student_email", "MISSING")[:32]
        name = user.get("student_name", "Unknown")[:18]
        pdf_count = len(user.get("pdf_urls", {}))
        email_sent = "✅" if user.get("email_sent") else "❌"
        print(f"{bundle_id:<12} {email:<35} {name:<20} {pdf_count:<5} {email_sent:<5}")
    
    return users

def fix_and_resend():
    """Fix missing emails and resend to all users"""
    
    print("\n" + "="*70)
    print("📧 FIX AND RESEND ALL EMAILS")
    print("="*70)
    
    # Step 1: Fix missing emails
    print("\n📌 Step 1: Fixing missing emails...")
    fixed = fix_missing_emails()
    
    # Step 2: Get all users with emails
    print("\n📌 Step 2: Getting users with emails...")
    client = MongoClient(MONGO_URI)
    db = client["supporting_docs"]
    collection = db["documents"]
    
    users = list(collection.find({
        "pdf_urls": {"$exists": True, "$ne": {}},
        "student_email": {"$exists": True, "$ne": None, "$ne": ""}
    }))
    
    print(f"   Found {len(users)} users with valid emails")
    
    if not users:
        print("❌ No users with valid emails found")
        return
    
    # Show users that will receive emails
    print("\n📋 Users that will receive emails:")
    print("-"*70)
    for i, user in enumerate(users, 1):
        bundle_id = user.get("bundle_id")
        email = user.get("student_email")
        name = user.get("student_name", "Unknown")
        pdf_count = len(user.get("pdf_urls", {}))
        print(f"{i:2}. {bundle_id} | {email} | {name[:20]} | {pdf_count} PDFs")
    
    confirm = input(f"\n⚠️ This will send emails to {len(users)} users. Continue? (yes/no): ")
    if confirm.lower() != 'yes':
        print("❌ Cancelled")
        return
    
    # Step 3: Send emails
    print("\n📌 Step 3: Sending emails...")
    
    success_count = 0
    fail_count = 0
    failed_users = []
    
    for i, user in enumerate(users, 1):
        bundle_id = user.get("bundle_id")
        student_name = user.get("student_name", "Student")
        student_email = user.get("student_email")
        tx_code = user.get("transaction_code", "N/A")
        form_types = user.get("form_types", [])
        total_amount = user.get("total_amount", 0)
        pdf_urls = user.get("pdf_urls", {})
        
        print(f"\n{i}/{len(users)} 📧 {student_email}")
        print(f"   Bundle: {bundle_id}")
        print(f"   Student: {student_name}")
        print(f"   Documents: {len(pdf_urls)}")
        
        if not student_email:
            print(f"   ⚠️ No email, skipping")
            fail_count += 1
            continue
        
        if not pdf_urls:
            print(f"   ⚠️ No PDF URLs, skipping")
            fail_count += 1
            continue
        
        success, message = send_email_direct(
            student_email, student_name, bundle_id,
            tx_code, form_types, total_amount, pdf_urls
        )
        
        if success:
            print(f"   ✅ Email sent!")
            collection.update_one(
                {"bundle_id": bundle_id},
                {"$set": {
                    "email_sent": True,
                    "email_resent": True,
                    "email_resent_at": datetime.now()
                }}
            )
            success_count += 1
        else:
            print(f"   ❌ Failed: {message}")
            failed_users.append({"bundle_id": bundle_id, "email": student_email, "error": message})
            fail_count += 1
        
        time.sleep(1)
    
    # Summary
    print("\n" + "="*70)
    print("📊 SUMMARY")
    print("="*70)
    print(f"✅ Emails sent: {success_count}")
    print(f"❌ Failed: {fail_count}")
    
    if failed_users:
        print("\n❌ Failed users:")
        for f in failed_users:
            print(f"   - {f['bundle_id']}: {f['email']} - {f['error']}")
    
    print("="*70)

if __name__ == "__main__":
    print("\n" + "="*70)
    print("📧 FIX AND RESEND ALL EMAILS")
    print("="*70)
    print("1. Show all users with PDFs")
    print("2. Fix missing emails and resend to all users")
    print("3. Fix missing emails only")
    print("4. Resend to users with existing emails")
    
    choice = input("\nEnter your choice (1-4): ").strip()
    
    if choice == '1':
        show_all_users()
    
    elif choice == '2':
        fix_and_resend()
    
    elif choice == '3':
        fix_missing_emails()
    
    elif choice == '4':
        # Just resend without fixing
        users = get_all_users_with_pdfs()
        # Filter to those with emails
        users_with_emails = [u for u in users if u.get("student_email")]
        print(f"Found {len(users_with_emails)} users with emails")
        if users_with_emails:
            confirm = input("Continue? (yes/no): ")
            if confirm.lower() == 'yes':
                # Send emails...
                pass
    else:
        print("❌ Invalid choice")