def test_core_feature_flags_are_not_reported_as_missing_plugins(monkeypatch):
    """`*_enabled` is also how core features are named, not only plugins.

    `otel_ingest_enabled` turns on a route on the API. Inferring a plugin name
    from the flag reported it as a missing plugin the moment it was switched
    on in production -- a brand-new false positive in the check whose whole
    purpose is separating real gaps from noise.
    """
    from hivepilot.services.config_doctor import _NON_PLUGIN_ENABLED_FLAG_EXCEPTIONS

    assert "otel_ingest" in _NON_PLUGIN_ENABLED_FLAG_EXCEPTIONS
