import re

def extract_code(text):

    blocks = re.findall(r"```(?:\w+)?\n(.*?)```", text, re.DOTALL)

    if blocks:
        return blocks[0].strip()

    return text.strip()
