import socket
import threading
import os
import sys
import time
import configparser
import asyncio
import websockets
import json
import google.generativeai as genai
from values import API_KEY
BUFFER_SIZE = 2048
LOG_FILE = "captured_intel.log"
MASTER_PROMPT_TELNET = """
You are 'SocketSnoop', an AI-powered honeypot. 
You are simulating a compromised Ubuntu 22.04 server running a standard Bash shell.
The user is an attacker who has just successfully 'logged in'. 

Your job is to respond to their commands exactly as a real bash terminal would.

CRITICAL RULES:
1.  **NEVER** break character. NEVER say you are an AI, a model, or a simulation.
2.  Your *entire* response must ONLY be the raw, fake terminal output. Do NOT add notes like "Here is the output:".
3.  If the command is invalid, output a realistic error (e.g., "bash: blah: command not found").
4.  You must maintain **STATEFULNESS**: 
    - If the user runs `touch secret.txt`, your *next* `ls` command MUST show `secret.txt`.
    - If the user runs `cd /var/www`, your *next* prompt MUST reflect this change.
    - Invent a believable but fake file system. Include tempting files like `config.php`, `user_backups.sql`, and `/etc/passwd`.
5.  Your response *MUST* end with the next shell prompt (e.g., `root@honeypot:/var/www# `). You must track the current user and directory in this prompt. The user is logged in as 'root'.
6.  If the user attempts a payload (like a reverse shell: `nc -e ...`, `python -c '...'`), your response must be an inert, realistic error (e.g., "bash: nc: command not found" or "Operation not permitted").
"""

# This is the NEW, correct, simple dictionary format for safety settings.
SAFETY_SETTINGS = [
    {
        "category": "HARM_CATEGORY_HARASSMENT",
        "threshold": "BLOCK_NONE"
    },
    {
        "category": "HARM_CATEGORY_HATE_SPEECH",
        "threshold": "BLOCK_NONE"
    },
    {
        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "threshold": "BLOCK_NONE"
    },
    {
        "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
        "threshold": "BLOCK_NONE"
    },
]
# We also create the GenerationConfig here, as a simple dictionary.
GENERATION_CONFIG = {"temperature": 0.2}


# --- (PHASE 5) THE "VAULT": UPGRADED LOGGER ---
log_lock = threading.Lock()
connected_clients = set() # For WebSocket
log_queue = asyncio.Queue() # The new, stable bridge
dashboard_event_loop = None # <-- THIS IS THE ASYNCIO FIX (Part 1)

async def broadcast(message):
    """Broadcasts a log message to all connected WebSocket clients."""
    if connected_clients:
        clients_copy = connected_clients.copy()
        for ws in clients_copy:
            try:
                await ws.send(message)
            except websockets.exceptions.ConnectionClosed:
                connected_clients.remove(ws)

def log_event(message_type, data):
    """
    A thread-safe function to log all events to console, file,
    and the live dashboard.
    """
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_message = f"[{timestamp}] [{message_type}] {data}"
    
    # 1. Log to console
    print(log_message)
    
    # 2. Log to file (thread-safe)
    try:
        with log_lock:
            with open(LOG_FILE, 'a') as f:
                f.write(log_message + "\n")
    except Exception as e:
        print(f"Log file error: {e}")
        
    # 3. Broadcast to Dashboard (as JSON)
    log_json = json.dumps({
        "timestamp": timestamp,
        "type": message_type,
        "data": data
    })
    
    # --- THIS IS THE ASYNCIO FIX (Part 2) ---
    try:
        # Use the *stored* event loop instead of trying to "get" it
        if dashboard_event_loop:
            dashboard_event_loop.call_soon_threadsafe(log_queue.put_nowait, log_json)
        else:
            # This will only happen in the first second before the dashboard is ready
            print("[MANAGER-WARNING] Dashboard event loop not ready. Log skipped.")
    except Exception as e:
        # No more silent failures!
        print(f"!!! CRITICAL QUEUE ERROR: {e} !!!")

