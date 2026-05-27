import os
import re
import time
import random
import base64
from io import BytesIO
import logging

from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Attempt importing selenium components
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.chrome.service import Service
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    logger.warning("Selenium or webdriver_manager not installed. Real scraping will not be functional.")

class VTUScraper:
    def __init__(self, session_id: str, custom_url: str = None, use_simulation: bool = True):
        self.session_id = session_id
        self.portal_url = custom_url or "https://results.vtu.ac.in/"
        self.use_simulation = use_simulation
        self.driver = None
        self.captcha_solution = None
        
        # Mock student lists for simulation
        self.mock_names = [
            "Rahul Sharma", "Priya Nair", "Amit Patel", "Sneha Rao", 
            "Vikram Singh", "Ananya Hegde", "Rohan Gupta", "Deepika K",
            "Karthik S", "Aishwarya M", "Siddharth Jain", "Meghana Bhat",
            "Nikhil Gowda", "Shreya Joshi", "Harish Kumar", "Divya Pillai",
            "Manjunath Prasad", "Pooja Reddy", "Abhishek Sen", "Swati Mishra"
        ]
        
        # Mock subjects for Computer Science branch
        self.mock_subjects = [
            {"code": "21CS61", "name": "Software Engineering & Project Management"},
            {"code": "21CS62", "name": "Full Stack Web Development"},
            {"code": "21CS63", "name": "Computer Graphics & Visualisation"},
            {"code": "21CS64X", "name": "Machine Learning Techniques"},
            {"code": "21CSL66", "name": "Full Stack Development Laboratory"},
            {"code": "21CSL67", "name": "Computer Graphics Lab with Mini Project"},
            {"code": "21CSP68", "name": "Mini Project - Mobile App Development"}
        ]

        # Element XPaths candidates (for resiliency)
        # NOTE: VTU MJ26cbcs portal uses name='lns' for the USN input field
        self.usn_xpath_candidates = [
            "//input[@name='lns']",       # Primary: MJ26cbcs portal (confirmed)
            "//input[@name='lnkns']",     # Legacy VTU portals
            "//input[@id='lnkns']",
            "//input[@name='usn']",
            "//input[@id='usn']",
            "//input[@alt='USN']",         # Alt attribute fallback
            "//input[contains(@placeholder, 'USN') or contains(@placeholder, 'usn')]",
            "//input[contains(@name, 'usn') or contains(@id, 'usn') or contains(@name, 'lns')]"
        ]
        
        self.captcha_input_candidates = [
            "//input[@name='captchacode']",
            "//input[@id='captchacode']",
            "//input[@name='captcha']",
            "//input[@id='captcha']",
            "//input[contains(@name, 'captcha') or contains(@id, 'captcha') or contains(@name, 'code') or contains(@id, 'code')]"
        ]
        
        self.captcha_img_candidates = [
            "//img[contains(@src, 'captcha') or contains(@src, 'Captcha') or contains(@src, 'CAPTCHA')]",
            "//img[contains(@id, 'captcha') or contains(@id, 'Captcha') or contains(@id, 'CAPTCHA')]",
            "//img[@id='captcha_img']",
            "//img[contains(@src, 'securimage')]",
            "//img[contains(@src, 'captcha_code')]"
        ]
        
        self.submit_btn_candidates = [
            "//input[@id='btnSubmit']",
            "//input[@type='submit']",
            "//button[@type='submit']",
            "//input[contains(@id, 'submit') or contains(@name, 'submit') or contains(@value, 'Submit') or contains(@value, 'SUBMIT')]",
            "//button[contains(text(), 'Submit') or contains(text(), 'SUBMIT')]"
        ]

    def _find_element_by_candidates(self, candidates, timeout=5):
        """Tries to find a web element using a list of XPaths in order."""
        if not self.driver:
            return None
        wait = WebDriverWait(self.driver, timeout)
        for xpath in candidates:
            try:
                element = wait.until(EC.presence_of_element_located((By.XPATH, xpath)))
                logger.info(f"Found element with XPath: {xpath}")
                return element
            except Exception:
                continue
        return None

    def initialize_browser(self):
        """Initializes the Selenium Chrome WebDriver."""
        if self.use_simulation:
            logger.info("Simulation mode active. Skipping browser initialization.")
            return True
            
        if not SELENIUM_AVAILABLE:
            raise RuntimeError("Selenium is not installed on this system. Please use Simulation Mode.")
            
        try:
            chrome_options = Options()
            chrome_options.add_argument("--headless=new")  # Run headless (modern)
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            
            # Initialize using WebDriverManager, fallback to standard selenium if it fails
            try:
                service = Service(ChromeDriverManager().install())
                self.driver = webdriver.Chrome(service=service, options=chrome_options)
            except Exception as wdm_err:
                logger.warning(f"WebDriverManager failed to fetch driver: {wdm_err}. Attempting direct Chrome driver initialization...")
                self.driver = webdriver.Chrome(options=chrome_options)

            self.driver.set_window_size(1280, 1024)
            logger.info("Selenium WebDriver successfully initialized.")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize browser driver: {str(e)}")
            self.use_simulation = True
            logger.warning("Falling back to Simulation Mode due to browser driver failure.")
            return False

    def close_browser(self):
        """Safely closes the browser instance."""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("Browser closed successfully.")
            except Exception as e:
                logger.error(f"Error closing browser: {str(e)}")
            finally:
                self.driver = None

    def generate_mock_captcha(self) -> tuple[str, str]:
        """Generates a mock captcha image and returns (captcha_text, base64_image_data)."""
        # Create a simple image with some noise
        width, height = 150, 50
        image = Image.new("RGB", (width, height), color=(240, 240, 240))
        draw = ImageDraw.Draw(image)
        
        # Random alphanumeric text of 6 characters (matches VTU real site maxlength=6)
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        captcha_text = "".join(random.choice(chars) for _ in range(6))
        
        # Add some noise lines
        for _ in range(5):
            x1 = random.randint(0, width)
            y1 = random.randint(0, height)
            x2 = random.randint(0, width)
            y2 = random.randint(0, height)
            draw.line((x1, y1, x2, y2), fill=random.choice([(150,150,150), (100,100,100), (200,100,100)]), width=2)
            
        # Draw text (use default font if custom font not available)
        try:
            # Try to load a system font, otherwise default to standard
            font_paths = ["arial.ttf", "cour.ttf", "consola.ttf"]
            font = None
            for p in font_paths:
                try:
                    font = ImageFont.truetype(p, 28)
                    break
                except IOError:
                    continue
            if not font:
                font = ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()
            
        # Draw characters with slight rotation/offset
        for i, char in enumerate(captcha_text):
            x_pos = 15 + i * 25 + random.randint(-3, 3)
            y_pos = 10 + random.randint(-5, 5)
            draw.text((x_pos, y_pos), char, fill=random.choice([(0,0,0), (0,0,150), (150,0,0), (0,150,0)]), font=font)
            
        # Add salt-and-pepper noise
        for _ in range(100):
            x = random.randint(0, width - 1)
            y = random.randint(0, height - 1)
            draw.point((x, y), fill=(50, 50, 50))
            
        # Convert to Base64
        buffered = BytesIO()
        image.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
        
        return captcha_text, f"data:image/png;base64,{img_str}"

    def generate_mock_subjects(self, usn: str) -> list:
        """Dynamically generates subjects based on the branch and semester detected from the USN."""
        # Parse USN to extract year and branch (typical format e.g. 1SG21CS001)
        match = re.search(r'\d([A-Z]{2,3})(\d{2})([A-Z]{2})\d+', usn.upper())
        if match:
            college, year_str, branch = match.groups()
            year = int(year_str)
        else:
            # Fallback simple regexes
            match_simple = re.search(r'\d{2}([A-Z]{2})\d+', usn.upper())
            branch = match_simple.group(1) if match_simple else "CS"
            
            match_year = re.search(r'\d(\d{2})', usn.upper())
            year = int(match_year.group(1)) if match_year else 21

        branch = branch.upper()

        # Determine scheme year (usually 18, 21, 22)
        if year >= 22:
            scheme = "22"
        elif year >= 21:
            scheme = "21"
        else:
            scheme = "18"

        # Determine semester (digit) based on year of joining (assuming current year is 2026)
        # 22 -> 8th sem, 23 -> 6th sem, 24 -> 4th sem, 25 -> 2nd sem
        sem_map = {22: 8, 23: 6, 24: 4, 25: 2}
        sem = sem_map.get(year, 6)

        # Subject templates for main branches
        subjects_templates = {
            "CS": {
                2: [
                    {"code": f"{scheme}CS21", "name": "Mathematics-II for Computer Science"},
                    {"code": f"{scheme}CS22", "name": "Data Structures & Applications"},
                    {"code": f"{scheme}CS23", "name": "Computer Organization & Architecture"},
                    {"code": f"{scheme}CS24", "name": "Object Oriented Programming with C++"},
                    {"code": f"{scheme}CSL26", "name": "Data Structures Laboratory"},
                    {"code": f"{scheme}CSL27", "name": "Object Oriented Programming Lab"}
                ],
                4: [
                    {"code": f"{scheme}CS41", "name": "Mathematical Foundations for Computing"},
                    {"code": f"{scheme}CS42", "name": "Design & Analysis of Algorithms"},
                    {"code": f"{scheme}CS43", "name": "Operating Systems"},
                    {"code": f"{scheme}CS44", "name": "Microcontroller & Embedded Systems"},
                    {"code": f"{scheme}CSL46", "name": "Design & Analysis of Algorithms Lab"},
                    {"code": f"{scheme}CSL47", "name": "Microcontroller Laboratory"}
                ],
                6: [
                    {"code": f"{scheme}CS61", "name": "Software Engineering & Project Management"},
                    {"code": f"{scheme}CS62", "name": "Full Stack Web Development"},
                    {"code": f"{scheme}CS63", "name": "Computer Graphics & Visualisation"},
                    {"code": f"{scheme}CS64", "name": "Machine Learning Techniques"},
                    {"code": f"{scheme}CSL66", "name": "Full Stack Development Laboratory"},
                    {"code": f"{scheme}CSL67", "name": "Computer Graphics Lab with Mini Project"},
                    {"code": f"{scheme}CSP68", "name": "Mini Project - Mobile App Development"}
                ],
                8: [
                    {"code": f"{scheme}CS81", "name": "Internet of Things"},
                    {"code": f"{scheme}CS82", "name": "Big Data Analytics"},
                    {"code": f"{scheme}CS83", "name": "Network Security"},
                    {"code": f"{scheme}CSP84", "name": "Major Project Work Phase-II"},
                    {"code": f"{scheme}CSS85", "name": "Technical Seminar"}
                ]
            },
            "IS": {
                2: [
                    {"code": f"{scheme}IS21", "name": "Mathematics-II for Information Science"},
                    {"code": f"{scheme}IS22", "name": "Data Structures & Applications"},
                    {"code": f"{scheme}IS23", "name": "Computer Organization"},
                    {"code": f"{scheme}IS24", "name": "COBOL & Database Systems"},
                    {"code": f"{scheme}ISL26", "name": "Data Structures Lab"},
                    {"code": f"{scheme}ISL27", "name": "Database Applications Lab"}
                ],
                4: [
                    {"code": f"{scheme}IS41", "name": "Discrete Mathematics"},
                    {"code": f"{scheme}IS42", "name": "Design & Analysis of Algorithms"},
                    {"code": f"{scheme}IS43", "name": "Operating Systems"},
                    {"code": f"{scheme}IS44", "name": "Data Communications"},
                    {"code": f"{scheme}ISL46", "name": "Algorithms Lab"},
                    {"code": f"{scheme}ISL47", "name": "Data Communication Lab"}
                ],
                6: [
                    {"code": f"{scheme}IS61", "name": "Software Engineering"},
                    {"code": f"{scheme}IS62", "name": "File Structures & Mining"},
                    {"code": f"{scheme}IS63", "name": "Cloud Computing"},
                    {"code": f"{scheme}IS64", "name": "Data Mining & Warehousing"},
                    {"code": f"{scheme}ISL66", "name": "File Structures Lab"},
                    {"code": f"{scheme}ISL67", "name": "Software Testing Lab"},
                    {"code": f"{scheme}ISP68", "name": "Mini Project"}
                ],
                8: [
                    {"code": f"{scheme}IS81", "name": "Information & Network Security"},
                    {"code": f"{scheme}IS82", "name": "Mobile Computing"},
                    {"code": f"{scheme}ISP84", "name": "Major Project Phase II"},
                    {"code": f"{scheme}ISS85", "name": "Technical Seminar"}
                ]
            },
            "EC": {
                2: [
                    {"code": f"{scheme}EC21", "name": "Advanced Calculus & Numerical Methods"},
                    {"code": f"{scheme}EC22", "name": "Network Theory"},
                    {"code": f"{scheme}EC23", "name": "Analog Electronic Circuits"},
                    {"code": f"{scheme}EC24", "name": "Digital System Design"},
                    {"code": f"{scheme}ECL26", "name": "Analog Electronics Lab"},
                    {"code": f"{scheme}ECL27", "name": "Digital Design Lab"}
                ],
                4: [
                    {"code": f"{scheme}EC41", "name": "Complex Analysis & Fourier Series"},
                    {"code": f"{scheme}EC42", "name": "Analog Circuits"},
                    {"code": f"{scheme}EC43", "name": "Control Systems"},
                    {"code": f"{scheme}EC44", "name": "Signals & Systems"},
                    {"code": f"{scheme}ECL46", "name": "Analog Circuits Lab"},
                    {"code": f"{scheme}ECL47", "name": "Microcontroller & HDL Lab"}
                ],
                6: [
                    {"code": f"{scheme}EC61", "name": "Digital Communication"},
                    {"code": f"{scheme}EC62", "name": "Embedded Systems"},
                    {"code": f"{scheme}EC63", "name": "Microwave & Antennas"},
                    {"code": f"{scheme}EC64", "name": "Digital Signal Processing"},
                    {"code": f"{scheme}ECL66", "name": "Communication Lab"},
                    {"code": f"{scheme}ECL67", "name": "Embedded Controller Lab"},
                    {"code": f"{scheme}ECP68", "name": "Mini Project"}
                ],
                8: [
                    {"code": f"{scheme}EC81", "name": "Wireless Communication"},
                    {"code": f"{scheme}EC82", "name": "Fiber Optics & Networks"},
                    {"code": f"{scheme}ECP84", "name": "Project Work Phase II"},
                    {"code": f"{scheme}ECS85", "name": "Technical Seminar"}
                ]
            }
        }

        # Fallback generator for other/unlisted branches (e.g. ME, CV, EE)
        if branch not in subjects_templates:
            return [
                {"code": f"{scheme}{branch}{sem}1", "name": f"Core Subject I - {branch}"},
                {"code": f"{scheme}{branch}{sem}2", "name": f"Core Subject II - {branch}"},
                {"code": f"{scheme}{branch}{sem}3", "name": f"Core Subject III - {branch}"},
                {"code": f"{scheme}{branch}{sem}4", "name": f"Professional Elective - {branch}"},
                {"code": f"{scheme}{branch}L{sem}6", "name": f"{branch} Laboratory I"},
                {"code": f"{scheme}{branch}L{sem}7", "name": f"{branch} Laboratory II"},
                {"code": f"{scheme}{branch}P{sem}8", "name": f"Mini Project - {branch}"}
            ]

        # Return specific semester subjects, default to 6th if semester not found
        return subjects_templates[branch].get(sem, subjects_templates[branch][6])

    def get_captcha(self, usn: str) -> tuple:
        """Navigates to the portal (real or simulation) and retrieves the captcha image as Base64.
        In simulation mode, returns a tuple (base64_img, auto_solution) so the backend can
        auto-submit without asking the user to solve the captcha.
        """
        if self.use_simulation:
            # Simulate network lag
            time.sleep(0.6)
            self.captcha_solution, base64_img = self.generate_mock_captcha()
            logger.info(f"Simulated Captcha auto-solved for USN {usn}: {self.captcha_solution}")
            # Return both the image and the pre-solved answer so the backend can bypass user input
            return base64_img, self.captcha_solution
            
        # Real scraping captcha retrieval
        try:
            logger.info(f"Real Scraper: Opening results page: {self.portal_url}")
            self.driver.get(self.portal_url)
            
            # Find captcha image element
            captcha_img_el = self._find_element_by_candidates(self.captcha_img_candidates)
            if not captcha_img_el:
                raise RuntimeError("Could not find the CAPTCHA image element on the VTU results portal.")
                
            # Take screenshot of the captcha element directly
            img_bytes = captcha_img_el.screenshot_as_png
            img_base64 = base64.b64encode(img_bytes).decode("utf-8")
            return f"data:image/png;base64,{img_base64}"
            
        except Exception as e:
            logger.error(f"Error fetching real CAPTCHA: {str(e)}")
            # Try to re-initialize browser and throw
            self.close_browser()
            raise e

    def submit_and_scrape(self, usn: str, captcha_input: str) -> dict:
        """Submits the USN and captcha, and scrapes the result."""
        if self.use_simulation:
            time.sleep(0.8)
            # In simulation mode, captcha is always auto-accepted (no user input needed)
            logger.info(f"Simulation Mode: Auto-accepting captcha for USN {usn} (bypass validation)")
                
            # Check if student is a "Not Found" case (simulate 5% chance of invalid USN)
            if random.random() < 0.05:
                logger.info(f"Simulated Result: USN {usn} not found.")
                return {"status": "not_found", "message": "Result not active or USN not found."}
                
            # Generate simulated result data
            student_name = random.choice(self.mock_names)
            # Remove from list to avoid repetition if possible
            if len(self.mock_names) > 5:
                self.mock_names.remove(student_name)
                
            mock_sub_list = self.generate_mock_subjects(usn)
            subjects = []
            total_marks = 0
            max_marks = 0
            failed_any = False
            
            # Generate grades for each subject
            for sub in mock_sub_list:
                # Lab vs Theory marks
                is_lab = "L" in sub["code"] or "P" in sub["code"]
                sub_max = 100
                
                # Distribution of internal/external marks
                if is_lab:
                    internal = random.randint(35, 50)  # Max 50
                    external = random.randint(30, 50)  # Max 50
                    total = internal + external
                else:
                    internal = random.randint(25, 40)  # Max 40
                    external = random.randint(20, 60)  # Max 60
                    total = internal + external
                
                # Check for fail
                # VTU passing standards: Min 35% in external, and 40% overall
                pass_external = external >= (21 if not is_lab else 18)  # 35%
                pass_total = total >= 40
                
                result_status = "P"
                if not (pass_external and pass_total):
                    result_status = "F"
                    failed_any = True
                    
                subjects.append({
                    "code": sub["code"],
                    "name": sub["name"],
                    "internal": internal,
                    "external": external,
                    "total": total,
                    "result": result_status
                })
                total_marks += total
                max_marks += sub_max
                
            percentage = round((total_marks / max_marks) * 100, 2)
            
            # Determine class / pass fail status
            if failed_any:
                status = "FAIL"
            elif percentage >= 70:
                status = "FIRST CLASS WITH DISTINCTION"
            elif percentage >= 60:
                status = "FIRST CLASS"
            elif percentage >= 50:
                status = "SECOND CLASS"
            else:
                status = "PASS CLASS"
                
            logger.info(f"Simulated Result scraped for {usn}: {student_name} - {status} ({percentage}%)")
            
            return {
                "status": "success",
                "data": {
                    "usn": usn,
                    "name": student_name.upper(),
                    "total_marks": total_marks,
                    "max_marks": max_marks,
                    "percentage": percentage,
                    "status": status,
                    "subjects": subjects
                }
            }

        # Real scraping submission
        main_window = None
        try:
            # Find input elements
            usn_input_el = self._find_element_by_candidates(self.usn_xpath_candidates)
            captcha_input_el = self._find_element_by_candidates(self.captcha_input_candidates)
            submit_btn_el = self._find_element_by_candidates(self.submit_btn_candidates)
            
            if not usn_input_el:
                raise RuntimeError("Could not locate USN input field (tried name='lns', 'lnkns', 'usn', alt='USN', placeholder='USN')")
            if not captcha_input_el:
                raise RuntimeError("Could not locate captcha input field on VTU Results website.")
            if not submit_btn_el:
                raise RuntimeError("Could not locate Submit button on VTU Results website.")
                
            # Enter USN and Captcha
            usn_input_el.clear()
            usn_input_el.send_keys(usn)
            
            captcha_input_el.clear()
            captcha_input_el.send_keys(captcha_input)
            
            # Save original window handle
            main_window = self.driver.current_window_handle
            
            # Click submit
            logger.info(f"Real Scraper: Submitting form for USN {usn} with captcha '{captcha_input}'")
            submit_btn_el.click()
            
            # Wait for result page to fully load — try WebDriverWait first, fallback to fixed sleep
            try:
                WebDriverWait(self.driver, 10).until(
                    lambda d: len(d.find_elements(By.TAG_NAME, "table")) > 0
                )
                time.sleep(0.8)  # Extra wait for full JS render
            except Exception:
                time.sleep(3)  # Fixed wait fallback
            
            logger.info(f"Real Scraper: Page loaded after submit. URL: {self.driver.current_url}")
            
            # 1. Check for Javascript Alert (Usually "Invalid Captcha" or "USN not found")
            alert_text = None
            try:
                alert = self.driver.switch_to.alert
                alert_text = alert.text
                alert.accept()
                logger.warning(f"Browser alert triggered: '{alert_text}'")
            except Exception:
                pass
                
            if alert_text:
                if "captcha" in alert_text.lower():
                    return {"status": "invalid_captcha", "error": "Invalid CAPTCHA code entered. Please try again."}
                else:
                    return {"status": "not_found", "message": f"Result not found for USN: {alert_text}"}
            
            # 2. Check for multiple windows (new tab/window opened by target="_blank")
            all_windows = self.driver.window_handles
            switched = False
            if len(all_windows) > 1:
                # Switch to the new window
                for win in all_windows:
                    if win != main_window:
                        self.driver.switch_to.window(win)
                        switched = True
                        break
                logger.info("Switched to the newly opened result tab.")
                time.sleep(1.0)
                
            # 3. Check page source for common errors
            page_src = self.driver.page_source
            
            if "Invalid Captcha" in page_src or "Captcha Code Required" in page_src or "invalid captcha" in page_src.lower():
                if switched:
                    try:
                        self.driver.close()
                    except Exception:
                        pass
                    try:
                        self.driver.switch_to.window(main_window)
                    except Exception:
                        pass
                return {"status": "invalid_captcha", "error": "Invalid CAPTCHA code entered. Please try again."}
                
            if "University Seat Number is not available" in page_src or "Results are not yet announced" in page_src or "seat number is not available" in page_src.lower():
                if switched:
                    try:
                        self.driver.close()
                    except Exception:
                        pass
                    try:
                        self.driver.switch_to.window(main_window)
                    except Exception:
                        pass
                return {"status": "not_found", "message": "USN not active or result not yet announced."}
                
            # 4. Successful landing - parse results page source
            parsed_data = self._parse_results_page(page_src, usn)
            
            # 5. Clean up window if switched
            if switched:
                try:
                    self.driver.close()
                except Exception:
                    pass
                try:
                    self.driver.switch_to.window(main_window)
                except Exception:
                    pass
                    
            if not parsed_data:
                # If page is empty or tables not found, treat as not found
                return {"status": "not_found", "message": "Failed to extract result table from page content."}
                
            return {"status": "success", "data": parsed_data}
            
        except Exception as e:
            logger.error(f"Error during Real submit_and_scrape: {str(e)}")
            # Try to switch back to main window just in case
            if main_window:
                try:
                    if len(self.driver.window_handles) > 1:
                        for win in self.driver.window_handles:
                            if win != main_window:
                                self.driver.switch_to.window(win)
                                self.driver.close()
                except Exception:
                    pass
                try:
                    self.driver.switch_to.window(main_window)
                except Exception:
                    pass
            return {"status": "error", "error": f"Scraping system exception: {str(e)}"}

    def _parse_results_page(self, html_content: str, usn: str) -> dict:
        """
        Parses the VTU results HTML page source.
        
        IMPORTANT: The VTU result page (resultpage.php) uses CSS div-tables with
        class names 'divTableRow' and 'divTableCell' — NOT actual <table>/<tr>/<td>
        HTML elements. This parser targets those div classes directly.
        """
        soup = BeautifulSoup(html_content, "html.parser")

        # ----------------------------------------------------------------
        # DEBUG: Save page source to exports dir for inspection
        # ----------------------------------------------------------------
        try:
            debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
            os.makedirs(debug_dir, exist_ok=True)
            debug_path = os.path.join(debug_dir, f"debug_{usn}.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.info(f"[DEBUG] Saved result page HTML to: {debug_path}")
        except Exception as de:
            logger.warning(f"Could not save debug HTML: {de}")

        # ----------------------------------------------------------------
        # 1. Extract student name — VTU uses TWO different page layouts:
        #
        #  Format A (newer Sem 8 pages): divTableCell divs
        #    <div class="divTableCell"><b>Student Name</b></div>
        #    <div class="divTableCell"> ABDUL MOIZZ AHMED</div>
        #
        #  Format B (older Sem 5 pages): real HTML <table>/<td>
        #    <td><b>Student Name </b></td>
        #    <td><b>:</b> AAROMAL KRISHNA C K</td>   ← name + colon in same td
        # ----------------------------------------------------------------
        student_name = "UNKNOWN STUDENT"

        # Strategy 1: HTML <td> format — find td with "Student Name", get next td's text
        all_tds = soup.find_all("td")
        for i, td in enumerate(all_tds):
            td_text = td.get_text(separator=" ", strip=True)
            if re.search(r"student\s*name", td_text, re.IGNORECASE) and i + 1 < len(all_tds):
                raw = all_tds[i + 1].get_text(separator=" ", strip=True)
                # Strip leading colon (e.g. ": AAROMAL KRISHNA C K")
                candidate = re.sub(r"^\s*:\s*", "", raw).strip()
                if candidate and len(candidate) > 1 and not re.search(r"student\s*name", candidate, re.IGNORECASE):
                    student_name = candidate
                    break

        # Strategy 2: divTableCell format — adjacent div cells
        if student_name == "UNKNOWN STUDENT":
            all_div_cells = soup.find_all("div", class_="divTableCell")
            for i, cell in enumerate(all_div_cells):
                cell_text = cell.get_text(separator=" ", strip=True)
                if re.search(r"student\s*name", cell_text, re.IGNORECASE):
                    if i + 1 < len(all_div_cells):
                        candidate = all_div_cells[i + 1].get_text(separator=" ", strip=True)
                        candidate = re.sub(r"^\s*:\s*", "", candidate).strip()
                        if candidate and len(candidate) > 1:
                            student_name = candidate
                            break
                    # Inline: "Student Name : FULL NAME"
                    match = re.search(r"student\s*name\s*[:\-]\s*(.+)", cell_text, re.IGNORECASE)
                    if match:
                        student_name = match.group(1).strip()
                        break

        logger.info(f"[PARSE] Student name: '{student_name}'")

        # ----------------------------------------------------------------
        # 2. Find all divTableRows (the marks rows are in these)
        # ----------------------------------------------------------------
        all_div_rows = soup.find_all("div", class_="divTableRow")
        logger.info(f"[PARSE] Found {len(all_div_rows)} divTableRow elements.")

        if not all_div_rows:
            logger.error("[PARSE] No divTableRow elements found. Is this the right page?")
            return None

        # ----------------------------------------------------------------
        # 3. Detect column mapping from the header divTableRow
        #    Header row has cells like: Subject Code | Subject Name | Internal Marks | ...
        # ----------------------------------------------------------------
        col_map = {}
        header_row_idx = -1

        for row_idx, row in enumerate(all_div_rows):
            cells = row.find_all("div", class_="divTableCell")
            if len(cells) < 4:
                continue
            row_text = " ".join(c.get_text(separator=" ", strip=True).lower() for c in cells)
            # Check if this looks like the header row
            if any(kw in row_text for kw in ["subject code", "internal", "external"]):
                for idx, cell in enumerate(cells):
                    txt = cell.get_text(separator=" ", strip=True).lower()
                    txt = re.sub(r"\s+", " ", txt)
                    if re.search(r"subject\s*code|sub\s*code", txt):
                        col_map["code"] = idx
                    elif re.search(r"subject\s*name|sub\s*name", txt):
                        col_map["name"] = idx
                    elif re.search(r"internal", txt):
                        col_map["internal"] = idx
                    elif re.search(r"external", txt):
                        col_map["external"] = idx
                    elif re.search(r"\btotal\b", txt):
                        col_map["total"] = idx
                    elif re.search(r"\bresult\b|\bgrade\b", txt):
                        col_map["result"] = idx
                if "code" in col_map or "internal" in col_map:
                    header_row_idx = row_idx
                    logger.info(f"[PARSE] Header row at index {row_idx}: {col_map}")
                    break

        # Fallback to standard VTU 7-column layout (observed in the actual page)
        if not col_map:
            logger.warning("[PARSE] Header row not found. Using default VTU div-table column positions.")
            col_map = {"code": 0, "name": 1, "internal": 2, "external": 3, "total": 4, "result": 5}

        logger.info(f"[PARSE] Column map: {col_map}")

        # ----------------------------------------------------------------
        # 4. Parse subject data rows (rows after the header)
        # ----------------------------------------------------------------
        subjects = []
        total_marks = 0
        max_marks = 0
        failed_any = False

        def safe_int(text: str) -> int:
            """Parse integer from cell text, handle AB/W/NA/NE etc."""
            clean = re.sub(r"\b(AB|A|W|NA|NE|X)\b", "0", text.strip(), flags=re.IGNORECASE)
            clean = re.sub(r"[^0-9]", "", clean)
            return int(clean) if clean else 0

        data_rows = all_div_rows[header_row_idx + 1:] if header_row_idx >= 0 else all_div_rows

        for row in data_rows:
            cells = row.find_all("div", class_="divTableCell")
            if len(cells) < 3:
                continue

            # --- Extract subject code ---
            code_idx = col_map.get("code", 0)
            if code_idx >= len(cells):
                continue
            raw_code = cells[code_idx].get_text(separator=" ", strip=True)
            code = re.sub(r"\s+", " ", raw_code).strip()

            # Pattern 1: Traditional VTU code starting with 2 digits (e.g. 21CS61, 18MAT31)
            m1 = re.search(r"\b([0-9]{2}[A-Za-z]{2,6}[0-9]{1,2}[A-Za-z]?)\b", code)
            if m1:
                code = m1.group(1).upper()
            else:
                # Pattern 2: Newer VTU codes starting with letters (e.g. BCS801, BINT803B, BIN803B)
                m2 = re.search(r"\b([A-Z]{1,5}[A-Z0-9]{2,8})\b", code.upper())
                if m2:
                    code = m2.group(1)
                elif not re.match(r"^[0-9A-Za-z]{3,12}$", code):
                    continue  # Not a valid code
                else:
                    code = code.upper()

            # Skip header/label text that may have slipped through
            if re.search(r"^(subject|semester|internal|external|total|result|marks|code|name|aicte|announced|nomenclature|abbreviation)$",
                         code, re.IGNORECASE):
                continue

            # --- Extract subject name ---
            name_idx = col_map.get("name", 1)
            subject_name = cells[name_idx].get_text(separator=" ", strip=True).replace("\xa0", " ") \
                if name_idx < len(cells) else code
            subject_name = re.sub(r"\s+", " ", subject_name).strip()

            # --- Extract marks ---
            try:
                internal_idx = col_map.get("internal", 2)
                external_idx = col_map.get("external", 3)
                total_idx    = col_map.get("total", 4)
                result_idx   = col_map.get("result", 5)

                internal = safe_int(cells[internal_idx].get_text()) if internal_idx < len(cells) else 0
                external = safe_int(cells[external_idx].get_text()) if external_idx < len(cells) else 0

                if total_idx < len(cells):
                    total = safe_int(cells[total_idx].get_text())
                    if total == 0 and (internal + external) > 0:
                        total = internal + external
                else:
                    total = internal + external

                # Infer subject max marks: Industry Internship can be out of 200
                sub_max = 200 if total > 100 else 100

                # --- Extract result ---
                if result_idx < len(cells):
                    res_text = cells[result_idx].get_text().strip().upper()
                else:
                    res_text = "F" if external < 18 else "P"

                if re.search(r"\bF\b|FAIL", res_text):
                    result_char = "F"
                    failed_any = True
                elif re.search(r"\bW\b|WITHHELD", res_text):
                    result_char = "W"
                elif re.search(r"\bAB\b|ABSENT", res_text):
                    result_char = "AB"
                    failed_any = True  # Absent = fail for analysis
                else:
                    result_char = "P"

                subjects.append({
                    "code": code,
                    "name": subject_name,
                    "internal": internal,
                    "external": external,
                    "total": total,
                    "result": result_char
                })
                total_marks += total
                max_marks += sub_max
                logger.info(f"[PARSE] ✓ {code} | {subject_name[:45]} | IM={internal} EM={external} T={total}/{sub_max} R={result_char}")

            except (ValueError, IndexError) as e:
                logger.debug(f"[PARSE] Skipping row (error: {e})")
                continue

        if not subjects:
            logger.error("[PARSE] Parsed 0 subjects from divTableRows. Check exports/debug_{usn}.html")
            return None

        percentage = round((total_marks / max_marks) * 100, 2) if max_marks > 0 else 0.0

        # ----------------------------------------------------------------
        # 5. Determine overall result class
        # ----------------------------------------------------------------
        if failed_any:
            status = "FAIL"
        elif percentage >= 70:
            status = "FIRST CLASS WITH DISTINCTION"
        elif percentage >= 60:
            status = "FIRST CLASS"
        elif percentage >= 50:
            status = "SECOND CLASS"
        else:
            status = "PASS CLASS"

        logger.info(f"[PARSE] ✅ {usn} | {student_name} | {len(subjects)} subjects | {total_marks}/{max_marks} = {percentage}% | {status}")

        return {
            "usn": usn,
            "name": student_name.upper(),
            "total_marks": total_marks,
            "max_marks": max_marks,
            "percentage": percentage,
            "status": status,
            "subjects": subjects
        }

