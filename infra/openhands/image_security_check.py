"""Verify the adopted upstream language-runtime and pip vendor fixes, offline."""

from __future__ import annotations

import importlib.util
import json
import ssl
import sys
from importlib.metadata import version

import certifi
import pip
from pip._vendor import msgpack, urllib3
from pip._vendor import certifi as pip_certifi


def main() -> None:
    assert sys.version_info[:2] == (3, 13), sys.version
    assert pip.__version__ == "26.2.2", pip.__version__
    assert urllib3.__version__ == "2.8.0", urllib3.__version__
    assert msgpack.__version__ == "1.2.1", msgpack.__version__
    assert importlib.util.find_spec("pip._vendor.pkg_resources") is None
    for bundle in (None, certifi.where(), pip_certifi.where()):
        context = ssl.create_default_context(cafile=bundle)
        for certificate in context.get_ca_certs():
            names = {
                value
                for group in certificate["subject"]
                for key, value in group
                if key == "commonName"
            }
            assert "emSign ECC Root CA - G3" not in names, bundle
    print(
        json.dumps(
            {
                "status": "ok",
                "python": sys.version.split()[0],
                "pip": pip.__version__,
                "pip_urllib3": urllib3.__version__,
                "pip_msgpack": msgpack.__version__,
                "certifi": version("certifi"),
                "retired_ca_absent": True,
                "pip_pkg_resources_absent": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
