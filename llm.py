import os
import json
import re
from openai import OpenAI
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Default free models on OpenRouter
DEFAULT_MODEL = "qwen/qwen3.7-plus"

def get_openrouter_client():
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("[!] OPENROUTER_API_KEY is not set in environment or .env file.")
        
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key
    )

def chat_with_llm(messages, model=None, temperature=0.4):
    """
    Send an ongoing harness conversation to OpenRouter and return assistant text.
    """
    if not model:
        model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)

    client = get_openrouter_client()
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        extra_headers={
            "HTTP-Referer": "https://github.com/deeterbleater/archive-archiver",
            "X-Title": "ALGE Archive Harness",
        },
        temperature=temperature,
    )
    return response.choices[0].message.content.strip()


def chat_completion(messages, model=None, tools=None, temperature=0.4):
    """
    Return the raw OpenRouter chat completion for harness tool-calling loops.
    """
    if not model:
        model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)

    client = get_openrouter_client()
    kwargs = {
        "model": model,
        "messages": messages,
        "extra_headers": {
            "HTTP-Referer": "https://github.com/deeterbleater/archive-archiver",
            "X-Title": "ALGE Archive Harness",
        },
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    return client.chat.completions.create(**kwargs)

def parse_page_with_llm(cleaned_html, url, model=None):
    """
    Sends cleaned webpage content to OpenRouter LLM to extract structured metadata.
    """
    if not model:
        model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        
    client = get_openrouter_client()
    
    prompt = f"""
You are an expert archive librarian and web scraper assistant.
Your task is to analyze the cleaned text and links of a webpage from a digital archive (like The Anarchist Library or Anna's Archive) and extract metadata about the work and all its available download formats/versions.

Webpage URL: {url}

Cleaned Webpage Content:
{cleaned_html}

Instructions:
1. Extract the work's "title" and "author".
2. Extract all available download files/versions. For each file, identify:
   - "format": The file format (e.g. EPUB, PDF, MOBI, HTML, TXT, Audio).
   - "url": The web URL where this file version can be found or downloaded. If it's a relative path, resolve it relative to the page URL.
   - "file_size": The size of the file (e.g., "1.2 MB", "450 KB", or "Unknown" if not specified).
   - "download_source": The name of the host or mirror (e.g., "Direct", "IPFS", "Libgen Mirror 1", "Anarchist Library HTTP", "Internet Archive").
   - "download_url": The direct download URL (if available, otherwise set to the same as "url").

Return ONLY a valid JSON object matching the following structure. Do not output any conversational text, explanations, or formatting other than the JSON block.

{{
  "title": "Work Title",
  "author": "Work Author or null",
  "files": [
    {{
      "format": "PDF",
      "url": "https://example.com/download.pdf",
      "file_size": "2.4 MB",
      "download_source": "Direct Download",
      "download_url": "https://example.com/download.pdf"
    }}
  ]
}}
"""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a precise data extraction agent. You output only structured JSON."},
                {"role": "user", "content": prompt}
            ],
            extra_headers={
                "HTTP-Referer": "https://github.com/google-gemini/antigravity",
                "X-Title": "Archive Crawler"
            },
            temperature=0.1
        )
        
        raw_output = response.choices[0].message.content.strip()
        
        # Extract JSON from markdown block if LLM formats it that way
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_output, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            json_str = raw_output
            
        # Parse JSON
        parsed_data = json.loads(json_str)
        return parsed_data
        
    except json.JSONDecodeError as e:
        print(f"[!] Error decoding JSON from LLM response: {e}")
        print("Raw output from LLM was:")
        print(raw_output)
    except Exception as e:
        print(f"[!] OpenRouter API call error: {e}")
        
    return None

def generate_search_queries(topic, model="z-ai/glm-5.2"):
    """
    Uses the top-level model (GLM-5.2) to generate 3 to 5 specific search terms
    corresponding to a broad research topic.
    """
    client = get_openrouter_client()
    
    prompt = f"""
You are an intelligent research coordinator model.
Your task is to break down a broad topic into 3 to 5 specific search queries (keywords or titles) that can be used to query archive indexes (like Archive.org, Anna's Archive, and The Anarchist Library).

Broad Topic: {topic}

Return ONLY a valid JSON object matching the following structure. Do not output any conversational text, explanations, or formatting other than the JSON block.

{{
  "queries": [
    "first specific search term",
    "second specific search term",
    "third specific search term"
  ]
}}
"""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a precise query generation assistant. You output only structured JSON."},
                {"role": "user", "content": prompt}
            ],
            extra_headers={
                "HTTP-Referer": "https://github.com/google-gemini/antigravity",
                "X-Title": "Archive Crawler"
            },
            temperature=0.7
        )
        
        raw_output = response.choices[0].message.content.strip()
        
        # Extract JSON
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_output, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            json_str = raw_output
            
        parsed = json.loads(json_str)
        return parsed.get("queries", [])
    except Exception as e:
        print(f"[!] Query generation error: {e}")
        
    # Fallback to simple queries if LLM fails
    words = topic.split()
    if len(words) > 2:
        return [topic, " ".join(words[:2]), " ".join(words[2:])]
    return [topic]

def generate_research_report(topic, works_data, model="z-ai/glm-5.2"):
    """
    Uses the top-level model (GLM-5.2) to synthesize the logged works
    and compile a beautiful Markdown research report.
    """
    client = get_openrouter_client()
    
    # Format works details into a text payload
    works_summary = ""
    for idx, work in enumerate(works_data):
        works_summary += f"{idx + 1}. Title: {work['title']} | Author: {work['author']}\n"
        works_summary += f"   Search Query: {work['search_query']}\n"
        works_summary += "   Available Versions:\n"
        for f in work.get("files", []):
            works_summary += f"     - [{f['format']}] Source: {f['download_source']} | Link: {f['download_url']} ({f['file_size']})\n"
        works_summary += "\n"
        
    prompt = f"""
You are an expert research synthesist.
Your goal is to write a comprehensive, beautifully structured Markdown research report based on a set of digitized works found on a specific topic.

Topic: {topic}

Collected Works and Download Resources:
{works_summary}

Instructions:
1. Provide an executive summary of the research topic, contextualizing the findings.
2. Structure the report with clear headings, bullets, and tables.
3. List the discovered works, organized logically (e.g. by author, theme, or archive source).
4. Provide direct markdown links to the download URLs for the versions (PDF, EPUB, etc.) so the user can easily download them.
5. Identify any potential gaps or areas for further research.
6. Make the layout look professional, using best practices in Markdown presentation.

Return ONLY the Markdown content. Do not add conversational intro/outro text.
"""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a professional academic research reporter. You output detailed Markdown analysis."},
                {"role": "user", "content": prompt}
            ],
            extra_headers={
                "HTTP-Referer": "https://github.com/google-gemini/antigravity",
                "X-Title": "Archive Crawler"
            },
            temperature=0.3
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[!] Report generation error: {e}")
        return None
