#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import ast
import typing as t
from functools import lru_cache
from itertools import chain, permutations
from pathlib import Path

import isort
import pytest
from pipfile import Pipfile  # type: ignore[import]

from tests.testlib import repo_path
from tests.testlib.utils import current_base_branch_name, is_enterprise_repo

IGNORED_LIBS = set(["cmk", "livestatus", "mk_jolokia"])  # our stuff
IGNORED_LIBS |= isort.stdlibs._all.stdlib  # builtin stuff
IGNORED_LIBS |= set(["__future__", "typing_extensions"])  # other builtin stuff

PACKAGE_REPLACEMENTS = ".-_"


PackageName = t.NewType("PackageName", str)  # Name in Pip(file)
ImportName = t.NewType("ImportName", str)  # Name in Source (import ...)


@pytest.mark.skipif(
    current_base_branch_name() == "master",
    reason="In master we use latest and greatest, but once we release we start pinning...",
)
def test_all_deployment_packages_pinned() -> None:
    parsed_pipfile = Pipfile.load(filename=repo_path() + "/Pipfile")
    unpinned_packages = [f"'{n}'" for n, v in parsed_pipfile.data["default"].items() if v == "*"]
    assert not unpinned_packages, (
        "The following packages are not pinned: %s. "
        "For the sake of reproducibility, all deployment packages must be pinned to a version!"
    ) % " ,".join(unpinned_packages)


def iter_sourcefiles(basepath: Path) -> t.Iterable[Path]:
    """iter over the repo and return all source files

    this could have been a easy glob, but we do not care for hidden files here:
    https://bugs.python.org/issue26096"""
    for sub_path in basepath.iterdir():
        if sub_path.name.startswith("."):
            continue
        if sub_path.is_file() and sub_path.name.endswith(".py"):
            yield sub_path
        if sub_path.is_dir():
            yield from iter_sourcefiles(sub_path)


