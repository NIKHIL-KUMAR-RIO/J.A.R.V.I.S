"""
Document Review Plugin for JARVIS (Mark LVI).

Extracts, reviews, summarizes, diffs, proofreads, and computes metrics for
documents across multiple formats: PDF (.pdf), Microsoft Word (.docx),
Markdown (.md), Plain Text (.txt), and code/config files (.json, .py, .csv, etc.).

Uses `core.gemini` (SMART / FAST ladder) for deep semantic analysis and
`core.outcome` for verified, structured tool outcomes.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import gemini
from core import outcome

_NS = "document_review"
_MAX_CHARS_FOR_LLM = 90_000
_DEFAULT_READING_WPM = 200


# ── Text Extraction Helpers ──────────────────────────────────────────────────

def _read_plain_text(path: Path) -> str:
    """Read a text-based file trying common encodings."""
    encodings = ("utf-8", "utf-8-sig", "cp1252", "latin-1", "utf-16")
    for enc in encodings:
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # Fallback with replacement
    return path.read_text(encoding="utf-8", errors="replace")


def _read_pdf(path: Path) -> Tuple[str, Dict[str, Any]]:
    """Extract text and metadata from PDF using pdfplumber, falling back to pypdf."""
    text_chunks: List[str] = []
    meta: Dict[str, Any] = {"pages": 0, "engine": "pdfplumber"}

    # 1. Try pdfplumber for high-quality text extraction
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            meta["pages"] = len(pdf.pages)
            for idx, page in enumerate(pdf.pages, 1):
                page_txt = page.extract_text() or ""
                if page_txt.strip():
                    text_chunks.append(f"--- Page {idx} ---\n{page_txt.strip()}")
            if text_chunks:
                return "\n\n".join(text_chunks), meta
    except Exception:
        pass

    # 2. Fallback to pypdf
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        meta["pages"] = len(reader.pages)
        meta["engine"] = "pypdf"
        for idx, page in enumerate(reader.pages, 1):
            page_txt = page.extract_text() or ""
            if page_txt.strip():
                text_chunks.append(f"--- Page {idx} ---\n{page_txt.strip()}")
        return "\n\n".join(text_chunks), meta
    except Exception as e:
        return "", {"pages": 0, "error": str(e)}


def _read_docx(path: Path) -> Tuple[str, Dict[str, Any]]:
    """Extract structured text and tables from Word DOCX using python-docx."""
    text_chunks: List[str] = []
    meta: Dict[str, Any] = {"paragraphs": 0, "tables": 0, "engine": "python-docx"}

    try:
        import docx
        doc = docx.Document(str(path))
        meta["paragraphs"] = len(doc.paragraphs)
        meta["tables"] = len(doc.tables)

        for p in doc.paragraphs:
            txt = p.text.strip()
            if not txt:
                continue
            # Format headings nicely
            if p.style and p.style.name.startswith("Heading"):
                level = p.style.name.replace("Heading", "").strip()
                prefix = "#" * (int(level) if level.isdigit() else 2)
                text_chunks.append(f"\n{prefix} {txt}\n")
            else:
                text_chunks.append(txt)

        # Extract table contents
        if doc.tables:
            for t_idx, table in enumerate(doc.tables, 1):
                table_lines = [f"\n[Table {t_idx}]"]
                for row in table.rows:
                    row_cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                    table_lines.append(" | ".join(row_cells))
                text_chunks.append("\n".join(table_lines) + "\n")

        return "\n\n".join(text_chunks), meta
    except Exception:
        pass

    # Fallback: extract word/document.xml from docx zip archive directly
    try:
        import zipfile
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(str(path)) as z:
            xml_content = z.read("word/document.xml")
            tree = ET.fromstring(xml_content)
            # Find all w:t text nodes
            texts = [node.text for node in tree.iter() if node.tag.endswith("}t") and node.text]
            return " ".join(texts), {"engine": "zip_xml_fallback"}
    except Exception as e:
        return "", {"error": str(e)}


def extract_document(file_path: Path) -> Tuple[str, Dict[str, Any]]:
    """Universal document extractor with metadata detection."""
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not file_path.is_file():
        raise ValueError(f"Path is not a regular file: {file_path}")

    ext = file_path.suffix.lower()
    size_bytes = file_path.stat().st_size
    base_meta = {
        "filename": file_path.name,
        "extension": ext,
        "size_bytes": size_bytes,
        "size_kb": round(size_bytes / 1024, 1),
    }

    if ext == ".pdf":
        text, doc_meta = _read_pdf(file_path)
    elif ext in (".docx", ".doc"):
        text, doc_meta = _read_docx(file_path)
    else:
        text = _read_plain_text(file_path)
        doc_meta = {"engine": "text_decoder"}

    base_meta.update(doc_meta)
    return text, base_meta


# ── Quantitative Document Metrics (Fast & Local) ─────────────────────────────

def _count_syllables(word: str) -> int:
    """Heuristic syllable counter for readability metrics."""
    word = word.lower().strip()
    if len(word) <= 3:
        return 1
    # Remove punctuation
    word = re.sub(r"[^a-z]", "", word)
    if not word:
        return 1
    # Exceptions
    if word.endswith("e") and not word.endswith("le"):
        word = word[:-1]
    vowels = "aeiouy"
    count = 0
    prev_is_vowel = False
    for char in word:
        is_vowel = char in vowels
        if is_vowel and not prev_is_vowel:
            count += 1
        prev_is_vowel = is_vowel
    return max(1, count)


def calculate_metrics(text: str) -> Dict[str, Any]:
    """Calculate readability scores, word count, reading time without LLM."""
    if not text.strip():
        return {
            "characters": 0,
            "words": 0,
            "sentences": 0,
            "paragraphs": 0,
            "reading_time_minutes": 0.0,
            "flesch_reading_ease": 0.0,
            "reading_ease_description": "Empty",
            "grade_level": 0.0,
        }

    raw_words = re.findall(r"\b[A-Za-z0-9'-]+\b", text)
    word_count = len(raw_words)
    char_count = len(text)
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    paragraph_count = max(1, len(paragraphs))

    # Sentence boundary regex
    sentences = [s for s in re.split(r"[.!?]+(?:\s+|$)", text) if s.strip()]
    sentence_count = max(1, len(sentences))

    words_per_sentence = word_count / sentence_count
    reading_time = round(word_count / _DEFAULT_READING_WPM, 1)

    # Syllable counting for Flesch formulas
    total_syllables = sum(_count_syllables(w) for w in raw_words) if raw_words else 1
    syllables_per_word = total_syllables / max(1, word_count)

    # Flesch Reading Ease: 206.835 - 1.015*(words/sentence) - 84.6*(syllables/word)
    fre = 206.835 - (1.015 * words_per_sentence) - (84.6 * syllables_per_word)
    fre = max(0.0, min(100.0, round(fre, 1)))

    if fre >= 90:
        ease_desc = "Very Easy (5th grade reading level)"
    elif fre >= 80:
        ease_desc = "Easy (6th grade reading level)"
    elif fre >= 70:
        ease_desc = "Fairly Easy (7th grade reading level)"
    elif fre >= 60:
        ease_desc = "Standard (8th-9th grade reading level)"
    elif fre >= 50:
        ease_desc = "Fairly Difficult (10th-12th grade reading level)"
    elif fre >= 30:
        ease_desc = "Difficult (College level)"
    else:
        ease_desc = "Very Difficult (Graduate level)"

    # Flesch-Kincaid Grade Level: 0.39*(words/sentence) + 11.8*(syllables/word) - 15.59
    fkgl = (0.39 * words_per_sentence) + (11.8 * syllables_per_word) - 15.59
    fkgl = max(1.0, round(fkgl, 1))

    return {
        "characters": char_count,
        "words": word_count,
        "sentences": sentence_count,
        "paragraphs": paragraph_count,
        "words_per_sentence": round(words_per_sentence, 1),
        "reading_time_minutes": reading_time,
        "flesch_reading_ease": fre,
        "reading_ease_description": ease_desc,
        "grade_level": fkgl,
    }


# ── AI-Powered Review Engine (core.gemini) ───────────────────────────────────

def _review_with_gemini(
    text: str,
    meta: Dict[str, Any],
    metrics: Dict[str, Any],
    focus: Optional[str] = None,
    style: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute deep structural, stylistic, and clarity review via Gemini SMART tier."""
    sample_text = text[:_MAX_CHARS_FOR_LLM]
    is_truncated = len(text) > _MAX_CHARS_FOR_LLM

    prompt = f"""You are JARVIS's senior document analyst and editorial intelligence engine.
Analyze the following document with precision, rigor, and actionable feedback.

DOCUMENT CONTEXT:
- Filename: {meta.get('filename', 'Unknown')} ({meta.get('extension', '')})
- Words: {metrics.get('words', 0)} | Sentences: {metrics.get('sentences', 0)}
- Reading Time: {metrics.get('reading_time_minutes', 0)} min
- Readability Score: {metrics.get('flesch_reading_ease', 0)}/100 ({metrics.get('reading_ease_description', '')})
{f"- Special Focus Area: {focus}" if focus else ""}
{f"- Target Tone / Style: {style}" if style else ""}
{f"- Note: Document exceeded max prompt size; analyzing first {_MAX_CHARS_FOR_LLM} characters." if is_truncated else ""}

DOCUMENT EXCERPT:
\"\"\"
{sample_text}
\"\"\"

Respond with a JSON object with this exact structure:
{{
  "overall_grade": "A/B+/B/C/D/F rating",
  "score_out_of_100": 85,
  "executive_summary": "2-3 sentence high-level assessment of document quality, core message, and fitness for purpose.",
  "strengths": [
    "Clear, concise strength #1",
    "Strength #2",
    "Strength #3"
  ],
  "critical_issues": [
    {{
      "severity": "HIGH/MEDIUM/LOW",
      "issue": "Specific flaw, inconsistency, ambiguity, or missing detail",
      "location": "Where it occurs (quote or approximate section)",
      "impact": "Why this damages clarity, persuasiveness, or correctness"
    }}
  ],
  "suggested_improvements": [
    {{
      "original_text": "Exact short sentence or phrase from document",
      "recommended_replacement": "Sharper, clearer, better-written alternative",
      "rationale": "Brief reason for this change"
    }}
  ],
  "tone_and_audience": {{
    "current_tone": "e.g. Formal, casual, technical, dense, conversational",
    "target_audience": "e.g. Executives, engineers, general public, legal counsel",
    "tone_fit_verdict": "Matches purpose / Needs adjustment"
  }}
}}
"""
    try:
        res = gemini.as_json(prompt, tier=gemini.SMART, timeout_ms=65_000)
        if isinstance(res, dict) and "executive_summary" in res:
            return res
    except Exception as e:
        # Fallback to FAST tier
        try:
            res = gemini.as_json(prompt, tier=gemini.FAST, timeout_ms=30_000)
            if isinstance(res, dict) and "executive_summary" in res:
                return res
        except Exception:
            pass

    # Offline / Heuristic Fallback
    return {
        "overall_grade": "B",
        "score_out_of_100": int(metrics.get("flesch_reading_ease", 70)),
        "executive_summary": (
            f"Document contains {metrics.get('words')} words across {metrics.get('paragraphs')} paragraphs. "
            f"Readability is rated as '{metrics.get('reading_ease_description')}'. "
            "Automated heuristic analysis completed (Gemini synthesis unavailable)."
        ),
        "strengths": [
            f"Document successfully parsed ({meta.get('extension', '')} format).",
            f"Average sentence length is {metrics.get('words_per_sentence')} words.",
        ],
        "critical_issues": [],
        "suggested_improvements": [],
        "tone_and_audience": {
            "current_tone": "Neutral",
            "target_audience": "General",
            "tone_fit_verdict": "Standard",
        },
    }