# --- (PHASE 3) THE "LABYRINTH": AI SANDBOX (TELNET) ---
def run_ai_sandbox(conn, addr, username):
    """
    Hands the connection over to the Gemini AI for the stateful,
    high-interaction deception (TELNET/BASH PERSONALITY).
    """
    log_event("AI-SANDBOX", f"{addr[0]}:{addr[1]} - Handed off to AI-TELNET Sandbox.")
    try:
        # All settings are passed HERE, when the model is created.
        model = genai.GenerativeModel(
            'gemini-pro-latest',  # <-- THIS IS THE MODEL NAME FIX
            safety_settings=SAFETY_SETTINGS,
            generation_config=GENERATION_CONFIG
        )
        
        # start_chat() now only takes the history.
        chat = model.start_chat(
            history=[
                {'role': 'user', 'parts': [MASTER_PROMPT_TELNET]},
                {'role': 'model', 'parts': [f"{username}@honeypot:/home/{username}# "]}
            ]
        )
        
        # Send the first prompt
        first_prompt = f"{username}@honeypot:/home/{username}# "
        conn.sendall(first_prompt.encode('utf-8'))

        while True:
            data = conn.recv(BUFFER_SIZE)
            if not data:
                log_event("CONN-DROP", f"{addr[0]}:{addr[1]} - Attacker disconnected.")
                break
            
            cmd = data.decode('utf-8', errors='ignore').strip()
            
            # Handle empty "Enter" presses
            if not cmd:
                conn.sendall(first_prompt.encode('utf-8')) # Just send the prompt again
                continue
            
            if cmd.lower() == 'exit':
                log_event("CONN-EXIT", f"{addr[0]}:{addr[1]} - Attacker typed 'exit'.")
                break
            
            log_event("TTP-CAPTURE", f"{addr[0]}:{addr[1]} (TELNET) - CMD: {cmd}")

            # send_message() now only takes the command.
            response = chat.send_message(cmd)
            ai_response = response.text
            
            log_event("AI-RESPONSE", f"{addr[0]}:{addr[1]} (TELNET) - RSP: {ai_response.strip()}")
            conn.sendall(ai_response.encode('utf-8'))
            
            # Update the prompt for the next loop (in case cd changed it)
            new_prompt_line = ai_response.strip().split('\n')[-1]
            if new_prompt_line.endswith(('# ', '$ ')): # Check if it's a valid prompt
                first_prompt = new_prompt_line + " " # Add a space for clean input
            elif not ai_response.strip():
                 conn.sendall(first_prompt.encode('utf-8'))

    except Exception as e:
        log_event("AI-ERROR", f"{addr[0]}:{addr[1]} - Error in AI Sandbox: {e}")
        # Send a generic error to the attacker
        try:
            conn.sendall(b"bash: command interpreter error. connection reset.\n")
        except socket.error:
            pass # Connection already closed

# --- (PHASE 2) THE "LURE": CLIENT HANDLERS ---

def handle_ftp_lure(conn, addr):
    """
    A simple, low-interaction FTP lure.
    It pretends to be a vsFTPd server and captures credentials.
    """
    client_ip, client_port = addr
    log_event("LURE-FTP", f"{client_ip}:{client_port} - FTP Lure Engaged.")
    try:
        conn.sendall(b'220 (vsFTPd 3.0.3) service ready.\r\n')
        
        # Get USER
        user_cmd = conn.recv(BUFFER_SIZE).decode('utf-8', errors='ignore').strip()
        if not user_cmd.upper().startswith('USER '):
            conn.sendall(b'500 USER required.\r\n')
            return
        username = user_cmd[5:]
        conn.sendall(b'331 Please specify the password.\r\n')
        
        # Get PASS
        pass_cmd = conn.recv(BUFFER_SIZE).decode('utf-8', errors='ignore').strip()
        if not pass_cmd.upper().startswith('PASS '):
            conn.sendall(b'500 PASS required.\r\n')
            return
        password = pass_cmd[5:]

        # *** CAPTURE ***
        log_event("CAPTURE-FTP", f"*** CAPTURE *** {client_ip}:{client_port} (FTP) - User: '{username}' Pass: '{password}'")
        
        time.sleep(1) # Dramatic pause
        conn.sendall(b'530 Login incorrect. Closing connection.\r\n')

    except socket.error:
        log_event("CONN-DROP", f"{client_ip}:{client_port} (FTP) - Client disconnected.")
    finally:
        conn.close()

def handle_ai_telnet(conn, addr):
    """
    Handles the AI-TELNET service.
    Performs the fake login and hands off to the AI sandbox.
    """
    client_ip, client_port = addr
    log_event("LURE-TELNET", f"{client_ip}:{client_port} - AI-TELNET Lure Engaged.")
    try:
        conn.sendall(b'login: ')
        username = conn.recv(BUFFER_SIZE).decode('utf-8', errors='ignore').strip()
        
        conn.sendall(b'Password: ')
        password = conn.recv(BUFFER_SIZE).decode('utf-8', errors='ignore').strip()

        # *** CAPTURE ***
        log_event("CAPTURE-TELNET", f"*** CAPTURE *** {client_ip}:{client_port} (TELNET) - User: '{username}' Pass: '{password}'")

        conn.sendall(b'\nWelcome! Login successful.\n')
        time.sleep(0.5)

        run_ai_sandbox(conn, addr, username if username else "root")

    except socket.error:
        log_event("CONN-DROP", f"{client_ip}:{client_port} (TELNET) - Client disconnected.")
    except Exception as e:
        log_event("ERR", f"Error in Telnet handler: {e}")
    finally:
        log_event("CONN-CLOSE", f"{client_ip}:{client_port} (TELNET) - Connection closed.")
        conn.close()

# --- (PHASE 1) THE "FORTRESS": MANAGER & LISTENERS ---

