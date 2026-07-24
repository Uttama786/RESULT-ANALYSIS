import os
import re
import uuid
import logging
from typing import Dict, List, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from scraper import VTUScraper
from analyzer import ResultAnalyzer

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Create the exports directory if it doesn't exist
EXPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
os.makedirs(EXPORTS_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

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

# Global dictionary to track session files
session_registry: Dict[str, Dict[str, Any]] = {}

def cleanup_old_sessions(max_sessions: int = 25):
    """Clean up oldest session files and memory entries to prevent disk/memory bloat."""
    global session_registry
    if len(session_registry) > max_sessions:
        keys_to_remove = list(session_registry.keys())[:-max_sessions]
        for key in keys_to_remove:
            session_data = session_registry.pop(key, None)
            if session_data:
                file_path = session_data.get("file_path")
                if file_path and os.path.exists(file_path):
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

@app.get("/api/download/{session_id}")
def download_excel(session_id: str):
    """Serves the generated Excel sheet for download."""
    if session_id not in session_registry:
        raise HTTPException(status_code=404, detail="Session ID not found or expired.")
        
    file_path = session_registry[session_id]["file_path"]
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Excel file does not exist on disk.")
        
    filename = f"VTU_Result_Analysis_{session_id[:8]}.xlsx"
    return FileResponse(
        path=file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename
    )

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
        scraper = VTUScraper(session_id=session_id, custom_url=portal_url, use_simulation=False)
        
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
                    # Get captcha screenshot
                    captcha_img_base64 = scraper.get_captcha(usn)
                    
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
                    
                    # Submit and scrape
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
            
            # Export to Excel
            excel_filename = f"VTU_Results_{session_id}.xlsx"
            excel_path = os.path.join(EXPORTS_DIR, excel_filename)
            analyzer.export_to_excel(excel_path)
            
            # Register session in in-memory database
            session_registry[session_id] = {
                "file_path": excel_path,
                "data": scraped_results,
                "analysis": analysis_data
            }
            cleanup_old_sessions()
            
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