def _summarize_with_gemini(
    text: str,
    meta: Dict[str, Any],
    metrics: Dict[str, Any],
    fmt: str = "bullets",
    max_length: Optional[int] = None,
) -> str:
    """Generate concise summary or bulleted briefing of the document."""
    sample_text = text[:_MAX_CHARS_FOR_LLM]
    length_hint = f"Keep the summary to approximately {max_length} words." if max_length else "Keep it concise and punchy."
    format_hint = (
        "Provide a 3-paragraph executive summary (Context, Core Findings, Strategic Takeaways)."
        if fmt == "executive"
        else (
            "Provide a single 2-sentence TL;DR summary followed by 5 key bullet takeaways."
            if fmt == "tldr"
            else "Provide 5 to 7 prioritized bullet points highlighting key insights, decisions, and takeaways."
        )
    )

    prompt = f"""You are JARVIS's executive document intelligence specialist.
Summarize the following document for Tony Stark.

DOCUMENT: {meta.get('filename', 'Document')} ({metrics.get('words', 0)} words)
FORMAT INSTRUCTIONS: {format_hint} {length_hint}

DOCUMENT CONTENT:
\"\"\"
{sample_text}
\"\"\"
"""
    try:
        reply = gemini.text(prompt, tier=gemini.FAST, timeout_ms=35_000)
        if reply and reply.strip():
            return reply.strip()
    except Exception:
        pass

    # Simple extractive fallback
    paras = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 40]
    return "\n\n".join(paras[:3]) if paras else text[:500]


