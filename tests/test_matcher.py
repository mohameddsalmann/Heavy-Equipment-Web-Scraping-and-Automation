from __future__ import annotations

import pytest
from app.matcher.equipment_matcher import EquipmentMatcher
from app.schemas.listing import MatchClassification, RawListing


@pytest.fixture
def matcher():
    return EquipmentMatcher()


def make_raw_listing(title: str, description: str = "", price: float = None) -> RawListing:
    return RawListing(
        source="test_source",
        external_id="test_id_123",
        title=title,
        description=description,
        price=price,
        url="https://example.com/item/123",
    )


class TestEquipmentMatcherAccepted:
    """Tests for machinery that MUST be classified as ACCEPT."""

    @pytest.mark.parametrize(
        "title,expected_mfg,expected_model,expected_type",
        [
            ("2008 CAT 966H Wheel Loader", "Caterpillar", "966H", "Wheel Loader"),
            ("CAT 966H Wheel Loader", "Caterpillar", "966H", "Wheel Loader"),
            ("CAT 966H loader 12,000 hours", "Caterpillar", "966H", "Wheel Loader"),
            ("2007 Caterpillar 980H", "Caterpillar", "980H", "Wheel Loader"),
            ("Caterpillar 966G 11,000 hours", "Caterpillar", "966G", "Wheel Loader"),
            ("1998 Caterpillar 970F Wheel Loader", "Caterpillar", "970F", "Wheel Loader"),
            ("CAT 972H Wheel Loader 4WD", "Caterpillar", "972H", "Wheel Loader"),
            ("1990 CAT 950E Wheel Loader", "Caterpillar", "950E", "Wheel Loader"),
            ("Caterpillar 950B Loader", "Caterpillar", "950B", "Wheel Loader"),
            ("CAT 936E Wheel Loader 1988", "Caterpillar", "936E", "Wheel Loader"),
            ("CAT 930 Wheel Loader", "Caterpillar", "930", "Wheel Loader"),
            ("CAT 920 Wheel Loader", "Caterpillar", "920", "Wheel Loader"),
            ("2002 CAT 416C Backhoe Loader", "Caterpillar", "416C", "Backhoe Loader"),
            ("2005 Caterpillar 420D 4x4 Backhoe", "Caterpillar", "420D", "Backhoe Loader"),
            ("CAT 416D Backhoe Loader 5,000 hrs", "Caterpillar", "416D", "Backhoe Loader"),
            ("CASE 580SL Backhoe Loader", "CASE", "580SL", "Backhoe Loader"),
            ("1998 CASE 580 Super L 4x4 Loader Backhoe", "CASE", "580SL", "Backhoe Loader"),
            ("CASE 580 Super-L Backhoe", "CASE", "580SL", "Backhoe Loader"),
        ],
    )
    def test_target_machines_accepted(self, matcher, title, expected_mfg, expected_model, expected_type):
        listing = make_raw_listing(title=title)
        result = matcher.match(listing)
        assert result.classification == MatchClassification.ACCEPT
        assert result.is_target_equipment is True, f"Failed for '{title}': reason={result.rejection_reason}"
        assert result.confidence >= matcher.accept_threshold
        assert result.manufacturer == expected_mfg
        assert result.equipment_type == expected_type
        assert result.model == expected_model

    def test_contextual_acceptance_with_accessories_and_parts_mentioned(self, matcher):
        listing = make_raw_listing(
            title="2015 Caterpillar 966H Wheel Loader - Bucket Included, New Tires, 8,200 hrs",
            description="Runs great, enclosed cab with AC, rebuilt transmission last year. Ready to work.",
            price=125000.0,
        )
        result = matcher.match(listing)
        assert result.classification == MatchClassification.ACCEPT
        assert result.is_target_equipment is True
        assert result.confidence >= 0.85
        assert result.extracted_year == 2015
        assert result.extracted_hours == 8200.0
        assert result.extracted_price == 125000.0


