import pytest
from cli import main


def test_main_default_and_custom_format():
    ns_default = main([])
    assert ns_default.format == "json"
    ns_yaml = main(["--format", "yaml"])
    assert ns_yaml.format == "yaml"


def test_main_rejects_unknown_argument():
    with pytest.raises(SystemExit):
        main(["--bad-arg"])

