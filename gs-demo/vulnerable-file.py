import os

	API_KEY = "sk-live-demo-hardcoded-key-12345"

def getRequest(url):
    headers = {"Authorization": f"Bearer {API_KEY}"}
    return headers, url

def run_command(message):
    os.system("echo " + message)
