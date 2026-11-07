"""Fetch official documents; search summaries are never answer evidence."""
from __future__ import annotations
import io, re, json, hashlib, logging, os, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from urllib.parse import urljoin, urlparse, parse_qs
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

log = logging.getLogger("nia.sources")
CACHE_DIR = Path(__file__).parent / ".official_cache"
def _ttl_seconds():
    try: return float(os.getenv("OFFICIAL_CACHE_HOURS", "6")) * 3600
    except ValueError: return 6 * 3600
CACHE_TTL = _ttl_seconds()  # OFFICIAL_CACHE_HOURS (default 6); 0 disables reuse
_CACHE_LOCK = Lock()

def _cache_path(url):
    return CACHE_DIR / (hashlib.sha256(("links-v2|" + url).encode()).hexdigest() + ".json")

def retrieved_on(url):
    """ISO date the cached copy of `url` was fetched (today if it is not cached)."""
    try:
        saved = json.loads(_cache_path(url).read_text(encoding="utf-8"))["saved"]
        return time.strftime("%Y-%m-%d", time.localtime(saved))
    except (OSError, ValueError, KeyError):
        return time.strftime("%Y-%m-%d")

def _cached_fetch(url, domains):
    if not allowed(url, domains): raise ValueError("Non-official URL")
    path = _cache_path(url)
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - entry["saved"] < CACHE_TTL:
            return entry["url"], entry["pages"], entry["links"]
    except (OSError, ValueError, KeyError): pass
    result = fetch(url, domains)
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        with _CACHE_LOCK:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"saved": time.time(), "url": result[0], "pages": result[1], "links": result[2]}), encoding="utf-8")
            os.replace(tmp, path)
    except OSError: pass
    return result

MAX_BYTES = 12 * 1024 * 1024

def allowed(url, domains):
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and u.port in (None, 443) and not u.username and any(host == d or host.endswith("." + d) for d in domains)

