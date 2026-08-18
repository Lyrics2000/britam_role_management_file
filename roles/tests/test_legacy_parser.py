"""Unit tests for the JS object-literal parser. No database involved."""

from __future__ import annotations

import pytest

from roles.legacy_html import LegacyParseError, _Scanner, extract_roles, iter_business_units


class TestScanner:
    def test_parses_double_quoted_string(self):
        assert _Scanner('"hello"').parse_value() == "hello"

    def test_parses_single_quoted_string(self):
        assert _Scanner("'hello'").parse_value() == "hello"

    def test_handles_escaped_quote_inside_string(self):
        assert _Scanner(r'"she said \"hi\""').parse_value() == 'she said "hi"'

    def test_handles_apostrophe_inside_double_quotes(self):
        # The real data is full of these: "0 -2 years' experience"
        assert _Scanner('"years\' experience"').parse_value() == "years' experience"

    def test_handles_unicode_escape(self):
        assert _Scanner(r'"café"').parse_value() == "café"

    def test_handles_newline_escape(self):
        assert _Scanner(r'"a\nb"').parse_value() == "a\nb"

    def test_parses_int_and_float(self):
        assert _Scanner("7").parse_value() == 7
        assert _Scanner("6.2").parse_value() == 6.2
        assert _Scanner("-1.5e2").parse_value() == -150.0

    def test_parses_bare_identifier_keys(self):
        assert _Scanner('{bu:"CX",bandN:6.2}').parse_value() == {"bu": "CX", "bandN": 6.2}

    def test_parses_quoted_keys(self):
        assert _Scanner('{"bu":"CX"}').parse_value() == {"bu": "CX"}

    def test_accepts_trailing_comma(self):
        assert _Scanner('[{a:1},]').parse_value() == [{"a": 1}]

    def test_accepts_nested_structures(self):
        parsed = _Scanner('{a:[1,2,{b:"c"}]}').parse_value()
        assert parsed == {"a": [1, 2, {"b": "c"}]}

    def test_value_containing_braces_and_commas_survives(self):
        # A regex-based extractor fails exactly here.
        parsed = _Scanner('{desc:"uses commas, braces {x} and \\"quotes\\""}').parse_value()
        assert parsed["desc"] == 'uses commas, braces {x} and "quotes"'

    def test_skips_line_comments(self):
        assert _Scanner('[ // note\n 1 ]').parse_value() == [1]

    def test_skips_block_comments(self):
        assert _Scanner('[ /* note */ 1 ]').parse_value() == [1]

    def test_true_false_null(self):
        assert _Scanner("[true,false,null]").parse_value() == [True, False, None]

    def test_unterminated_string_raises_coded_error(self):
        with pytest.raises(LegacyParseError) as excinfo:
            _Scanner('"abc').parse_value()
        assert excinfo.value.code == "PARSE-006"

    def test_unterminated_block_comment_raises(self):
        with pytest.raises(LegacyParseError) as excinfo:
            _Scanner("[ /* oops ").parse_value()
        assert excinfo.value.code == "PARSE-002"

    def test_missing_separator_raises(self):
        with pytest.raises(LegacyParseError) as excinfo:
            _Scanner('{a:1 b:2}').parse_value()
        assert excinfo.value.code == "PARSE-012"

    def test_unsupported_bare_word_raises(self):
        with pytest.raises(LegacyParseError) as excinfo:
            _Scanner("{a:someVariable}").parse_value()
        assert excinfo.value.code == "PARSE-004"


class TestExtractRoles:
    def test_extracts_every_record(self, legacy_html):
        result = extract_roles(legacy_html)
        assert result.count == 5
        assert result.skipped == []

    def test_maps_legacy_keys_to_model_fields(self, legacy_html):
        first = extract_roles(legacy_html).records[0]
        assert first["business_unit_name"] == "Internal Audit"
        assert first["position"] == "Head of Internal Audit"
        assert first["purpose"] == "Integrates audit services across the Group."
        assert first["band_numeric"] == 3

    def test_missing_optional_fields_become_empty_strings(self, legacy_html):
        # The first record has no techcomp/leadcomp keys at all.
        first = extract_roles(legacy_html).records[0]
        assert first["technical_competencies"] == ""
        assert first["leadership_competencies"] == ""

    def test_band_without_a_number_yields_null_band_numeric(self, legacy_html):
        exco = [r for r in extract_roles(legacy_html).records if r["band"] == "Executive"]
        assert len(exco) == 1
        assert exco[0]["band_numeric"] is None

    def test_float_band_is_preserved(self, legacy_html):
        claims = [r for r in extract_roles(legacy_html).records if r["position"] == "Claims Assistant"]
        assert claims[0]["band_numeric"] == 9.0

    def test_business_units_in_first_appearance_order(self, legacy_html):
        result = extract_roles(legacy_html)
        assert list(iter_business_units(result)) == ["Internal Audit", "BLA", "CX", "Group Exco"]

    def test_missing_file_raises_seed_001(self, tmp_path):
        with pytest.raises(LegacyParseError) as excinfo:
            extract_roles(tmp_path / "nope.html")
        assert excinfo.value.code == "SEED-001"

    def test_html_without_roles_array_raises_seed_002(self, tmp_path):
        path = tmp_path / "empty.html"
        path.write_text("<html><body>nothing here</body></html>", encoding="utf-8")
        with pytest.raises(LegacyParseError) as excinfo:
            extract_roles(path)
        assert excinfo.value.code == "SEED-002"

    def test_records_missing_mandatory_fields_are_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "partial.html"
        path.write_text(
            'const ROLES = [{bu:"CX",pos:"Good"},{bu:"",pos:"No BU"},{bu:"CX",pos:""},"junk"];',
            encoding="utf-8",
        )
        result = extract_roles(path)
        assert result.count == 1
        assert result.records[0]["position"] == "Good"
        assert len(result.skipped) == 3

    def test_real_production_file_parses_if_present(self):
        """Guards against a real-world regression in the shipped data file."""
        from django.conf import settings

        if not settings.LEGACY_HTML_PATH.is_file():
            pytest.skip("production HTML not present in this checkout")
        result = extract_roles(settings.LEGACY_HTML_PATH)
        assert result.count > 250, f"expected the full library, parsed only {result.count}"
        assert result.skipped == []
        assert all(record["business_unit_name"] for record in result.records)
        assert all(record["position"] for record in result.records)
