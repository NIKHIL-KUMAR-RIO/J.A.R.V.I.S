import json
import math
import re
import sqlite3
from datetime import datetime
from threading import Lock
from pathlib import Path
import sys


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
MEMORY_PATH      = BASE_DIR / "memory" / "long_term.json"
DB_PATH          = BASE_DIR / "memory" / "knowledge.db"
_lock            = Lock()
MAX_VALUE_LENGTH = 380

# ── Why there are two very different numbers here ────────────────────────────
#
# There used to be one: MEMORY_MAX_CHARS = 2200, applied to the whole store. It
# was a *storage* limit, and it existed only because the entire memory was
# pasted into the system prompt on every connect — so growing the memory grew
# every single request. When it filled, _trim_to_limit() deleted the oldest
# entries and printed one line to a console nobody reads. A memory described as
# "deeply remembers projects, preferences and personal context" was in practice
# two pages long, and quietly forgot your sister's name after a few weeks.
#
# Storage and prompt budget are now separate concerns:
#
#   MEMORY_MAX_CHARS  — a runaway guard, not a feature limit. Nothing normal
#                       reaches it; a bug writing in a loop does.
#   PROMPT_CORE_CHARS — what actually rides in the system prompt every session.
#                       Smaller than the old whole-memory dump, so sessions
#                       start *faster* than before, not slower.
#
# Everything above the core stays on disk and is fetched on demand by the
# recall_memory tool — see search_memory() and format_memory_for_prompt().
MEMORY_MAX_CHARS  = 200_000
PROMPT_CORE_CHARS = 900
PROMPT_INDEX_CHARS = 420
# Most entries any one category may contribute to the core block, so a person
# with forty stored preferences still gets their sister into the prompt.
PROMPT_MAX_PER_CATEGORY = 6

def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "wishes":        {},
        "notes":         {},
    }

def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return _empty_memory()
    with _lock:
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_memory()
                for key in base:
                    if key not in data:
                        data[key] = {}
                return data
            return _empty_memory()
        except Exception as e:
            print(f"[Memory] ⚠️ Load error: {e}")
            return _empty_memory()

def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


# Set by main.py so a trim can reach the activity log. Deleting something a
# person told you and mentioning it only on stdout is how a memory loses trust.
_trim_notifier = None


def set_trim_notifier(fn) -> None:
    """Register a callable(str) that surfaces trims to the user."""
    global _trim_notifier
    _trim_notifier = fn


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    entries = _all_entries(memory)
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    dropped = []
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        dropped.append(f"{cat}/{key}")
        print(f"[Memory] 🗑️  Trimmed {cat}/{key}")
    if dropped and _trim_notifier:
        try:
            _trim_notifier(
                f"SYS: Memory full — forgot {len(dropped)} oldest entries "
                f"({', '.join(dropped[:3])}{'…' if len(dropped) > 3 else ''})"
            )
        except Exception:
            pass
    return memory

