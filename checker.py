import google.generativeai as genai
import sys

# --- IMPORTANT ---
# Paste your API key here (the same one from honeynet_manager.py)
API_KEY = "AIzaSyASlXNe4VWu5h7HjgmeZ9pU5D8Bj60a_zM"

if API_KEY == "...":
    print("="*50)
    print("ERROR: Please edit 'check_models.py' and paste your API key.")
    print("="*50)
    sys.exit(1)

try:
    genai.configure(api_key=API_KEY)
except Exception as e:
    print(f"Error configuring API: {e}")
    sys.exit(1)

print("\n--- Finding Models Your API Key Can Use ---")

found_models = False
for m in genai.list_models():
  # We check for the 'generateContent' method, because that's what our honeypot uses
  if 'generateContent' in m.supported_generation_methods:
    print(f"\n[+] Found usable model: {m.name}")
    print(f"    Supported Methods: {m.supported_generation_methods}")
    found_models = True

if not found_models:
    print("\n--- No models found that support 'generateContent' ---")

print("\n----------------------------------------------")
print("Check the list above. The most common model name is 'gemini-pro'.")
print("Copy that model name and paste it into 'honeynet_manager.py'.")