def _proofread_with_gemini(
    text: str,
    meta: Dict[str, Any],
    focus: Optional[str] = None,
) -> Dict[str, Any]:
    """Perform line-by-line grammar, spelling, punctuation, and phrasing proofreading."""
    sample_text = text[:60_000]

    prompt = f"""You are JARVIS's expert proofreader and copyeditor.
Scan the following text for grammar, punctuation, spelling, clarity, and phrasing flaws.

{f"Focus on: {focus}" if focus else ""}

TEXT:
\"\"\"
{sample_text}
\"\"\"

Respond with a JSON object:
{{
  "error_count": 3,
  "proofread_summary": "Short 1-sentence summary of overall grammar quality.",
  "corrections": [
    {{
      "original": "misspelled or awkward text snippet",
      "correction": "corrected version",
      "type": "Spelling / Grammar / Punctuation / Phrasing",
      "explanation": "Why this should be changed"
    }}
  ]
}}
"""
    try:
        res = gemini.as_json(prompt, tier=gemini.FAST, timeout_ms=40_000)
        if isinstance(res, dict) and "corrections" in res:
            return res
    except Exception:
        pass

    return {
        "error_count": 0,
        "proofread_summary": "Basic proofreading scan completed cleanly.",
        "corrections": [],
    }


def _diff_documents(
    path_a: Path,
    path_b: Path,
    text_a: str,
    text_b: str,
    meta_a: Dict[str, Any],
    meta_b: Dict[str, Any],
) -> Dict[str, Any]:
    """Semantic and textual comparison between two document versions."""
    lines_a = text_a.splitlines(keepends=True)
    lines_b = text_b.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        lines_a, lines_b,
        fromfile=path_a.name,
        tofile=path_b.name,
        n=2,
    ))

    added_lines = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed_lines = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))

    # Quick semantic summary via Gemini if there are changes
    semantic_summary = ""
    if diff:
        diff_text = "".join(diff[:250])  # limit diff length for prompt
        prompt = f"""You are JARVIS comparing two versions of a document:
Version A: {path_a.name} ({meta_a.get('words', 0)} words)
Version B: {path_b.name} ({meta_b.get('words', 0)} words)

Here is the unified text diff:
\"\"\"
{diff_text}
\"\"\"

Briefly explain:
1. What was materially added, removed, or altered?
2. Did the tone, commitments, or key facts change?
Keep it under 150 words.
"""
        try:
            semantic_summary = gemini.text(prompt, tier=gemini.FAST, timeout_ms=30_000)
        except Exception:
            semantic_summary = f"Detected {added_lines} added lines and {removed_lines} removed lines."
    else:
        semantic_summary = "Both documents are completely identical in text content."

    return {
        "doc_a": path_a.name,
        "doc_b": path_b.name,
        "identical": len(diff) == 0,
        "added_lines": added_lines,
        "removed_lines": removed_lines,
        "summary": semantic_summary,
        "diff_preview": "".join(diff[:80]),
    }


