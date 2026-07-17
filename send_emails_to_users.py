# send_emails_to_users.py
import os
import time
import requests
from datetime import datetime
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

def send_emails_to_migrated_users():
    """Send emails to all migrated users"""
    
    client = MongoClient(os.getenv("MONGO_URI"))
    db = client["supporting_docs"]
    collection = db["documents"]
    
    # Find all users with Cloudinary URLs but no email resent flag
    users = list(collection.find({
        "pdf_urls": {"$exists": True, "$ne": {}},
        "email_resent": {"$ne": True}
    }))
    
    print(f"📧 Sending emails to {len(users)} users")
    print("="*50)
    
    success_count = 0
    fail_count = 0
    
    for user in users:
        bundle_id = user.get("bundle_id")
        student_email = user.get("student_email")
        student_name = user.get("student_name", "Student")
        
        if not student_email:
            print(f"⚠️ No email for {bundle_id}, skipping")
            fail_count += 1
            continue
        
        print(f"\n📧 Sending to {student_email} ({bundle_id})")
        print(f"   Student: {student_name}")
        
        # Check if PDFs exist
        pdf_urls = user.get("pdf_urls", {})
        if not pdf_urls:
            print(f"   ⚠️ No PDF URLs found, skipping")
            fail_count += 1
            continue
        
        print(f"   PDFs: {len(pdf_urls)} documents")
        
        # Trigger email via test_callback
        try:
            response = requests.post(
                f"http://127.0.0.1:8080/test_callback/{bundle_id}",
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                print(f"   ✅ Email triggered: {data.get('status', 'OK')}")
                success_count += 1
            else:
                print(f"   ❌ Failed: {response.status_code} - {response.text[:100]}")
                fail_count += 1
        except requests.exceptions.ConnectionError:
            print(f"   ❌ Connection error - Flask app not running on port 8080")
            print(f"   💡 Start Flask: python app.py")
            fail_count += 1
        except Exception as e:
            print(f"   ❌ Error: {e}")
            fail_count += 1
        
        # Small delay to avoid overwhelming the server
        time.sleep(0.5)
    
    # Summary
    print("\n" + "="*50)
    print("📊 SUMMARY")
    print("="*50)
    print(f"✅ Success: {success_count}")
    print(f"❌ Failed: {fail_count}")
    print("="*50)

if __name__ == "__main__":
    print("\n" + "="*50)
    print("📧 EMAIL RESEND SCRIPT")
    print("="*50)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*50)
    
    confirm = input("\n⚠️ This will resend emails to all migrated users. Continue? (yes/no): ")
    
    if confirm.lower() == 'yes':
        send_emails_to_migrated_users()
    else:
        print("❌ Cancelled")