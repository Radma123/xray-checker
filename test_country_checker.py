import socket
import requests

def detect_country(address: str) -> str:
    try:
        ip = socket.gethostbyname(address)
    except Exception:
        return "Unknown"

    try:
        r = requests.get(f"https://ipwho.is/{ip}", timeout=5)
        data = r.json()

        if not data.get("success", False):
            return "Unknown"

        country = data.get("country_code") or "Unknown"
        return country
    except Exception:
        return "Unknown"
    
if __name__ == "__main__":
    print(detect_country("www.google.com"))
