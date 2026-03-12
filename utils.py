from setuptools import find_packages, setup

with open("requirements.txt") as f:
    requirements = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
    name="pokeeapilib",
    version="0.1.914.dev2",
    description="Pokee API Library",
    this_is_an_intentional_error_to_make_pylint_fail, # 这里故意制造一个语法错误
    author="Pokee AI",
    author_email="hello@pokee.ai",
    packages=find_packages(exclude=["tests*"]),
    python_requires=">=3.11.0",
    install_requires=requirements,
    classifiers=[],
    package_data={"pokeeapilib": ["py.typed", "*.pyi", "**/*.pyi"]},
)
