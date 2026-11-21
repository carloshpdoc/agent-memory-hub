"""scripts/store/config.py: SSL_CERT_FILE do .env chega ao processo."""
import pytest

from store import config


def test_ca_bundle_from_env_file_reaches_the_process(tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    environ = {}
    config.apply_ca_bundle({"SSL_CERT_FILE": str(bundle)}, environ)
    assert environ["SSL_CERT_FILE"] == str(bundle)


def test_process_variable_wins_and_missing_key_is_noop(tmp_path):
    environ = {"SSL_CERT_FILE": "/ja/definido"}
    config.apply_ca_bundle({"SSL_CERT_FILE": str(tmp_path / "x.pem")}, environ)
    assert environ == {"SSL_CERT_FILE": "/ja/definido"}
    environ = {}
    config.apply_ca_bundle({}, environ)
    assert environ == {}


def test_missing_bundle_is_an_error_not_silence(tmp_path):
    with pytest.raises(RuntimeError, match="não existe"):
        config.apply_ca_bundle({"SSL_CERT_FILE": str(tmp_path / "nao.pem")}, {})
