import requests
import json

def query_ollama(model, prompt, url, logger=None):

    if logger:
        logger(f"LLM request -> {model}")

    try:

        r = requests.post(
            url,
            json={
                "model": model,
                "prompt": prompt,
                "stream": False
            },
            timeout=1800
        )

        data = r.json()

        return data.get("response", "")

    except requests.exceptions.Timeout:

        if logger:
            logger(f"TIMEOUT {model}")

        return ""

    except Exception as e:

        if logger:
            logger(f"ERROR {model}: {str(e)}")

        return ""
