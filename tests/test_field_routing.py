import pytest
from azure.ai.contentunderstanding.models import AnalysisResult, ArrayField, DocumentContent, DocumentContentSegment

from goselect_docproc.contracts import ContentType, Segment
from goselect_docproc.producers.content_understanding import _fields_by_segment


def test_category_selects_schedule_beside_drawing_and_ambiguity_fails():
    router = DocumentContent(markdown="drawing and schedule")
    segments = [Segment(segment_id=kind.value, file_id="file", first_page=1, last_page=1,
                        content_type=kind, confidence=1, regions=[]) for kind in (ContentType.DRAWING, ContentType.SCHEDULE)]
    blocks = [DocumentContentSegment(segment_id=segment.segment_id) for segment in segments]
    fields = DocumentContent(category="schedule", start_page_number=1, end_page_number=1, fields={"rows": ArrayField(value_array=[])})
    response = AnalysisResult(contents=[router, fields])
    assert set(_fields_by_segment(response, router, blocks, segments)) == {"SCHEDULE"}
    segments.append(segments[1].model_copy(update={"segment_id": "another-schedule"}))
    blocks.append(DocumentContentSegment(segment_id="another-schedule"))
    with pytest.raises(ValueError, match="ambiguous"):
        _fields_by_segment(response, router, blocks, segments)
    fields.segments = [blocks[1]]
    assert set(_fields_by_segment(response, router, blocks, segments)) == {"SCHEDULE"}
    response.contents.append(fields)
    with pytest.raises(ValueError, match="overwrite"):
        _fields_by_segment(response, router, blocks, segments)