def _init_sqlite() -> None:
    """Initialize the SQLite database for indexed knowledge and facts."""
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(DB_PATH), timeout=5.0) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    category TEXT,
                    key TEXT,
                    value TEXT,
                    updated TEXT,
                    PRIMARY KEY(category, key)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT,
                    title TEXT,
                    content TEXT,
                    created_at TEXT
                )
            """)
            conn.commit()
    except Exception as e:
        print(f"[Memory] ⚠️ SQLite init warning: {e}")


def _sync_to_sqlite(memory: dict) -> None:
    """Sync memory dictionary to SQLite knowledge database for indexing and querying."""
    try:
        _init_sqlite()
        with sqlite3.connect(str(DB_PATH), timeout=5.0) as conn:
            cur = conn.cursor()
            for cat, items in memory.items():
                if not isinstance(items, dict):
                    continue
                for key, entry in items.items():
                    val = _entry_value(entry)
                    if not val:
                        continue
                    up = (entry.get("updated", "") if isinstance(entry, dict) else "") or datetime.now().strftime("%Y-%m-%d")
                    cur.execute(
                        "INSERT OR REPLACE INTO facts (category, key, value, updated) VALUES (?, ?, ?, ?)",
                        (cat, key, val, up),
                    )
            conn.commit()
    except Exception as e:
        print(f"[Memory] ⚠️ SQLite sync warning: {e}")


def save_memory(memory: dict) -> None:
    if not isinstance(memory, dict):
        return
    memory = _trim_to_limit(memory)
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    _sync_to_sqlite(memory)


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            new_val  = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            entry    = {"value": new_val, "updated": datetime.now().strftime("%Y-%m-%d")}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    memory = load_memory()
    if _recursive_update(memory, memory_update):
        save_memory(memory)
        print(f"[Memory] 💾 Saved: {list(memory_update.keys())}")
    return memory

def _entry_value(entry) -> str:
    """Accept both the {'value': ..., 'updated': ...} shape and a bare string,
    because early versions of the store wrote plain strings."""
    if isinstance(entry, dict):
        return str(entry.get("value", "") or "").strip()
    return str(entry or "").strip()


def _pretty(key: str) -> str:
    return key.replace("_", " ").strip()


# Identity is always in the prompt; these categories compete for the remaining
# budget by recency.
_CATEGORY_LABELS = {
    "preferences":   "Preferences",
    "projects":      "Active projects / goals",
    "relationships": "People in their life",
    "wishes":        "Wishes / plans",
    "notes":         "Notes",
}

_IDENTITY_FIELDS = ["name", "age", "birthday", "city", "job",
                    "language", "school", "nationality"]


def format_memory_for_prompt(memory: dict | None) -> str:
    """Build the memory block that goes into the system prompt.

    This used to dump everything. It now sends three things:

      1. IDENTITY  - always, in full. It is small, and it is wrong for the
         assistant to have to look up your name.
      2. RECENT    - the most recently updated entries from every other
         category, up to PROMPT_CORE_CHARS. Recency is the cheapest useful
         relevance signal available without embeddings.
      3. AN INDEX  - the *keys* of everything else, values omitted.

    Point 3 is what makes recall work at all. A model cannot decide to look
    something up if it does not know the thing exists: with only points 1 and 2,
    "who is Ayse?" would get "I don't know" while ayse_sister sat on disk
    unread. The index costs a few hundred characters and turns recall from a
    gamble into a lookup.

    Net effect on latency: this block is SMALLER than the old full dump, so
    every session connects with fewer tokens. Occasionally the model spends one
    extra round trip on recall_memory - covered by the acknowledgment it
    already speaks before any slow step."""
    if not memory:
        return ""

    core_lines: list[str] = []

    # 1. Identity - always, in full
    identity = memory.get("identity", {}) or {}
    for field in _IDENTITY_FIELDS:
        val = _entry_value(identity.get(field))
        if not val:
            continue
        if field == "language":
            # Labelled as an observation, not a setting. A bare "Language:
            # English" line written months ago reads like a standing order and
            # was one of the reasons a Turkish question came back in English.
            core_lines.append(
                f"Has spoken to you in: {val} (an observation about the past — "
                f"always answer in the language of their CURRENT message)")
        else:
            core_lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in _IDENTITY_FIELDS:
            continue
        val = _entry_value(entry)
        if val:
            core_lines.append(f"{_pretty(key).title()}: {val}")

    # 2. Everything else, most recently updated first
    rest: list[tuple[str, str, str, str]] = []   # (updated, cat, key, value)
    for cat in _CATEGORY_LABELS:
        for key, entry in (memory.get(cat, {}) or {}).items():
            val = _entry_value(entry)
            if not val:
                continue
            updated = (entry.get("updated", "") if isinstance(entry, dict) else "") or "0000-00-00"
            rest.append((updated, cat, key, val))
    rest.sort(key=lambda t: t[0], reverse=True)

    used    = sum(len(l) + 1 for l in core_lines)
    shown: dict[str, list[str]] = {}
    overflow: dict[str, list[str]] = {}

    # Recency decides order, but no single category may take the whole budget.
    # Without the cap, someone with forty stored preferences gets a prompt that
    # is forty preferences and not one person's name — the categories that
    # matter most in conversation are also the ones that change least often, so
    # pure recency systematically buries them.
    per_cat_used: dict[str, int] = {}
    for _updated, cat, key, val in rest:
        line = f"  - {_pretty(key).title()}: {val}"
        if (per_cat_used.get(cat, 0) < PROMPT_MAX_PER_CATEGORY
                and used + len(line) + 1 <= PROMPT_CORE_CHARS):
            shown.setdefault(cat, []).append(line)
            per_cat_used[cat] = per_cat_used.get(cat, 0) + 1
            used += len(line) + 1
        else:
            overflow.setdefault(cat, []).append(_pretty(key))

    # The index is a table of contents, so it is interleaved across categories
    # rather than continuing in recency order. Sorted by recency it would list
    # twenty-four preferences before the first relationship, and the one entry
    # the index exists for — the old fact the model has no other way to know
    # about — would fall off the end.
    indexed: list[str] = []
    if overflow:
        cats  = [c for c in _CATEGORY_LABELS if overflow.get(c)]
        cursor = {c: 0 for c in cats}
        while cats:
            for cat in list(cats):
                i = cursor[cat]
                if i >= len(overflow[cat]):
                    cats.remove(cat)
                    continue
                indexed.append(overflow[cat][i])
                cursor[cat] = i + 1

    for cat, label in _CATEGORY_LABELS.items():
        if shown.get(cat):
            core_lines.append("")
            core_lines.append(f"{label}:")
            core_lines.extend(shown[cat])

    if not core_lines and not indexed:
        return ""

    out = [
        "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]",
        *core_lines,
    ]

    # 3. The index of what is on disk but not in this prompt
    if indexed:
        budget, names = PROMPT_INDEX_CHARS, []
        for n in indexed:
            if budget - len(n) - 2 < 0:
                break
            names.append(n)
            budget -= len(n) + 2
        if names:
            out.append("")
            out.append(
                "[ALSO REMEMBERED — values not shown here. Call recall_memory "
                "with a keyword to read any of these before saying you do not know]"
            )
            out.append(", ".join(names)
                       + (f" (+{len(indexed) - len(names)} more)"
                          if len(indexed) > len(names) else ""))

    return "\n".join(out) + "\n"


# ── BM25 & Semantic Hybrid Recall Engine (OpenJarvis Inspired) ─────────────────

class BM25MemoryEngine:
    """
    Okapi BM25 ranking engine combined with hybrid key/category bonuses
    and exponential recency decay. Inspired by OpenJarvis BM25 & Hybrid storage.
    """
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b

    @staticmethod
    def tokenize(text: str) -> list[str]:
        return [w for w in re.split(r"[^\w]+", (text or "").lower()) if len(w) > 1]

    def score_entries(self, query: str, entries: list[dict]) -> list[tuple[float, dict]]:
        query_terms = self.tokenize(query)
        if not query_terms:
            return [(1.0, e) for e in entries]

        N = len(entries)
        if N == 0:
            return []

        doc_tokens = []
        doc_lengths = []
        df: dict[str, int] = {}
        for entry in entries:
            toks = self.tokenize(f"{entry.get('key', '')} {entry.get('value', '')} {entry.get('category', '')}")
            doc_tokens.append(toks)
            doc_lengths.append(len(toks))
            for t in set(toks):
                df[t] = df.get(t, 0) + 1

        avgdl = sum(doc_lengths) / max(1, N)
        now_dt = datetime.now()
        scored: list[tuple[float, dict]] = []

        for idx, entry in enumerate(entries):
            toks = doc_tokens[idx]
            d_len = doc_lengths[idx]
            bm25 = 0.0

            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1

            for q in query_terms:
                n_q = df.get(q, 0)
                if n_q == 0:
                    continue
                # Okapi BM25 formula
                idf = math.log(1.0 + (N - n_q + 0.5) / (n_q + 0.5))
                f_q = tf.get(q, 0)
                denom = f_q + self.k1 * (1.0 - self.b + self.b * (d_len / max(1.0, avgdl)))
                bm25 += idf * ((f_q * (self.k1 + 1.0)) / max(0.001, denom))

            # Hybrid Bonus 1: Key exact or partial match
            key_clean = _pretty(entry.get("key", "")).lower()
            key_boost = 0.0
            for q in query_terms:
                if q == key_clean:
                    key_boost += 14.0
                elif q in key_clean:
                    key_boost += 7.0
                if q in entry.get("category", "").lower():
                    key_boost += 2.5

            # Hybrid Bonus 2: Recency decay (recent facts get a relevance boost)
            recency_boost = 1.0
            updated_str = entry.get("updated", "")
            if updated_str:
                try:
                    up_dt = datetime.strptime(updated_str, "%Y-%m-%d")
                    days_ago = max(0, (now_dt - up_dt).days)
                    recency_boost = 1.0 + 1.8 * math.exp(-days_ago / 30.0)
                except Exception:
                    pass

            total_score = (bm25 + key_boost) * recency_boost
            if total_score > 0:
                scored.append((total_score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored


def search_memory(query: str, limit: int = 8) -> str:
    """Find stored facts matching `query` using BM25 semantic scoring and SQLite indexing.
    Backs the recall_memory tool.
    """
    memory = load_memory()
    flat_entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue                     # skip 'sessions', which is a list
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            up = (entry.get("updated", "") if isinstance(entry, dict) else "")
            flat_entries.append({
                "category": cat,
                "key": key,
                "value": val,
                "updated": up,
            })

    engine = BM25MemoryEngine()
    scored = engine.score_entries(query, flat_entries)

    # Also search SQLite document knowledge if query provided
    doc_matches = search_documents(query, limit=3) if query else []

    if not scored and not doc_matches:
        return (f"Nothing stored about '{query}'." if query
                else "I have not stored anything about this person yet.")

    lines = []
    for _score, item in scored[:max(1, limit)]:
        lines.append(f"{item['category']}/{_pretty(item['key'])}: {item['value']}")

    head = (f"Stored facts matching '{query}':" if query
            else "Everything currently stored:")
    more = (f"\n(+{len(scored) - len(lines)} more — search with a narrower keyword)"
            if len(scored) > len(lines) else "")

    result_text = head + "\n" + "\n".join(lines) + more

    if doc_matches:
        result_text += "\n\n[KNOWLEDGE & DOCUMENTS]:"
        for d in doc_matches:
            result_text += f"\n- {d['title']}: {d['content'][:180]}…"

    return result_text


def index_document(content: str, title: str = "", source: str = "") -> str:
    """Store arbitrary knowledge or document excerpts in SQLite knowledge base."""
    if not content or not content.strip():
        return "No content provided to index."
    try:
        _init_sqlite()
        with sqlite3.connect(str(DB_PATH), timeout=5.0) as conn:
            conn.execute(
                "INSERT INTO documents (source, title, content, created_at) VALUES (?, ?, ?, ?)",
                (source or "direct_input", title or "Untitled Document", content.strip(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
        return f"Successfully indexed knowledge: '{title or 'Document'}' ({len(content)} chars)."
    except Exception as e:
        return f"Failed to index knowledge: {e}"


def search_documents(query: str, limit: int = 4) -> list[dict]:
    """Retrieve document chunks from SQLite matching query using BM25."""
    if not query:
        return []
    try:
        _init_sqlite()
        with sqlite3.connect(str(DB_PATH), timeout=5.0) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, title, source, content, created_at FROM documents")
            rows = cur.fetchall()

        if not rows:
            return []

        doc_entries = []
        for r in rows:
            doc_entries.append({
                "id": r[0],
                "title": r[1] or "",
                "source": r[2] or "",
                "content": r[3] or "",
                "key": r[1] or "",
                "value": r[3] or "",
                "category": "document",
                "created_at": r[4] or "",
            })

        engine = BM25MemoryEngine()
        scored = engine.score_entries(query, doc_entries)
        return [doc for _score, doc in scored[:limit]]
    except Exception as e:
        print(f"[Memory] ⚠️ Document search warning: {e}")
        return []


def all_entries_for_ui() -> list[dict]:
    """Flat list for the memory panel: what JARVIS knows, and when it learned it.
    Sorted newest first so the panel opens on what changed most recently."""
    memory = load_memory()
    rows = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            rows.append({
                "category": cat,
                "key":      key,
                "value":    val,
                "updated":  (entry.get("updated", "") if isinstance(entry, dict) else ""),
            })
    rows.sort(key=lambda r: (r["updated"] or "0000-00-00"), reverse=True)
    return rows

def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    if category not in valid:
        category = "notes"
    update_memory({category: {key: {"value": value}}})
    return f"Remembered: {category}/{key} = {value}"


def forget(key: str, category: str = "notes") -> str:
    memory = load_memory()
    cat    = memory.get(category, {})
    if key in cat:
        del cat[key]
        memory[category] = cat
        save_memory(memory)
        return f"Forgotten: {category}/{key}"
    return f"Not found: {category}/{key}"


forget_memory = forget


# ── Session memory ─────────────────────────────────────────────────────────────

_SESSION_MAX = 3   # safety cap — in practice 0-1 entries after pop


def save_session_summary(summary: str, language: str = "") -> None:
    """Append a 1-2 sentence session summary to long_term.json['sessions']."""
    summary = (summary or "").strip()
    if not summary:
        return
    memory   = load_memory()
    sessions = memory.get("sessions", [])
    if not isinstance(sessions, list):
        sessions = []
    entry: dict = {
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "summary": summary[:280],
    }
    if language:
        entry["language"] = language
    sessions.append(entry)
    memory["sessions"] = sessions[-_SESSION_MAX:]
    with _lock:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    # Index session summary into SQLite knowledge base for long-term recall
    try:
        index_document(summary, title=f"Conversation on {entry['date']}", source="session_history")
    except Exception:
        pass
    print(f"[Memory] 📝 Session saved ({entry['date']}): {summary[:60]}…")


def pop_last_session() -> dict | None:
    """
    Return AND remove the most recent session entry.
    Calling this consumes the entry so it is never repeated in future briefings.
    """
    with _lock:
        if not MEMORY_PATH.exists():
            return None
        try:
            memory   = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            sessions = memory.get("sessions", [])
            if not isinstance(sessions, list) or not sessions:
                return None
            entry = sessions.pop()          # remove the last entry
            memory["sessions"] = sessions
            MEMORY_PATH.write_text(
                json.dumps(memory, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return entry
        except Exception as e:
            print(f"[Memory] ⚠️ pop_last_session error: {e}")
            return None