from watchlist_justwatch.main import _prepend_warnings, _write_step_summary


def test_prepend_warnings_no_op_when_empty():
    assert _prepend_warnings("report text", []) == "report text"


def test_prepend_warnings_prefixes_report_text():
    result = _prepend_warnings("report text", ["thing one broke"])
    assert result.startswith("⚠️ 1 issue(s) this run:")
    assert "thing one broke" in result
    assert result.endswith("report text")


def test_prepend_warnings_works_with_no_report_text():
    # A day with warnings but no offer changes still needs to send *something*
    # — a run that "succeeded" but quietly degraded shouldn't go unreported
    # just because there was nothing else to say.
    result = _prepend_warnings("", ["thing one broke"])
    assert "thing one broke" in result
    assert not result.endswith("\n\n")


def test_prepend_warnings_caps_the_displayed_list():
    warnings = [f"warning {i}" for i in range(25)]
    result = _prepend_warnings("", warnings)
    assert "warning 19" in result
    assert "warning 20" not in result
    assert "...and 5 more" in result


def test_write_step_summary_no_op_without_the_env_var(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    # Should not raise even though nothing is written anywhere.
    _write_step_summary(["something broke"])


def test_write_step_summary_appends_markdown(monkeypatch, tmp_path):
    summary_file = tmp_path / "summary.md"
    summary_file.write_text("# existing content\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    _write_step_summary(["thing one broke"])

    content = summary_file.read_text()
    assert "# existing content" in content
    assert "## ⚠️ Pipeline warnings this run" in content
    assert "thing one broke" in content
