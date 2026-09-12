#!/usr/bin/env python3
"""Prior-art sweep: GitHub repos related to LibreOffice + AI/agents/LLM."""
import json, subprocess, sys, time

def api(path):
    out = subprocess.run(
        ["curl", "-s", "-m", "30", "-H", "Accept: application/vnd.github+json",
         "https://api.github.com" + path],
        capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except Exception:
        return {}

QUERIES = [
    "libreoffice+llm", "libreoffice+chatgpt", "libreoffice+openai",
    "libreoffice+ollama", "libreoffice+ai+extension", "libreoffice+mcp",
    "libreoffice+chatbot", "libreoffice+agent", "libreoffice+copilot",
    "libreoffice+gpt", "libreoffice+claude", "libreoffice+uno+ai",
    "localwriter", "librethinker", "libreoffice+sidecar",
    "libreoffice+headless+api", "uno+python+automation+ai",
]

seen = {}
for q in QUERIES:
    d = api(f"/search/repositories?q={q}&sort=stars&per_page=20")
    for r in d.get("items", []):
        key = r["full_name"]
        rec = {"stars": r["stargazers_count"],
               "desc": (r["description"] or "")[:150],
               "url": r["html_url"],
               "lang": r.get("language"),
               "pushed": (r.get("pushed_at") or "")[:10],
               "topics": r.get("topics", [])}
        if key not in seen or rec["stars"] > seen[key]["stars"]:
            seen[key] = rec
    time.sleep(2)

print(f"# GitHub prior-art sweep — {len(seen)} unique repos\n")
for k, v in sorted(seen.items(), key=lambda x: -x[1]["stars"]):
    if v["stars"] < 2 and "libre" not in k.lower() and "uno" not in k.lower():
        continue
    print(f"{v['stars']:6d} {v['pushed']} {str(v['lang']):14s} {k}")
    print(f"         {v['desc']}")
    if v["topics"]:
        print(f"         topics: {','.join(v['topics'][:12])}")
    print(f"         {v['url']}")
