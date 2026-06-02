"""Req_Find.kind coercion — recover the model's singular-enum slip.

Found in the v0.1.171 PROD run (run-22SV4N, t095): the model emitted a find tool
call with kind='file' (singular). NextStep validation rejected it, the inline
retry produced the same slip → "double validation failure" → the trial terminated
with NO ANSWER (guaranteed 0). The valid enum is all/files/dirs. Coercing the
obvious singular/alias forms before validation turns a guaranteed-0 into a normal
step. Anything genuinely invalid still raises (no silent wrong behaviour).
"""
import pytest
from pydantic import ValidationError

from bitgn_contest_agent.schemas import Req_Find


class TestFindKindCoercion:
    def test_singular_file_coerced_to_files(self):
        assert Req_Find(tool="find", name="x.json", kind="file").kind == "files"

    def test_singular_dir_coerced_to_dirs(self):
        assert Req_Find(tool="find", name="x", kind="dir").kind == "dirs"

    def test_directory_alias_coerced_to_dirs(self):
        assert Req_Find(tool="find", name="x", kind="directory").kind == "dirs"

    def test_folder_alias_coerced_to_dirs(self):
        assert Req_Find(tool="find", name="x", kind="folder").kind == "dirs"

    def test_uppercase_coerced(self):
        assert Req_Find(tool="find", name="x", kind="FILES").kind == "files"

    def test_valid_values_unchanged(self):
        for k in ("all", "files", "dirs"):
            assert Req_Find(tool="find", name="x", kind=k).kind == k

    def test_default_unchanged(self):
        assert Req_Find(tool="find", name="x").kind == "all"

    def test_genuinely_invalid_still_raises(self):
        with pytest.raises(ValidationError):
            Req_Find(tool="find", name="x", kind="banana")