def start_service_listener(port, service_type):
    """
    Binds to a port and listens for a specific service type.
    This runs in its own thread.
    """
    service_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    service_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        service_socket.bind((CONFIG['MANAGER']['HOST'], port))
        service_socket.listen(5)
        log_event("MANAGER-START", f"Service [{service_type}] listening on port {port}")

        while True:
            conn, addr = service_socket.accept()
            
            if service_type == 'AI-TELNET':
                handler_thread = threading.Thread(target=handle_ai_telnet, args=(conn, addr))
            elif service_type == 'LURE-FTP':
                handler_thread = threading.Thread(target=handle_ftp_lure, args=(conn, addr))
            else:
                log_event("MANAGER-ERROR", f"No handler found for service type: {service_type}")
                conn.close()
                continue
                
            handler_thread.daemon = True
            handler_thread.start()

    except PermissionError:
        log_event("MANAGER-FATAL", f"Permission denied to bind to port {port}. Try a port > 1024 or run as sudo.")
    except Exception as e:
        log_event("MANAGER-FATAL", f"Service [{service_type}] on port {port} crashed: {e}")
    finally:
        service_socket.close()

# --- DASHBOARD WEBSOCKET SERVER (NEW ROBUST VERSION) ---

async def log_consumer():
    """
    NEW: This async task endlessly waits for messages in the queue
    and broadcasts them to all connected dashboard clients.
    """
    log_event("DASHBOARD", "Log consumer started. Waiting for logs...")
    while True:
        try:
            message = await log_queue.get()
            await broadcast(message)
            log_queue.task_done()
        except Exception as e:
            print(f"Log consumer error: {e}")

async def websocket_handler(websocket):
    """Handles new WebSocket connections from the dashboard."""
    log_event("DASHBOARD", f"Dashboard client connected: {websocket.remote_address}")
    connected_clients.add(websocket)
    try:
        await websocket.wait_closed()
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        log_event("DASHBOARD", f"Dashboard client disconnected: {websocket.remote_address}")
        connected_clients.remove(websocket)

async def start_websocket_server(host, port):
    """Starts the WebSocket server AND the new log consumer."""
    log_event("MANAGER-START", f"Starting Dashboard WebSocket server on ws://{host}:{port}")
    
    # NEW: Create the consumer task
    consumer_task = asyncio.create_task(log_consumer())
    
    async with websockets.serve(websocket_handler, host, port):
        await consumer_task # Run forever

def run_websocket_server_in_thread(host, port):
    """Runs the async WebSocket server in a separate thread."""
    global dashboard_event_loop # <-- THIS IS THE ASYNCIO FIX (Part 3a)
    # Set a new event loop for this thread
    asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()
    dashboard_event_loop = loop # <-- THIS IS THE ASYNCIO FIX (Part 3b)
    loop.run_until_complete(start_websocket_server(host, port))

# --- MAIN ---
def main():
    global CONFIG
    CONFIG = configparser.ConfigParser()
    
    if API_KEY == "YOUR_GEMINI_API_KEY_HERE":
        print("="*50)
        print("ERROR: API Key not set!")
        print("Please edit 'honeynet_manager.py' and replace 'YOUR_GEMINI_API_KEY_HERE'")
        print("="*50)
        sys.exit(1)
    
    try:
        genai.configure(api_key=API_KEY)
        genai.GenerativeModel('gemini-pro-latest') # Test with the correct model
        print("Gemini API key configured successfully.")
    except Exception as e:
        print(f"Error configuring Gemini API: {e}")
        sys.exit(1)
        
    # --- Load Config ---
    if not os.path.exists('config.ini'):
        print("FATAL_ERR: 'config.ini' not found. Please create it.")
        sys.exit(1)
    CONFIG.read('config.ini')
    
    log_event("MANAGER-INIT", "*** SocketSnoop AI Honeynet Manager Starting Up (v3 - FINAL) ***")

    try:
        ws_host = CONFIG['MANAGER']['HOST']
        ws_port = int(CONFIG['MANAGER']['WEBSOCKET_PORT'])
        ws_thread = threading.Thread(target=run_websocket_server_in_thread, args=(ws_host, ws_port))
        ws_thread.daemon = True
        ws_thread.start()
        # Give the event loop a moment to start
        time.sleep(1) 
    except Exception as e:
        log_event("MANAGER-FATAL", f"Could not start WebSocket server: {e}")
        sys.exit(1)

    # --- Start Honeynet Service Listeners ---
    if 'SERVICES' not in CONFIG:
        log_event("MANAGER-FATAL", "No [SERVICES] section in config.ini. Nothing to do.")
        sys.exit(1)

    for port_str, service_type in CONFIG['SERVICES'].items():
        try:
            port = int(port_str)
            listener_thread = threading.Thread(target=start_service_listener, args=(port, service_type))
            listener_thread.daemon = True
            listener_thread.start()
        except ValueError:
            log_event("MANAGER-ERROR", f"Invalid port '{port_str}' in config.ini. Skipping.")
        except Exception as e:
            log_event("MANAGER-ERROR", f"Error starting service {service_type}: {e}")

    log_event("MANAGER-INIT", "All services launched. Honeynet is active.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log_event("MANAGER-SHUTDOWN", "Shutdown signal received. Exiting.")

if __name__ == "__main__":
    main()