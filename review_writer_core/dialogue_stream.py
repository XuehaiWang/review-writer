"""Expose only the reply string from an incomplete JSON model response."""
import json
import re


def partial_reply(raw):
    match = re.match(r'^\s*(?:```(?:json)?\s*)?\{\s*"reply"\s*:\s*"', raw)
    if not match:
        return ""
    tail = raw[match.end():]
    # Decode complete JSON string characters only, never candidate JSON or reasoning.
    index = 0
    while index < len(tail):
        char = tail[index]
        if char == '"':
            break
        if char == '\\':
            size = 6 if tail[index:index + 2] == '\\u' else 2
            if index + size > len(tail):
                break
            try:
                json.loads('"' + tail[index:index + size] + '"')
            except ValueError:
                break
            index += size
        else:
            index += 1
    try:
        result = json.loads('"' + tail[:index] + '"')
    except ValueError:
        return ""
    return result.encode('utf-8', errors='ignore').decode('utf-8')
