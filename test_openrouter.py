import os
import requests
from dotenv import load_dotenv
load_dotenv()

api_key = os.getenv("OPENROUTER_API_KEY")
print("API Key exists:", bool(api_key))

url = "https://openrouter.ai/api/v1/chat/completions"
headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}
data = {
    "model": "z-ai/glm-5.2",
    "messages": [
        {"role": "user", "content": "Say hello!"}
    ]
}

print("Sending request to OpenRouter...")
try:
    response = requests.post(url, headers=headers, json=data, timeout=30)
    print("Status code:", response.status_code)
    print("Response JSON:")
    print(response.json())
except Exception as e:
    print("Error occurred:", e)