def fetch(url, domains):
    embedded = parse_qs(urlparse(url).query).get("file", [])
    if embedded:
        direct = urljoin(url, embedded[0])
        if allowed(direct, domains): url = direct
    started = time.monotonic()
    # Check every redirect before following it; bounded downloads and timeouts.
    for _ in range(5):
        if not allowed(url, domains):
            raise ValueError("Non-official URL")
        with requests.get(url, timeout=(4, 8), stream=True, allow_redirects=False) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers["Location"])
                continue
            r.raise_for_status()
            chunks, size = [], 0
            for chunk in r.iter_content(65536):
                if time.monotonic() - started > 15: raise TimeoutError("Download deadline")
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("Document too large")
                chunks.append(chunk)
            data = b"".join(chunks)
            if data.startswith(b"%PDF"):
                reader = PdfReader(io.BytesIO(data))
                if len(reader.pages) > 300:
                    raise ValueError("Too many pages")
                pages = [(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]
                return url, pages, []
            soup = BeautifulSoup(data, "html.parser")
            links = []
            # SEBI PDF viewer URLs encode the actual PDF in ?file=.
            embedded = parse_qs(urlparse(url).query).get("file", [])
            for value in embedded:
                candidate = urljoin(url, value)
                if allowed(candidate, domains): links.append(candidate)
            for tag in soup.find_all(["a", "iframe", "embed", "object"]):
                target = tag.get("href") or tag.get("src") or tag.get("data") or ""
                candidate = urljoin(url, target)
                if (".pdf" in candidate.lower() or "file=" in candidate.lower()) and allowed(candidate, domains):
                    links.append(candidate)
            for tag in soup(["script", "style", "nav", "footer", "header"]): tag.decompose()
            # Preserve labels and actual targets, including HTML portals and help kits.
            # Text-only extraction previously discarded GNSA's download links.
            for anchor in soup.find_all("a", href=True):
                target = urljoin(url, anchor["href"])
                if allowed(target, domains):
                    label = anchor.get_text(" ", strip=True) or "Resource"
                    anchor.replace_with(f"{label} [link: {target}]")
            return url, [(1, soup.get_text(" ", strip=True))], list(dict.fromkeys(links))[:3]
    raise ValueError("Too many redirects")

def _document(item, domains):
    url, title = item["url"], item.get("title")
    seen = set()
    for _ in range(3):
        if url in seen: return None
        seen.add(url)
        try:
            final, pages, links = _cached_fetch(url, domains)
        except Exception as exc:
            log.warning("Source fetch failed (%s): %s", type(exc).__name__, url)
            return None
        if item.get("follow_attachments", True) and links and not final.lower().split("?")[0].endswith(".pdf"):
            url = links[0]
            continue
        if sum(len(text) for _, text in pages) < 250: return None
        return {"url": final, "title": title or urlparse(final).hostname or "Official source", "pages": pages,
                "retrieved_on": retrieved_on(url)}
    return None

def collect(candidates, question, domains, max_documents=4, budget=24000):
    unique = {item["url"]: item for item in candidates if item.get("url") and allowed(item["url"], domains)}
    terms_for_rank = set(re.findall(r"[a-z0-9]+", question.lower())) - {
        "what", "how", "the", "is", "for", "and", "are", "with", "from", "this", "your", "can", "you"}
    if "srn" in terms_for_rank:
        terms_for_rank.update({"service", "request", "number", "iepf", "5"})
    ranked = sorted(unique.values(), key=lambda item: -len(
        terms_for_rank & set(re.findall(r"[a-z0-9]+", (item.get("title", "") + " " + item["url"]).lower()))))
    # Try a second batch when the first URLs fail. Still bounded: up to 8
    # candidates and 4 documents; never silently disable TLS verification.
    selected = ranked[:max(max_documents, 8)]
    docs, seen = [], set()
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for start in range(0, len(selected), 4):
            if start and time.monotonic() - started >= 30:
                break
            results = list(pool.map(lambda item: _document(item, domains), selected[start:start + 4]))
            for doc in results:
                if not doc or doc["url"] in seen:
                    continue
                seen.add(doc["url"])
                doc["id"] = "S" + str(len(docs) + 1)
                docs.append(doc)
                if len(docs) >= max_documents:
                    break
            if len(docs) >= max_documents:
                break
    terms = set(re.findall(r"[a-z0-9]+", question.lower())) - {"what", "how", "the", "for", "and", "are", "with", "from", "this", "your", "have", "can", "you", "only", "that"}
    # Synonyms make short KYC questions match the actual circular terminology.
    if terms & {"srn"}: terms.update({"service", "request", "number", "submission", "correspondence"})
    if terms & {"kyc", "pan"}: terms.update({"kyc", "pan", "contact", "signature", "nomination"})
    if terms & {"email", "mobile"}: terms.update({"email", "mail", "mobile", "contact"})
    for doc in docs:
        cap = budget // max(1, len(docs))
        chunks = []
        for number, content in doc["pages"]:
            for pos in range(0, len(content), 1100):
                piece = content[pos:pos + 1400]
                words = re.findall(r"[a-z0-9]+", piece.lower())
                score = sum(min(words.count(term), 4) for term in terms)
                chunks.append((score, number, pos, piece))
        ranked = sorted(chunks, key=lambda chunk: chunk[0], reverse=True)
        chosen, used = {}, 0
        # Keep commencement/header context without spending entire budget on page 1.
        first = doc["pages"][0]
        header = first[1][:1200]
        chosen[(first[0], 0)] = header
        used += len(header)
        for score, number, pos, piece in ranked:
            if (number, pos) in chosen or score == 0: continue
            if used + len(piece) > cap: continue
            chosen[(number, pos)] = piece
            used += len(piece)
        doc["excerpt"] = "\n".join(f"[Page {number}, excerpt] {piece}" for (number, pos), piece in sorted(chosen.items()))
        del doc["pages"]
    return docs