class TestTriStateClassification:
    """Tests proving explicit tri-state classification: ACCEPT, REVIEW, REJECT."""

    def test_state_accept(self, matcher):
        listing = make_raw_listing(title="2012 CAT 966H Wheel Loader 7,000 hours")
        result = matcher.match(listing)
        assert result.classification == MatchClassification.ACCEPT
        assert result.is_target_equipment is True
        assert result.confidence >= matcher.accept_threshold
        assert result.rejection_reason is None

    def test_state_review(self, matcher):
        # Model (0.40) + inferred mfg (0.10) = 0.50 confidence.
        # review_threshold (0.50) <= confidence < accept_threshold (0.70) => REVIEW
        listing = make_raw_listing(title="966H for sale in good condition")
        result = matcher.match(listing)
        assert result.classification == MatchClassification.REVIEW
        # Critical rule: REVIEW listings MUST NOT be flagged as confirmed target equipment
        assert result.is_target_equipment is False
        assert matcher.review_threshold <= result.confidence < matcher.accept_threshold
        assert "requires human/AI review" in result.rejection_reason

    def test_review_listings_do_not_send_confirmed_notifications(self, matcher):
        """Proves that REVIEW listings have is_target_equipment=False to block instant alerts."""
        listing = make_raw_listing(title="580SL backhoe loader")
        result = matcher.match(listing)
        if result.classification == MatchClassification.REVIEW:
            assert result.is_target_equipment is False

    def test_state_reject_by_low_confidence(self, matcher):
        # Generic query match with no target model
        listing = make_raw_listing(title="Loader heavy equipment for farm")
        result = matcher.match(listing)
        assert result.classification == MatchClassification.REJECT
        assert result.is_target_equipment is False
        assert result.confidence < matcher.review_threshold

    def test_state_reject_by_hard_disqualifier(self, matcher):
        listing = make_raw_listing(title="CAT 966H radiator for sale")
        result = matcher.match(listing)
        assert result.classification == MatchClassification.REJECT
        assert result.is_target_equipment is False
        assert result.confidence == 0.0


class TestCanonicalModelAliases:
    """Tests that model aliases configured in watchlist.yaml canonicalize properly to a single model value."""

    @pytest.mark.parametrize(
        "title",
        [
            "CASE 580SL Backhoe Loader",
            "CASE 580 SL Backhoe",
            "CASE 580 Super L 4WD Backhoe",
            "CASE 580 Super-L Backhoe Loader",
            "1998 CASE 580L Backhoe",
        ],
    )
    def test_case_580_aliases_canonicalize_to_580sl_with_brand(self, matcher, title):
        listing = make_raw_listing(title=title)
        result = matcher.match(listing)
        assert result.manufacturer == "CASE"
        assert result.model == "580SL", f"Failed for '{title}': resolved to '{result.model}' instead of '580SL'"
        assert result.classification in [MatchClassification.ACCEPT, MatchClassification.REVIEW]

    @pytest.mark.parametrize(
        "title",
        [
            "580SL Backhoe Loader 4x4",
            "580 SL Backhoe Loader",
            "580 Super L Loader Backhoe",
            "580 Super-L Backhoe Loader",
        ],
    )
    def test_case_580_aliases_canonicalize_to_580sl_inferred_brand(self, matcher, title):
        """Proves that even when 'CASE' is omitted, all aliases infer manufacturer=CASE and model=580SL."""
        listing = make_raw_listing(title=title)
        result = matcher.match(listing)
        assert result.manufacturer == "CASE"
        assert result.model == "580SL", f"Failed for '{title}': resolved to '{result.model}' instead of '580SL'"


class TestSearchQueriesSeparation:
    """
    Tests proving discovery search queries are used ONLY for marketplace discovery.
    Generic search query matches alone MUST NOT cause a listing to be accepted.
    All results must still pass through EquipmentMatcher.
    """

    @pytest.mark.parametrize(
        "title",
        [
            "CAT loader for sale",
            "CAT wheel loader commercial use",
            "Caterpillar loader in good condition",
            "Caterpillar wheel loader heavy duty",
            "CASE backhoe ready to work",
            "CASE backhoe loader farm equipment",
            "CASE 580 equipment machine",
            "CAT 988K Large Wheel Loader",  # Non-target model
            "CAT 992K Mining Wheel Loader",  # Non-target model
        ],
    )
    def test_search_queries_alone_do_not_cause_acceptance(self, matcher, title):
        listing = make_raw_listing(title=title)
        result = matcher.match(listing)
        # Without matching a specific canonical target model on the watchlist, must NOT be ACCEPT
        assert result.classification != MatchClassification.ACCEPT
        assert result.is_target_equipment is False