def scan_for_imports(root_node: ast.Module) -> t.Iterable[ImportName]:
    """walk the tree and yield all imported packages"""
    for node in ast.walk(root_node):
        if isinstance(node, ast.Import):
            yield from (ImportName(n.name) for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                # relative imports
                continue
            assert node.module is not None
            yield ImportName(node.module)


def toplevel_importname(name: ImportName) -> ImportName:
    """return top level import

    >>> toplevel_importname("foo")
    'foo'
    >>> toplevel_importname("foo.bar")
    'foo'
    >>> toplevel_importname("foo.bar.baz")
    'foo'
    """
    try:
        top_level_lib, _sub_libs = name.split(".", maxsplit=1)
        return ImportName(top_level_lib)
    except ValueError:
        return name


def prune_imports(import_set: t.Set[ImportName]) -> t.Set[ImportName]:
    """throw out all our own libraries and use only top-level names"""
    return {
        top_level_lib
        for import_name in import_set
        for top_level_lib in [toplevel_importname(import_name)]
        if top_level_lib not in IGNORED_LIBS
    }


@lru_cache(maxsize=None)
def get_imported_libs(repopath: Path) -> t.Set[ImportName]:
    """Scan the repo for import statements, return only non local ones"""
    imports: t.Set[ImportName] = set()
    for source_path in iter_sourcefiles(repopath):
        if source_path.name.startswith("."):
            continue
        with source_path.open() as source_file:
            try:
                root = ast.parse(source_file.read(), source_path)  # type: ignore
                imports.update(scan_for_imports(root))
            except SyntaxError:
                # We have various py2 scripts which raise SyntaxErrors.
                # e.g. agents/pugins/*_2.py also some google test stuff...
                # If we should check them they would fail the unittests,
                # providing a whitelist here is not really maintainable
                continue

    return prune_imports(imports)


def packagename_for(path: Path) -> PackageName:
    """Check a METADATA file and return the PackageName"""
    with path.open() as metadata:
        for line in metadata.readlines():
            if line.startswith("Name:"):
                return PackageName(line[5:].strip())

    raise NotImplementedError("No 'Name:' in METADATA file")


def importnames_for(packagename: PackageName, path: Path) -> t.List[ImportName]:
    """return a list of importable libs which belong to the package"""
    top_level_txt_path = path.with_name("top_level.txt")
    if not top_level_txt_path.is_file():
        return [ImportName(packagename)]

    with top_level_txt_path.open() as top_level_file:
        return [ImportName(x.strip()) for x in top_level_file.readlines() if x.strip()]


def packagenames_to_libnames(repopath: Path) -> t.Dict[PackageName, t.List[ImportName]]:
    """scan the site-packages folder for package infos"""
    return {
        packagename: importnames_for(packagename, metadata_path)
        for metadata_path in repopath.glob(".venv/lib/python*/site-packages/*.dist-info/METADATA")
        for packagename in [packagename_for(metadata_path)]
    }


@lru_cache(maxsize=None)
def get_pipfile_libs(repopath: Path) -> t.Dict[PackageName, t.List[ImportName]]:
    """Collect info from Pipfile with additions from site-packages

    The dict has as key the Pipfile package name and as value a list with all import names
    from top_level.txt

    packagenames may differ from the import names,
    also the site-package folder can be different."""
    site_packages = packagenames_to_libnames(repopath)
    pipfile_to_libs: t.Dict[PackageName, t.List[ImportName]] = {}

    parsed_pipfile = Pipfile.load(filename=repopath / "Pipfile")
    for name, details in parsed_pipfile.data["default"].items():
        if "path" in details:
            # Ignoring some of our own sub-packages e.g. marcv
            continue

        if name in site_packages:
            pipfile_to_libs[name] = site_packages[name]
            continue

        for char_to_be_replaced, replacement in permutations(PACKAGE_REPLACEMENTS, 2):
            fuzzy_name = PackageName(name.replace(char_to_be_replaced, replacement))
            if fuzzy_name in site_packages:
                pipfile_to_libs[name] = site_packages[fuzzy_name]
                break
        else:
            raise NotImplementedError("Could not find package %s in site_packages" % name)
    return pipfile_to_libs


def get_unused_dependencies() -> t.Iterable[PackageName]:
    """Iterate over declared dependencies which are not imported"""
    imported_libs = get_imported_libs(Path(repo_path()))
    pipfile_libs = get_pipfile_libs(Path(repo_path()))
    for packagename, import_names in pipfile_libs.items():
        if set(import_names).isdisjoint(imported_libs):
            yield packagename


def get_undeclared_dependencies() -> t.Iterable[ImportName]:
    """Iterate over imported dependencies which could not be found in the Pipfile"""
    imported_libs = get_imported_libs(Path(repo_path()) / "cmk")
    pipfile_libs = get_pipfile_libs(Path(repo_path()))
    declared_libs = set(chain.from_iterable(pipfile_libs.values()))

    yield from imported_libs - declared_libs


CEE_UNUSED_PACKAGES = [
    "Babel",
    "Cython",
    "Flask",
    "MarkupSafe",
    "PyJWT",
    "PyMySQL",
    "PyNaCl",
    "attrs",
    "bcrypt",
    "cachetools",
    "certifi",
    "cffi",
    "chardet",
    "click",
    "defusedxml",
    "dnspython",
    "docutils",
    "gunicorn",
    "idna",
    "importlib_metadata",
    "itsdangerous",
    "jmespath",
    "jsonschema",
    "more-itertools",
    "multidict",
    "ordered-set",
    "pbr",
    "ply",
    "psycopg2-binary",
    "pyasn1-modules",
    "pycparser",
    "pykerberos",
    "pymssql",
    "pyprof2calltree",
    "pyrsistent",
    "requests-kerberos",
    "requests-toolbelt",
    "rsa",
    "s3transfer",
    "semver",
    "setuptools-git",
    "setuptools_scm",
    "snmpsim",
    "tenacity",
    "uvicorn",
    "websocket_client",
    "wrapt",
    "yarl",
    "zipp",
]


@pytest.mark.skipif(not is_enterprise_repo(), reason="Test is only for CEE")
def test_dependencies_are_used_cee() -> None:
    assert sorted(get_unused_dependencies()) == CEE_UNUSED_PACKAGES


@pytest.mark.skipif(is_enterprise_repo(), reason="Test is only for CCE")
def test_dependencies_are_used_cce() -> None:
    unused_packages = CEE_UNUSED_PACKAGES + [
        "PyPDF3",  # is only used in CEE
        "numpy",  # is only used in CEE
        "roman",  # is only used in CEE
    ]
    assert sorted(get_unused_dependencies()) == sorted(unused_packages)


def test_dependencies_are_declared() -> None:
    """Test for unknown imports which could not be mapped to the Pipfile

    mostly optional imports and OMD-only shiped packages.
    issubset() is used since the dependencies vary between the versions."""

    assert set(get_undeclared_dependencies()).issubset(
        set(
            [
                "NaElement",  # Optional import cmk/special_agents/agent_netapp.py
                "NaServer",  # Optional import cmk/special_agents/agent_netapp.py
                "lxml",  # Optional import cmk/special_agents/agent_netapp.py
                "matplotlib",  # Disabled debug code in enterprise/cmk/gui/cee/sla.py
                "mock",  # Mixin prod and test code... cmk/gui/plugins/openapi/restful_objects/constructors.py
                "mpld3",  # Disabled debug code in enterprise/cmk/gui/cee/sla.py
                "netsnmp",  # We ship it with omd/packages
                "pymongo",  # Optional except ImportError...
                "pytest",  # In __main__ guarded section in cmk/special_agents/utils/misc.py
                "tinkerforge",  # agents/plugins/mk_tinkerforge.py has its own install routine
            ]
        )
    )
