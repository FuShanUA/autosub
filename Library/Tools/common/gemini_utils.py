import os
import sys
import time
import threading
import concurrent.futures
import google.generativeai as genai
from enum import Enum
from typing import List, Dict, Optional, Union

# Try to load keys from .env if present
# Determine the .env path
def get_env_path():
    if getattr(sys, 'frozen', False):
        USER_DATA_DIR = os.path.join(os.path.expanduser("~"), "Documents", "AutoSub")
        return os.path.join(USER_DATA_DIR, ".env")
    
    # Search upwards for .env starting from current file
    curr = os.path.dirname(os.path.abspath(__file__))
    while curr != os.path.dirname(curr): # Not at root
        potential = os.path.join(curr, ".env")
        if os.path.exists(potential): return potential
        # Also check one level up from Library/Tools
        curr = os.path.dirname(curr)
    return os.path.join(os.getcwd(), ".env")

ENV_PATH = get_env_path()

try:
    from dotenv import load_dotenv
    if os.path.exists(ENV_PATH):
        load_dotenv(ENV_PATH)
    else:
        # Try finding it in parent or current if not found
        load_dotenv() 
except ImportError:
    pass

class GeminiTier(Enum):
    FREE = "free"       # ~15 RPM
    TIER_1 = "tier1"    # ~300 RPM (Flash), [Tier 1 Level]
    PAYG = "payg"       # ~1000 RPM (Pay-as-you-go)
    ENTERPRISE = "ent"  # ~2000+ RPM

class RateLimiter:
    """Simple thread-safe rate limiter based on RPM."""
    def __init__(self, rpm: int):
        self.rpm = rpm
        self.interval = 60.0 / rpm
        self.last_request_time = 0
        self.lock = threading.Lock()

    def wait(self):
        sleep_time = 0
        with self.lock:
            current_time = time.time()
            # If multiple threads come here, last_request_time might be in the future (reserved by previous thread)
            # So we check against the greater of real time or last scheduled time
            basis_time = max(current_time, self.last_request_time)
            
            # The next allowed request time is basis_time + interval? 
            # Actually, standard logic:
            # next_slot = max(now, last_slot) + interval?
            # No, that's for strict spacing.
            
            # Simple logic: 
            # We enforce that requests are spaced by 'interval'.
            # next_available = self.last_request_time + self.interval
            next_available = self.last_request_time + self.interval
            
            if next_available > current_time:
                sleep_time = next_available - current_time
                self.last_request_time = next_available
            else:
                self.last_request_time = current_time
                
        if sleep_time > 0:
            time.sleep(sleep_time)
            
    def update_rpm(self, rpm: int):
        with self.lock:
            self.rpm = rpm
            self.interval = 60.0 / rpm

class GeminiClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError("API Key not found. Set GEMINI_API_KEY environment variable.")
            
        genai.configure(api_key=self.api_key)
        
        # Determine strictness based on user config or environment
        # Default to PAYG for speed unless specified otherwise
        tier_str = os.environ.get("GEMINI_TIER", "tier1").lower()
        if tier_str == "free":
            self.tier = GeminiTier.FREE
            self.rpm_limit = 12 # Safety margin below 15
            self.max_workers = 1 # Serial execution essentially
        elif tier_str == "tier1":
            self.tier = GeminiTier.TIER_1
            # 300 RPM for Flash models (Tier 1 limit for Flash is around 300)
            # We set slightly conservative limit
            self.rpm_limit = 250 
            self.max_workers = 15 
        else:
            self.tier = GeminiTier.PAYG
            self.rpm_limit = 500 # Conservative high limit
            self.max_workers = 20 # Good concurrency level
            
        self.limiter = RateLimiter(self.rpm_limit)
        
    
        self.retry_lock = threading.Lock() # For limiting retries globally if needed? 
        
    def generate_content(self, prompt: str, model_name: str = "auto-best", fallback: bool = True) -> Optional[str]:
        """
        Synchronous generation with automatic fallback support.
        Default order: Gemini 3 Pro -> Gemini 3 Flash -> Gemini 2.0 Flash.
        """
        
        # 1. Determine Model Chain
        if model_name == "auto-best" or not model_name:
            # User wants best effort. Start with Flagship, then fallback.
            chain = ["gemini-3-pro", "gemini-3-flash", "gemini-2.0-flash"]
        else:
            # User specified a model. Start with that.
            chain = [model_name]
            # If fallback is ON (default), append safer models if they aren't the primary.
            if fallback:
                defaults = ["gemini-3-flash-preview", "gemini-2.0-flash"]
                for d in defaults:
                    if d not in chain:
                        chain.append(d)
        
        last_error = None
        
        # 2. Iterate Models
        for current_model in chain:
            model = genai.GenerativeModel(current_model)
            
            # Simple retry within same model for transient errors
            retries = 2
            backoff = 2
            
            while retries > 0:
                self.limiter.wait()
                try:
                    response = model.generate_content(prompt)
                    if response.text:
                        return response.text.strip()
                    # If empty response without error? Maybe recitation check failure (400) or just empty.
                    # Usually signifies failure to obey prompt.
                    # We treat as empty/fail and maybe retry once then fallback.
                    print(f"⚠️ Empty response from {current_model}. Retrying...")
                    retries -= 1
                    continue
                    
                except Exception as e:
                    error_str = str(e)
                    
                    # 429: Too Many Requests (Quota)
                    # 400: Often Recitation/Safety but can be invalid request.
                    # 503: Service Unavailable.
                    
                    if "429" in error_str:
                        print(f"⚠️ Rate Limit (429) on {current_model}. Switching...")
                        last_error = e
                        break # Break inner loop -> Go to next model in chain
                        
                    elif "500" in error_str or "503" in error_str:
                        # Server error, worth retrying same model
                        print(f"⚠️ Server Error ({error_str[:30]}). Retrying...")
                        time.sleep(backoff)
                        backoff *= 1.5
                        retries -= 1
                        last_error = e
                        
                    else:
                        # Other errors (400, 403 etc)
                        # Might be model specific (e.g. 3 Pro strict safety).
                        # Fallback might help.
                        print(f"❌ Error on {current_model}: {e}")
                        last_error = e
                        break # Try next model
            
            # If we fall through here, it means this model failed completely after retries.
            print(f"🔻 Falling back from {current_model}...")
            
        # If all models failed
        print(f"❌ All models failed. Last error: {last_error}")
        return None

    def generate_batch(self, tasks: List[Dict], model_name: str = "gemini-3-flash") -> List[Dict]:
        """
        Executes a batch of generation tasks in parallel.
        Tasks list format: [{'id': 1, 'prompt': '...'}, ...]
        Returns: [{'id': 1, 'result': '...'}, ...] same order or dict
        """
        results = []
        total = len(tasks)
        print(f"🚀 Starting batch generation for {total} items (Workers: {self.max_workers}, Model: {model_name})...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Create a future for each task
            future_to_task = {
                executor.submit(self.generate_content, task['prompt'], model_name): task 
                for task in tasks
            }
            
            completed = 0
            for future in concurrent.futures.as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    result_text = future.result()
                    results.append({
                        **task,
                        'result': result_text
                    })
                except Exception as e:
                    print(f"❌ Task failed: {e}")
                    results.append({
                        **task,
                        'result': None,
                        'error': str(e)
                    })
                
                completed += 1
                print(f"   Progress: {completed}/{total} (chunks)", flush=True)
                    
        # Sort results by original order if 'index' or 'id' is present?
        # The caller handles sorting usually.
        return results

    def list_accessible_models(self) -> List[str]:
        """
        Returns a list of available Gemini models (e.g. ['gemini-2.0-flash', ...]).
        Filters for 'generateContent' capable models.
        """
        try:
            # We look for models that support generateContent
            models = []
            for m in genai.list_models():
                if 'generateContent' in m.supported_generation_methods:
                    name = m.name.replace("models/", "")
                    # Filter for only gemini models to be safe/clean
                    if "gemini" in name.lower():
                        models.append(name)
            
            # Sort: Pro first, then Flash, then others? 
            # Or just alphabetical. Let's do a custom sort.
            # Prefer newer versions (3 > 2 > 1.5)
            # This is a simple heuristic based on version number extraction
            try:
                models.sort(key=lambda n: float(re.search(r'gemini-(\d+\.?\d*)', n).group(1)) if re.search(r'gemini-(\d+\.?\d*)', n) else 0, reverse=True)
            except:
                pass # Fallback to default sort if regex fails
                
            return models
        except Exception as e:
            print(f"❌ Failed to list models: {e}")
            return []

# Singleton instance accessor if needed
_CLIENT = None
def get_client():
    global _CLIENT
    if not _CLIENT:
        _CLIENT = GeminiClient()
    return _CLIENT