class TestEquipmentMatcherRejected:
    """Tests for parts, merchandise, toys, and irrelevant items that MUST be rejected."""

    @pytest.mark.parametrize(
        "title,rejection_keyword",
        [
            ("CAT 966H radiator", "radiator"),
            ("CAT 966H bucket", "bucket"),
            ("966H bucket attachment", "attachment"),
            ("966H tires", "tires"),
            ("CAT 966H diecast", "diecast"),
            ("966H diecast model", "diecast"),
            ("966H toy", "toy"),
            ("966H service manual", "manual"),
            ("966H transmission for sale", "transmission"),
            ("CAT boots", "boots"),
            ("Caterpillar T-Shirt Vintage", "shirt"),
            ("CAT 980H Poster Wall Art", "poster"),
            ("966G hydraulic pump only", "hydraulic pump"),
            ("CAT 420D parting out / salvage parts", "parting out"),
            ("Caterpillar 950B operator handbook brochure", "handbook"),
        ],
    )
    def test_disqualifying_items_rejected(self, matcher, title, rejection_keyword):
        listing = make_raw_listing(title=title)
        result = matcher.match(listing)
        assert result.classification == MatchClassification.REJECT
        assert result.is_target_equipment is False, f"Item '{title}' was incorrectly accepted with confidence {result.confidence}!"
        assert result.rejection_reason is not None


class TestAdversarialModelBoundaries:
    """Adversarial boundary tests ensuring model matching does not falsely match substring numbers or prices."""

    @pytest.mark.parametrize(
        "title,reason_snippet",
        [
            ("CAT 9300 loader", "No target model"),
            ("$9,300 CAT parts", "part"),
            ("930 tires", "tires"),
            ("Model 9200", "No target model"),
            ("9360 Caterpillar machine", "No target model"),
        ],
    )
    def test_adversarial_model_boundaries(self, matcher, title, reason_snippet):
        listing = make_raw_listing(title=title)
        result = matcher.match(listing)
        assert result.classification == MatchClassification.REJECT
        assert result.is_target_equipment is False, f"Adversarial case '{title}' was incorrectly accepted!"
        assert result.model not in ["930", "920", "936"], f"Incorrectly extracted model '{result.model}' from '{title}'"


class TestOverlappingModels:
    """Tests that model matching prefers the longest/most specific model match."""

    def test_specific_model_preferred_over_base_model(self, matcher):
        listing_936e = make_raw_listing(title="CAT 936E Wheel Loader")
        result_936e = matcher.match(listing_936e)
        assert result_936e.classification == MatchClassification.ACCEPT
        assert result_936e.model == "936E", f"Expected '936E', but got '{result_936e.model}'"

        listing_936 = make_raw_listing(title="CAT 936 Wheel Loader")
        result_936 = matcher.match(listing_936)
        assert result_936.classification == MatchClassification.ACCEPT
        assert result_936.model == "936", f"Expected '936', but got '{result_936.model}'"


class TestNormalizationVariants:
    """Tests that various spacing, casing, and punctuation variants resolve consistently."""

    @pytest.mark.parametrize(
        "title",
        [
            "CAT 966H",
            "CAT 966-H",
            "CAT 966 H",
            "Caterpillar 966H",
            "Caterpillar 966 H",
            "cat 966h",
            "CAT-966H",
        ],
    )
    def test_966h_normalization_variants(self, matcher, title):
        listing = make_raw_listing(title=title)
        result = matcher.match(listing)
        assert result.classification == MatchClassification.ACCEPT
        assert result.is_target_equipment is True, f"Failed for variant '{title}': {result.rejection_reason}"
        assert result.manufacturer == "Caterpillar"
        assert result.model == "966H"
        assert result.equipment_type == "Wheel Loader"


