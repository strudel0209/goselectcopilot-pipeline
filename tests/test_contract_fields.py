"""Contract-shaped service output -> domain payload.

The doubles below mirror what Content Understanding actually returned for
``sample_docs/98624_1_VFDSchedule.pdf``: nested value objects, a ``D(page,...)``
source string and a per-field confidence.
"""

from __future__ import annotations

import pytest

from goselect_docproc.contracts import ContentType, Span, WorkItem
from goselect_docproc.extractors import (
    NullModel,
    RoutedFieldExtractor,
    SegmentContext,
    default_extractors,
    expand_contract,
    parse_source,
)
from goselect_docproc.reconcile import TagLexicon


class Field:
    """Stands in for a ContentField: value plus grounding."""

    def __init__(self, value=None, *, obj=None, array=None, source=None, confidence=None):
        self.value = value
        self.value_object = obj
        self.value_array = array
        self.source = source
        self.confidence = confidence
        self.spans = []


def measure(value, unit=None):
    return Field(obj={"value": Field(value), "unit": Field(unit)})


def pair_entry(tag="VFD-401", *, confidence=0.52, source="D(3,1.0,2.0,4.0,2.0,4.0,3.0,1.0,3.0)"):
    return Field(
        obj={
            "tag": Field(tag, source=source, confidence=confidence),
            "location": Field("MECH ROOM"),
            "notes": Field("1,2,3,4"),
            "vfd": Field(
                obj={
                    "enclosure_rating": Field(obj={"value": Field("NEMA 12")}),
                    "harmonic_mitigation": Field(obj={"value": Field("Passive filter")}),
                    "electrical": Field(
                        obj={
                            "input_voltage": measure("480", "V"),
                            "output_current": measure("124", "A"),
                            "power_rating": measure("75", "kW"),
                        }
                    ),
                }
            ),
            "motor": Field(
                obj={
                    "frame_size": Field("256T"),
                    "electrical": Field(
                        obj={
                            "voltage": measure("460", "V"),
                            "frequency": measure("60", "Hz"),
                            "power_rating": measure("100", "HP"),
                        }
                    ),
                }
            ),
        }
    )


def context(content_type=ContentType.SCHEDULE, lexicon=None):
    item = WorkItem(
        job_id="j", correlation_id="c", file_id="f1", file_ordinal=0,
        segment_id="f1-seg-002", content_type=content_type,
        first_page=1, last_page=1, spans=[Span(offset=0, length=20)],
        section_root="5.1 Motor schedule", layout_uri="u",
    )
    return SegmentContext(content="x" * 20, item=item, lexicon=lexicon)


@pytest.fixture
def fields():
    return {"vfd_motor_pairs": Field(array=[pair_entry()])}


class TestParseSource:
    def test_reads_page_and_polygon(self):
        page, polygon = parse_source("D(3,1.0,2.0,4.0,2.0,4.0,3.0,1.0,3.0)")
        assert page == 3
        assert len(polygon) == 8

    def test_takes_the_first_of_several_regions(self):
        page, _ = parse_source("D(7,1,1,2,1,2,2,1,2);D(8,1,1,2,1,2,2,1,2)")
        assert page == 7

    def test_survives_anything_unexpected(self):
        assert parse_source(None) == (None, None)
        assert parse_source("AV(1.0,2.0)") == (None, None)
        assert parse_source("D(nonsense)") == (None, None)


class TestExpandContract:
    def test_one_pair_yields_a_drive_a_motor_and_a_pairing(self, fields):
        payload = expand_contract(fields, context(), ContentType.SCHEDULE)

        assert len(payload.vfds) == 1
        assert len(payload.motors) == 1
        assert len(payload.pairs) == 1
        assert payload.pairs[0].vfd_tag == "VFD-401"
        assert payload.pairs[0].motor_tag is None, "the contract has no motor tag field"

    def test_measurements_keep_the_verbatim_reading(self, fields):
        payload = expand_contract(fields, context(), ContentType.SCHEDULE)

        power = payload.vfds[0].power
        assert (power.value, power.unit, power.raw) == (75.0, "kW", "75")

    def test_grounding_survives_onto_evidence(self, fields):
        payload = expand_contract(fields, context(), ContentType.SCHEDULE)

        evidence = payload.vfds[0].evidence[0]
        assert evidence.page == 3
        assert evidence.polygon and len(evidence.polygon) == 8
        assert evidence.confidence == 0.52
        assert evidence.section_path == "5.1 Motor schedule"

    def test_prose_never_asserts_a_pairing(self):
        """A specification states requirements; it does not wire a drive to a
        motor. The schedule grid is what asserts the relationship."""
        payload = expand_contract(
            {"vfd_motor_pairs": Field(array=[pair_entry()])},
            context(ContentType.TEXT),
            ContentType.TEXT,
        )

        assert payload.vfds and payload.motors
        assert payload.pairs == []

    def test_tags_are_repaired_against_the_lexicon(self):
        payload = expand_contract(
            {"vfd_motor_pairs": Field(array=[pair_entry(tag="VFD-4O1")])},
            context(lexicon=TagLexicon({"VFD-401"})),
            ContentType.SCHEDULE,
        )

        assert payload.vfds[0].tag == "VFD-401"

    def test_an_empty_result_is_not_a_crash(self):
        assert expand_contract({}, context(), ContentType.SCHEDULE).vfds == []


class TestRoutedFieldExtractor:
    def test_routed_fields_are_used_instead_of_a_model(self, fields):
        model = NullModel()
        extractor = default_extractors(model)[ContentType.SCHEDULE]
        ctx = context()
        ctx.fields = fields

        payload = extractor.extract(ctx)

        assert payload.vfds
        assert model.calls == [], "the service already extracted this segment"

    def test_the_model_is_the_fallback_when_nothing_was_routed(self):
        model = NullModel()
        extractor = default_extractors(model)[ContentType.SCHEDULE]

        extractor.extract(context())

        assert len(model.calls) == 1

    def test_without_a_fallback_it_says_so_rather_than_inventing(self):
        payload = RoutedFieldExtractor(content_type=ContentType.TEXT).extract(context())

        assert payload.vfds == []
        assert any("no fields returned" in note for note in payload.notes)

    def test_drawings_are_never_routed(self):
        """They need native-resolution tiles the service will not produce."""
        from goselect_docproc.extractors import ModelExtractor

        assert isinstance(default_extractors(NullModel())[ContentType.DRAWING], ModelExtractor)
