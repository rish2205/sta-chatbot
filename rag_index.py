"""Category-based document index for the STA assistant (PDF and DOCX).

Documents live in uploads/<Category folder>/ (see rag_config.json). Each category is
chunked page by page, embedded once, and stored in a SQLite file. Chunks carry category,
filename, page and any document date / version metadata.

Re-indexing is incremental:
  * a category whose files are unchanged is skipped entirely;
  * otherwise it is re-chunked, but only chunk texts never embedded before are sent to the
    embedding API (embeddings are cached by text hash);
  * chunks of changed or deleted files are removed, identical chunks are stored once.

CLI (run with the project's interpreter, no venv activation needed):
  .venv\\Scripts\\python.exe rag_index.py              # sync uploads -> index
  .venv\\Scripts\\python.exe rag_index.py --status     # what is indexed
  .venv\\Scripts\\python.exe rag_index.py --rebuild    # re-chunk everything (embeddings stay cached)
  .venv\\Scripts\\python.exe rag_index.py --search "signature mismatch" --category kyc
"""
from __future__ import annotations

import argparse
import array
import fnmatch
from functools import lru_cache
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
CHUNKER_VERSION = 2
SUPPORTED = (".pdf", ".docx")
log = logging.getLogger("nia.rag")


def load_secrets() -> None:
    """Reuse .streamlit/secrets.toml so keys are configured in one place."""
    path = BASE_DIR / ".streamlit" / "secrets.toml"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r'\s*([A-Z_]+)\s*=\s*(?:"([^"]*)"|(\d+|[Tt]rue|[Ff]alse))\s*(?:#.*)?$', line)
        if m and m.group(1) not in os.environ:
            os.environ[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)


def load_config() -> dict:
    return json.loads((BASE_DIR / "rag_config.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ extraction
_GLYPH = re.compile(r"/c(\d{1,3})")


def clean_text(text: str) -> str:
    """Repair common PDF extraction damage and normalise whitespace (keeps line breaks)."""
    if len(_GLYPH.findall(text)) > 20:  # fonts that expose raw glyph ids, e.g. "/c49/c46"
        text = _GLYPH.sub(lambda m: chr(int(m.group(1))), text)
    # Lone surrogates (seen in symbol-font bullets, e.g. the joint-holder transmission PDF) cannot be
    # UTF-8 encoded for hashing or storage; drop them rather than abort the whole category.
    text = re.sub(r"[\ud800-\udfff]", "", text)
    text = re.sub(r"�{2,}", "____", text)
    text = re.sub(r"(?<=\w)�(?=\w)", "'", text)
    text = re.sub(r"(?<=\s)�(?=\s)", "-", text)
    text = text.replace("�", "")
    lines = [" ".join(line.split()) for line in text.replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line)


def extract_pdf(path: Path) -> list[tuple[int | None, str]]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return [(i, clean_text(page.extract_text() or "")) for i, page in enumerate(reader.pages, 1)]


def extract_docx(path: Path) -> list[tuple[int | None, str]]:
    """Plain-text extraction with the standard library (paragraphs and tables, no layout/pages)."""
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))

    def para(p) -> str:
        parts = []
        for node in p.iter():
            if node.tag == w + "t" and node.text:
                parts.append(node.text)
            elif node.tag in (w + "tab", w + "br"):
                parts.append(" ")
        return "".join(parts).strip()

    lines: list[str] = []
    body = root.find(w + "body")
    for child in (body if body is not None else []):
        if child.tag == w + "p":
            lines.append(para(child))
        elif child.tag == w + "tbl":
            for row in child.iter(w + "tr"):
                cells = [" ".join(para(p) for p in cell.iter(w + "p")).strip() for cell in row.findall(w + "tc")]
                lines.append(" | ".join(c for c in cells if c))
    return [(None, clean_text("\n".join(lines)))]


def extract(path: Path) -> list[tuple[int | None, str]]:
    return extract_pdf(path) if path.suffix.lower() == ".pdf" else extract_docx(path)


# -------------------------------------------------------------------- metadata
_DATE_RE = re.compile(
    r"dated\s+(?:the\s+)?([A-Za-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\.?,?\s+\d{4})",
    re.I,
)


