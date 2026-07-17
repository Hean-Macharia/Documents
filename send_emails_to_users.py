# resend_emails_with_direct_links.py
import os
import sys
import time
import requests
from datetime import datetime
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

# Configuration
MONGO_URI = os.getenv("MONGO_URI")
BASE_URL = os.getenv("BASE_URL", "https://yourdomain.com")  # Set your actual domain

def get_users_with_valid_emails():
    """Get users with valid emails and payment success"""
    client = MongoClient(MONGO_URI)
    db = client["supporting_docs"]
    collection = db["documents"]
    
    users = list(collection.find({
        "payment_status": "success",
        "pdf_urls": {"$exists": True, "$ne": {}},
        "student_email": {"$exists": True, "$ne": None, "$ne": ""}
    }).sort("created_at", -1))
    
    return users

def send_email_with_direct_links(to_email, to_name, bundle_id, transaction_code, form_types, total_amount, pdf_urls):
    """Send email with direct Cloudinary download links"""
    
    api_key = os.getenv("BREVO_API_KEY")
    if not api_key:
        return False, "BREVO_API_KEY not configured"
    
    # Build document download sections with DIRECT Cloudinary URLs
    doc_buttons = ""
    form_type_display = {
        "medical": "Medical Form",
        "sponsorship": "Sponsorship Letter",
        "single_parent": "Single Parent Certification"
    }
    
    for ft in form_types:
        display_name = form_type_display.get(ft, ft)
        # Use the FULL Cloudinary URL directly
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
                (PDF, opens in browser - click save to download)
            </span>
        </div>
        '''
    
    # Download All button (app URL - still works if app is running)
    if len(form_types) > 1:
        all_download_url = f"https://supportingdocs.com/download_all/{bundle_id}"  # Update with your domain
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
        .button-secondary {{ display: inline-block; padding: 14px 35px; background: #3B82F6; color: white; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 16px; border: none; cursor: pointer; box-shadow: 0 2px 5px rgba(0,0,0,0.2); }}
        .button-secondary:hover {{ transform: scale(1.02); background: #2563EB; }}
        .footer {{ padding: 20px; text-align: center; color: #6B7280; font-size: 13px; border-top: 1px solid #e5e7eb; }}
        .tip {{ background: #fef3c7; padding: 15px; border-radius: 8px; border-left: 4px solid #F59E0B; margin: 20px 0; }}
        .tip p {{ margin: 0; font-size: 14px; color: #92400E; }}
        @media only screen and (max-width: 480px) {{
            .content {{ padding: 20px; }}
            .header {{ padding: 20px; }}
            .button-primary, .button-secondary {{ display: block; text-align: center; margin: 10px 0; width: 100%; }}
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
                    Click the buttons below to view or download each document.
                </p>
                {doc_buttons}
            </div>
            {download_all}
            <div class="tip">
                <p>💡 <strong>Tip:</strong> Click a download button above. The PDF will open in your browser. 
                Look for the save/download icon (usually a floppy disk or downward arrow) to save it to your device.</p>
            </div>
            <p style="margin-top: 30px; font-size: 15px;">
                Thank you for using our service.<br>
                <strong style="color: #10B981;">Supporting Documents Team</strong>
            </p>
        </div>
        <div class="footer">
            <p>This is an automated message. Please do not reply to this email.</p>
            <p>&copy; 2026 Supporting Documents. All rights reserved.</p>
            <p style="font-size: 11px; color: #9CA3AF;">Need help? Contact us at support@supportingdocs.com</p>
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

def resend_with_direct_links():
    """Resend emails with direct Cloudinary links"""
    
    users = get_users_with_valid_emails()
    
    print("\n" + "="*70)
    print("📧 RESENDING WITH DIRECT CLOUDINARY LINKS")
    print("="*70)
    print(f"Total users with valid emails: {len(users)}")
    print("="*70)
    
    if not users:
        print("❌ No users with valid emails found")
        return
    
    # Show users
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
        
        # Show the direct links being sent
        for ft, url in pdf_urls.items():
            print(f"   📄 {ft}: {url[:60]}...")
        
        success, message = send_email_with_direct_links(
            student_email, student_name, bundle_id,
            tx_code, form_types, total_amount, pdf_urls
        )
        
        if success:
            print(f"   ✅ Email sent with direct links!")
            client = MongoClient(MONGO_URI)
            db = client["supporting_docs"]
            collection = db["documents"]
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

def verify_cloudinary_urls():
    """Verify all Cloudinary URLs are accessible"""
    client = MongoClient(MONGO_URI)
    db = client["supporting_docs"]
    collection = db["documents"]
    
    users = list(collection.find({
        "pdf_urls": {"$exists": True, "$ne": {}}
    }))
    
    print("\n🔍 VERIFYING CLOUDINARY URLs")
    print("="*70)
    
    total_urls = 0
    working = 0
    broken = 0
    
    for user in users:
        bundle_id = user.get("bundle_id")
        pdf_urls = user.get("pdf_urls", {})
        
        for ft, url in pdf_urls.items():
            total_urls += 1
            try:
                response = requests.head(url, timeout=5)
                if response.status_code == 200:
                    working += 1
                    status = "✅"
                else:
                    broken += 1
                    status = f"❌ ({response.status_code})"
                print(f"{status} {bundle_id}/{ft}: {url[:60]}...")
            except Exception as e:
                broken += 1
                print(f"❌ {bundle_id}/{ft}: Error - {e}")
    
    print("\n" + "="*70)
    print(f"📊 Total URLs: {total_urls}")
    print(f"✅ Working: {working}")
    print(f"❌ Broken: {broken}")
    print("="*70)

if __name__ == "__main__":
    print("\n" + "="*70)
    print("📧 EMAIL RESEND - DIRECT CLOUDINARY LINKS")
    print("="*70)
    print("This sends emails with direct Cloudinary URLs (no app needed)")
    print("="*70)
    print("\n1. Verify Cloudinary URLs")
    print("2. Show users with valid emails")
    print("3. Resend emails with direct Cloudinary links")
    print("4. Check broken URLs")
    
    choice = input("\nEnter your choice (1-4): ").strip()
    
    if choice == '1':
        verify_cloudinary_urls()
    
    elif choice == '2':
        users = get_users_with_valid_emails()
        print(f"\n✅ Total users with valid emails: {len(users)}")
        print("-"*70)
        for user in users:
            bundle_id = user.get("bundle_id")
            email = user.get("student_email")
            name = user.get("student_name", "Unknown")
            pdf_count = len(user.get("pdf_urls", {}))
            print(f"{bundle_id} | {email} | {name[:20]} | {pdf_count} PDFs")
    
    elif choice == '3':
        resend_with_direct_links()
    
    elif choice == '4':
        verify_cloudinary_urls()
    
    else:
        print("❌ Invalid choice")