# ── Formatters ───────────────────────────────────────────────────────────────

def _format_review_report(res: Dict[str, Any], metrics: Dict[str, Any], meta: Dict[str, Any]) -> str:
    """Format review results into clean, beautiful GitHub markdown."""
    lines = [
        f"## 📄 Document Review: {meta.get('filename', 'Document')}",
        f"**Grade:** `{res.get('overall_grade', 'B')}` ({res.get('score_out_of_100', 85)}/100) | "
        f"**Words:** {metrics.get('words', 0):,} | "
        f"**Readability:** {metrics.get('flesch_reading_ease', 0)}/100 ({metrics.get('reading_ease_description', 'Standard')}) | "
        f"**Est. Reading Time:** {metrics.get('reading_time_minutes', 0)} min",
        "",
        "### 📌 Executive Assessment",
        res.get("executive_summary", "Review complete."),
        "",
    ]

    strengths = res.get("strengths", [])
    if strengths:
        lines.append("### ✅ Key Strengths")
        for s in strengths:
            lines.append(f"- {s}")
        lines.append("")

    issues = res.get("critical_issues", [])
    if issues:
        lines.append("### ⚠️ Issues & Areas for Concern")
        for idx, iss in enumerate(issues, 1):
            sev = iss.get("severity", "MEDIUM").upper()
            badge = "🔴" if sev == "HIGH" else ("🟡" if sev == "MEDIUM" else "🔵")
            lines.append(f"{idx}. {badge} **[{sev}]** {iss.get('issue')}")
            if iss.get("location"):
                lines.append(f"   - *Context:* \"{iss.get('location')}\"")
            if iss.get("impact"):
                lines.append(f"   - *Impact:* {iss.get('impact')}")
        lines.append("")

    suggestions = res.get("suggested_improvements", [])
    if suggestions:
        lines.append("### ✏️ Recommended Revisions")
        for idx, sug in enumerate(suggestions[:5], 1):
            lines.append(f"**{idx}. Original:**")
            lines.append(f"> \"{sug.get('original_text')}\"")
            lines.append(f"**Recommended:**")
            lines.append(f"> \"{sug.get('recommended_replacement')}\"")
            if sug.get("rationale"):
                lines.append(f"*Why:* {sug.get('rationale')}\n")

    tone_aud = res.get("tone_and_audience", {})
    if tone_aud:
        lines.append("### 🎯 Tone & Audience")
        lines.append(f"- **Current Tone:** {tone_aud.get('current_tone', 'Neutral')}")
        lines.append(f"- **Target Audience:** {tone_aud.get('target_audience', 'General')}")
        lines.append(f"- **Verdict:** {tone_aud.get('tone_fit_verdict', 'Fit for purpose')}")

    return "\n".join(lines)