class TestDifficultContextDiscrimination:
    """
    Tests distinguishing between:
    - a machine that has a part (ACCEPT)
    - the part itself being sold (REJECT)
    """

    @pytest.mark.parametrize(
        "title",
        [
            "CAT 966H wheel loader, new tires",
            "CAT 966H wheel loader, bucket included",
            "CAT 966H complete machine, rebuilt transmission",
            "2007 CAT 966H runs and operates, new engine installed",
        ],
    )
    def test_machine_with_part_accepted(self, matcher, title):
        listing = make_raw_listing(title=title)
        result = matcher.match(listing)
        assert result.classification == MatchClassification.ACCEPT
        assert result.is_target_equipment is True, f"Machine '{title}' was rejected! Reason: {result.rejection_reason}"
        assert result.confidence >= matcher.accept_threshold

    @pytest.mark.parametrize(
        "title",
        [
            "CAT 966H tires for sale",
            "CAT 966H bucket for sale",
            "CAT 966H rebuilt transmission for sale",
            "CAT 966H engine for sale",
            "CAT 966H cab assembly",
            "CAT 966H hydraulic cylinder",
            "CAT 966H salvage parts",
            "CAT 966H dismantling",
        ],
    )
    def test_part_itself_sold_rejected(self, matcher, title):
        listing = make_raw_listing(title=title)
        result = matcher.match(listing)
        assert result.classification == MatchClassification.REJECT
        assert result.is_target_equipment is False, f"Part listing '{title}' was incorrectly accepted! Confidence: {result.confidence}"
        assert result.rejection_reason is not None


class TestManufacturerModelRelationships:
    """Tests manufacturer and model association and conflict prevention."""

    def test_case_models_resolve_to_case(self, matcher):
        listing_sl = make_raw_listing(title="CASE 580SL Backhoe")
        res_sl = matcher.match(listing_sl)
        assert res_sl.classification == MatchClassification.ACCEPT
        assert res_sl.manufacturer == "CASE"
        assert res_sl.model == "580SL"

        listing_super_l = make_raw_listing(title="1999 CASE 580 Super L Loader Backhoe")
        res_super_l = matcher.match(listing_super_l)
        assert res_super_l.classification == MatchClassification.ACCEPT
        assert res_super_l.manufacturer == "CASE"
        assert res_super_l.model == "580SL"

    def test_caterpillar_model_not_assigned_to_case(self, matcher):
        listing_conflict = make_raw_listing(title="CASE 966H Wheel Loader")
        res_conflict = matcher.match(listing_conflict)
        assert res_conflict.classification == MatchClassification.REJECT
        assert res_conflict.is_target_equipment is False
        assert "Manufacturer conflict" in (res_conflict.rejection_reason or "")


class TestDistinctiveModelManufacturerInference:
    """Tests that a distinctive model without explicit manufacturer infers the manufacturer and flags it."""

    def test_missing_manufacturer_inferred(self, matcher):
        listing = make_raw_listing(title="2008 966H Wheel Loader")
        result = matcher.match(listing)
        assert result.classification == MatchClassification.ACCEPT
        assert result.is_target_equipment is True
        assert result.manufacturer == "Caterpillar"
        assert result.manufacturer_inferred is True
        assert result.model == "966H"
        assert any("inferred_manufacturer:Caterpillar" in r for r in result.matched_rules)

    def test_explicit_manufacturer_not_marked_inferred(self, matcher):
        listing = make_raw_listing(title="2008 CAT 966H Wheel Loader")
        result = matcher.match(listing)
        assert result.classification == MatchClassification.ACCEPT
        assert result.is_target_equipment is True
        assert result.manufacturer == "Caterpillar"
        assert result.manufacturer_inferred is False


class TestConfigurableThresholds:
    """Tests configurable acceptance thresholds."""

    def test_custom_threshold_strict(self):
        strict_matcher = EquipmentMatcher(accept_threshold=0.98, review_threshold=0.60)
        listing = make_raw_listing(title="Caterpillar 966G")
        result = strict_matcher.match(listing)
        assert result.confidence < 0.98
        assert result.classification != MatchClassification.ACCEPT

    def test_custom_threshold_lenient(self):
        lenient_matcher = EquipmentMatcher(accept_threshold=0.60, review_threshold=0.40)
        listing = make_raw_listing(title="Caterpillar 966G")
        result = lenient_matcher.match(listing)
        assert result.classification == MatchClassification.ACCEPT
        assert result.is_target_equipment is True


class TestEquipmentMatcherMetadataExtraction:
    """Tests verifying metadata extraction accuracy."""

    def test_extract_year_hours_price(self, matcher):
        listing = make_raw_listing(
            title="2012 CAT 972H Wheel Loader $95,000",
            description="Condition is good. Meter reads 7,450 hrs. Machine ready for jobsite.",
        )
        result = matcher.match(listing)
        assert result.classification == MatchClassification.ACCEPT
        assert result.is_target_equipment is True
        assert result.extracted_year == 2012
        assert result.extracted_hours == 7450.0
        assert result.extracted_price == 95000.0
        assert result.model == "972H"
        assert result.manufacturer == "Caterpillar"
