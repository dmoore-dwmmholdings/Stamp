"""CSV batch stamping - see :mod:`stamp.batch`.

The dry run and the real run have to agree.  A simulation that calls a row
ready and a run that then refuses it is worse than no simulation: somebody
queues four hundred parts on the strength of it.
"""

from __future__ import annotations

import pytest

from stamp.batch import BatchError, _output_name, _substitute, placeholders, simulate_batch
from stamp.core.document import BasePart, CodeSpec, Document, Feature, ProfileRef, TextSpec
from stamp.io.project import save


@pytest.fixture
def template(tmp_path):
    def build(*features):
        path = tmp_path / "template.stamp"
        save(
            Document(base=BasePart(mode="solid", runtime=object()), features=list(features)),
            path,
        )
        return path

    return build


def _text(text: str) -> Feature:
    return Feature(profile=ProfileRef(text=TextSpec(text=text)))


class TestTheNameARowWritesTo:
    def test_a_foreign_suffix_does_not_survive_the_format(self):
        """part.txt used to be kept, with STEP written inside it."""
        assert str(_output_name("part.txt", "step")) == "part.txt.step"
        assert str(_output_name("part", "3mf")) == "part.3mf"

    def test_the_wrong_model_suffix_is_corrected(self):
        assert str(_output_name("part.stl", "step")) == "part.step"
        assert str(_output_name("part.STEP", "step")) == "part.STEP"

    def test_a_dot_in_a_part_number_is_not_a_suffix(self):
        """"PN-1234.5" is a name, not a file type, and must keep its .5."""
        assert str(_output_name("PN-1234.5", "stl")) == "PN-1234.5.stl"

    def test_the_batch_report_cannot_be_written_over(self):
        """The check used to be made after the suffix had gone on.

        By then the name was "stamp-batch-report.json.step" and the reserved
        one it was compared against never matched, so a row could quietly
        overwrite the report of the run it was part of.
        """
        for name in ("stamp-batch-report.json", "STAMP-Batch-Report.json",
                     "stamp-batch-report", "stamp-batch-report.step"):
            with pytest.raises(BatchError, match="batch report"):
                _output_name(name, "step")

    def test_a_name_that_is_only_a_suffix_is_refused(self):
        """"part." was written as "part..step" and ".step" as ".step.step"."""
        for name in ("part.", ".step", ".stl", "..."):
            with pytest.raises(BatchError, match="no file name"):
                _output_name(name, "step")

    def test_a_folder_is_not_a_name_to_write_to(self):
        """Path drops the trailing separator, so "dir/" became "dir.step"."""
        with pytest.raises(BatchError, match="folder"):
            _output_name("dir/", "step")
        with pytest.raises(BatchError, match="folder"):
            _output_name("sub/dir/", "step")

    def test_a_path_that_leaves_the_folder_is_refused(self):
        with pytest.raises(BatchError, match="inside"):
            _output_name("../escape", "stl")

    def test_a_link_out_of_the_folder_is_refused_too(self, tmp_path):
        """The lexical check cannot see where a symlink points; this can."""
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "out"
        root.mkdir()
        (root / "away").symlink_to(outside, target_is_directory=True)

        with pytest.raises(BatchError, match="inside"):
            _output_name("away/part.stl", "stl", root)


class TestPlaceholders:
    def test_a_column_name_with_a_dash_or_a_space_is_substituted(self):
        """The pattern only matched identifiers, so these went in unchanged.

        A CSV header is whatever somebody typed in a spreadsheet, and a part
        that ships reading "{{serial-no}}" is a part that has to be made again.
        """
        document = Document(features=[_text("SN {{serial-no}} / {{Part Number}}")])
        _substitute(document, {"serial-no": "0042", "Part Number": "PN-9"})
        assert document.features[0].profile.text.text == "SN 0042 / PN-9"

    def test_spaces_inside_the_braces_are_ignored(self):
        document = Document(features=[_text("{{ serial }}")])
        _substitute(document, {"serial": "7"})
        assert document.features[0].profile.text.text == "7"

    def test_they_are_found_in_codes_as_well_as_text(self):
        document = Document(
            features=[_text("{{a}}"), Feature(profile=ProfileRef(code=CodeSpec(payload="{{b}}")))]
        )
        assert placeholders(document) == {"a", "b"}


class TestTheDryRun:
    def test_a_placeholder_no_column_fills_is_reported(self, template, tmp_path):
        """It used to be silently left in the part and never mentioned."""
        path = template(_text("SN-{{serial-no}}"))
        csv = tmp_path / "rows.csv"
        csv.write_text("input,output\npart.step,marked\n", encoding="utf-8")

        with pytest.raises(BatchError, match="serial-no"):
            simulate_batch(path, csv, "stl")

    def test_a_template_the_csv_fills_passes(self, template, tmp_path):
        path = template(_text("SN-{{serial-no}}"))
        csv = tmp_path / "rows.csv"
        csv.write_text("input,serial-no,output\npart.step,0042,marked\n", encoding="utf-8")

        report = simulate_batch(path, csv, "stl")
        assert [row.status for row in report.rows] == ["ready"]
        assert report.rows[0].output == "marked.stl"

    def test_it_reports_the_name_the_run_would_write(self, template, tmp_path):
        """Ready for part.txt, then the run wrote part.txt.step somewhere else."""
        path = template()
        csv = tmp_path / "rows.csv"
        csv.write_text("input,output\npart.step,marked.txt\n", encoding="utf-8")

        report = simulate_batch(path, csv, "step")
        assert report.rows[0].output == "marked.txt.step"

    def test_it_refuses_what_the_run_refuses(self, template, tmp_path):
        """Given the folder, the dry run makes the same decision the run does."""
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "out"
        root.mkdir()
        (root / "away").symlink_to(outside, target_is_directory=True)
        path = template()
        csv = tmp_path / "rows.csv"
        csv.write_text("input,output\npart.step,away/marked\n", encoding="utf-8")

        assert simulate_batch(path, csv, "stl").rows[0].status == "ready"
        report = simulate_batch(path, csv, "stl", output_dir=root)
        assert report.rows[0].status == "failed"
        assert "inside" in report.rows[0].detail
