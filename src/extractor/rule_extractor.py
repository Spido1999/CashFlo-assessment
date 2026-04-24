"""
LLM-based Rule Extractor for AP Policy Documents.

Pipeline:
  ParsedDocument → section-by-section LLM calls → List[Rule] → RuleSet

Supports:
  - Groq  (FREE — llama-3.3-70b-versatile)   provider="groq"
  - OpenAI API (GPT-4o / GPT-4-turbo)          provider="openai"
  - Anthropic Claude (claude-3-5-sonnet)        provider="anthropic"
  - Fallback to pre-extracted rules (no API key required)

Get a free Groq API key at: https://console.groq.com

Usage:
    extractor = RuleExtractor(provider="groq")
    rule_set = extractor.extract(parsed_doc)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional

from src.models import (
    ConflictReport,
    Rule,
    RuleSet,
)
from src.extractor.prompts import (
    CONFLICT_PROMPT,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
)
from src.parser.document_parser import ParsedDocument, Section

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------

class LLMClient:
    """Thin wrapper around LLM provider SDKs."""

    def __init__(self, provider: str = "openai", model: Optional[str] = None):
        self.provider = provider.lower()
        self.model = model or self._default_model()

    def _default_model(self) -> str:
        if self.provider == "groq":
            return "llama-3.3-70b-versatile"
        if self.provider == "openai":
            return "gpt-4o"
        if self.provider == "anthropic":
            return "claude-3-5-sonnet-20241022"
        raise ValueError(f"Unknown LLM provider: {self.provider!r}")

    def chat(self, system: str, user: str) -> str:
        """Call the LLM and return the raw response string."""
        if self.provider == "groq":
            return self._groq_chat(system, user)
        if self.provider == "openai":
            return self._openai_chat(system, user)
        if self.provider == "anthropic":
            return self._anthropic_chat(system, user)
        raise ValueError(f"Unsupported provider: {self.provider!r}")

    def _groq_chat(self, system: str, user: str) -> str:
        """Call Groq's free LLM API (OpenAI-compatible)."""
        try:
            from groq import Groq  # type: ignore
        except ImportError:
            raise ImportError(
                "groq package not installed. Run: pip install groq\n"
                "Get a free API key at: https://console.groq.com"
            )

        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "GROQ_API_KEY environment variable not set.\n"
                "Get a free key at: https://console.groq.com"
            )

        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            max_tokens=4096,
        )
        return response.choices[0].message.content or ""

    def _openai_chat(self, system: str, user: str) -> str:
        try:
            from openai import OpenAI  # type: ignore
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError("OPENAI_API_KEY environment variable not set.")

        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,       # Low temperature for deterministic extraction
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""

    def _anthropic_chat(self, system: str, user: str) -> str:
        try:
            import anthropic  # type: ignore
        except ImportError:
            raise ImportError("anthropic package not installed. Run: pip install anthropic")

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY environment variable not set.")

        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text


# ---------------------------------------------------------------------------
# Rule Extractor
# ---------------------------------------------------------------------------

class RuleExtractor:
    """
    Extracts structured rules from a ParsedDocument using an LLM.

    Args:
        provider: "openai" or "anthropic"
        model:    Override the default model
    """

    def __init__(
        self,
        provider: str = "openai",
        model: Optional[str] = None,
    ):
        self.llm = LLMClient(provider=provider, model=model)
        self._extracted_ids: List[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(self, doc: ParsedDocument) -> RuleSet:
        """
        Run the full extraction pipeline on a parsed document.

        Returns a RuleSet with all rules and detected conflicts.
        """
        all_rules: List[Rule] = []

        for section in doc.sections:
            logger.info("Extracting rules from Section %s: %s",
                        section.section_id, section.title)
            section_rules = self._extract_section(section, doc.source_path)
            all_rules.extend(section_rules)
            self._extracted_ids.extend(r.rule_id for r in section_rules)

        # Deduplicate by rule_id (keep first occurrence)
        seen: set[str] = set()
        unique_rules: List[Rule] = []
        for r in all_rules:
            if r.rule_id not in seen:
                seen.add(r.rule_id)
                unique_rules.append(r)

        # Sort by priority
        unique_rules.sort(key=lambda r: r.priority)

        # Detect conflicts
        conflicts = self._detect_conflicts(unique_rules)

        return RuleSet(
            version="1.0",
            source_document=doc.source_path,
            extraction_date=date.today().isoformat(),
            rules=unique_rules,
            conflicts=conflicts,
        )

    # ------------------------------------------------------------------
    # Per-section extraction
    # ------------------------------------------------------------------

    def _extract_section(
        self, section: Section, doc_name: str
    ) -> List[Rule]:
        """Call the LLM for one section and parse the result."""
        user_prompt = USER_PROMPT_TEMPLATE.format(
            section_text=section.raw_text,
            doc_name=doc_name,
            section_id=section.section_id,
            section_title=section.title,
            existing_ids=", ".join(self._extracted_ids) if self._extracted_ids else "none",
        )

        raw = self._safe_llm_call(SYSTEM_PROMPT, user_prompt)
        if raw is None:
            return []

        return self._parse_rules_response(raw, section.section_id)

    def _safe_llm_call(self, system: str, user: str) -> Optional[str]:
        """Call the LLM with retry on transient errors."""
        import time
        for attempt in range(3):
            try:
                return self.llm.chat(system, user)
            except Exception as exc:
                logger.warning("LLM call failed (attempt %d/3): %s", attempt + 1, exc)
                if attempt < 2:
                    time.sleep(2 ** attempt)
        return None

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_rules_response(
        self, raw: str, section_id: str
    ) -> List[Rule]:
        """Parse a JSON array (or object wrapping an array) of rule dicts."""
        raw = raw.strip()

        # Strip optional markdown fences
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("JSON parse error in section %s: %s\nRaw: %s",
                         section_id, exc, raw[:500])
            return []

        # LLM sometimes wraps array in {"rules": [...]}
        if isinstance(data, dict):
            for key in ("rules", "result", "data"):
                if key in data and isinstance(data[key], list):
                    data = data[key]
                    break
            else:
                data = list(data.values())[0] if data else []

        if not isinstance(data, list):
            logger.error("Expected JSON array for section %s; got %s",
                         section_id, type(data))
            return []

        rules: List[Rule] = []
        for item in data:
            try:
                rule = Rule.model_validate(item)
                rules.append(rule)
            except Exception as exc:
                logger.warning("Skipping invalid rule dict: %s — %s", item.get("rule_id", "?"), exc)

        return rules

    # ------------------------------------------------------------------
    # Conflict detection
    # ------------------------------------------------------------------

    def _detect_conflicts(self, rules: List[Rule]) -> List[ConflictReport]:
        """Use LLM to detect conflicts among extracted rules."""
        rules_json = json.dumps(
            [r.model_dump(exclude_none=True) for r in rules],
            indent=2
        )
        prompt = CONFLICT_PROMPT.format(rules_json=rules_json)
        raw = self._safe_llm_call("You are an AP policy conflict analyst.", prompt)
        if raw is None:
            return []

        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Could not parse conflict detection response.")
            return []

        if isinstance(data, dict):
            data = data.get("conflicts", [])

        conflicts: List[ConflictReport] = []
        for item in data:
            try:
                conflicts.append(ConflictReport.model_validate(item))
            except Exception as exc:
                logger.warning("Skipping invalid conflict: %s", exc)

        return conflicts
