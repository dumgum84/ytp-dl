from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

setup(
    name="ytp-dl",
    version="0.2.85",
    author="dumgum82",
    author_email="dumgum42@gmail.com",
    description="YouTube video downloader with Mullvad VPN integration and Flask API",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/ytp-dl",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    python_requires=">=3.8",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "ytp-dl=ytp_dl.cli:main",
            "ytp-dl-api=ytp_dl.api:main",
        ],
    },
    include_package_data=True,
)