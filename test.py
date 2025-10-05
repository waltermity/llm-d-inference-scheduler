import requests

gateway_url="http://localhost:30080"

try:
    endpoint = f"{gateway_url}/v1/models"
    headers = {"Content-Type": "application/json",
               "x-qos": "premium"
    }
    
    r = requests.get(endpoint, headers=headers, timeout=10)
    r.raise_for_status()
    response = r.json()

    print("Inference result:", response)

    #---------------Completion---------------------
    completion_endpoint = f"{gateway_url}/v1/completions"
    payload = {"model": "food-review", "prompt": "How are you today?"}
    resp = requests.post(completion_endpoint, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    print("\n\nCompletion result:", resp.json())
except Exception as e:
    print(f"Error: {e}")
'''
try:
    #---------------Mistral 7B Instruct---------------------
    completion_endpoint = f"{gateway_url}/v1/completions"
    payload = {
        "model": "mistralai/Mistral-7B-Instruct-v0.2",
        "prompt": "hi",
        "max_tokens": 16,
        "temperature": 0
    }
    resp = requests.post(completion_endpoint, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    print("\n\nMistral completion result:", resp.json())
except Exception as e:
    print(f"Error: {e}")
'''
    