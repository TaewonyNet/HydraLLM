from setuptools import find_packages, setup

setup(
    name="multi-llm-gateway",
    version="1.0.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "fastapi>=0.104.1",
        "uvicorn[standard]>=0.24.0",
        "pydantic>=2.5.0",
        "python-dotenv>=1.0.0",
        "openai>=1.2.1",
        "google-generativeai>=0.8.6",
        "requests>=2.31.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.4.3",
            "pytest-asyncio>=0.23.3",
            "pytest-cov>=4.1.0",
            "mypy>=1.5.1",
            "black>=23.11.0",
            "ruff>=0.1.6",
        ]
    },
    python_requires=">=3.10",
)
