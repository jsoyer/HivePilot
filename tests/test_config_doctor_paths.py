def test_logs_dir_is_tracked_as_a_cwd_relative_path():
    """`logs_dir` defaults to a RELATIVE `runs/logs`, and it bit us.

    Five copies of `runs/logs/hivepilot.log` exist on the production host,
    one per directory a command was ever typed from; only the one under `/`
    is the live log. The check that exists to name cwd-relative paths did not
    list the field that produced the most copies of them.
    """
    from hivepilot.services.config_doctor import _PATH_FIELDS

    assert "logs_dir" in {field[1] for field in _PATH_FIELDS}
