from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from app.config import WatchlistConfig, load_watchlist, settings
from app.matcher.normalization import (
    build_model_regex,
    extract_hours,
    extract_price,
    extract_year,
    normalize_text,
)
from app.schemas.listing import MatchClassification, MatchResult, RawListing


class EquipmentMatcher:
    """
    Deterministic, high-precision equipment matching and filtering engine.
    Supports tri-state classification: ACCEPT, REVIEW, REJECT.
    """

    def __init__(
        self,
        watchlist: Optional[WatchlistConfig] = None,
        accept_threshold: Optional[float] = None,
        review_threshold: Optional[float] = None,
    ) -> None:
        self.watchlist = watchlist or load_watchlist()

        self.accept_threshold = (
            accept_threshold
            if accept_threshold is not None
            else getattr(
                self.watchlist.matcher_thresholds,
                "accept_threshold",
                settings.MATCH_ACCEPT_THRESHOLD,
            )
        )
        self.review_threshold = (
            review_threshold
            if review_threshold is not None
            else getattr(
                self.watchlist.matcher_thresholds,
                "review_threshold",
                settings.MATCH_REVIEW_THRESHOLD,
            )
        )

        self._init_matchers()

    def _init_matchers(self) -> None:
        """Precompile regex patterns for manufacturers, models, equipment terms, and filters."""
        # 1. Manufacturer patterns
        mfg_alias_map: Dict[str, Set[str]] = {}

        # From top-level target_models
        for tm in getattr(self.watchlist, "target_models", []):
            canon_mfg = tm.manufacturer
            if canon_mfg not in mfg_alias_map:
                mfg_alias_map[canon_mfg] = {canon_mfg.lower()}
            for a in tm.manufacturer_aliases:
                mfg_alias_map[canon_mfg].add(a.lower())

        # From manufacturers config section
        for mfg_key, mfg_cfg in self.watchlist.manufacturers.items():
            canon_mfg = mfg_cfg.canonical_name
            if canon_mfg not in mfg_alias_map:
                mfg_alias_map[canon_mfg] = {canon_mfg.lower()}
            for a in mfg_cfg.aliases:
                mfg_alias_map[canon_mfg].add(a.lower())

        self.manufacturer_patterns: Dict[str, Tuple[re.Pattern, str]] = {}
        for canon_mfg, aliases in mfg_alias_map.items():
            sorted_aliases = sorted(aliases, key=len, reverse=True)
            escaped_aliases = [re.escape(alias) for alias in sorted_aliases]
            alias_pattern = rf"\b(?:{'|'.join(escaped_aliases)})\b"
            self.manufacturer_patterns[canon_mfg] = (
                re.compile(alias_pattern, re.IGNORECASE),
                canon_mfg,
            )

        # 2. Model patterns sorted by alias length descending
        # Each entry: (pattern, mfg_key, eq_type, canonical_model_name, canonical_mfg)
        raw_model_aliases: List[Tuple[str, str, str, str, str]] = []
        seen_aliases: Set[Tuple[str, str]] = set()

        # From target_models
        for tm in getattr(self.watchlist, "target_models", []):
            canon_mfg = tm.manufacturer
            canon_model = tm.canonical_model
            eq_type = tm.equipment_type or "Heavy Equipment"
            for alias in tm.aliases:
                key = (alias.lower(), canon_model)
                if key not in seen_aliases:
                    seen_aliases.add(key)
                    raw_model_aliases.append(
                        (alias, canon_mfg, eq_type, canon_model, canon_mfg)
                    )

        # From manufacturers categories
        for mfg_key, mfg_cfg in self.watchlist.manufacturers.items():
            canonical_mfg = mfg_cfg.canonical_name
            for _, cat_cfg in mfg_cfg.categories.items():
                for model_cfg in cat_cfg.target_models:
                    canon_model = model_cfg.canonical_name
                    for alias in model_cfg.aliases:
                        key = (alias.lower(), canon_model)
                        if key not in seen_aliases:
                            seen_aliases.add(key)
                            raw_model_aliases.append(
                                (alias, mfg_key, cat_cfg.equipment_type, canon_model, canonical_mfg)
                            )

        # Sort longer aliases first to prefer specific matches
        raw_model_aliases.sort(key=lambda x: len(x[0]), reverse=True)

        self.model_patterns: List[Tuple[re.Pattern, str, str, str, str]] = []
        for alias_str, mfg_key, eq_type, canon_model, canon_mfg in raw_model_aliases:
            pattern = build_model_regex(alias_str)
            self.model_patterns.append(
                (pattern, mfg_key, eq_type, canon_model, canon_mfg)
            )

        # Equipment terms
        eq_terms = [re.escape(term) for term in self.watchlist.equipment_terms]
        self.equipment_terms_pattern = re.compile(
            rf"\b(?:{'|'.join(eq_terms)})\b", re.IGNORECASE
        )

        # Machine wholeness indicators
        self.wholeness_pattern = re.compile(
            r"\b(?:wheel\s+loader|loader\s+backhoe|backhoe\s+loader|complete\s+machine|"
            r"complete\s+unit|whole\s+machine|runs\s+and\s+operates|runs\s+great|"
            r"runs\s+well|ready\s+to\s+work|ready\s+for\s+work|job\s+ready|clean\s+machine)\b",
            re.IGNORECASE,
        )

        # Hard negative terms: Merchandise, toys, documentation
        merch_and_docs = (
            self.watchlist.negative_terms.merchandise_and_toys
            + self.watchlist.negative_terms.documentation
        )
        escaped_merch = [re.escape(t) for t in merch_and_docs]
        self.hard_negative_pattern = re.compile(
            rf"\b(?:{'|'.join(escaped_merch)})\b", re.IGNORECASE
        )

        # Explicit part-sale patterns
        self.part_sale_pattern = re.compile(
            r"\b(?:(?:(?:rebuilt|new|used)\s+)?(?:tires?|tyres?|bucket|transmission|engine|radiator|cab|cylinder|pump|parts)\s+for\s+sale|"
            r"(?:for\s+sale|wanted)\s*[:\-]?\s*(?:tires?|tyres?|bucket|transmission|engine|radiator|cab|cylinder|pump|parts)|"
            r"(?:cab|engine|transmission|differential|axle|radiator)\s+assembly|"
            r"hydraulic\s+cylinder|salvage\s+parts|dismantling|parting\s+out|wrecking|parts\s+only|for\s+parts|parts\s+machine)\b",
            re.IGNORECASE,
        )

        # Standalone part / attachment terms
        parts_list = self.watchlist.negative_terms.parts_and_accessories
        escaped_parts = [re.escape(p) for p in parts_list]
        self.parts_negative_pattern = re.compile(
            rf"\b(?:{'|'.join(escaped_parts)})\b", re.IGNORECASE
        )

        # Positive contextual modifiers
        pos_contexts = self.watchlist.positive_context_indicators
        escaped_pos = [re.escape(c) for c in pos_contexts]
        self.positive_context_pattern = re.compile(
            rf"\b(?:{'|'.join(escaped_pos)})\b", re.IGNORECASE
        )

    def match(self, listing: RawListing) -> MatchResult:
        """
        Evaluate a listing against target equipment criteria.
        Returns a MatchResult with tri-state classification (ACCEPT, REVIEW, REJECT).
        """
        matched_rules: List[str] = []
        norm_title = normalize_text(listing.title)
        norm_desc = normalize_text(listing.description or "")
        full_text = f"{norm_title} {norm_desc}".strip()

        # Step 1: Scan for Hard Negative Terms
        hard_neg_title_match = self.hard_negative_pattern.search(norm_title)
        if hard_neg_title_match:
            term = hard_neg_title_match.group(0)
            return MatchResult(
                classification=MatchClassification.REJECT,
                is_target_equipment=False,
                confidence=0.0,
                matched_rules=[f"reject_hard_negative:{term}"],
                rejection_reason=f"Contains disqualifying merchandise/toy/manual term: '{term}' in title",
            )

        # Step 2: Check for Explicit Part-Sale, Assembly, or Salvage patterns
        part_sale_match = self.part_sale_pattern.search(norm_title) or self.part_sale_pattern.search(norm_desc)
        if part_sale_match:
            term = part_sale_match.group(0)
            return MatchResult(
                classification=MatchClassification.REJECT,
                is_target_equipment=False,
                confidence=0.0,
                matched_rules=[f"reject_part_sale:{term}"],
                rejection_reason=f"Disqualified by explicit part-sale/component/salvage indicator: '{term}'",
            )

        # Step 3: Metadata Extraction
        extracted_year = extract_year(norm_title) or extract_year(norm_desc)
        extracted_hours = extract_hours(norm_title) or extract_hours(norm_desc)
        extracted_price = listing.price or extract_price(norm_title) or extract_price(norm_desc)

        if extracted_year:
            matched_rules.append(f"year_detected:{extracted_year}")
        if extracted_hours:
            matched_rules.append(f"hours_detected:{extracted_hours}")
        if extracted_price:
            matched_rules.append(f"price_detected:{extracted_price}")

        # Step 4: Wholeness and Contextual Signals
        has_wholeness = bool(self.wholeness_pattern.search(full_text))
        has_positive_context = bool(self.positive_context_pattern.search(full_text))
        has_equip_term_in_title = bool(self.equipment_terms_pattern.search(norm_title))
        has_equip_term_in_desc = bool(self.equipment_terms_pattern.search(norm_desc))

        if has_equip_term_in_title or has_equip_term_in_desc:
            matched_rules.append("equipment_term_matched")
        if has_wholeness:
            matched_rules.append("machine_wholeness_indicator")
        if has_positive_context:
            matched_rules.append("positive_machine_context")

        # Step 5: Part / Attachment Disqualification Analysis in Title
        part_title_match = self.parts_negative_pattern.search(norm_title)
        if part_title_match:
            part_term = part_title_match.group(0)

            is_protected_whole_machine = (
                has_wholeness
                and (extracted_year is not None or extracted_hours is not None or has_positive_context)
            )

            is_part_of_positive_phrase = False
            for pos_match in self.positive_context_pattern.finditer(norm_title):
                if part_term in pos_match.group(0):
                    is_part_of_positive_phrase = True
                    break

            if not is_protected_whole_machine and not is_part_of_positive_phrase:
                return MatchResult(
                    classification=MatchClassification.REJECT,
                    is_target_equipment=False,
                    confidence=0.0,
                    matched_rules=[f"reject_part_attachment:{part_term}"],
                    rejection_reason=f"Contains disqualifying part/attachment term: '{part_term}' as title subject",
                    extracted_year=extracted_year,
                    extracted_hours=extracted_hours,
                    extracted_price=extracted_price,
                )

        # Check description hard negatives if no machine indicators exist
        hard_neg_desc_match = self.hard_negative_pattern.search(norm_desc)
        if hard_neg_desc_match and not (has_wholeness or extracted_year or extracted_hours):
            term = hard_neg_desc_match.group(0)
            return MatchResult(
                classification=MatchClassification.REJECT,
                is_target_equipment=False,
                confidence=0.0,
                matched_rules=[f"reject_hard_negative_desc:{term}"],
                rejection_reason=f"Contains disqualifying merchandise/toy/manual term: '{term}' in description",
            )

        # Step 6: Manufacturer Matching
        detected_manufacturer: Optional[str] = None
        manufacturer_in_title = False
        manufacturer_in_desc = False

        for mfg_key, (mfg_pat, canonical_name) in self.manufacturer_patterns.items():
            if mfg_pat.search(norm_title):
                detected_manufacturer = canonical_name
                manufacturer_in_title = True
                matched_rules.append(f"manufacturer_in_title:{canonical_name}")
                break
            elif mfg_pat.search(norm_desc):
                detected_manufacturer = canonical_name
                manufacturer_in_desc = True
                matched_rules.append(f"manufacturer_in_desc:{canonical_name}")
                break

        # Step 7: Model Matching
        detected_model: Optional[str] = None
        detected_equipment_type: Optional[str] = None
        model_canonical_mfg: Optional[str] = None
        model_found = False

        for pat, mfg_key, eq_type, canon_model, canon_mfg in self.model_patterns:
            if pat.search(norm_title):
                detected_model = canon_model
                detected_equipment_type = eq_type
                model_canonical_mfg = canon_mfg
                matched_rules.append(f"model_in_title:{canon_model}")
                model_found = True
                break

        if not model_found and (detected_manufacturer or has_equip_term_in_title):
            for pat, mfg_key, eq_type, canon_model, canon_mfg in self.model_patterns:
                if pat.search(norm_desc):
                    detected_model = canon_model
                    detected_equipment_type = eq_type
                    model_canonical_mfg = canon_mfg
                    matched_rules.append(f"model_in_desc:{canon_model}")
                    model_found = True
                    break

        # Step 8: Manufacturer / Model Conflict Verification & Inference
        manufacturer_inferred = False

        if model_found:
            if detected_manufacturer:
                if detected_manufacturer != model_canonical_mfg:
                    return MatchResult(
                        classification=MatchClassification.REJECT,
                        is_target_equipment=False,
                        confidence=0.0,
                        manufacturer=detected_manufacturer,
                        model=detected_model,
                        equipment_type=detected_equipment_type,
                        matched_rules=matched_rules,
                        rejection_reason=(
                            f"Manufacturer conflict: stated '{detected_manufacturer}' "
                            f"does not match model '{detected_model}' manufacturer '{model_canonical_mfg}'"
                        ),
                        extracted_year=extracted_year,
                        extracted_hours=extracted_hours,
                        extracted_price=extracted_price,
                    )
            else:
                detected_manufacturer = model_canonical_mfg
                manufacturer_inferred = True
                matched_rules.append(f"inferred_manufacturer:{detected_manufacturer}")

        # Step 9: Rejection if no target model detected
        if not detected_model:
            return MatchResult(
                classification=MatchClassification.REJECT,
                is_target_equipment=False,
                manufacturer=detected_manufacturer,
                manufacturer_inferred=manufacturer_inferred,
                confidence=0.0,
                matched_rules=matched_rules,
                rejection_reason="No target model detected from watchlist",
                extracted_year=extracted_year,
                extracted_hours=extracted_hours,
                extracted_price=extracted_price,
            )

        # Step 10: Contextual Confidence Scoring
        score = 0.0

        if detected_model:
            score += 0.40

        if detected_manufacturer:
            if manufacturer_in_title:
                score += 0.25
            elif manufacturer_in_desc:
                score += 0.15
            elif manufacturer_inferred:
                score += 0.10

        if manufacturer_in_title and any(r.startswith("model_in_title") for r in matched_rules):
            score += 0.05
            matched_rules.append("mfg_model_title_cooccurrence")

        if has_equip_term_in_title:
            score += 0.20
        elif has_equip_term_in_desc:
            score += 0.10

        if has_wholeness:
            score += 0.10
        if extracted_year:
            score += 0.10
        if extracted_hours:
            score += 0.10
        if extracted_price:
            score += 0.05
        if has_positive_context:
            score += 0.05

        part_desc_match = self.parts_negative_pattern.search(norm_desc)
        if part_desc_match and not has_wholeness and not has_positive_context:
            score -= 0.10

        confidence = round(max(0.0, min(1.0, score)), 2)

        # Step 11: Tri-State Classification Gate
        if confidence >= self.accept_threshold:
            classification = MatchClassification.ACCEPT
            is_target = True
            rejection_reason = None
        elif confidence >= self.review_threshold:
            classification = MatchClassification.REVIEW
            is_target = False  # Held for review, not sent as confirmed alert
            rejection_reason = f"Confidence ({confidence:.2f}) requires human/AI review (threshold {self.accept_threshold:.2f})"
        else:
            classification = MatchClassification.REJECT
            is_target = False
            rejection_reason = f"Confidence ({confidence:.2f}) below review threshold {self.review_threshold:.2f}"

        return MatchResult(
            classification=classification,
            is_target_equipment=is_target,
            manufacturer=detected_manufacturer,
            manufacturer_inferred=manufacturer_inferred,
            model=detected_model,
            equipment_type=detected_equipment_type,
            confidence=confidence,
            matched_rules=matched_rules,
            rejection_reason=rejection_reason,
            extracted_year=extracted_year,
            extracted_hours=extracted_hours,
            extracted_price=extracted_price,
        )
