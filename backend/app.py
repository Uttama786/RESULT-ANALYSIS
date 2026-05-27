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

def parse_usn_range(start_usn: str, end_usn: str) -> List[str]:
    """Generates an list of USNs from start to end range (inclusive)."""
    start = start_usn.strip().upper()
    end = end_usn.strip().upper()
    
    # Match standard alphanumeric USN pattern, e.g., 1SG21CS001
    match_start = re.match(r"^([A-Z0-9]+?)(\d+)$", start)
    match_end = re.match(r"^([A-Z0-9]+?)(\d+)$", end)
    
    if not (match_start and match_end):
        return [start]
        
    prefix_start, num_start = match_start.groups()
    prefix_end, num_end = match_end.groups()
    
    if prefix_start != prefix_end:
        # If prefixes differ, range is invalid, return endpoints
        return [start, end]
        
    width = len(num_start)
    s_idx = int(num_start)
    e_idx = int(num_end)
    
    step = 1 if s_idx <= e_idx else -1
    
    usns = []
    for i in range(s_idx, e_idx + step, step):
        usns.append(f"{prefix_start}{i:0{width}d}")
    return usns

def parse_multiple_usns(usn_input_str: str) -> List[str]:
    """Parses a comma-separated list of individual USNs."""
    raw_list = usn_input_str.split(",")
    cleaned_list = []
    for item in raw_list:
        cleaned = item.strip().upper()
        if cleaned:
            cleaned_list.append(cleaned)
    return cleaned_list

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
        
        start_usn = config.get("start_usn", "").strip()
        end_usn = config.get("end_usn", "").strip()
        usn_list_str = config.get("usn_list", "").strip()
        portal_url = config.get("portal_url", "").strip()
        use_simulation = config.get("use_simulation", True)
        
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
            
        # Log parsed count
        await safe_send({
            "type": "log",
            "message": f"Successfully loaded {len(target_usns)} target USNs into the scraping queue."
        })
        
        # Step 2: Initialize crawler
        scraper = VTUScraper(session_id=session_id, custom_url=portal_url, use_simulation=use_simulation)
        
        # Try initializing chrome driver, fallback to simulation if failed
        if not use_simulation:
            await safe_send({"type": "log", "message": "Launching automated Selenium Chrome driver in background..."})
            success = scraper.initialize_browser()
            if not success:
                await safe_send({
                    "type": "log",
                    "message": "⚠️ Google Chrome or WebDriver was not detected. Falling back to high-fidelity Simulation Mode automatically."
                })
                # Re-create scraper in simulation mode
                scraper = VTUScraper(session_id=session_id, custom_url=portal_url, use_simulation=True)
                use_simulation = True  # Update local flag so the captcha logic uses auto-mode
        else:
            await safe_send({"type": "log", "message": "🚀 Launching scraping simulation pipeline..."})
            
        scraped_results = []
        
        # Step 3: Run Scraping loop for each USN
        for idx, usn in enumerate(target_usns):
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
                    captcha_result = scraper.get_captcha(usn)
                    
                    if use_simulation:
                        # In simulation mode: auto-solve captcha, no user input needed
                        captcha_img_base64, auto_captcha_code = captcha_result
                        await safe_send({
                            "type": "log",
                            "message": f"[SIM] Auto-resolving captcha for {usn} — no manual input required."
                        })
                        captcha_code = auto_captcha_code
                    else:
                        # Real mode: send captcha image to frontend and wait for user input
                        captcha_img_base64 = captcha_result
                        await safe_send({
                            "type": "captcha_required",
                            "usn": usn,
                            "captcha_img": captcha_img_base64,
                            "attempt": attempts,
                            "message": "Invalid CAPTCHA code. Please try again." if attempts > 1 else "Enter CAPTCHA code to fetch result."
                        })
                        
                        # Wait for client solution input — detect disconnect
                        logger.info(f"Waiting for CAPTCHA solution for {usn} (Attempt {attempts}/{max_attempts})")
                        try:
                            client_response = await websocket.receive_json()
                        except WebSocketDisconnect:
                            logger.info(f"Client disconnected while waiting for captcha for {usn}")
                            return  # Exit handler cleanly
                        captcha_code = client_response.get("captcha_code", "").strip()
                    
                    # Submit and scrape
                    result = scraper.submit_and_scrape(usn, captcha_code)
                    
                    if result["status"] == "invalid_captcha":
                        await safe_send({
                            "type": "log",
                            "message": f"❌ CAPTCHA verification failed for {usn}. Retrying with a new code..."
                        })
                        continue  # Re-loops to fetch new captcha for same USN
                        
                    elif result["status"] == "not_found":
                        await safe_send({
                            "type": "progress",
                            "usn": usn,
                            "status": "NOT_FOUND",
                            "message": f"🔍 {usn}: Result not found or USN not registered."
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
