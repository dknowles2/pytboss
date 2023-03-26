from pathlib import Path

from setuptools import find_packages, setup

VERSION = "2023.3.2"

setup(
    name="pytboss",
    version=VERSION,
    description="Python library for interacting with Pitboss grills and smokers.",
    long_description=Path("README.md").read_text(),
    long_description_content_type="text/markdown",
    classifiers=[
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    download_url="https://github.com/dknowles2/pytboss/tarball/" + VERSION,
    keywords="pitboss,api,iot,ble",
    author="David Knowles",
    author_email="dknowles2@gmail.com",
    packages=find_packages(),
    python_requires=">=3.8",
    url="https://github.com/dknowles2/pytboss",
    license="Apache License 2.0",
    install_requires=[
        "bleak",
    ],
    include_package_data=True,
    zip_safe=True,
)
