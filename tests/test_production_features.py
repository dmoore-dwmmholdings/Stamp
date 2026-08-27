from stamp.batch import _substitute
from stamp.core.document import (
    Anchor,
    AnchorKind,
    CodeKind,
    CodeSpec,
    DatumDefinition,
    Document,
    EdgeRef,
    Feature,
    FeatureMetadata,
    Plane,
    PointRef,
    ProfileRef,
)
from stamp.core.inspection import inspect_document
from stamp.io.code_profile import build_code_profile


def test_code_and_metadata_round_trip_in_document():
    feature = Feature(
        name="Serial code",
        profile=ProfileRef(code=CodeSpec(kind=CodeKind.DATA_MATRIX, payload="SN-{{serial}}")),
        metadata=FeatureMetadata(identifier="MARK-7", process="laser", notes="scan after mark"),
    )
    restored = Document.from_dict(Document(features=[feature]).to_dict())
    got = restored.features[0]
    assert got.profile.code.kind is CodeKind.DATA_MATRIX
    assert got.profile.code.payload == "SN-{{serial}}"
    assert got.metadata.identifier == "MARK-7"


def test_batch_substitutes_code_payload():
    document = Document(features=[Feature(profile=ProfileRef(code=CodeSpec(payload="{{serial}}-{{lot}}")))])
    _substitute(document, {"serial": "0042", "lot": "A"})
    assert document.features[0].profile.code.payload == "0042-A"


def test_qr_and_data_matrix_become_vector_profiles():
    for kind in (CodeKind.QR, CodeKind.DATA_MATRIX):
        profile = build_code_profile(CodeSpec(kind=kind, payload="X", module_mm=0.5))
        assert profile.faces
        assert not profile.blocked


def test_generated_codes_are_decoded_again_before_manufacturing():
    from stamp.io.code_profile import verify_code

    for kind in (CodeKind.QR, CodeKind.DATA_MATRIX):
        verification = verify_code(CodeSpec(kind=kind, payload="STAMP-0042", quiet_zone=4))
        assert verification.readable, verification.detail


def test_inspection_warns_for_small_code_and_depth():
    feature = Feature(profile=ProfileRef(code=CodeSpec(module_mm=0.1)), name="Code")
    feature.operation.depth = 0.1
    warnings = inspect_document(Document(features=[feature]))
    assert any("module" in warning for warning in warnings)
    assert any("depth" in warning for warning in warnings)


def test_named_datum_and_edge_alignment_round_trip():
    datum = DatumDefinition(name="fixture", plane=Plane((1, 2, 3), (0, 0, 1), (1, 0, 0)))
    feature = Feature()
    feature.placement.anchor = Anchor(
        kind=AnchorKind.DATUM,
        datum=datum.id,
        alignment_ref=EdgeRef((1, 2, 3), (1, 0, 0), 10),
        origin_ref=PointRef((4, 5, 6), "vertex"),
    )
    restored = Document.from_dict(Document(features=[feature], datums=[datum]).to_dict())
    assert restored.datums[0].name == "fixture"
    assert restored.features[0].placement.anchor.alignment_ref.length == 10
    assert restored.features[0].placement.anchor.origin_ref.point == (4, 5, 6)