def parse_date(raw: str) -> str | None:
    raw = re.sub(r"(\d)(st|nd|rd|th)", r"\1", raw.replace(".", "").replace(",", " "), flags=re.I)
    raw = " ".join(raw.split())
    for fmt in ("%B %d %Y", "%d %B %Y", "%b %d %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def detect_doc_date(pages: list[tuple[int | None, str]]) -> str | None:
    """First 'dated <date>' in the opening pages. Filenames are never used for dates."""
    head = " ".join(" ".join(t.split()) for _, t in pages[:2])
    m = _DATE_RE.search(head)
    return parse_date(m.group(1)) if m else None


# -------------------------------------------------------------------- chunking
def norm(text: str) -> str:
    return " ".join(text.lower().split())


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    out, start, n = [], 0, len(text)
    while start < n:
        end = min(n, start + size)
        if end < n:
            window = text[start + int(size * 0.6):end]
            cut = max(window.rfind("\n"), window.rfind(". "))
            if cut > 0:
                end = start + int(size * 0.6) + cut + 1
        piece = text[start:end].strip()
        if len(piece) >= 40:
            out.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


# ------------------------------------------------------------------ embeddings
def openai_embedder(model: str):
    from openai import OpenAI

    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set (needed to embed new content)")
    client = OpenAI(api_key=key, timeout=15.0, max_retries=0)
    import pii_mask
    pii_mask.guard(client)  # query embeddings are audited like every other OpenAI call

    def embed(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            resp = client.embeddings.create(model=model, input=texts[i:i + 64])
            out.extend(d.embedding for d in resp.data)
        return out

    return embed



def _pack(vec) -> bytes:
    length = math.sqrt(sum(x * x for x in vec)) or 1.0
    return array.array("f", (x / length for x in vec)).tobytes()


def _unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


# ------------------------------------------------------------------- retrieval
_STOP = set(
    "a an and are as at be by can do does for from how i if in is it me my of on or the to what when "
    "which who why will with you your should would could about this that there their they".split()
)


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _STOP:
            continue
        if len(w) > 4 and w.endswith("s"):
            w = w[:-1]
        out.append(w)
        equivalents = {"paper": "document", "paperwork": "document", "proof": "document",
                       "finish": "complete", "compliant": "kyc",
                       "deceased": "death", "passed": "death", "lost": "duplicate",
                       "payout": "dividend"}
        if w in equivalents:
            out.append(equivalents[w])
    return out


def is_iepf_filing_query(query: str) -> bool:
    words = set(tokens(query))
    if "transfer" in words and not words & {"claim", "refund", "sample"}:
        return False
    return bool(words & {"sample", "procedure", "proces", "process", "online", "file",
                         "filing", "fill", "submit", "submission", "apply", "application", "refund"})


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS categories (category TEXT PRIMARY KEY, fingerprint TEXT, synced_at TEXT);
CREATE TABLE IF NOT EXISTS docs (
  category TEXT, filename TEXT, relpath TEXT, sha256 TEXT, size INTEGER, pages INTEGER, chunks INTEGER,
  title TEXT, doc_date TEXT, effective TEXT, supersedes TEXT, version TEXT, form_code TEXT, indexed_at TEXT,
  PRIMARY KEY (category, filename));
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY, category TEXT, filename TEXT, page INTEGER, ord INTEGER, text TEXT, text_sha TEXT);
CREATE INDEX IF NOT EXISTS chunks_cat ON chunks(category);
CREATE TABLE IF NOT EXISTS embeddings (text_sha TEXT, model TEXT, vec BLOB, PRIMARY KEY (text_sha, model));
"""


class RagIndex:
    def __init__(self, db_path: Path | None = None, embed=None, config: dict | None = None,
                 uploads_dir: Path | None = None):
        self.cfg = config or load_config()
        self.db_path = Path(db_path) if db_path else BASE_DIR / self.cfg["index_path"]
        self.uploads = Path(uploads_dir) if uploads_dir else BASE_DIR / self.cfg["uploads_dir"]
        self.model = os.getenv("EMBED_MODEL", self.cfg.get("embedding_model", "text-embedding-3-small"))
        self._embed = embed
        self.size = int(self.cfg.get("chunk_chars", 1100))
        self.overlap = int(self.cfg.get("chunk_overlap", 150))

    # ---------------------------------------------------------------- plumbing
    def _con(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
        return con

    def _embedder(self):
        if self._embed is None:
            self._embed = openai_embedder(self.model)
        return self._embed

    @lru_cache(maxsize=128)
    def _query_vector(self, query: str):
        # Cache successful query embeddings in memory; no repeat API call for
        # identical retrieval text while this server/index instance is alive.
        return _unpack(_pack(self._embedder()([query])[0]))

    def _form_files(self) -> dict[str, str]:
        """relpath (lower-case) -> form code, from forms.json 'file' entries."""
        path = BASE_DIR / "forms.json"
        if not path.exists():
            return {}
        return {
            f["file"].replace("\\", "/").lower(): f["code"]
            for f in json.loads(path.read_text(encoding="utf-8"))
            if f.get("file")
        }

    def _form_versions(self) -> dict[str, str]:
        path = BASE_DIR / "forms.json"
        if not path.exists():
            return {}
        return {f["code"]: f.get("version", "") for f in json.loads(path.read_text(encoding="utf-8"))}

    def discover(self) -> tuple[dict[str, list[Path]], list[str]]:
        """Files per category. Folder names are matched case-insensitively against the config."""
        found: dict[str, list[Path]] = {c: [] for c in self.cfg["categories"]}
        warnings: list[str] = []
        folders = {p.name.lower(): p for p in self.uploads.iterdir() if p.is_dir()} if self.uploads.exists() else {}
        known = set()
        for cat, spec in self.cfg["categories"].items():
            folder = folders.get(spec["folder"].lower())
            known.add(spec["folder"].lower())
            if folder is None:
                warnings.append(f"folder uploads/{spec['folder']} not found for category '{cat}'")
                continue
            files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)
            stems_pdf = {p.with_suffix("").as_posix().lower() for p in files if p.suffix.lower() == ".pdf"}
            for p in files:
                if any(fnmatch.fnmatch(p.name, pat) for pat in spec.get("skip_files", [])):
                    continue  # e.g. extracted annexure PDFs: kept for download, not indexed
                if p.suffix.lower() == ".docx" and p.with_suffix("").as_posix().lower() in stems_pdf:
                    warnings.append(f"{p.name}: a PDF with the same name exists, indexing the PDF only")
                    continue
                found[cat].append(p)
        for name in folders:
            if name not in known:
                warnings.append(f"uploads/{folders[name].name} is not a configured category; ignored")
        return found, warnings

    @staticmethod
    def _sha_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()

    # -------------------------------------------------------------------- sync
    def sync(self, rebuild: bool = False, lexical_only: bool = False) -> dict:
        found, warnings = self.discover()
        form_files = self._form_files()
        form_versions = self._form_versions()
        overrides = self.cfg.get("documents", {})
        report: dict = {"warnings": warnings, "errors": [], "categories": {}}
        con = self._con()
        try:
            stored = {r["category"]: r["fingerprint"] for r in con.execute("SELECT * FROM categories")}
            for cat in list(stored):  # category dropped from config
                if cat not in self.cfg["categories"]:
                    for tbl in ("chunks", "docs", "categories"):
                        con.execute(f"DELETE FROM {tbl} WHERE category=?", (cat,))
            for cat, files in found.items():
                shas = {p: self._sha_file(p) for p in files}
                fp = hashlib.sha256(json.dumps([
                    CHUNKER_VERSION, self.size, self.overlap, "lex" if lexical_only else self.model,
                    sorted((p.relative_to(self.uploads).as_posix(), s) for p, s in shas.items()),
                    {k: v for k, v in overrides.items() if k in {p.name for p in files}},
                ]).encode()).hexdigest()
                if not rebuild and stored.get(cat) == fp:
                    report["categories"][cat] = {"status": "unchanged", "files": len(files)}
                    continue
                self._rebuild_category(con, cat, files, shas, fp, form_files, form_versions, overrides,
                                       lexical_only, report)
            con.execute(
                "DELETE FROM embeddings WHERE text_sha NOT IN (SELECT text_sha FROM chunks)")
            con.commit()
        finally:
            con.close()
        return report

    def _rebuild_category(self, con, cat, files, shas, fp, form_files, form_versions, overrides,
                          lexical_only, report):
        docs, chunks, dup = [], {}, 0
        skipped = []
        for path in files:
            rel = path.relative_to(self.uploads).as_posix()
            try:
                pages = extract(path)
            except Exception as exc:  # corrupt / encrypted file: report, keep going
                report["errors"].append(f"{rel}: {type(exc).__name__}: {exc}")
                skipped.append(rel)
                continue
            if sum(len(t) for _, t in pages) < 40:
                report["errors"].append(f"{rel}: no extractable text (scanned image? convert/OCR it first)")
                skipped.append(rel)
                continue
            ov = overrides.get(path.name, {})
            code = form_files.get(rel.lower())
            count = 0
            for page, text in pages:
                for n, piece in enumerate(chunk_text(text, self.size, self.overlap)):
                    key = norm(piece)
                    cid = hashlib.sha256((cat + "|" + key).encode()).hexdigest()[:40]
                    if cid in chunks:
                        dup += 1
                        continue
                    chunks[cid] = (cid, cat, path.name, page, n, piece, hashlib.sha256(key.encode()).hexdigest())
                    count += 1
            docs.append((
                cat, path.name, rel, shas[path], path.stat().st_size, len(pages), count,
                ov.get("title"), ov.get("date") or detect_doc_date(pages), ov.get("effective"),
                ov.get("supersedes"), ov.get("version") or (form_versions.get(code) if code else None) or None,
                code, datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ))
        embedded = 0
        if not lexical_only:
            have = {r[0] for r in con.execute("SELECT text_sha FROM embeddings WHERE model=?", (self.model,))}
            todo = {c[6]: c[5] for c in chunks.values() if c[6] not in have}
            if todo:
                shas_list = list(todo)
                vectors = self._embedder()([todo[s] for s in shas_list])  # may raise: nothing written yet
                con.executemany("INSERT OR REPLACE INTO embeddings VALUES (?,?,?)",
                                [(s, self.model, _pack(v)) for s, v in zip(shas_list, vectors)])
                embedded = len(shas_list)
        con.execute("DELETE FROM chunks WHERE category=?", (cat,))
        con.execute("DELETE FROM docs WHERE category=?", (cat,))
        con.executemany("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", docs)
        con.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?)", list(chunks.values()))
        con.execute("INSERT OR REPLACE INTO categories VALUES (?,?,?)",
                    (cat, fp, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        con.commit()
        report["categories"][cat] = {
            "status": "indexed", "files": len(docs), "chunks": len(chunks), "embedded_now": embedded,
            "duplicates_skipped": dup, "unreadable": skipped,
        }

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        con = self._con()
        try:
            out = {"index_path": str(self.db_path), "model": self.model, "categories": {}}
            for r in con.execute(
                    "SELECT category, COUNT(*) f, SUM(chunks) c FROM docs GROUP BY category ORDER BY category"):
                out["categories"][r["category"]] = {"files": r["f"], "chunks": r["c"]}
            out["files"] = [dict(r) for r in con.execute(
                "SELECT category, filename, pages, chunks, doc_date, effective, version, form_code FROM docs "
                "ORDER BY category, filename")]
            out["total_chunks"] = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            out["embeddings"] = con.execute("SELECT COUNT(*) FROM embeddings WHERE model=?", (self.model,)).fetchone()[0]
            return out
        finally:
            con.close()

    def doc_path(self, relpath: str) -> Path | None:
        """Indexed documents only: resolves a relpath through the index, never the raw filesystem."""
        con = self._con()
        try:
            row = con.execute("SELECT relpath FROM docs WHERE lower(relpath)=lower(?)", (relpath,)).fetchone()
        finally:
            con.close()
        if not row:
            return None
        p = self.uploads / row["relpath"]
        return p if p.is_file() else None

    # ------------------------------------------------------------------ search
    def kyc_overview_evidence(self, query):
        """Read registered local KYC forms directly; retain indexed guidance as well.

        This temporary path does not depend on missing/stale form-code metadata.
        Only forms registered in the configured KYC folder are eligible.
        """
        seeds = self.search('kyc', query)
        registry = json.loads((BASE_DIR / 'forms.json').read_text(encoding='utf-8'))
        folder = self.cfg['categories']['kyc']['folder']
        hits, seen = [], set()
        for form in registry:
            relative = form.get('file', '')
            path = (self.uploads / relative).resolve()
            category_root = (self.uploads / folder).resolve()
            if not relative or not path.is_relative_to(category_root) or path.suffix.lower() != '.pdf':
                continue
            if path in seen:
                continue
            seen.add(path)
            if not path.is_file():
                log.warning('Registered KYC PDF missing: %s', relative)
                continue
            for page, text in extract_pdf(path):
                if not text.strip():
                    continue
                hits.append({'id': hashlib.sha256((relative + str(page) + text).encode()).hexdigest(),
                    'category': 'kyc', 'filename': path.name, 'page': page, 'text': text,
                    'relpath': path.relative_to(self.uploads).as_posix(), 'form_code': form['code'],
                    'title': form.get('name'), 'version': form.get('version'),
                    'doc_date': None, 'effective': None, 'supersedes': None,
                    'score': 1.0, 'cos': 0, 'lex': 0})
        filenames = {hit['filename'] for hit in hits}
        log.info('KYC registered PDF evidence forms=%s pages=%s',
                 sorted({h['form_code'] for h in hits}), len(hits))
        return hits + [h for h in seeds if h['filename'] not in filenames]

    def search(self, category: str, query: str, k: int | None = None, use_vectors: bool = True) -> list[dict]:
        """Chunks of `category` plus chunks of explicitly mapped supporting forms (rag_config 'supporting')."""
        k = k or int(self.cfg.get("top_k", 8))
        support = self.cfg.get("supporting", {}).get(category, [])
        con = self._con()
        try:
            marks = ",".join("?" * len(support)) or "''"
            rows = con.execute(
                "SELECT c.id, c.category, c.filename, c.page, c.text, c.text_sha, d.relpath, d.doc_date, d.effective, "
                "d.version, d.title, d.supersedes, d.form_code, e.vec FROM chunks c "
                "JOIN docs d ON d.category=c.category AND d.filename=c.filename "
                "LEFT JOIN embeddings e ON e.text_sha=c.text_sha AND e.model=? "
                f"WHERE c.category=? OR d.form_code IN ({marks})",
                [self.model, category, *support],
            ).fetchall()
        finally:
            con.close()
        if not rows:
            return []
        # Expand concepts, not fixed menu strings: semantic vectors remain primary
        # when present, and lexical synonyms also work without an API key.
        query_terms = set(tokens(query))
        expansions = []
        if category == "kyc":
            if query_terms & {"document", "proof", "paper", "paperwork", "acceptable", "submit", "kyc", "compliant"}:
                expansions.append("PAN proof address bank cancelled cheque passbook statement documents mode submission post electronic e-sign")
            if query_terms & {"signature", "sign", "mismatch", "specimen"}:
                expansions.append("specimen signature mismatch banker confirmation ISR-2")
            if query_terms & {"complete", "process", "compliant", "compliance", "convert", "finish", "kyc"}:
                expansions.append("contact mobile email signature mode submission post electronic e-sign")
        broad_kyc = category == "kyc" and bool(expansions) and not bool(
            query_terms & {"signature", "mismatch", "nomination", "nominee", "opt"})
        if category == "iepf" and is_iepf_filing_query(query):
            expansions.append("IEPF-5 claim form attachments nodal officer indemnity acknowledgement "
                              "cancelled cheque client master proof entitlement submission")
        retrieval_query = query + " " + " ".join(expansions)
        q_tokens = tokens(retrieval_query)
        docs_tokens = [tokens(r["text"]) for r in rows]
        n = len(rows)
        avg = (sum(len(t) for t in docs_tokens) / n) or 1.0
        df: dict[str, int] = {}
        for toks in docs_tokens:
            for w in set(toks):
                df[w] = df.get(w, 0) + 1
        lex = []
        for toks in docs_tokens:
            score = 0.0
            for w in set(q_tokens):
                tf = toks.count(w)
                if tf:
                    idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                    score += idf * tf * 2.4 / (tf + 1.4 * (0.25 + 0.75 * len(toks) / avg))
            lex.append(score)
        top_lex = max(lex) or 1.0
        qvec = None
        if use_vectors and any(r["vec"] for r in rows):
            try:
                qvec = self._query_vector(retrieval_query)
            except Exception as exc:  # no key / network: fall back to keyword-only ranking
                log.warning("Query embedding unavailable (%s); using keyword ranking only", type(exc).__name__)
        min_cos = float(self.cfg.get("min_cosine", 0.28))
        hits = []
        for r, score in zip(rows, lex):
            cos = 0.0
            if qvec is not None and r["vec"]:
                cos = sum(a * b for a, b in zip(qvec, _unpack(r["vec"])))
            if score == 0 and cos < min_cos:
                continue
            blended = (0.6 * cos + 0.4 * score / top_lex) if qvec is not None else score / top_lex
            hits.append({
                "id": r["id"], "category": r["category"], "filename": r["filename"], "relpath": r["relpath"],
                "page": r["page"], "text": r["text"], "doc_date": r["doc_date"], "effective": r["effective"],
                "version": r["version"], "title": r["title"], "supersedes": r["supersedes"],
                "form_code": r["form_code"], "score": round(blended, 4), "cos": round(cos, 4),
                "lex": round(score, 4),
            })
        hits.sort(key=lambda h: -h["score"])
        if broad_kyc:
            # Include submission instructions as well as the form's opening fields.
            # Still use the uploaded evidence; never manufacture an answer.
            preferred = [h for h in hits if h["form_code"] == "ISR-1"
                         or re.search(r"ISR[-_ ]?1", h["filename"], re.I)]
            if preferred:
                by_page = {}
                preferred.sort(key=lambda h: (0 if "e-sign" in h["text"].lower() else 1, -h["score"]))
                for h in preferred:
                    if h["page"] and h["page"] > 4:
                        continue
                    by_page.setdefault(h["page"], []).append(h)
                chosen = []
                while by_page and len(chosen) < k:
                    for page in list(by_page):
                        if len(chosen) >= k:
                            break
                        chosen.append(by_page[page].pop(0))
                        if not by_page[page]:
                            del by_page[page]
                # Preserve newer guidance alongside the form's document tables.
                guidance = [h for h in hits if h not in preferred
                            and "faq" in h["filename"].lower()][:2]
                if guidance:
                    chosen = chosen[:max(1, k - len(guidance))] + guidance
                ids = {h["id"] for h in chosen}
                return (chosen + [h for h in hits if h["id"] not in ids])[:k]
        if category == "iepf" and is_iepf_filing_query(query):
            # The filing checklist is on page 5 of the uploaded sample. A short
            # question should not let repeated IEPF mentions in reference notes
            # crowd out the actual form's instructions.
            sample = [h for h in hits if h["form_code"] == "IEPF-5-SAMPLE"]
            sample.sort(key=lambda h: ({5: 0, 4: 1}.get(h["page"], 2), -h["score"]))
            chosen = sample[:min(4, k)]
            ids = {h["id"] for h in chosen}
            return (chosen + [h for h in hits if h["id"] not in ids])[:k]
        return hits[:k]


def _print_report(rep: dict, idx: RagIndex) -> None:
    for w in rep["warnings"]:
        print("warning:", w)
    for cat, r in rep["categories"].items():
        print(f"{cat:13s}", r)
    for e in rep["errors"]:
        print("ERROR:", e)
    st = idx.status()
    print(f"\nIndex file : {st['index_path']}")
    print(f"Documents  : {sum(c['files'] for c in st['categories'].values())}   "
          f"Chunks: {st['total_chunks']}   Embeddings({st['model']}): {st['embeddings']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sync uploads/ into the STA document index")
    ap.add_argument("--rebuild", action="store_true", help="re-chunk all categories (cached embeddings are reused)")
    ap.add_argument("--lexical-only", action="store_true", help="index text without embeddings (no API key needed)")
    ap.add_argument("--status", action="store_true", help="show what is indexed and exit")
    ap.add_argument("--search", help="run a sample retrieval")
    ap.add_argument("--category", default="kyc")
    args = ap.parse_args(argv)
    load_secrets()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    idx = RagIndex()
    if args.status:
        print(json.dumps(idx.status(), indent=2))
        return 0
    if args.search:
        for h in idx.search(args.category, args.search):
            print(f"{h['score']:.3f} cos={h['cos']:.2f} lex={h['lex']:.1f}  {h['category']}/{h['filename']} p.{h['page']}"
                  f"  date={h['doc_date']}\n    {' '.join(h['text'].split())[:200]}")
        return 0
    try:
        rep = idx.sync(rebuild=args.rebuild, lexical_only=args.lexical_only)
    except Exception as exc:
        print(f"Indexing failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    _print_report(rep, idx)
    return 1 if rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
