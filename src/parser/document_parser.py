"""
Document Parser for AP Policy documents.

Responsibilities:
  - Load policy from plain-text or PDF
  - Segment document into Sections → Sub-sections → Clauses
  - Resolve cross-references (e.g. "Refer Section 2.3(b)")
  - Return a structured DocumentModel ready for rule extraction
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class Clause:
    clause_id: str          # e.g. "2.3(b)"
    text: str
    parent_section: str


@dataclass
class Section:
    section_id: str         # e.g. "2"
    title: str
    raw_text: str
    clauses: List[Clause] = field(default_factory=list)


@dataclass
class ParsedDocument:
    source_path: str
    raw_text: str
    sections: List[Section] = field(default_factory=list)
    cross_references: Dict[str, List[str]] = field(default_factory=dict)
    # Maps clause_id → list of clause_ids that reference it


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# Matches top-level section headers in plain text or markdown:
# "Section 1: Title", "### Section 2: Title", "## Section 3: Title"
SECTION_HEADER = re.compile(
    r"^(?:#{1,6}\s+)?Section\s+(\d+)\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE
)

# Matches numbered clauses at first level: "1.1 ", "2.3 ", "7.2 "
CLAUSE_L1 = re.compile(
    r"^(\d+\.\d+)\s+(.+?)(?=\n\d+\.\d+\s|\nSection\s+\d+\s*:|\Z)",
    re.DOTALL | re.MULTILINE
)

# Matches sub-clauses with letter designators: "  a. ", "  b. "
SUB_CLAUSE = re.compile(
    r"^\s+([a-z])\.\s+(.+?)(?=\n\s+[a-z]\.\s|\n\d+\.\d+\s|\nSection\s+\d+\s*:|\Z)",
    re.DOTALL | re.MULTILINE
)

# Cross-reference pattern: "Section 2.3(b)", "Sections 1–4", "Section 6"
CROSS_REF = re.compile(
    r"[Ss]ection[s]?\s+([\d]+(?:\.\d+)?(?:\([a-z]\))?(?:\s*[–-]\s*[\d]+(?:\.\d+)?(?:\([a-z]\))?)?)",
    re.IGNORECASE
)


# ---------------------------------------------------------------------------
# DocumentParser
# ---------------------------------------------------------------------------

class DocumentParser:
    """Parses plain-text or PDF policy documents into a structured form."""

    def parse(self, source: str | Path) -> ParsedDocument:
        """
        Parse a policy document.

        Args:
            source: Path to .txt or .pdf file, or raw text string.

        Returns:
            ParsedDocument with sections, clauses, and cross-references.
        """
        path = Path(source) if not isinstance(source, Path) else source

        if path.exists():
            raw_text = self._load_file(path)
            source_label = str(path)
        else:
            # Treat as raw text
            raw_text = str(source)
            source_label = "<raw_text>"

        sections = self._split_sections(raw_text)
        cross_refs = self._extract_cross_references(sections)

        return ParsedDocument(
            source_path=source_label,
            raw_text=raw_text,
            sections=sections,
            cross_references=cross_refs,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_file(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return self._load_pdf(path)
        # .md, .txt and any other text-based format read as plain text
        return path.read_text(encoding="utf-8", errors="replace")

    def _load_pdf(self, path: Path) -> str:
        try:
            import pdfplumber  # type: ignore
            with pdfplumber.open(path) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
            return "\n".join(pages)
        except ImportError:
            raise ImportError(
                "pdfplumber is required to parse PDF files. "
                "Install it with: pip install pdfplumber"
            )

    def _split_sections(self, text: str) -> List[Section]:
        """Split document into Section objects, each containing Clauses."""
        # Find all section boundaries
        section_matches = list(SECTION_HEADER.finditer(text))
        sections: List[Section] = []

        for idx, match in enumerate(section_matches):
            sec_id = match.group(1)
            sec_title = match.group(2).strip()
            start = match.start()
            end = section_matches[idx + 1].start() if idx + 1 < len(section_matches) else len(text)
            sec_text = text[start:end].strip()

            clauses = self._extract_clauses(sec_id, sec_text)
            sections.append(Section(
                section_id=sec_id,
                title=sec_title,
                raw_text=sec_text,
                clauses=clauses,
            ))

        if not sections:
            # Fallback: whole document as one section
            sections.append(Section(
                section_id="0",
                title="Full Document",
                raw_text=text,
                clauses=self._extract_clauses("0", text),
            ))

        return sections

    def _extract_clauses(self, section_id: str, text: str) -> List[Clause]:
        """Extract individual clauses from a section block."""
        clauses: List[Clause] = []

        for m in CLAUSE_L1.finditer(text):
            clause_num = m.group(1)       # e.g. "2.3"
            clause_body = m.group(2).strip()

            # Check whether this clause has sub-items (a., b., c., …)
            sub_matches = list(SUB_CLAUSE.finditer(m.group(0)))
            if sub_matches:
                for sm in sub_matches:
                    letter = sm.group(1)
                    sub_text = sm.group(2).strip()
                    clause_id = f"{clause_num}({letter})"
                    clauses.append(Clause(
                        clause_id=clause_id,
                        text=sub_text,
                        parent_section=section_id,
                    ))
                # Also store the parent clause text (without sub-items)
                parent_text = self._strip_sub_clauses(clause_body)
                if parent_text:
                    clauses.append(Clause(
                        clause_id=clause_num,
                        text=parent_text,
                        parent_section=section_id,
                    ))
            else:
                clauses.append(Clause(
                    clause_id=clause_num,
                    text=clause_body,
                    parent_section=section_id,
                ))

        return clauses

    def _strip_sub_clauses(self, text: str) -> str:
        """Remove sub-clause lines (a., b., …) from a clause body."""
        lines = text.splitlines()
        top_lines = [ln for ln in lines if not re.match(r"^\s+[a-z]\.\s", ln)]
        return " ".join(top_lines).strip()

    def _extract_cross_references(
        self, sections: List[Section]
    ) -> Dict[str, List[str]]:
        """
        Build a mapping of  target_clause_id → [source_clause_id, …].
        E.g. if clause 3.2(b) mentions "Refer Section 2.3(b)",
        cross_refs["2.3(b)"] = ["3.2(b)"].
        """
        refs: Dict[str, List[str]] = {}

        for section in sections:
            for clause in section.clauses:
                for match in CROSS_REF.finditer(clause.text):
                    target = self._normalise_ref(match.group(1))
                    refs.setdefault(target, []).append(clause.clause_id)

        return refs

    def _normalise_ref(self, ref: str) -> str:
        """Normalise a cross-reference string to a consistent clause_id format."""
        ref = ref.strip()
        # "2.3(b)" already in correct form; "2.3 (b)" → "2.3(b)"
        ref = re.sub(r"\s+\(", "(", ref)
        return ref

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def get_clause_text(self, doc: ParsedDocument, clause_id: str) -> Optional[str]:
        """Look up raw text for a given clause_id across all sections."""
        for section in doc.sections:
            for clause in section.clauses:
                if clause.clause_id == clause_id:
                    return clause.text
        return None

    def get_section_text(self, doc: ParsedDocument, section_id: str) -> Optional[str]:
        for section in doc.sections:
            if section.section_id == section_id:
                return section.raw_text
        return None

    def summarise(self, doc: ParsedDocument) -> str:
        """Return a readable summary of the parsed document structure."""
        lines = [f"Document: {doc.source_path}",
                 f"Sections: {len(doc.sections)}"]
        for s in doc.sections:
            lines.append(f"  Section {s.section_id}: {s.title} ({len(s.clauses)} clauses)")
        lines.append(f"Cross-references found: {sum(len(v) for v in doc.cross_references.values())}")
        return "\n".join(lines)
