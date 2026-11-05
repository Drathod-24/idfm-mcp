from idfm_mcp import __version__
from idfm_mcp.server import mcp


def test_version():
    assert __version__ == "0.1.0"


def test_server_name():
    assert mcp.name == "idfm-mcp"