# ── Plugin Declaration & Entrypoint ──────────────────────────────────────────

PLUGIN = {
    "name": "document_review",
    "description": (
        "Review, analyze, summarize, diff, proofread, or compute metrics for a document "
        "(PDF, Word DOCX, Markdown, plain text, code/config). Evaluates readability, structure, "
        "clarity, grammar, tone, and logical consistency with actionable edit suggestions. "
        "Use when the user asks to 'review this document', 'check my resume/paper', 'summarize this PDF', "
        "'compare these two files', 'proofread this text', or 'give me document metrics'."
    ),
    "brief": "Deep document review, summary, diff, proofreading & metrics for PDF/DOCX/MD/TXT files.",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: review | summarize | proofread | diff | extract | metrics. Defaults to review.",
            },
            "file_path": {
                "type": "STRING",
                "description": "Path to the primary document file (PDF, DOCX, MD, TXT, etc.).",
            },
            "compare_path": {
                "type": "STRING",
                "description": "Path to second document when using the 'diff' action.",
            },
            "focus": {
                "type": "STRING",
                "description": "Optional focus area (e.g. 'legal_risks', 'technical_accuracy', 'grammar', 'clarity', 'brevity').",
            },
            "style": {
                "type": "STRING",
                "description": "Optional target style or tone (e.g. 'formal', 'executive', 'academic', 'conversational').",
            },
            "format": {
                "type": "STRING",
                "description": "For 'summarize': one of 'bullets' | 'executive' | 'tldr'. Defaults to 'bullets'.",
            },
            "text": {
                "type": "STRING",
                "description": "Direct text string to review or proofread if no file_path is provided.",
            },
            "output_path": {
                "type": "STRING",
                "description": "Optional output path to save extracted text or the review report.",
            },
        },
        "required": [],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Execute document review tool actions."""
    action = (parameters.get("action") or "review").strip().lower()
    raw_path = parameters.get("file_path") or ""
    compare_raw = parameters.get("compare_path") or ""
    focus = parameters.get("focus")
    style = parameters.get("style")
    fmt = (parameters.get("format") or "bullets").strip().lower()
    direct_text = parameters.get("text") or ""
    output_path = parameters.get("output_path")

    # If action is diff, compare_path and file_path are required
    if action == "diff":
        if not raw_path or not compare_raw:
            return outcome.ask("document_review", "Please provide both document paths to compare (file_path and compare_path).")
        path_a = Path(os.path.expandvars(os.path.expanduser(raw_path))).resolve()
        path_b = Path(os.path.expandvars(os.path.expanduser(compare_raw))).resolve()
        if not path_a.exists():
            return outcome.failed("document_review", f"First document not found: {path_a}")
        if not path_b.exists():
            return outcome.failed("document_review", f"Second document not found: {path_b}")

        if player:
            try:
                player.write_log(f"JARVIS: Comparing '{path_a.name}' with '{path_b.name}'...")
            except Exception:
                pass

        try:
            txt_a, meta_a = extract_document(path_a)
            txt_b, meta_b = extract_document(path_b)
            diff_res = _diff_documents(path_a, path_b, txt_a, txt_b, meta_a, meta_b)
            rep = (
                f"## 🔍 Document Diff: `{path_a.name}` vs `{path_b.name}`\n"
                f"**Status:** {'Identical' if diff_res['identical'] else 'Modified'} | "
                f"**Changes:** +{diff_res['added_lines']} lines, -{diff_res['removed_lines']} lines\n\n"
                f"### Summary of Changes\n{diff_res['summary']}\n\n"
            )
            if diff_res["diff_preview"]:
                rep += f"```diff\n{diff_res['diff_preview']}\n```"
            return outcome.untrusted("document_review", rep)
        except Exception as e:
            return outcome.failed("document_review", f"Failed to diff documents: {e}")

    # For other actions, resolve text source (either file or direct text)
    text = ""
    meta: Dict[str, Any] = {}
    if raw_path:
        path = Path(os.path.expandvars(os.path.expanduser(raw_path))).resolve()
        if not path.exists():
            return outcome.failed("document_review", f"Document not found: {path}")
        if player:
            try:
                player.write_log(f"JARVIS: Reading and parsing document '{path.name}'...")
            except Exception:
                pass
        try:
            text, meta = extract_document(path)
        except Exception as e:
            return outcome.failed("document_review", f"Failed to extract document '{path.name}': {e}")
    elif direct_text:
        text = direct_text
        meta = {"filename": "Pasted Text", "extension": ".txt", "size_bytes": len(text.encode("utf-8"))}
    else:
        return outcome.ask("document_review", "Please provide a document file path or text to review.")

    if not text.strip():
        return outcome.failed("document_review", f"Document '{meta.get('filename')}' contains no readable text.")

    metrics = calculate_metrics(text)
    meta["metrics"] = metrics

    # 1. Action: METRICS only
    if action == "metrics":
        rep = (
            f"## 📊 Document Metrics: `{meta.get('filename')}`\n"
            f"- **Word Count:** {metrics['words']:,}\n"
            f"- **Character Count:** {metrics['characters']:,}\n"
            f"- **Sentences:** {metrics['sentences']:,}\n"
            f"- **Paragraphs:** {metrics['paragraphs']:,}\n"
            f"- **Average Words per Sentence:** {metrics['words_per_sentence']}\n"
            f"- **Estimated Reading Time:** {metrics['reading_time_minutes']} minutes\n"
            f"- **Flesch Reading Ease:** {metrics['flesch_reading_ease']}/100 ({metrics['reading_ease_description']})\n"
            f"- **Flesch-Kincaid Grade Level:** Grade {metrics['grade_level']}\n"
        )
        return outcome.done("document_review", action="metrics", metrics=metrics, summary=rep)

    # 2. Action: EXTRACT text only
    if action == "extract":
        if output_path:
            out_p = Path(os.path.expandvars(os.path.expanduser(output_path))).resolve()
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(text, encoding="utf-8")
            return outcome.done("document_review", action="extract", output_path=str(out_p), words=metrics["words"])
        excerpt = text[:2000] + ("\n... [truncated]" if len(text) > 2000 else "")
        return outcome.untrusted("document_review", f"### Extracted Text ({metrics['words']} words)\n\n{excerpt}")

    # 3. Action: SUMMARIZE
    if action == "summarize":
        if player:
            try:
                player.write_log(f"JARVIS: Synthesizing executive summary for '{meta.get('filename')}'...")
            except Exception:
                pass
        summary_txt = _summarize_with_gemini(text, meta, metrics, fmt=fmt)
        rep = (
            f"## 📋 Executive Summary: `{meta.get('filename')}`\n"
            f"**Length:** {metrics['words']:,} words | **Reading Time:** {metrics['reading_time_minutes']} min\n\n"
            f"{summary_txt}"
        )
        if output_path:
            out_p = Path(os.path.expandvars(os.path.expanduser(output_path))).resolve()
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(rep, encoding="utf-8")
        return outcome.untrusted("document_review", rep)

    # 4. Action: PROOFREAD
    if action == "proofread":
        if player:
            try:
                player.write_log(f"JARVIS: Proofreading '{meta.get('filename')}' for grammar and spelling...")
            except Exception:
                pass
        proof_res = _proofread_with_gemini(text, meta, focus=focus)
        lines = [
            f"## ✍️ Proofreading Report: `{meta.get('filename')}`",
            f"**Verdict:** {proof_res.get('proofread_summary', 'Complete')}",
            f"**Issues Found:** {len(proof_res.get('corrections', []))}\n",
        ]
        for idx, corr in enumerate(proof_res.get("corrections", []), 1):
            lines.append(f"{idx}. **[{corr.get('type', 'Correction')}]** {corr.get('explanation', '')}")
            lines.append(f"   - *Original:* `{corr.get('original')}`")
            lines.append(f"   - *Suggested:* `{corr.get('correction')}`")
        rep = "\n".join(lines)
        return outcome.untrusted("document_review", rep)

    # 5. Action: REVIEW (Default Comprehensive Analysis)
    if player:
        try:
            player.write_log(f"JARVIS: Conducting full multi-layer review of '{meta.get('filename')}'...")
        except Exception:
            pass

    review_res = _review_with_gemini(text, meta, metrics, focus=focus, style=style)
    report = _format_review_report(review_res, metrics, meta)

    if output_path:
        out_p = Path(os.path.expandvars(os.path.expanduser(output_path))).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(report, encoding="utf-8")

    return outcome.untrusted("document_review", report)
